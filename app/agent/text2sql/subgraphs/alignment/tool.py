"""使用 Pydantic 定义业务对齐子图的全部 Function Calling 工具协议。"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.text2sql.function_calling.schema import (
    build_pydantic_tool_definition,
)
from app.agent.text2sql.function_calling.arguments import (
    validate_tool_arguments_with_embedded_json_fallback,
)


THINKING_TOOL_NAME: Final[str] = "think"
ASK_USER_TOOL_NAME: Final[str] = "ask_user"
SUBMIT_ALIGNED_QUERY_TOOL_NAME: Final[str] = "submit_aligned_query"
ABANDON_ALIGNMENT_TOOL_NAME: Final[str] = "abandon_alignment"


class ThinkingToolArguments(BaseModel):
    """梳理业务对齐当前所需的事实、歧义和下一步判断。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        max_length=350,
        description=(
            "在业务对齐范围内梳理已确认概念、影响查询结果的关键歧义、待确认内容"
            "以及当前倾向的下一步动作，最多350个字符；每次调用应推进判断，"
            "避免重复已有结论或记录与当前决策无关的内容"
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


class SubmitAlignedQueryArguments(BaseModel):
    """当查询需求已经完成业务对齐且没有关键歧义时，提交自然语言结果。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        description=(
            "简要说明采用的词汇含义、确定业务规则和无需继续澄清的依据；"
            "不得包含数据库实现、工具协议或与当前对齐无关的推理"
        )
    )
    aligned_question: str = Field(
        description=(
            "使用标准业务语言写成的完整自然语言查询需求；必须保留查询主体、筛选条件、"
            "集合与数量口径、返回内容、展示要求和结果范围，不得包含表名、字段名或 SQL"
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


# 校验只包含对齐依据和完整自然语言需求的最终提交参数。
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
    "AlignmentAbandonment",
    "AskUserToolArguments",
    "SubmitAlignedQueryArguments",
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
