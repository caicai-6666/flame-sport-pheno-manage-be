"""使用 Pydantic 定义业务对齐子图的全部 Function Calling 工具协议。"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.text2sql.shared.tools.pydantic_schema import (
    build_pydantic_tool_definition,
)
from app.agent.text2sql.shared.tools.argument_compatibility import (
    validate_tool_arguments_with_embedded_json_fallback,
)


THINKING_TOOL_NAME: Final[str] = "think"
ASK_USER_TOOL_NAME: Final[str] = "ask_user"
SUBMIT_ALIGNED_QUERY_TOOL_NAME: Final[str] = "submit_aligned_query"
ABANDON_ALIGNMENT_TOOL_NAME: Final[str] = "abandon_alignment"


class ThinkingToolArguments(BaseModel):
    """记录决定业务对齐下一步动作所必需的一条简短判断，不输出完整推理过程。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        max_length=240,
        description=(
            "直接决定下一步是继续对齐、询问用户、提交结果还是放弃的一条关键判断；"
            "不得复述用户问题、表结构或罗列多个备选方案"
        ),
    )


class AskUserToolArguments(BaseModel):
    """当关键业务歧义无法从已有上下文消除时，向用户提出一个必要的澄清问题。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(
        description=(
            "需要用户回答的一个简洁、具体问题；一次只询问一个会改变查询结果的关键事实，"
            "不得询问可由已有业务知识确认的内容，也不得使用数据库或技术术语"
        )
    )


class ResolvedBusinessConcept(BaseModel):
    """描述用户原始表达与标准业务概念之间的一项确定映射。"""

    model_config = ConfigDict(extra="forbid")

    user_term: str = Field(description="用户问题中实际出现的原始业务表达")
    canonical_term: str = Field(description="业务词汇表中与原始表达对应的标准业务概念")
    alignment_reason: str = Field(
        description="说明该映射依据的词汇表定义或稳定业务规则，不得引用数据库实现"
    )


class AlignedLogicalConstraint(BaseModel):
    """描述查询主体关联集合必须满足的全部、任一、数量等业务约束。"""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(description="量化约束作用的业务主体，例如参与赛季的用户")
    collection: str = Field(description="需要判断的主体关联对象集合，例如用户锁定的运动项目")
    quantifier: Literal[
        "all", "any", "none", "exactly", "at_least", "at_most"
    ] = Field(
        description=(
            "集合量词：all 表示全部，any 表示至少一个，none 表示没有，exactly 表示恰好，"
            "at_least 表示至少指定数量，at_most 表示至多指定数量"
        )
    )
    predicate: str = Field(description="集合中每个成员需要判断的标准业务条件")
    count: int | None = Field(
        default=None,
        ge=0,
        description=(
            "exactly、at_least、at_most 对应的非负数量；"
            "quantifier 为 all、any 或 none 时必须传 null"
        ),
    )

    # 保证数量量词与 count 同时出现，避免规划层重新猜测数量约束。
    @model_validator(mode="after")
    def validate_quantifier_count(self) -> "AlignedLogicalConstraint":
        count_quantifiers = {"exactly", "at_least", "at_most"}
        if self.quantifier in count_quantifiers and self.count is None:
            raise ValueError(f"量词 {self.quantifier} 必须提供 count")
        if self.quantifier not in count_quantifiers and self.count is not None:
            raise ValueError(f"量词 {self.quantifier} 的 count 必须为 null")
        return self


class AlignedPresentationRequirement(BaseModel):
    """描述用户要求的最终结果行粒度及普通表格或横向展开布局。"""

    model_config = ConfigDict(extra="forbid")

    layout: Literal["table", "pivot"] = Field(
        description="普通逐行结果使用 table；同类对象按序展开为多列时使用 pivot"
    )
    result_row_granularity: str = Field(
        description="最终结果每一行代表的唯一业务主体，不得使用表名或字段名"
    )
    dynamic_column_subject: str | None = Field(
        default=None,
        description="pivot 时按序展开为多列的业务对象；table 时必须传 null",
    )
    dynamic_value_subject: str | None = Field(
        default=None,
        description="pivot 动态列中实际展示的业务内容；table 时必须传 null",
    )
    column_label_pattern: str | None = Field(
        default=None,
        description="pivot 动态列标题模板且必须包含 {index}；table 时必须传 null",
    )

    # 保证普通表格与横向展开只携带各自有效的布局参数。
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


class SubmittedAlignedQuery(BaseModel):
    """描述提交给查询规划层的完整业务需求，但不包含工作流自动保存的原问题和问答。"""

    model_config = ConfigDict(extra="forbid")

    aligned_question: str = Field(
        description=(
            "使用标准业务概念完整改写后的查询需求；必须保留查询主体、筛选条件、"
            "返回内容和展示要求，不得包含表名、字段名、SQL 或连接方式"
        )
    )
    resolved_concepts: list[ResolvedBusinessConcept] = Field(
        description="本次实际完成的用户表达与标准业务概念映射；没有映射时传空列表"
    )
    business_constraints: list[str] = Field(
        description="本次查询必须遵守的稳定业务规则；不得写入数据库实现；没有时传空列表"
    )
    applied_business_rules: list[str] = Field(
        default_factory=list,
        description="本次实际采用的核心规则稳定标识；没有采用时传空列表",
    )
    logical_constraints: list[AlignedLogicalConstraint] = Field(
        default_factory=list,
        description="从用户需求提取的集合量化或数量约束；没有时传空列表",
    )
    requested_outputs: list[str] = Field(
        default_factory=list,
        description=(
            "用户要求在最终结果中看到的业务信息，按业务名称逐项列出；"
            "不得退化为只返回内部唯一标识，除非用户明确只需要标识"
        ),
    )
    presentation_requirements: list[AlignedPresentationRequirement] = Field(
        default_factory=list,
        description="用户明确提出的结果行粒度和布局要求；没有明确要求时传空列表",
    )
    result_scope: Literal["complete", "bounded", "unspecified"] = Field(
        default="unspecified",
        description=(
            "结果范围：要求全部或导出完整名单时使用 complete，明确限定结果数量时使用 bounded，"
            "没有说明数量范围时使用 unspecified"
        ),
    )
    requested_limit: int | None = Field(
        default=None,
        ge=1,
        description="result_scope 为 bounded 时填写用户指定的正整数数量，否则必须传 null",
    )

    # 保证结果范围与数量限制语义闭合，避免规划层猜测是否允许截断结果。
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
    """当需求已完成业务概念对齐且没有关键歧义时，提交结果并成功结束业务对齐。"""

    model_config = ConfigDict(extra="forbid")

    aligned_request: SubmittedAlignedQuery = Field(
        description=(
            "传递给查询规划层的完整业务需求；用户原问题和已完成的澄清问答由工作流自动补充，"
            "不得在此重复提交"
        )
    )


class AbandonAlignmentArguments(BaseModel):
    """仅在问题超出业务范围或关键歧义无法通过必要澄清解决时，主动结束业务对齐。"""

    model_config = ConfigDict(extra="forbid")

    reason_type: Literal[
        "unresolved_ambiguity", "insufficient_information", "out_of_scope"
    ] = Field(
        description=(
            "放弃原因：无法消除的歧义使用 unresolved_ambiguity，缺少继续所必需的信息使用 "
            "insufficient_information，超出当前业务查询范围使用 out_of_scope"
        )
    )
    reason: str = Field(
        description=(
            "面向用户说明无法继续的具体业务原因，并指出用户补充什么信息后可以重新查询；"
            "不得包含模型、工具、Schema 或数据库异常"
        )
    )
    unresolved_terms: list[str] = Field(
        description="仍无法确认且会改变查询口径的用户原始表达；没有时传空列表"
    )


class AlignmentAbandonment(BaseModel):
    """保存业务对齐主动放弃后可传给流水线和用户的结构化结果。"""

    model_config = ConfigDict(extra="forbid")

    reason: Literal[
        "unresolved_ambiguity", "insufficient_information", "out_of_scope"
    ] = Field(description="已经校验的放弃原因分类")
    user_message: str = Field(description="可直接向用户展示的放弃原因和下一步建议")
    unresolved_terms: list[str] = Field(description="仍未解决且影响查询口径的用户表达")


# 使用思考参数模型的类说明和字段描述生成标准 Function Calling 工具。
def build_thinking_tool_definition() -> dict[str, object]:
    return build_pydantic_tool_definition(
        tool_name=THINKING_TOOL_NAME,
        arguments_model=ThinkingToolArguments,
    )


# 使用用户澄清参数模型的类说明和字段描述生成标准 Function Calling 工具。
def build_ask_user_tool_definition() -> dict[str, object]:
    return build_pydantic_tool_definition(
        tool_name=ASK_USER_TOOL_NAME,
        arguments_model=AskUserToolArguments,
    )


# 使用最终对齐参数模型的完整嵌套说明生成提交工具。
def build_submit_aligned_query_tool_definition() -> dict[str, object]:
    return build_pydantic_tool_definition(
        tool_name=SUBMIT_ALIGNED_QUERY_TOOL_NAME,
        arguments_model=SubmitAlignedQueryArguments,
    )


# 使用放弃参数模型生成工具，并补充当前业务域允许查询的动态范围。
def build_abandon_alignment_tool_definition(
    query_scope: str = "当前业务查询范围",
) -> dict[str, object]:
    return build_pydantic_tool_definition(
        tool_name=ABANDON_ALIGNMENT_TOOL_NAME,
        description=(
            f"{AbandonAlignmentArguments.__doc__ or ''}"
            f" 当前允许查询的是{query_scope}。"
            "不得把可由已有业务知识或一次必要用户澄清解决的问题直接放弃。"
        ),
        arguments_model=AbandonAlignmentArguments,
    )


# 将模型返回的思考工具参数交给对应 Pydantic 模型进行本地校验。
def parse_thinking_tool_arguments(arguments_json: str) -> ThinkingToolArguments:
    return ThinkingToolArguments.model_validate_json(arguments_json)


# 将模型返回的用户澄清工具参数交给对应 Pydantic 模型进行本地校验。
def parse_ask_user_tool_arguments(arguments_json: str) -> AskUserToolArguments:
    return AskUserToolArguments.model_validate_json(arguments_json)


# 优先严格校验最终提交参数，失败时兼容工具解析器对 aligned_request 的二次序列化。
def parse_submit_aligned_query_arguments(
    arguments_json: str,
) -> SubmitAlignedQueryArguments:
    return validate_tool_arguments_with_embedded_json_fallback(
        arguments_json,
        SubmitAlignedQueryArguments,
    )


# 将模型返回的放弃参数校验并转换为流水线使用的稳定结果模型。
def parse_abandon_alignment_arguments(arguments_json: str) -> AlignmentAbandonment:
    arguments = AbandonAlignmentArguments.model_validate_json(arguments_json)
    return AlignmentAbandonment(
        reason=arguments.reason_type,
        user_message=arguments.reason,
        unresolved_terms=arguments.unresolved_terms,
    )


__all__ = [
    "ABANDON_ALIGNMENT_TOOL_NAME",
    "ASK_USER_TOOL_NAME",
    "SUBMIT_ALIGNED_QUERY_TOOL_NAME",
    "THINKING_TOOL_NAME",
    "AbandonAlignmentArguments",
    "AlignedLogicalConstraint",
    "AlignedPresentationRequirement",
    "AlignmentAbandonment",
    "AskUserToolArguments",
    "ResolvedBusinessConcept",
    "SubmitAlignedQueryArguments",
    "SubmittedAlignedQuery",
    "ThinkingToolArguments",
    "build_abandon_alignment_tool_definition",
    "build_ask_user_tool_definition",
    "build_submit_aligned_query_tool_definition",
    "build_thinking_tool_definition",
    "parse_abandon_alignment_arguments",
    "parse_ask_user_tool_arguments",
    "parse_submit_aligned_query_arguments",
    "parse_thinking_tool_arguments",
]
