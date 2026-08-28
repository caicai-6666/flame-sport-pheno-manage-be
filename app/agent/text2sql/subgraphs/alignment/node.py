"""定义业务对齐子图的状态、节点及其运行逻辑。"""

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, Final, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agent.text2sql.domains.base import AlignmentPolicyIssue, QueryDomainProfile
from app.agent.text2sql.events.publisher import AgentProgressReporter, ProgressEmitter
from app.core.config import Settings, get_settings
from app.agent.text2sql.shared.tools.ask_user import (
    UserInteraction,
)
from app.agent.text2sql.subgraphs.alignment.prompt.prompt import (
    build_business_alignment_prompt,
)
from app.agent.text2sql.shared.model_options import (
    DEFAULT_ALIGNMENT_MAX_TOKENS,
    get_model_request_profile,
    resolve_model_provider_connection,
)
from app.agent.text2sql.shared.tool_tag_template import (
    build_tool_tag_prefixed_task_content,
    load_tool_tag_template,
    resolve_query_tool_tag_template_filename,
)
from app.agent.text2sql.shared.yaml_context import (
    parse_tagged_context_records,
    render_yaml_context,
)
from app.agent.text2sql.shared.tools.argument_feedback import (
    build_tool_argument_error_message,
)
from app.agent.text2sql.subgraphs.alignment.tool import (
    ABANDON_ALIGNMENT_TOOL_NAME,
    ASK_USER_TOOL_NAME,
    SUBMIT_ALIGNED_QUERY_TOOL_NAME,
    THINKING_TOOL_NAME,
    AlignedLogicalConstraint,
    AlignedPresentationRequirement,
    AlignmentAbandonment,
    ResolvedBusinessConcept,
    ThinkingToolArguments,
    build_abandon_alignment_tool_definition,
    build_ask_user_tool_definition,
    build_submit_aligned_query_tool_definition,
    build_thinking_tool_definition,
    parse_abandon_alignment_arguments,
    parse_ask_user_tool_arguments,
    parse_submit_aligned_query_arguments,
    parse_thinking_tool_arguments,
)


DEFAULT_MAX_ALIGNMENT_GENERATION_COUNT: Final[int] = 4
MAX_TERMINAL_ARGUMENT_REPAIR_COUNT: Final[int] = 1
ALIGNMENT_TOOL_CHOICE: Final[str] = "auto"
TraceWriter = Callable[[str], None]
UserInputReader = Callable[[str], Awaitable[str]]


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


# 将工具选择或调用顺序错误作为原工具调用的可重试结果返回模型。
def _build_alignment_protocol_tool_error_message(
    tool_call_id: str,
    tool_name: str,
    error_code: str,
    message: str,
    repair_action: str,
) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(
            {
                "status": "failure",
                "error": {
                    "code": error_code,
                    "tool_name": tool_name,
                    "message": message,
                    "details": [
                        {
                            "error_type": "tool_protocol_violation",
                            "field_path": "$",
                            "message": message,
                            "repair_action": repair_action,
                        }
                    ],
                },
                "retryable": True,
                "next_action": repair_action,
            },
            ensure_ascii=False,
        ),
    }


# 在模型没有产生 tool_call_id 时返回协议错误，并按配置附加对应模型的工具标签格式。
def _build_missing_tool_call_feedback_message(
    tool_tag_template: str | None,
) -> dict[str, str]:
    error: dict[str, Any] = {
        "code": "alignment_tool_call_missing",
        "message": "本轮没有调用工具，普通文本不能推进业务对齐流程。",
        "repair_action": (
            "立即重新生成本轮响应，并且只调用一个已提供工具；"
            "首个有效工具调用必须是 think。"
        ),
    }
    if tool_tag_template is not None:
        error["repair_action"] = (
            f"{error['repair_action']} 必须严格按照下方 tool-tag 格式输出，"
            "确保服务端能够解析出 tool_calls，不能把工具调用写成普通文本。"
        )
        error["tool_call_format_guidance"] = {
            "instruction": (
                "严格仿照 template 的标签结构调用本轮唯一工具；"
                "工具名和参数必须替换为本轮真实值。"
            ),
            "template": tool_tag_template,
        }
    return {
        "role": "user",
        "content": json.dumps(
            {
                "context_type": "workflow_protocol_feedback",
                "status": "failure",
                "error": error,
                "retryable": True,
            },
            ensure_ascii=False,
        ),
    }


# 将稳定工具标签放在动态问题之前，兼顾 auto 工具解析与跨请求前缀缓存。
def _build_alignment_task_message(
    user_question: str,
    tool_tag_template: str | None,
) -> dict[str, str]:
    return {
        "role": "user",
        "content": build_tool_tag_prefixed_task_content(
            user_question,
            tool_tag_template,
            (
                "本任务必须通过工具调用推进。请严格按照以下 tool-tag 结构输出，"
                "使服务端能够解析出 OpenAI tool_calls；不要把工具调用标签或参数"
                "当作普通正文解释。工具名和参数必须替换为本轮真实值。"
            ),
            "用户问题",
        ),
    }


