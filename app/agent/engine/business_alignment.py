"""使用 LangGraph 编排用户问题到标准业务查询需求的独立对齐子图。"""

import json
import re
from collections.abc import Callable
from typing import Any, Final, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agent.domains.base import AlignmentPolicyIssue, QueryDomainProfile
from app.agent.events.publisher import AgentProgressReporter, ProgressEmitter
from app.core.config import Settings, get_settings
from app.agent.tools.ask_user import (
    ASK_USER_TOOL_NAME,
    UserInteraction,
    build_ask_user_tool_definition,
    parse_ask_user_tool_arguments,
)
from app.agent.engine.business_alignment_prompt import build_business_alignment_prompt
from app.agent.runtime.model_options import (
    DEFAULT_ALIGNMENT_MAX_TOKENS,
    build_non_thinking_completion_options,
    build_strict_tools_base_url,
)
from app.agent.runtime.yaml_context import (
    parse_tagged_context_records,
    render_yaml_context,
)
from app.agent.tools.strict_schema import build_strict_tool_definition
from app.agent.tools.thinking import (
    THINKING_TOOL_NAME,
    ThinkingToolArguments,
    build_thinking_tool_definition,
    parse_thinking_tool_arguments,
)
from app.agent.tools.argument_feedback import build_tool_argument_error_message


DEFAULT_MAX_ALIGNMENT_GENERATION_COUNT: Final[int] = 4
MAX_TERMINAL_ARGUMENT_REPAIR_COUNT: Final[int] = 1
SUBMIT_ALIGNED_QUERY_TOOL_NAME: Final[str] = "submit_aligned_query"
ABANDON_ALIGNMENT_TOOL_NAME: Final[str] = "abandon_alignment"
TraceWriter = Callable[[str], None]
UserInputReader = Callable[[str], str]
# 按对齐轮次强制工具协议：首轮固定记录关键判断，后续只能以任一注册工具继续推进。
def _build_alignment_tool_choice(generation_count: int) -> str | dict[str, object]:
    if generation_count == 1:
        return {
            "type": "function",
            "function": {"name": THINKING_TOOL_NAME},
        }
    return "required"


class ResolvedBusinessConcept(BaseModel):
    """一个用户表达与标准业务概念之间的可审计对齐结果。"""

    model_config = ConfigDict(extra="forbid")

    user_term: str = Field(description="用户问题中的原始业务表达")
    canonical_term: str = Field(description="词汇表中的标准业务概念")
    alignment_reason: str = Field(description="依据词汇表得到该对齐的简短原因")


class AlignedLogicalConstraint(BaseModel):
    """不包含数据库实现的集合量化或数量业务约束。"""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(description="约束作用的业务主体")
    collection: str = Field(description="需要判断的主体关联对象集合")
    quantifier: Literal[
        "all", "any", "none", "exactly", "at_least", "at_most"
    ] = Field(description="全部、任一、没有、恰好、至少或至多")
    predicate: str = Field(description="集合成员需要满足的标准业务条件")
    count: int | None = Field(
        default=None,
        ge=0,
        description="数量型量词对应的数量；all、any、none 时传 null",
    )

    # 保证数量约束和集合约束使用匹配的 count，避免规划层猜测量词含义。
    @model_validator(mode="after")
    def validate_quantifier_count(self) -> "AlignedLogicalConstraint":
        count_quantifiers = {"exactly", "at_least", "at_most"}
        if self.quantifier in count_quantifiers and self.count is None:
            raise ValueError(f"量词 {self.quantifier} 必须提供 count")
        if self.quantifier not in count_quantifiers and self.count is not None:
            raise ValueError(f"量词 {self.quantifier} 的 count 必须为 null")
        return self


