"""定义 Planning 查询、塑形与最终结果选择工具的 Function Calling 契约。"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from app.agent.text2sql.function_calling.arguments import (
    validate_tool_arguments_with_embedded_json_fallback,
)
from app.agent.text2sql.function_calling.schema import (
    build_pydantic_tool_definition,
)


QUERY_MATERIAL_DATA_TOOL_NAME: Final[str] = "query_material_data"
SHAPE_MATERIAL_DATA_TOOL_NAME: Final[str] = "shape_material_data"
SUBMIT_FINAL_QUERY_RESULT_TOOL_NAME: Final[str] = "submit_final_query_result"


class QueryMaterialDataArguments(BaseModel):
    """根据业务指导和真实表范围查询供后续塑形使用的完整原料。"""

    model_config = ConfigDict(extra="forbid")

    guidance: str = Field(
        min_length=1,
        description=(
            "使用简洁 Markdown bullet 完整说明查询主体、业务范围、资格条件、"
            "排序或数量要求，以及必须返回的全部业务值、稳定标识和塑形技术原料；"
            "所有数据库筛选、集合资格和统计口径都必须在此完成，不得包含 SQL"
        ),
    )
    required_tables: list[str] = Field(
        min_length=1,
        description=(
            "本次原料查询实际需要读取的全部真实数据库表名，按依赖顺序排列；"
            "必须覆盖筛选、关联、返回值、稳定主体标识、动态列值和排序值的来源"
        ),
    )


class ShapeMaterialDataArguments(BaseModel):
    """基于一份本轮成功取得的完整原料结果执行确定性结果布局。"""

    model_config = ConfigDict(extra="forbid")

    material_result_id: str = Field(
        min_length=1,
        description=(
            "本轮 query_material_data 成功结果返回的唯一原料结果 ID；"
            "不得使用失败调用、其他轮次或自行编造的 ID"
        ),
    )
    shaping_guidance: str = Field(
        min_length=1,
        description=(
            "使用简洁 Markdown bullet 说明原料输入行粒度、最终行粒度、可见字段及顺序、"
            "稳定分组值、组内排序、动态列、隐藏技术原料和唯一动态列数量声明；"
            "只能整理已有原料，不得增加筛选、资格判断、业务计算或新业务值"
        ),
    )


class SubmitFinalQueryResultArguments(BaseModel):
    """选择一份已经观察并确认满足用户需求的成功塑形结果结束规划。"""

    model_config = ConfigDict(extra="forbid")

    shaped_result_id: str = Field(
        min_length=1,
        description=(
            "本轮 shape_material_data 成功结果返回且已经验证的唯一塑形结果 ID；"
            "该结果将作为后续翻译层的完整输入"
        ),
    )
    reason: str = Field(
        min_length=1,
        description=(
            "简要说明最终一行代表的主体、可见字段和布局为何符合已对齐的用户需求；"
            "不得声称预览之外的数据事实"
        ),
    )


# 从严格 Pydantic 参数模型生成原料查询工具，确保 Planning 只能提交指导和真实表范围。
def build_query_material_data_tool_definition() -> dict[str, object]:
    return build_pydantic_tool_definition(
        tool_name=QUERY_MATERIAL_DATA_TOOL_NAME,
        arguments_model=QueryMaterialDataArguments,
    )


# 从严格 Pydantic 参数模型生成塑形工具，只允许引用已保存原料并提交布局指导。
def build_shape_material_data_tool_definition() -> dict[str, object]:
    return build_pydantic_tool_definition(
        tool_name=SHAPE_MATERIAL_DATA_TOOL_NAME,
        arguments_model=ShapeMaterialDataArguments,
    )


# 从严格 Pydantic 参数模型生成成功终止工具，最终选择本身不重新查询或塑形。
def build_submit_final_query_result_tool_definition() -> dict[str, object]:
    return build_pydantic_tool_definition(
        tool_name=SUBMIT_FINAL_QUERY_RESULT_TOOL_NAME,
        arguments_model=SubmitFinalQueryResultArguments,
    )


# 严格解析原料查询参数，并兼容部分模型把 required_tables 二次编码为 JSON 字符串。
def parse_query_material_data_tool_arguments(
    arguments_json: str,
) -> QueryMaterialDataArguments:
    return validate_tool_arguments_with_embedded_json_fallback(
        arguments_json,
        QueryMaterialDataArguments,
    )


# 严格解析原料结果 ID 与塑形指导，拒绝模型混入结构化塑形计划或其他额外参数。
def parse_shape_material_data_tool_arguments(
    arguments_json: str,
) -> ShapeMaterialDataArguments:
    return validate_tool_arguments_with_embedded_json_fallback(
        arguments_json,
        ShapeMaterialDataArguments,
    )


# 严格解析最终塑形结果选择，保证终止调用只携带结果引用和可核验选择理由。
def parse_submit_final_query_result_tool_arguments(
    arguments_json: str,
) -> SubmitFinalQueryResultArguments:
    return validate_tool_arguments_with_embedded_json_fallback(
        arguments_json,
        SubmitFinalQueryResultArguments,
    )


__all__ = [
    "QUERY_MATERIAL_DATA_TOOL_NAME",
    "SHAPE_MATERIAL_DATA_TOOL_NAME",
    "SUBMIT_FINAL_QUERY_RESULT_TOOL_NAME",
    "QueryMaterialDataArguments",
    "ShapeMaterialDataArguments",
    "SubmitFinalQueryResultArguments",
    "build_query_material_data_tool_definition",
    "build_shape_material_data_tool_definition",
    "build_submit_final_query_result_tool_definition",
    "parse_query_material_data_tool_arguments",
    "parse_shape_material_data_tool_arguments",
    "parse_submit_final_query_result_tool_arguments",
]