# 在模型完成修正后移除无效响应及错误反馈，避免历史错误继续影响后续工具选择。
def _remove_repaired_context(
    messages: list[Any],
    repair_context_start: int | None,
    corrected_turn_start: int,
) -> bool:
    if repair_context_start is None:
        return False
    del messages[repair_context_start:corrected_turn_start]
    return True


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


# 在调用方未注入异步交互出口时明确终止，正式服务禁止退化为不可见的控制台输入。
async def _raise_missing_user_interaction(_: str) -> str:
    raise RuntimeError("业务对齐需要用户回答，但未配置用户交互出口")


class BusinessAlignmentSubgraph:
    """仅使用业务词汇表和四个受限函数调用的独立 LangGraph 业务对齐子图。"""

    # 初始化模型、请求体系、纠错模板和客户端所有权，不接收表结构、关系或规划状态。
    def __init__(
        self,
        client: Any,
        model: str,
        domain_profile: QueryDomainProfile,
        user_input_reader: UserInputReader = _raise_missing_user_interaction,
        trace_writer: TraceWriter | None = None,
        progress_emitter: ProgressEmitter | None = None,
        max_tokens: int = DEFAULT_ALIGNMENT_MAX_TOKENS,
        close_client_after_run: bool = False,
        request_profile: str = "deepseek",
        tool_tag_template: str | None = None,
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
        self._close_client_after_run = close_client_after_run
        self._request_profile = get_model_request_profile(request_profile)
        self._tool_tag_template = tool_tag_template

        workflow = StateGraph(_BusinessAlignmentState)
        workflow.add_node("run_alignment_loop", self._run_alignment_loop)
        workflow.add_edge(START, "run_alignment_loop")
        workflow.add_edge("run_alignment_loop", END)
        self._workflow = workflow.compile()

    # 从标准地址创建异步 OpenAI 兼容客户端，并按配置装配请求体系与纠错模板。
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
        connection = resolve_model_provider_connection(resolved_settings)
        client = AsyncOpenAI(
            api_key=connection.api_key,
            base_url=connection.base_url,
            timeout=connection.timeout_seconds,
        )
        return cls(
            client=client,
            model=connection.model,
            domain_profile=domain_profile,
            user_input_reader=user_input_reader,
            trace_writer=trace_writer,
            progress_emitter=progress_emitter,
            max_tokens=resolved_settings.deepseek_query_alignment_max_tokens,
            close_client_after_run=True,
            request_profile=connection.provider,
            tool_tag_template=load_tool_tag_template(
                resolve_query_tool_tag_template_filename(resolved_settings)
            ),
        )

    # 在显式启用内部追踪时记录对齐响应和问答结果，默认不向标准输出泄漏模型轨迹。
    def _write_trace(self, content: str) -> None:
        if self._trace_writer is not None:
            self._trace_writer(content)

    # 循环处理四类工具并反馈可修复协议错误，修正后清除临时错误上下文但保留诊断轨迹。
    async def _run_alignment_loop(
        self,
        state: _BusinessAlignmentState,
    ) -> dict[str, BusinessAlignmentResult]:
        messages: list[Any] = [
            {"role": "system", "content": self._base_prompt},
            _build_alignment_task_message(
                state["user_question"],
                self._tool_tag_template,
            ),
        ]
        raw_responses: list[str] = []
        user_interactions: list[UserInteraction] = []
        thoughts: list[ThinkingToolArguments] = []
        terminal_argument_repair_count = 0
        repair_context_start: int | None = None
        initial_think_completed = False
        max_generation_count = state["max_generation_count"]
        tools = [
            build_thinking_tool_definition(),
            build_ask_user_tool_definition(),
            build_submit_aligned_query_tool_definition(),
            build_abandon_alignment_tool_definition(self._domain_profile.query_scope),
        ]

        for generation_count in range(1, max_generation_count + 1):
            current_turn_start = len(messages)
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=tools,
                tool_choice=ALIGNMENT_TOOL_CHOICE,
                **self._request_profile.build_non_thinking_options(
                    self._max_tokens
                ),
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
                if repair_context_start is None:
                    repair_context_start = current_turn_start
                feedback_message = _build_missing_tool_call_feedback_message(
                    self._tool_tag_template
                )
                messages.extend((message, feedback_message))
                self._write_trace(
                    "\n----- 业务对齐工具协议校验结果 -----\n"
                    "error_code: alignment_tool_call_missing\n"
                    f"result: {feedback_message['content']}"
                )
                continue
            messages.append(message)
            if len(tool_calls) != 1:
                if repair_context_start is None:
                    repair_context_start = current_turn_start
                for tool_call in tool_calls:
                    error_message = _build_alignment_protocol_tool_error_message(
                        tool_call.id,
                        tool_call.function.name,
                        "alignment_multiple_tool_calls",
                        "业务对齐每轮必须且只能调用一个工具，本轮调用了多个工具。",
                        "重新生成本轮响应，并且只调用一个符合当前状态的工具。",
                    )
                    messages.append(error_message)
                    self._write_trace(
                        "\n----- 业务对齐工具协议校验结果 -----\n"
                        f"tool_call_id: {tool_call.id}\n"
                        f"tool_name: {tool_call.function.name}\n"
                        f"result: {error_message['content']}"
                    )
                continue

            tool_call = tool_calls[0]
            tool_name = tool_call.function.name
            if not initial_think_completed and tool_name != THINKING_TOOL_NAME:
                if repair_context_start is None:
                    repair_context_start = current_turn_start
                error_message = _build_alignment_protocol_tool_error_message(
                    tool_call.id,
                    tool_name,
                    "alignment_initial_think_required",
                    "首个有效工具调用必须是 think，本次工具调用未执行。",
                    "只调用 think 提交一条简短的业务对齐关键判断。",
                )
                messages.append(error_message)
                self._write_trace(
                    "\n----- 业务对齐工具顺序校验结果 -----\n"
                    f"tool_call_id: {tool_call.id}\n"
                    f"tool_name: {tool_name}\n"
                    f"result: {error_message['content']}"
                )
                continue

            registered_tool_names = {
                THINKING_TOOL_NAME,
                ASK_USER_TOOL_NAME,
                SUBMIT_ALIGNED_QUERY_TOOL_NAME,
                ABANDON_ALIGNMENT_TOOL_NAME,
            }
            if tool_name not in registered_tool_names:
                if repair_context_start is None:
                    repair_context_start = current_turn_start
                error_message = _build_alignment_protocol_tool_error_message(
                    tool_call.id,
                    tool_name,
                    "alignment_unknown_tool",
                    f"工具 `{tool_name}` 未在业务对齐层注册，本次调用未执行。",
                    "从本轮提供的工具列表中选择并且只调用一个工具。",
                )
                messages.append(error_message)
                self._write_trace(
                    "\n----- 业务对齐工具协议校验结果 -----\n"
                    f"tool_call_id: {tool_call.id}\n"
                    f"tool_name: {tool_name}\n"
                    f"result: {error_message['content']}"
                )
                continue

            terminal_tool_calls = (
                [tool_call]
                if tool_name
                in {SUBMIT_ALIGNED_QUERY_TOOL_NAME, ABANDON_ALIGNMENT_TOOL_NAME}
                else []
            )
            if not terminal_tool_calls:
                try:
                    if tool_name == THINKING_TOOL_NAME:
                        thought = parse_thinking_tool_arguments(
                            tool_call.function.arguments
                        )
                        thoughts.append(thought)
                        initial_think_completed = True
                        self._progress_reporter.reasoning_progress("alignment")
                        tool_result: dict[str, Any] = {
                            "status": "success",
                            "result": f"已记录关键判断：{thought.reason}",
                        }
                    else:
                        arguments = parse_ask_user_tool_arguments(
                            tool_call.function.arguments
                        )
                        interaction = UserInteraction(
                            question=arguments.question,
                            answer=await self._user_input_reader(arguments.question),
                        )
                        user_interactions.append(interaction)
                        tool_result = {
                            "status": "success",
                            "result": interaction.model_dump(),
                        }
                except ValidationError as error:
                    if repair_context_start is None:
                        repair_context_start = current_turn_start
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
                if _remove_repaired_context(
                    messages,
                    repair_context_start,
                    current_turn_start,
                ):
                    repair_context_start = None
                    self._write_trace(
                        "\n----- 业务对齐修复上下文清理 -----\n"
                        "模型已完成协议或参数修正；此前无效响应及错误反馈已从后续模型上下文移除。"
                    )
                continue

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
                            if repair_context_start is None:
                                repair_context_start = current_turn_start
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
                        tuple(aligned_request.applied_business_rules),
                        tuple(aligned_request.logical_constraints),
                    )
                    if alignment_issues:
                        raise BusinessAlignmentPolicyError(alignment_issues)
                except (ValidationError, ValueError) as error:
                    if terminal_argument_repair_count < MAX_TERMINAL_ARGUMENT_REPAIR_COUNT:
                        terminal_argument_repair_count += 1
                        if repair_context_start is None:
                            repair_context_start = current_turn_start
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

    # 异步运行独立对齐子图，模型请求和用户澄清等待均不阻塞调用方事件循环。
    async def run(
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
        try:
            state = await self._workflow.ainvoke(
                {
                    "user_question": normalized_question,
                    "max_generation_count": max_generation_count,
                }
            )
        finally:
            if self._close_client_after_run:
                await self._client.close()
        return state["result"]
