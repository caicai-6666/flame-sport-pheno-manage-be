"""为规划子图及其下游消费者复用成功表结构读取结果。"""

from collections.abc import Callable
import threading

from app.agent.text2sql.subgraphs.planning.tools.table_schema import TableSchemaToolResponse


SchemaReader = Callable[[str], TableSchemaToolResponse]


class CachingTableSchemaReader:
    """线程安全缓存进程生命周期内成功的表结构读取结果。"""

    # 保存底层读取器、成功结果缓存和互斥锁；失败结果不缓存以允许短暂故障恢复后重试。
    def __init__(self, schema_reader: SchemaReader) -> None:
        self._schema_reader = schema_reader
        self._schema_results: dict[str, TableSchemaToolResponse] = {}
        self._lock = threading.RLock()

    # 在互斥区内按表复用已成功结构，缓存未命中时只读取一次且不固化失败响应。
    def read(self, table_name: str) -> TableSchemaToolResponse:
        with self._lock:
            cached_result = self._schema_results.get(table_name)
            if cached_result is not None:
                return cached_result

            schema_result = self._schema_reader(table_name)
            if schema_result.status == "success":
                normalized_result = schema_result.model_copy(
                    update={"table_name": table_name}
                )
                self._schema_results[table_name] = normalized_result
                return normalized_result
            return schema_result
