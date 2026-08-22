"""定义查询智能体记录关键判断的 Pydantic 思考工具模型。"""

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from app.agent.tools.strict_schema import build_strict_tool_definition

THINKING_TOOL_NAME: Final[str] = "think"


class ThinkingToolArguments(BaseModel):
    """调用思考工具时必须提供的关键判断。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        max_length=240,
        description=(
            "仅记录影响下一步工具调用的一条关键判断，最多240个字符；"
            "不能复述问题、表结构或备选方案"
        ),
    )


# 基于 Pydantic 参数模型生成函数调用定义，供模型在生成联合查询前提交关键判断。
def build_thinking_tool_definition() -> dict[str, object]:
    return build_strict_tool_definition(
        tool_name=THINKING_TOOL_NAME,
        description=(
            "记录本轮下一步工具调用所必需的一条关键判断，不是推理日志；"
            "不得复述问题、表结构或反复比较备选方案。"
        ),
        arguments_model=ThinkingToolArguments,
    )


# 将模型返回的函数参数 JSON 按 Pydantic 模型校验为单次思考结果。
def parse_thinking_tool_arguments(arguments_json: str) -> ThinkingToolArguments:
    return ThinkingToolArguments.model_validate_json(arguments_json)