class AlignedPresentationRequirement(BaseModel):
    """用户要求的最终行粒度和表格布局，不涉及数据库字段。"""

    model_config = ConfigDict(extra="forbid")

    layout: Literal["table", "pivot"] = Field(
        description="普通表格使用 table；同类对象按序横向展开使用 pivot"
    )
    result_row_granularity: str = Field(description="最终结果每一行代表的业务主体")
    dynamic_column_subject: str | None = Field(
        default=None,
        description="pivot 时按列展开的业务对象；table 时传 null",
    )
    dynamic_value_subject: str | None = Field(
        default=None,
        description="pivot 动态列中展示的业务内容；table 时传 null",
    )
    column_label_pattern: str | None = Field(
        default=None,
        description="pivot 动态列标题模板，包含 {index}；table 时传 null",
    )

    # 只在用户要求横向展开时保留动态列定义，普通表格不携带无效塑形参数。
    @model_validator(mode="after")
    def validate_layout_configuration(self) -> "AlignedPresentationRequirement":
        dynamic_values = (
            self.dynamic_column_subject,
            self.dynamic_value_subject,
            self.column_label_pattern,
        )
        if self.layout == "table":
            if any(value is not None for value in dynamic_values):
                raise ValueError("layout 为 table 时全部动态列字段必须为 null")
            return self
        if any(value is None for value in dynamic_values):
            raise ValueError("layout 为 pivot 时全部动态列字段都必须提供")
        assert self.column_label_pattern is not None
        if "{index}" not in self.column_label_pattern:
            raise ValueError("column_label_pattern 必须包含 {index} 占位符")
        return self


class AlignedQueryRequest(BaseModel):
    """业务对齐子图输出给查询规划阶段的无数据库实现查询需求。"""

    model_config = ConfigDict(extra="forbid")

    original_question: str = Field(description="未经改写的用户原问题")
    aligned_question: str = Field(description="只使用标准业务概念改写后的查询需求")
    resolved_concepts: list[ResolvedBusinessConcept] = Field(
        description="已经由词汇表确认的用户词汇与标准概念"
    )
    business_constraints: list[str] = Field(
        description="为后续规划保留的稳定业务规则，不包含数据库实现"
    )
    applied_business_rules: list[str] = Field(
        default_factory=list,
        description="本次对齐实际采用的 core rules.rule 稳定标识",
    )
    logical_constraints: list[AlignedLogicalConstraint] = Field(
        default_factory=list,
        description="从用户问题和业务规则提取的集合量化或数量约束",
    )
    requested_outputs: list[str] = Field(
        default_factory=list,
        description="用户明确要求返回的业务信息",
    )
    presentation_requirements: list[AlignedPresentationRequirement] = Field(
        default_factory=list,
        description="用户明确要求的最终行粒度和表格布局",
    )
    result_scope: Literal["complete", "bounded", "unspecified"] = Field(
        default="unspecified",
        description="完整导出、明确数量范围或未指定结果范围",
    )
    requested_limit: int | None = Field(
        default=None,
        ge=1,
        description="用户明确要求的结果数量；未明确指定时传 null",
    )
    user_clarifications: list[UserInteraction] = Field(
        description="本轮对齐中实际向用户确认并得到的事实；无提问时为空列表"
    )

    # 保证传入规划层的结果范围已经闭合，不让规划模型猜测完整导出是否允许 LIMIT。
    @model_validator(mode="after")
    def validate_result_scope(self) -> "AlignedQueryRequest":
        if self.result_scope == "bounded" and self.requested_limit is None:
            raise ValueError("result_scope 为 bounded 时 requested_limit 不能为空")
        if self.result_scope != "bounded" and self.requested_limit is not None:
            raise ValueError(
                "result_scope 为 complete 或 unspecified 时 requested_limit 必须为 null"
            )
        return self

    # 将已校验的业务语义渲染为规划阶段的唯一上游输入，保留必要约束但不泄漏模型轨迹或数据库实现。
    def render_for_query_planning(self) -> str:
        return render_yaml_context(
            {
                "aligned_query": {
                    "question": self.aligned_question,
                    "resolved_concepts": [
                        {
                            "user_term": concept.user_term,
                            "canonical_term": concept.canonical_term,
                        }
                        for concept in self.resolved_concepts
                    ],
                    "business_constraints": self.business_constraints,
                    "applied_business_rules": self.applied_business_rules,
                    "logical_constraints": [
                        constraint.model_dump()
                        for constraint in self.logical_constraints
                    ],
                    "requested_outputs": self.requested_outputs,
                    "presentation_requirements": [
                        requirement.model_dump()
                        for requirement in self.presentation_requirements
                    ],
                    "result_scope": self.result_scope,
                    "requested_limit": self.requested_limit,
                    "user_clarifications": [
                        {
                            "question": interaction.question,
                            "answer": interaction.answer,
                        }
                        for interaction in self.user_clarifications
                    ],
                }
            }
        )


