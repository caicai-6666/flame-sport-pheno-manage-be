"""定义原料结果塑形计划的 Function Calling 工具协议。"""

from typing import Final

from app.agent.text2sql.function_calling.arguments import (
    validate_tool_arguments_with_embedded_json_fallback,
)
from app.agent.text2sql.function_calling.schema import (
    build_pydantic_tool_definition,
)
from app.agent.text2sql.subgraphs.shaping.models import MaterialResultShapePlan


SUBMIT_MATERIAL_SHAPE_PLAN_TOOL_NAME: Final[str] = "submit_material_shape_plan"


# 使用 Pydantic 模型生成非 strict 标准工具定义，实际调用数量由塑形状态机校验。
def build_material_shape_plan_tool_definition() -> dict[str, object]:
    return build_pydantic_tool_definition(
        tool_name=SUBMIT_MATERIAL_SHAPE_PLAN_TOOL_NAME,
        description=(
            "提交对现有 SQL 原料结果执行透传或动态转列的布局计划；"
            "不得增加筛选、业务计算或数据库字段。"
        ),
        arguments_model=MaterialResultShapePlan,
    )


# 解析塑形工具参数，并兼容部分 vLLM 对嵌套对象和数组的二次 JSON 编码。
def parse_material_shape_plan_tool_arguments(
    arguments_json: str,
) -> MaterialResultShapePlan:
    return validate_tool_arguments_with_embedded_json_fallback(
        arguments_json,
        MaterialResultShapePlan,
    )


__all__ = [
    "SUBMIT_MATERIAL_SHAPE_PLAN_TOOL_NAME",
    "build_material_shape_plan_tool_definition",
    "parse_material_shape_plan_tool_arguments",
]
