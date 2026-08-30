"""定义查询规划子图按需读取单张表实际数据的 Pydantic 工具模型。"""

from collections.abc import Collection
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.text2sql.function_calling.arguments import (
    validate_tool_arguments_with_embedded_json_fallback,
)
from app.agent.text2sql.function_calling.schema import (
    build_pydantic_tool_definition,
)


TABLE_DATA_INSPECTION_TOOL_NAME: Final[str] = "inspect_table_data"
NEXT_TABLE_DATA_INSPECTION_PAGE_TOOL_NAME: Final[str] = "get_next_inspection_page"
CLEAR_TABLE_DATA_INSPECTION_CONTEXT_TOOL_NAME: Final[str] = "clear_inspection_context"
DataInspectionPurpose = Literal["entity_resolution", "value_confirmation"]


class TableDataInspectionToolArguments(BaseModel):
    """调用单表数据检索子智能体时必须提供的目标表和检索意图。"""

    model_config = ConfigDict(extra="forbid")

    table_name: str = Field(description="需要检索实际数据的业务域白名单表名")
    request: str = Field(
        description="希望从该表确认的具体内容，例如查找与用户描述匹配的实体候选值"
    )
    lookup_value: str = Field(
        min_length=1,
        description=(
            "用户原问题中需要确认的原始值；实体解析时用于按声明式实体配置执行精确和相似候选匹配，"
            "不得写入 SQL、字段名或额外说明"
        ),
    )
    purpose: DataInspectionPurpose = Field(
        description="仅允许实体解析或筛选值确认，不能用于预览最终业务查询结果"
    )
    reason: str = Field(
        description="说明为什么用户原问题和表结构不足以生成准确最终 SQL，且无法直接由最终 SQL 处理"
    )


class TableDataInspectionResponse(BaseModel):
    """单表数据检索子智能体的统一结果，供规划模型继续判断或向用户澄清。"""

    status: Literal["success", "failure"] = Field(description="数据检索状态")
    result: str = Field(description="成功时为合法 YAML 候选页；失败时为安全原因摘要")
    inspection_id: str | None = Field(
        default=None,
        description="成功检索创建的临时结果标识；可用于顺序读取下一页",
    )
    page_id: str | None = Field(
        default=None,
        description="本次返回页面的临时标识；可在上下文清理时保留关键页面",
    )
    has_more: bool = Field(
        default=False,
        description="是否仍可读取同一检索结果的下一页",
    )
    sql: str | None = Field(default=None, description="成功时执行的受限只读 SQL")
    raw_response: str | None = Field(
        default=None, description="子智能体的原始模型响应，供受限诊断回放"
    )
    model_generation_count: int = Field(
        default=0, description="本次子智能体实际消耗的模型生成次数"
    )

    # 仅向规划模型暴露继续推理需要的结果，原始模型响应和内部 SQL 留在受限诊断对象中。
    def render_for_planning(self) -> dict[str, str | bool | None]:
        return {
            "status": self.status,
            "result": self.result,
            "inspection_id": self.inspection_id,
            "page_id": self.page_id,
            "has_more": self.has_more,
        }


class NextTableDataInspectionPageArguments(BaseModel):
    """读取已创建单表检索结果下一页时必须提供的临时标识。"""

    model_config = ConfigDict(extra="forbid")

    inspection_id: str = Field(
        min_length=1,
        description="此前 inspect_table_data 成功响应返回的 inspection_id",
    )


class ClearTableDataInspectionContextArguments(BaseModel):
    """用户确认候选后按页清理检索上下文时必须提供的决策。"""

    model_config = ConfigDict(extra="forbid")

    inspection_id: str = Field(
        min_length=1,
        description="需要清理的 inspect_table_data 检索结果 ID",
    )
    decision: Literal["confirmed", "rejected"] = Field(
        description="用户对候选的确认结果；confirmed 表示是目标，rejected 表示不是目标"
    )
    candidate_result: str = Field(
        min_length=1,
        max_length=1000,
        description=(
            "本次被用户确认或排除的候选；必须保留原始 ID、名称和必要筛选值，"
            "不能复述整页候选数据"
        ),
    )
    preserved_page_ids: list[str] = Field(
        description=(
            "清理后必须保留原始页面内容的 page_id 列表；应包含仍有未确认候选的页面，"
            "无须保留的页面传空列表"
        )
    )