class SubmittedAlignedQuery(BaseModel):
    """业务对齐模型通过终止工具提交的可生成部分，不包含工作流持有的事实。"""

    model_config = ConfigDict(extra="forbid")

    aligned_question: str = Field(description="只使用标准业务概念改写后的查询需求")
    resolved_concepts: list[ResolvedBusinessConcept] = Field(
        description="已经由词汇表确认的用户词汇与标准概念"
    )
    business_constraints: list[str] = Field(
        description="为后续规划保留的稳定业务规则，不包含数据库实现"
    )
    applied_business_rules: list[str] = Field(
        default_factory=list,
        description="实际采用的 core rules.rule 标识；无时传空列表",
    )
    logical_constraints: list[AlignedLogicalConstraint] = Field(
        default_factory=list,
        description="集合量化或数量业务约束；无时传空列表",
    )
    requested_outputs: list[str] = Field(
        default_factory=list,
        description="用户明确要求返回的业务信息；无时传空列表",
    )
    presentation_requirements: list[AlignedPresentationRequirement] = Field(
        default_factory=list,
        description="最终行粒度和布局要求；无明确要求时传空列表",
    )
    result_scope: Literal["complete", "bounded", "unspecified"] = Field(
        default="unspecified",
        description="完整导出传 complete，明确数量传 bounded，否则传 unspecified",
    )
    requested_limit: int | None = Field(
        default=None,
        ge=1,
        description="用户明确要求的数量；未指定时传 null",
    )

    # 保证完整导出、明确数量和未指定范围使用互不矛盾的数量字段。
    @model_validator(mode="after")
    def validate_result_scope(self) -> "SubmittedAlignedQuery":
        if self.result_scope == "bounded" and self.requested_limit is None:
            raise ValueError("result_scope 为 bounded 时 requested_limit 不能为空")
        if self.result_scope != "bounded" and self.requested_limit is not None:
            raise ValueError(
                "result_scope 为 complete 或 unspecified 时 requested_limit 必须为 null"
            )
        return self


class SubmitAlignedQueryArguments(BaseModel):
    """业务对齐模型提交最终标准化需求时使用的函数调用参数。"""

    model_config = ConfigDict(extra="forbid")

    aligned_request: SubmittedAlignedQuery = Field(
        description=(
            "模型生成的不含数据库实现细节的业务对齐内容；"
            "用户原问题由工作流自动保留，不得提交"
        )
    )


class AlignmentAbandonment(BaseModel):
    """业务对齐无法继续时面向用户的结构化放弃说明。"""

    model_config = ConfigDict(extra="forbid")

    reason: Literal[
        "unresolved_ambiguity", "insufficient_information", "out_of_scope"
    ] = Field(description="放弃原因分类")
    user_message: str = Field(description="向用户说明无法继续的简洁提示")
    unresolved_terms: list[str] = Field(
        description="仍无法确认且影响查询结果的用户表达；无时传空列表"
    )


class AbandonAlignmentArguments(BaseModel):
    """业务对齐模型主动终止当前问题时使用的函数调用参数。"""

    model_config = ConfigDict(extra="forbid")

    reason_type: Literal[
        "unresolved_ambiguity", "insufficient_information", "out_of_scope"
    ] = Field(description="放弃原因分类")
    reason: str = Field(
        description="必须说明无法继续的具体理由，并给出用户可采取的下一步"
    )
    unresolved_terms: list[str] = Field(
        description="仍无法确认且影响查询结果的用户表达；无时传空列表"
    )


# 基于 Pydantic 对齐结果生成终止工具定义，避免模型以普通文本提交不稳定 JSON。
def build_submit_aligned_query_tool_definition() -> dict[str, object]:
    return build_strict_tool_definition(
        tool_name=SUBMIT_ALIGNED_QUERY_TOOL_NAME,
        description="提交最终业务对齐查询需求并结束对齐流程。",
        arguments_model=SubmitAlignedQueryArguments,
    )


# 基于 Pydantic 放弃结果生成终止工具定义，使业务无法继续与技术异常具有不同的可观测状态。
def build_abandon_alignment_tool_definition(
    query_scope: str = "当前业务查询范围",
) -> dict[str, object]:
    return build_strict_tool_definition(
        tool_name=ABANDON_ALIGNMENT_TOOL_NAME,
        description=(
            f"当问题超出{query_scope}、关键歧义在向用户询问后仍无法消除，"
            "或缺少继续对齐所必需的业务信息时，提交放弃原因并结束流程。"
            "不得把可通过词汇表或询问用户解决的问题直接放弃。"
        ),
        arguments_model=AbandonAlignmentArguments,
    )


