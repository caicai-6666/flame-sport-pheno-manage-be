"""定义查询规划子图按业务域白名单读取表结构的工具模型。"""

from enum import Enum
from collections.abc import Collection
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.text2sql.shared.yaml_context import render_yaml_context
from app.agent.text2sql.shared.tools.argument_compatibility import (
    validate_tool_arguments_with_embedded_json_fallback,
)
from app.agent.text2sql.shared.tools.pydantic_schema import (
    build_pydantic_tool_definition,
)

TABLE_SCHEMA_TOOL_NAME: Final[str] = "get_table_schema"
AllowedTableName = str


class TableSchemaToolArguments(BaseModel):
    """读取指定允许表详细结构时必须提供的表名。"""

    model_config = ConfigDict(extra="forbid")

    table_name: str = Field(description="需要查看详细结构的业务域白名单表名")


class TableSchemaColumn(BaseModel):
    """表结构查询成功时直接返回给调用流程的单个字段。"""

    field_name: str = Field(description="字段名")
    data_type: str = Field(description="数据库字段的数据类型")
    foreign_key: str | None = Field(default=None, description="外键关联；不存在时为空")
    comment: str | None = Field(default=None, description="字段备注；不存在时为空")


class TableSchemaLookupError(str, Enum):
    """表结构读取失败时可安全回传给 SQL 分析智能体的错误分类。"""

    DATABASE_UNAVAILABLE = "database_unavailable"
    TABLE_NOT_FOUND = "table_not_found"
    QUERY_FAILED = "query_failed"


class TableSchemaToolResponse(BaseModel):
    """表结构工具的统一响应，成功结果直接使用，失败结果交由 SQL 分析逻辑处理。"""

    status: Literal["success", "failure"] = Field(description="表结构读取状态")
    table_name: AllowedTableName | None = Field(
        default=None,
        description="本次读取的实际表名；历史兼容结果未提供时为空",
    )
    result: str = Field(description="成功时为合法 YAML 表结构；失败时为错误原因摘要")


# 基于业务域白名单生成表名枚举，使远端工具提示和本地权限采用同一来源。
def build_table_schema_tool_definition(
    allowed_tables: Collection[str],
) -> dict[str, object]:
    if not allowed_tables:
        raise ValueError("表结构工具至少需要一个允许表")
    definition = build_pydantic_tool_definition(
        tool_name=TABLE_SCHEMA_TOOL_NAME,
        description="查看指定表的详细结构，包括字段、数据类型、外键和备注。仅在简短表概览不足以生成查询时调用。",
        arguments_model=TableSchemaToolArguments,
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


# 将模型返回的函数参数 JSON 按白名单和 Pydantic 模型校验为单个表结构查询请求。
def parse_table_schema_tool_arguments(arguments_json: str) -> TableSchemaToolArguments:
    return validate_tool_arguments_with_embedded_json_fallback(
        arguments_json,
        TableSchemaToolArguments,
    )


# 在执行任何数据库读取前再次校验表权限，不能只信任模型供应商的远端 Schema。
def ensure_allowed_table_name(
    table_name: str,
    allowed_tables: Collection[str],
) -> str:
    if table_name not in allowed_tables:
        raise ValueError(f"表 {table_name} 不属于当前查询业务域的允许范围")
    return table_name


# 将已读取字段按稳定键名渲染为合法 YAML，供规划、检索和 SQL 模型复用同一结构。
def build_success_table_schema_response(
    table_name: AllowedTableName,
    columns: list[TableSchemaColumn],
) -> TableSchemaToolResponse:
    return TableSchemaToolResponse(
        status="success",
        table_name=table_name,
        result=render_yaml_context(
            {
                "table": table_name,
                "columns": [column.model_dump() for column in columns],
            }
        ),
    )


# 将底层数据库异常归纳为安全、可行动的错误摘要，避免把连接凭证或原始异常细节暴露给智能体。
def build_failure_table_schema_response(
    error: TableSchemaLookupError,
    table_name: AllowedTableName | None = None,
) -> TableSchemaToolResponse:
    error_summary = {
        TableSchemaLookupError.DATABASE_UNAVAILABLE: "数据库不可达，无法读取表结构。请检查数据库服务和连接配置后重试。",
        TableSchemaLookupError.TABLE_NOT_FOUND: "请求的表不存在或当前连接无权读取该表结构。请检查表名和数据库权限。",
        TableSchemaLookupError.QUERY_FAILED: "读取表结构时发生数据库查询错误。请检查查询执行环境后重试。",
    }[error]
    return TableSchemaToolResponse(
        status="failure",
        table_name=table_name,
        result=error_summary,
    )
