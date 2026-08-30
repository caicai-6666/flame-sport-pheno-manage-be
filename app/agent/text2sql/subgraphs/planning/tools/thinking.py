"""定义 Text-to-SQL 子图记录关键判断的 Pydantic 思考工具模型。"""

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from app.agent.text2sql.function_calling.arguments import (
    validate_tool_arguments_with_embedded_json_fallback,
)
from app.agent.text2sql.function_calling.schema import (
    build_pydantic_tool_definition,
)

THINKING_TOOL_NAME: Final[str] = "think"


class ThinkingToolArguments(BaseModel):
    """调用思考工具时必须提供的关键判断。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        max_length=350,
        description=(
            "在本轮写清已经确认的事实、仍需判断的问题和倾向的下一步工具动作，"
            "最多350个字符；不得复述问题或表结构，也不得重复上一轮已有结论"
        ),
    )


# 基于 Pydantic 参数模型生成函数调用定义，供模型在生成联合查询前提交关键判断。
def build_thinking_tool_definition() -> dict[str, object]:
    return build_pydantic_tool_definition(
        tool_name=THINKING_TOOL_NAME,
        description=(
            "记录本轮已确认事实、仍需判断的问题和倾向的下一步动作，不是长篇推理日志；"
            "只有尚有会改变下一步动作的新问题时才继续调用，不得重复已有结论。"
        ),
        arguments_model=ThinkingToolArguments,
    )


# 将模型返回的函数参数 JSON 按 Pydantic 模型校验为单次思考结果。
def parse_thinking_tool_arguments(arguments_json: str) -> ThinkingToolArguments:
    return validate_tool_arguments_with_embedded_json_fallback(
        arguments_json,
        ThinkingToolArguments,
    )
