"""定义 Text-to-SQL 子图通过外部交互出口澄清事实的 Pydantic 工具模型。"""

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from app.agent.text2sql.shared.tools.argument_compatibility import (
    validate_tool_arguments_with_embedded_json_fallback,
)
from app.agent.text2sql.shared.tools.pydantic_schema import (
    build_pydantic_tool_definition,
)

ASK_USER_TOOL_NAME: Final[str] = "ask_user"


class AskUserToolArguments(BaseModel):
    """向用户请求一个关键澄清信息时必须提供的问题。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(
        description="需要用户回答的一个简洁、具体且无法通过现有工具确认的问题"
    )


class UserInteraction(BaseModel):
    """一次由规划流程发起、并由当前查询用户完成的澄清或复核问答。"""

    question: str = Field(description="规划流程提出的澄清或复核问题")
    answer: str = Field(description="用户通过交互接口提交的回答")


# 基于 Pydantic 参数模型生成函数调用定义，使模型能在关键事实不足时向用户反问。
def build_ask_user_tool_definition() -> dict[str, object]:
    return build_pydantic_tool_definition(
        tool_name=ASK_USER_TOOL_NAME,
        description=(
            "当用户问题存在会影响结果的关键歧义，或关键业务口径无法从用户原问题、"
            "表概览或表结构工具获得时，向用户提出一个简洁具体的澄清问题。"
            "不要询问可通过表结构工具确认的事实。"
        ),
        arguments_model=AskUserToolArguments,
    )


# 将模型返回的函数参数 JSON 按 Pydantic 模型校验为单次用户澄清问题。
def parse_ask_user_tool_arguments(arguments_json: str) -> AskUserToolArguments:
    return validate_tool_arguments_with_embedded_json_fallback(
        arguments_json,
        AskUserToolArguments,
    )
