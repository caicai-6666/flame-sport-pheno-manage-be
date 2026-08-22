"""从目标业务库的 information_schema 读取允许表的详细结构。"""

import asyncio

import asyncmy
from asyncmy.cursors import DictCursor

from app.core.config import Settings
from app.agent.tools.table_schema import (
    TableSchemaColumn,
    TableSchemaLookupError,
    TableSchemaToolResponse,
    build_failure_table_schema_response,
    build_success_table_schema_response,
    ensure_allowed_table_name,
)


class InformationSchemaTableSchemaReader:
    """以只读 metadata 查询实现表结构工具的执行端。"""

    # 保存目标数据库连接配置，后续仅用于 information_schema 的白名单只读查询。
    def __init__(self, settings: Settings, allowed_tables: tuple[str, ...]) -> None:
        self._settings = settings
        self._allowed_tables = frozenset(allowed_tables)

    # 以同步工具接口调用异步 metadata 查询，供当前同步 LangGraph 节点使用。
    def read(self, table_name: str) -> TableSchemaToolResponse:
        try:
            ensure_allowed_table_name(table_name, self._allowed_tables)
            return asyncio.run(self._read_async(table_name))
        except ValueError:
            return build_failure_table_schema_response(
                TableSchemaLookupError.TABLE_NOT_FOUND,
                table_name,
            )
        except (OSError, asyncmy.Error):
            return build_failure_table_schema_response(
                TableSchemaLookupError.DATABASE_UNAVAILABLE,
                table_name,
            )
        except RuntimeError:
            return build_failure_table_schema_response(
                TableSchemaLookupError.QUERY_FAILED,
                table_name,
            )

    # 查询字段、类型、备注和外键引用，并保持字段声明顺序。
    async def _read_async(self, table_name: str) -> TableSchemaToolResponse:
        connection = await asyncmy.connect(
            host=self._settings.mysql_host,
            port=self._settings.mysql_port,
            user=self._settings.mysql_user,
            password=self._settings.mysql_password.get_secret_value(),
            db="information_schema",
            charset=self._settings.mysql_charset,
            cursor_cls=DictCursor,
        )
        try:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT
                      columns_info.COLUMN_NAME AS field_name,
                      columns_info.COLUMN_TYPE AS data_type,
                      columns_info.COLUMN_COMMENT AS comment,
                      key_usage.REFERENCED_TABLE_NAME AS referenced_table_name,
                      key_usage.REFERENCED_COLUMN_NAME AS referenced_column_name
                    FROM COLUMNS AS columns_info
                    LEFT JOIN KEY_COLUMN_USAGE AS key_usage
                      ON key_usage.TABLE_SCHEMA = columns_info.TABLE_SCHEMA
                      AND key_usage.TABLE_NAME = columns_info.TABLE_NAME
                      AND key_usage.COLUMN_NAME = columns_info.COLUMN_NAME
                      AND key_usage.REFERENCED_TABLE_NAME IS NOT NULL
                    WHERE columns_info.TABLE_SCHEMA = %s
                      AND columns_info.TABLE_NAME = %s
                    ORDER BY columns_info.ORDINAL_POSITION
                    """,
                    (self._settings.mysql_database, table_name),
                )
                rows = await cursor.fetchall()
        finally:
            connection.close()

        if not rows:
            return build_failure_table_schema_response(
                TableSchemaLookupError.TABLE_NOT_FOUND,
                table_name,
            )
        columns = [
            TableSchemaColumn(
                field_name=row["field_name"],
                data_type=row["data_type"],
                foreign_key=(
                    f"{row['referenced_table_name']}.{row['referenced_column_name']}"
                    if row["referenced_table_name"]
                    else None
                ),
                comment=row["comment"] or None,
            )
            for row in rows
        ]
        return build_success_table_schema_response(table_name, columns)