# 基于 Pydantic 参数模型生成函数调用定义，使规划模型可按需检索一张白名单表的实际数据。
def build_table_data_inspection_tool_definition(
    allowed_tables: Collection[str],
) -> dict[str, object]:
    if not allowed_tables:
        raise ValueError("单表检索工具至少需要一个允许表")
    definition = build_pydantic_tool_definition(
        tool_name=TABLE_DATA_INSPECTION_TOOL_NAME,
        description=(
            "通过单步数据检索子智能体，从一张白名单表读取少量实际数据以确认用户提到的实体名称"
            "或其他值。只能查询指定的一张表，必须显式选择必要字段；"
            "每页最多返回 10 行；"
            "只允许用于实体解析或筛选值确认，禁止用于预览或回答最终业务查询结果；"
            "实体解析必须传入原始 lookup_value；工具会按声明式配置返回受限扫描内的相似候选。"
            "仅当没有精确或相似候选且 has_more 为 true 时才使用翻页工具继续查找；"
            "已找到多个匹配候选时必须再调用 ask_user，不能自行任选。"
        ),
        arguments_model=TableDataInspectionToolArguments,
    )
    function = definition["function"]
    assert isinstance(function, dict)
    parameters = function["parameters"]
    assert isinstance(parameters, dict)
    properties = parameters["properties"]
    assert isinstance(properties, dict)
    table_property = properties["table_name"]
    assert isinstance(table_property, dict)
    table_property["enum"] = list(allowed_tables)
    return definition


# 基于 Pydantic 参数模型生成顺序翻页工具，模型不能指定页码或 offset。
def build_next_table_data_inspection_page_tool_definition() -> dict[str, object]:
    return build_pydantic_tool_definition(
        tool_name=NEXT_TABLE_DATA_INSPECTION_PAGE_TOOL_NAME,
        description=(
            "读取指定单表检索结果的下一页。只能按顺序翻页，不能指定页码或 offset；"
            "当前页未找到匹配项且此前响应 has_more 为 true 时必须调用，"
            "其他确有必要继续确认实体或值的情况也可调用。"
        ),
        arguments_model=NextTableDataInspectionPageArguments,
    )


# 基于 Pydantic 参数模型生成检索上下文清理工具，避免已确认实体的多页候选长期占用上下文。
def build_clear_table_data_inspection_context_tool_definition() -> dict[str, object]:
    return build_pydantic_tool_definition(
        tool_name=CLEAR_TABLE_DATA_INSPECTION_CONTEXT_TOOL_NAME,
        description=(
            "在已从指定单表检索结果确认足够事实、不再需要查看其候选页时，"
            "按页清除该 ID 对应的无关历史页面内容。"
            "相似候选必须先通过 ask_user 获得用户确认或否定；"
            "必须在 preserved_page_ids 中保留仍含未确认候选的关键页，"
            "并在 candidate_result 中保留本次确认或排除的原始 ID、名称和必要筛选值。"
        ),
        arguments_model=ClearTableDataInspectionContextArguments,
    )


# 将模型返回的函数参数 JSON 按 Pydantic 模型校验为单表数据检索请求。
def parse_table_data_inspection_tool_arguments(
    arguments_json: str,
) -> TableDataInspectionToolArguments:
    return validate_tool_arguments_with_embedded_json_fallback(
        arguments_json,
        TableDataInspectionToolArguments,
    )


# 将模型返回的翻页工具参数校验为既有检索结果的唯一标识。
def parse_next_table_data_inspection_page_tool_arguments(
    arguments_json: str,
) -> NextTableDataInspectionPageArguments:
    return validate_tool_arguments_with_embedded_json_fallback(
        arguments_json,
        NextTableDataInspectionPageArguments,
    )


# 将模型返回的清理参数校验为检索结果 ID、用户决策、候选摘要和关键页列表。
def parse_clear_table_data_inspection_context_tool_arguments(
    arguments_json: str,
) -> ClearTableDataInspectionContextArguments:
    return validate_tool_arguments_with_embedded_json_fallback(
        arguments_json,
        ClearTableDataInspectionContextArguments,
    )