# 将最终工具参数 JSON 按 Pydantic 严格校验为可传给查询规划阶段的对齐需求。
def parse_submit_aligned_query_arguments(
    arguments_json: str,
) -> SubmitAlignedQueryArguments:
    return SubmitAlignedQueryArguments.model_validate_json(arguments_json)


# 将放弃工具参数 JSON 按 Pydantic 严格校验为可供 Pipeline 正常终止的业务结果。
def parse_abandon_alignment_arguments(arguments_json: str) -> AlignmentAbandonment:
    arguments = AbandonAlignmentArguments.model_validate_json(arguments_json)
    return AlignmentAbandonment(
        reason=arguments.reason_type,
        user_message=arguments.reason,
        unresolved_terms=arguments.unresolved_terms,
    )


class BusinessAlignmentResult(BaseModel):
    """对齐子图运行后的标准需求、问答轨迹和原始模型响应。"""

    model_config = ConfigDict(extra="forbid")

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "abandoned"] = Field(
        default="success", description="对齐成功或已按业务原因主动放弃"
    )
    aligned_request: AlignedQueryRequest | None = Field(
        default=None, description="成功时可传给查询规划阶段的对齐查询需求"
    )
    abandonment: AlignmentAbandonment | None = Field(
        default=None, description="已放弃时面向用户的原因和提示"
    )
    thoughts: list[ThinkingToolArguments] = Field(description="对齐过程中记录的简短关键判断")
    user_interactions: list[UserInteraction] = Field(description="对齐过程中实际发起的用户澄清")
    raw_responses: list[str] = Field(description="每次模型生成的原始响应")
    generation_count: int = Field(description="实际发起的模型生成次数")
    max_generation_count: int = Field(description="允许的最大模型生成次数")

    # 约束成功和放弃两种终态互斥，避免 Pipeline 在放弃后误把空结果交给规划阶段。
    @model_validator(mode="after")
    def validate_terminal_payload(self) -> "BusinessAlignmentResult":
        if self.status == "success":
            if self.aligned_request is None or self.abandonment is not None:
                raise ValueError("业务对齐成功结果必须且只能包含 aligned_request")
        elif self.aligned_request is not None or self.abandonment is None:
            raise ValueError("业务对齐放弃结果必须且只能包含 abandonment")
        return self


class BusinessAlignmentExecutionError(RuntimeError):
    """业务对齐运行失败时保留已获得的原始模型响应，供受限诊断使用。"""

    # 保存安全错误摘要和已序列化响应，使调用方在未获得最终对齐结果时仍可回放失败位置。
    def __init__(self, message: str, raw_responses: list[str]) -> None:
        super().__init__(message)
        self.raw_responses = raw_responses


class BusinessAlignmentPolicyError(ValueError):
    """业务域对齐结果未满足可执行交付约束，需反馈原工具调用修复。"""

    # 保存业务域返回的结构化违规，避免模型只能根据不稳定的自然语言猜测修复方式。
    def __init__(self, issues: tuple[AlignmentPolicyIssue, ...]) -> None:
        super().__init__("业务对齐结果不满足业务交付约束")
        self.issues = issues


# 按业务域声明的受保护标识符构造完整词匹配，避免对齐层泄漏数据库实现。
def _build_database_identifier_pattern(
    profile: QueryDomainProfile,
) -> re.Pattern[str]:
    escaped_identifiers = sorted(
        (re.escape(identifier) for identifier in profile.protected_database_identifiers),
        key=len,
        reverse=True,
    )
    return re.compile(
        r"\b(?:" + "|".join(escaped_identifiers) + r")\b",
        flags=re.IGNORECASE,
    )


# 校验业务对齐文本不包含当前业务域的表名或字段名，错误继续走原工具修复循环。
def _validate_business_only_texts(
    pattern: re.Pattern[str],
    texts: list[str],
    payload_name: str,
) -> None:
    for text in texts:
        matched_identifier = pattern.search(text)
        if matched_identifier is not None:
            raise ValueError(
                f"{payload_name}不能包含数据库表名或字段名："
                f"{matched_identifier.group(0)}"
            )


