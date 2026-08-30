"""定义用户可见消息格式化子图的 Function Calling 协议。"""

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from app.agent.text2sql.function_calling.arguments import (
    validate_tool_arguments_with_embedded_json_fallback,
)
from app.agent.text2sql.function_calling.schema import (
    build_pydantic_tool_definition,
)


SUBMIT_FORMATTED_USER_MESSAGE_TOOL_NAME: Final[str] = "submit_formatted_user_message"


class FormatUserMessageArguments(BaseModel):
    """提交只调整了空白字符、可直接沿用原字段返回前端的消息。"""

    model_config = ConfigDict(extra="forbid")

    formatted_text: str = Field(
        min_length=1,
        max_length=1000,
        description=(
            "格式化后的完整原文；只能调整换行、空格和缩进，"
            "所有非空白字符及其顺序必须与输入完全一致"
        ),
    )


# 使用 Pydantic 类说明和字段约束生成标准 OpenAI Function Calling 定义。
def build_format_user_message_tool_definition() -> dict[str, object]:
    return build_pydantic_tool_definition(
        tool_name=SUBMIT_FORMATTED_USER_MESSAGE_TOOL_NAME,
        arguments_model=FormatUserMessageArguments,
    )


# 校验格式化工具参数，并兼容嵌套 JSON 字符串形式的 OpenAI 兼容响应。
def parse_format_user_message_arguments(
    arguments_json: str,
) -> FormatUserMessageArguments:
    return validate_tool_arguments_with_embedded_json_fallback(
        arguments_json,
        FormatUserMessageArguments,
    )


__all__ = [
    "SUBMIT_FORMATTED_USER_MESSAGE_TOOL_NAME",
    "FormatUserMessageArguments",
    "build_format_user_message_tool_definition",
    "parse_format_user_message_arguments",
]