# 将业务文本越界作为可修复工具结果返回，明确要求删除数据库标识并改用业务语言。
def _build_business_text_validation_message(
    tool_call_id: str,
    tool_name: str,
    error: ValueError,
) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(
            {
                "status": "failure",
                "error": {
                    "code": "business_text_contains_database_identifier",
                    "tool_name": tool_name,
                    "message": str(error),
                    "details": [
                        {
                            "error_type": "database_identifier_forbidden",
                            "field_path": "$",
                            "message": str(error),
                            "repair_action": (
                                "删除错误中指出的数据库表名或字段名，"
                                "仅使用用户能够理解的业务概念重新提交完整工具参数。"
                            ),
                        }
                    ],
                },
                "retryable": True,
                "next_action": "按 repair_action 修复后重新调用同一终止工具。",
            },
            ensure_ascii=False,
        ),
    }


# 将业务域对齐约束转换为原终止工具的结构化失败结果，供模型在同一上下文中精确修复。
def _build_alignment_policy_validation_message(
    tool_call_id: str,
    tool_name: str,
    issues: tuple[AlignmentPolicyIssue, ...],
) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(
            {
                "status": "failure",
                "error": {
                    "code": "alignment_business_policy_violation",
                    "tool_name": tool_name,
                    "message": "业务对齐结果不满足业务交付约束",
                    "details": [
                        {
                            "error_type": "business_delivery_requirement_missing",
                            "field_path": issue.field_path,
                            "message": issue.message,
                            "repair_action": issue.repair_action,
                        }
                        for issue in issues
                    ],
                },
                "retryable": True,
                "next_action": "按每项 repair_action 修复后重新调用同一终止工具。",
            },
            ensure_ascii=False,
        ),
    }


class _BusinessAlignmentState(TypedDict):
    """独立业务对齐子图在单个循环节点前后传递的状态。"""

    user_question: str
    max_generation_count: int
    result: BusinessAlignmentResult


# 在真实联调中显示对齐模型的文本和工具调用，便于观察词汇命中与歧义判断是否合理。
def _format_alignment_trace(
    generation_index: int,
    max_generation_count: int,
    message: Any,
    tool_calls: list[Any],
) -> str:
    sections = [
        "\n" + "=" * 14 + f" 业务对齐第 {generation_index} / {max_generation_count} 次模型调用 " + "=" * 14
    ]
    reasoning_content = getattr(message, "reasoning_content", None)
    if reasoning_content:
        sections.append(f"【模型思考】\n{reasoning_content}")
    content = getattr(message, "content", None)
    if content:
        sections.append(f"【模型输出】\n{content}")
    if tool_calls:
        sections.append(
            "【工具调用】\n"
            + "\n".join(
                f"- `{tool_call.function.name}`（{tool_call.id}）\n{tool_call.function.arguments}"
                for tool_call in tool_calls
            )
        )
    else:
        sections.append("【工具调用】\n模型未调用工具。")
    return "\n\n".join(sections)


# 将 OpenAI 兼容响应完整序列化为内部诊断信息，避免摘要掩盖模型实际返回内容。
def _serialize_raw_response(response: Any) -> str:
    model_dump_json = getattr(response, "model_dump_json", None)
    if callable(model_dump_json):
        return model_dump_json(indent=2)
    return repr(response)


# 在调用方未注入交互出口时明确终止，正式服务禁止退化为不可见的控制台输入。
def _raise_missing_user_interaction(_: str) -> str:
    raise RuntimeError("业务对齐需要用户回答，但未配置用户交互出口")


class BusinessAlignmentSubgraph:
    """仅使用业务词汇表和四个受限函数调用的独立 LangGraph 业务对齐子图。"""

    # 初始化模型、固定业务上下文和受限函数调用，不接收表结构、关系或规划状态。
    def __init__(
        self,
        client: Any,
        model: str,
        domain_profile: QueryDomainProfile,
        user_input_reader: UserInputReader = _raise_missing_user_interaction,
        trace_writer: TraceWriter | None = None,
        progress_emitter: ProgressEmitter | None = None,
        max_tokens: int = DEFAULT_ALIGNMENT_MAX_TOKENS,
    ) -> None:
        self._client = client
        self._model = model
        self._domain_profile = domain_profile
        self._base_prompt = build_business_alignment_prompt(domain_profile)
        self._database_identifier_pattern = _build_database_identifier_pattern(
            domain_profile
        )
        self._allowed_business_rule_ids = frozenset(
            str(record["rule"])
            for record in parse_tagged_context_records(
                domain_profile.core_rules_path.read_text(encoding="utf-8").strip()
            )
            if "rule" in record
        )
        self._user_input_reader = user_input_reader
        self._trace_writer = trace_writer
        self._progress_reporter = AgentProgressReporter(
            domain_profile,
            progress_emitter,
        )
        self._max_tokens = max_tokens

        workflow = StateGraph(_BusinessAlignmentState)
        workflow.add_node("run_alignment_loop", self._run_alignment_loop)
        workflow.add_edge(START, "run_alignment_loop")
        workflow.add_edge("run_alignment_loop", END)
        self._workflow = workflow.compile()

    # 从应用配置创建真实 DeepSeek 客户端，业务对齐阶段不创建数据库或表结构读取器。
    @classmethod
    def from_settings(
        cls,
        domain_profile: QueryDomainProfile,
        settings: Settings | None = None,
        user_input_reader: UserInputReader = _raise_missing_user_interaction,
        trace_writer: TraceWriter | None = None,
        progress_emitter: ProgressEmitter | None = None,
    ) -> "BusinessAlignmentSubgraph":
        resolved_settings = settings or get_settings()
        if resolved_settings.deepseek_api_key is None:
            raise RuntimeError("未配置 DEEPSEEK_API_KEY，无法执行业务对齐")
        client = OpenAI(
            api_key=resolved_settings.deepseek_api_key.get_secret_value(),
            base_url=build_strict_tools_base_url(str(resolved_settings.deepseek_base_url)),
            timeout=resolved_settings.deepseek_http_timeout_seconds,
        )
        return cls(
            client=client,
            model=resolved_settings.deepseek_model,
            domain_profile=domain_profile,
            user_input_reader=user_input_reader,
            trace_writer=trace_writer,
            progress_emitter=progress_emitter,
            max_tokens=resolved_settings.deepseek_query_alignment_max_tokens,
        )

    # 在显式启用内部追踪时记录对齐响应和问答结果，默认不向标准输出泄漏模型轨迹。
    def _write_trace(self, content: str) -> None:
        if self._trace_writer is not None:
            self._trace_writer(content)

    # 循环处理思考、用户澄清、成功提交和正常放弃工具，任何普通文本响应都视为不符合对齐协议。
    def _run_alignment_loop(
        self,
        state: _BusinessAlignmentState,
    ) -> dict[str, BusinessAlignmentResult]:
        messages: list[Any] = [
            {"role": "system", "content": self._base_prompt},
            {"role": "user", "content": state["user_question"]},
        ]
        raw_responses: list[str] = []
        user_interactions: list[UserInteraction] = []
        thoughts: list[ThinkingToolArguments] = []
        terminal_argument_repair_count = 0
        max_generation_count = state["max_generation_count"]
        tools = [
            build_thinking_tool_definition(),
            build_ask_user_tool_definition(),
            build_submit_aligned_query_tool_definition(),
            build_abandon_alignment_tool_definition(self._domain_profile.query_scope),
        ]

        for generation_count in range(1, max_generation_count + 1):
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=tools,
                tool_choice=_build_alignment_tool_choice(generation_count),
                **build_non_thinking_completion_options(self._max_tokens),
            )
            raw_responses.append(_serialize_raw_response(response))
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None) or []
            self._write_trace(
                _format_alignment_trace(
                    generation_count,
                    max_generation_count,
                    message,
                    tool_calls,
                )
            )
            if not tool_calls:
                content_preview = (getattr(message, "content", None) or "").strip()[:400]
                raise BusinessAlignmentExecutionError(
                    "业务对齐模型未调用规定工具；"
                    f"模型文本：{content_preview or '（空）'}",
                    raw_responses,
                )
            messages.append(message)
            terminal_tool_calls = [
                tool_call
                for tool_call in tool_calls
                if tool_call.function.name
                in {SUBMIT_ALIGNED_QUERY_TOOL_NAME, ABANDON_ALIGNMENT_TOOL_NAME}
            ]
            if len(terminal_tool_calls) > 1:
                raise BusinessAlignmentExecutionError(
                    "同一轮只能调用一次终止工具",
                    raw_responses,
                )
            if terminal_tool_calls and any(
                tool_call.function.name == ASK_USER_TOOL_NAME
                for tool_call in tool_calls
            ):
                raise BusinessAlignmentExecutionError(
                    "终止工具不能与 ask_user 在同一轮调用",
                    raw_responses,
                )

            for tool_call in tool_calls:
                tool_name = tool_call.function.name
                if tool_name in {
                    SUBMIT_ALIGNED_QUERY_TOOL_NAME,
                    ABANDON_ALIGNMENT_TOOL_NAME,
                }:
                    continue
                try:
                    if tool_name == THINKING_TOOL_NAME:
                        thought = parse_thinking_tool_arguments(
                            tool_call.function.arguments
                        )
                        thoughts.append(thought)
                        self._progress_reporter.reasoning_progress("alignment")
                        tool_result: dict[str, Any] = {
                            "status": "success",
                            "result": f"已记录关键判断：{thought.reason}",
                        }
                    elif tool_name == ASK_USER_TOOL_NAME:
                        arguments = parse_ask_user_tool_arguments(
                            tool_call.function.arguments
                        )
                        interaction = UserInteraction(
                            question=arguments.question,
                            answer=self._user_input_reader(arguments.question),
                        )
                        user_interactions.append(interaction)
                        tool_result = {
                            "status": "success",
                            "result": interaction.model_dump(),
                        }
                    else:
                        raise BusinessAlignmentExecutionError(
                            f"业务对齐模型调用了未注册工具：{tool_name}",
                            raw_responses,
                        )
                except ValidationError as error:
                    error_message = build_tool_argument_error_message(
                        tool_call.id,
                        tool_name,
                        error,
                    )
                    messages.append(error_message)
                    self._write_trace(
                        "\n----- 业务对齐工具参数校验结果 -----\n"
                        f"tool_call_id: {tool_call.id}\n"
                        f"tool_name: {tool_name}\n"
                        f"result: {error_message['content']}"
                    )
                    continue
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": render_yaml_context(tool_result),
                    }
                )
                self._write_trace(
                    "\n----- 业务对齐工具执行结果 -----\n"
                    f"tool_call_id: {tool_call.id}\n"
                    f"tool_name: {tool_name}\n"
                    f"result: {json.dumps(tool_result, ensure_ascii=False, indent=2)}"
                )

            if terminal_tool_calls:
                terminal_tool_call = terminal_tool_calls[0]
                if terminal_tool_call.function.name == ABANDON_ALIGNMENT_TOOL_NAME:
                    try:
                        abandonment = parse_abandon_alignment_arguments(
                            terminal_tool_call.function.arguments
                        )
                        _validate_business_only_texts(
                            self._database_identifier_pattern,
                            [abandonment.user_message, *abandonment.unresolved_terms],
                            "业务对齐放弃结果",
                        )
                    except (ValidationError, ValueError) as error:
                        if terminal_argument_repair_count < MAX_TERMINAL_ARGUMENT_REPAIR_COUNT:
                            terminal_argument_repair_count += 1
                            error_message = (
                                build_tool_argument_error_message(
                                    terminal_tool_call.id,
                                    ABANDON_ALIGNMENT_TOOL_NAME,
                                    error,
                                )
                                if isinstance(error, ValidationError)
                                else _build_business_text_validation_message(
                                    terminal_tool_call.id,
                                    ABANDON_ALIGNMENT_TOOL_NAME,
                                    error,
                                )
                            )
                            messages.append(error_message)
                            self._write_trace(
                                "\n----- 业务对齐终止工具参数修复 -----\n"
                                f"tool_call_id: {terminal_tool_call.id}\n"
                                f"tool_name: {ABANDON_ALIGNMENT_TOOL_NAME}\n"
                                f"result: {error_message['content']}"
                            )
                            continue
                        raise BusinessAlignmentExecutionError(
                            "业务对齐放弃工具参数不符合约束",
                            raw_responses,
                        ) from error
                    return {
                        "result": BusinessAlignmentResult(
                            status="abandoned",
                            abandonment=abandonment,
                            thoughts=thoughts,
                            user_interactions=user_interactions,
                            raw_responses=raw_responses,
                            generation_count=generation_count,
                            max_generation_count=max_generation_count,
                        )
                    }
                try:
                    submitted_alignment = parse_submit_aligned_query_arguments(
                        terminal_tool_call.function.arguments
                    ).aligned_request
                    # 工作流注入不可由模型改写的原问题和实际问答，保证审计事实来源唯一。
                    aligned_request = AlignedQueryRequest(
                        original_question=state["user_question"],
                        user_clarifications=user_interactions,
                        **submitted_alignment.model_dump(),
                    )
                    _validate_business_only_texts(
                        self._database_identifier_pattern,
                        [
                            aligned_request.aligned_question,
                            *aligned_request.business_constraints,
                            *aligned_request.requested_outputs,
                            *(
                                constraint.subject
                                for constraint in aligned_request.logical_constraints
                            ),
                            *(
                                constraint.collection
                                for constraint in aligned_request.logical_constraints
                            ),
                            *(
                                constraint.predicate
                                for constraint in aligned_request.logical_constraints
                            ),
                            *(
                                requirement.result_row_granularity
                                for requirement in aligned_request.presentation_requirements
                            ),
                            *(
                                requirement.dynamic_column_subject or ""
                                for requirement in aligned_request.presentation_requirements
                            ),
                            *(
                                requirement.dynamic_value_subject or ""
                                for requirement in aligned_request.presentation_requirements
                            ),
                            *(
                                requirement.column_label_pattern or ""
                                for requirement in aligned_request.presentation_requirements
                            ),
                            *(
                                concept.canonical_term
                                for concept in aligned_request.resolved_concepts
                            ),
                            *(
                                concept.alignment_reason
                                for concept in aligned_request.resolved_concepts
                            ),
                        ],
                        "业务对齐输出",
                    )
                    unknown_rule_ids = sorted(
                        set(aligned_request.applied_business_rules)
                        - self._allowed_business_rule_ids
                    )
                    if unknown_rule_ids:
                        raise BusinessAlignmentPolicyError(
                            (
                                AlignmentPolicyIssue(
                                    field_path=(
                                        "aligned_request.applied_business_rules"
                                    ),
                                    message=(
                                        "存在核心规则文件中未定义的 rule 标识："
                                        + "、".join(unknown_rule_ids)
                                    ),
                                    repair_action=(
                                        "删除上述未定义标识；只保留输入 rules 中"
                                        "实际用于本次对齐的 rule 值。"
                                    ),
                                ),
                            )
                        )
                    alignment_issues = self._domain_profile.validate_alignment(
                        state["user_question"],
                        aligned_request.aligned_question,
                        tuple(aligned_request.business_constraints),
                    )
                    if alignment_issues:
                        raise BusinessAlignmentPolicyError(alignment_issues)
                except (ValidationError, ValueError) as error:
                    if terminal_argument_repair_count < MAX_TERMINAL_ARGUMENT_REPAIR_COUNT:
                        terminal_argument_repair_count += 1
                        error_message = (
                            build_tool_argument_error_message(
                                terminal_tool_call.id,
                                SUBMIT_ALIGNED_QUERY_TOOL_NAME,
                                error,
                            )
                            if isinstance(error, ValidationError)
                            else _build_alignment_policy_validation_message(
                                terminal_tool_call.id,
                                SUBMIT_ALIGNED_QUERY_TOOL_NAME,
                                error.issues,
                            )
                            if isinstance(error, BusinessAlignmentPolicyError)
                            else _build_business_text_validation_message(
                                terminal_tool_call.id,
                                SUBMIT_ALIGNED_QUERY_TOOL_NAME,
                                error,
                            )
                        )
                        messages.append(error_message)
                        self._write_trace(
                            "\n----- 业务对齐终止工具参数修复 -----\n"
                            f"tool_call_id: {terminal_tool_call.id}\n"
                            f"tool_name: {SUBMIT_ALIGNED_QUERY_TOOL_NAME}\n"
                            f"result: {error_message['content']}"
                        )
                        continue
                    raise BusinessAlignmentExecutionError(
                        "业务对齐最终工具参数不符合约束",
                        raw_responses,
                    ) from error
                return {
                    "result": BusinessAlignmentResult(
                        status="success",
                        aligned_request=aligned_request,
                        thoughts=thoughts,
                        user_interactions=user_interactions,
                        raw_responses=raw_responses,
                        generation_count=generation_count,
                        max_generation_count=max_generation_count,
                    )
                }
        raise BusinessAlignmentExecutionError(
            f"业务对齐模型生成次数超过最大限制：{max_generation_count}",
            raw_responses,
        )

    # 运行独立对齐子图，输入只接受一条非空用户问题并限制模型生成次数。
    def run(
        self,
        user_question: str,
        max_generation_count: int = DEFAULT_MAX_ALIGNMENT_GENERATION_COUNT,
    ) -> BusinessAlignmentResult:
        normalized_question = user_question.strip()
        if not normalized_question:
            raise ValueError("用户问题不能为空")
        if (
            isinstance(max_generation_count, bool)
            or not isinstance(max_generation_count, int)
            or max_generation_count < 1
        ):
            raise ValueError("最大模型生成次数必须是大于零的整数")
        state = self._workflow.invoke(
            {
                "user_question": normalized_question,
                "max_generation_count": max_generation_count,
            }
        )
        return state["result"]
