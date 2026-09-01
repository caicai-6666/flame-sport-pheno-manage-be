"""使用 SQLite 持久化成功查询对前端公开的安全响应。"""

import asyncio
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import TypeVar

from app.schemas.agent_query import (
    AgentQueryResultResponse,
    AgentQuerySessionResponse,
    AgentQueryTraceResponse,
)

HistoryOperationResult = TypeVar("HistoryOperationResult")
HISTORY_CACHE_MAX_ENTRIES = 300


class AgentQueryHistoryStore:
    """按查询标识保存和加载成功终态，不持久化运行时会话对象。"""

    # 保存文件位置和保留期限；实际建库延迟到首次读写，避免构造阶段阻塞事件循环。
    def __init__(
        self,
        database_path: Path,
        retention_days: int,
        cache_ttl_seconds: int = 600,
    ) -> None:
        self._database_path = database_path
        self._retention_days = retention_days
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache: OrderedDict[
            tuple[str, str],
            tuple[float, float, str | None],
        ] = OrderedDict()
        self._lock = asyncio.Lock()

    # 清除同一查询三个响应的旧缓存，确保覆盖写入后下一次读取使用最新文件内容。
    def _invalidate_query_cache(self, query_id: str) -> None:
        for column in ("session_json", "trace_json", "result_json"):
            self._cache.pop((query_id, column), None)

    # 写入有界 LRU 缓存；超过容量时优先淘汰最久未访问的响应字段。
    def _cache_json(
        self,
        query_id: str,
        column: str,
        history_expires_at: float,
        raw: str | None,
    ) -> None:
        if self._cache_ttl_seconds <= 0:
            return
        key = (query_id, column)
        self._cache[key] = (
            time.monotonic() + self._cache_ttl_seconds,
            history_expires_at,
            raw,
        )
        self._cache.move_to_end(key)
        while len(self._cache) > HISTORY_CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)

    # 串行执行短 SQLite 操作并移出事件循环，避免同步文件锁阻塞其他 HTTP 请求。
    async def _run(
        self,
        operation: Callable[..., HistoryOperationResult],
        *arguments: object,
    ) -> HistoryOperationResult:
        async with self._lock:
            return await asyncio.to_thread(operation, *arguments)

    # 创建父目录、数据表和时间索引，并启用适合少量写入与并发读取的 WAL 模式。
    def _connect(self) -> sqlite3.Connection:
        self._database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
            mode=0o700,
        )
        connection = sqlite3.connect(self._database_path, timeout=5.0)
        self._database_path.chmod(0o600)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_query_history (
                query_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at REAL NOT NULL,
                session_json TEXT NOT NULL,
                trace_json TEXT NOT NULL,
                result_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_query_history_created_at
            ON agent_query_history(created_at DESC)
            """
        )
        connection.commit()
        return connection

    # 原子覆盖同一查询的三个公开响应，确保读取者不会看到部分写入的历史记录。
    def _save_sync(
        self,
        session: AgentQuerySessionResponse,
        trace: AgentQueryTraceResponse,
        result: AgentQueryResultResponse,
    ) -> None:
        connection = self._connect()
        try:
            expires_at = session.updated_at + timedelta(
                days=self._retention_days
            )
            with connection:
                connection.execute(
                    """
                    INSERT INTO agent_query_history (
                        query_id,
                        created_at,
                        updated_at,
                        expires_at,
                        session_json,
                        trace_json,
                        result_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(query_id) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        expires_at = excluded.expires_at,
                        session_json = excluded.session_json,
                        trace_json = excluded.trace_json,
                        result_json = excluded.result_json
                    """,
                    (
                        session.query_id,
                        session.created_at.isoformat(),
                        session.updated_at.isoformat(),
                        expires_at.timestamp(),
                        session.model_dump_json(),
                        trace.model_dump_json(),
                        result.model_dump_json(),
                    ),
                )
            self._invalidate_query_cache(session.query_id)
        finally:
            connection.close()

    # 删除过期记录后按列读取目标 JSON，避免状态或轨迹查询加载完整结果表。
    def _load_json_sync(self, query_id: str, column: str) -> str | None:
        if column not in {"session_json", "trace_json", "result_json"}:
            raise ValueError("不支持的查询历史字段")
        key = (query_id, column)
        cached = self._cache.get(key)
        if cached is not None:
            cache_expires_at, history_expires_at, raw = cached
            if (
                time.monotonic() < cache_expires_at
                and time.time() < history_expires_at
            ):
                self._cache.move_to_end(key)
                return raw
            self._cache.pop(key, None)
        connection = self._connect()
        try:
            now = time.time()
            with connection:
                connection.execute(
                    "DELETE FROM agent_query_history WHERE expires_at <= ?",
                    (now,),
                )
            row = connection.execute(
                f"""
                SELECT {column}, expires_at
                FROM agent_query_history
                WHERE query_id = ?
                """,
                (query_id,),
            ).fetchone()
            raw = str(row[0]) if row is not None else None
            history_expires_at = (
                float(row[1]) if row is not None else float("inf")
            )
            self._cache_json(
                query_id,
                column,
                history_expires_at,
                raw,
            )
            return raw
        finally:
            connection.close()

    # 清理过期记录并按创建时间倒序返回有限查询标识，供历史列表按需加载详情。
    def _list_query_ids_sync(self, limit: int) -> list[str]:
        connection = self._connect()
        try:
            now = time.time()
            with connection:
                connection.execute(
                    "DELETE FROM agent_query_history WHERE expires_at <= ?",
                    (now,),
                )
            rows = connection.execute(
                """
                SELECT query_id
                FROM agent_query_history
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [str(row[0]) for row in rows]
        finally:
            connection.close()

    # 仅由成功终态调用，保存前端能够读取的状态、轨迹和表格结果。
    async def save_success(
        self,
        session: AgentQuerySessionResponse,
        trace: AgentQueryTraceResponse,
        result: AgentQueryResultResponse,
    ) -> None:
        if (
            session.status != "completed"
            or trace.status != "completed"
            or result.status != "completed"
            or not session.result_available
            or len({session.query_id, trace.query_id, result.query_id}) != 1
        ):
            raise ValueError("只允许持久化标识一致且已成功完成的查询结果")
        await self._run(self._save_sync, session, trace, result)

    # 按标识加载成功查询状态；不存在或已过期时返回空值。
    async def load_session(
        self,
        query_id: str,
    ) -> AgentQuerySessionResponse | None:
        raw = await self._run(self._load_json_sync, query_id, "session_json")
        return AgentQuerySessionResponse.model_validate_json(raw) if raw else None

    # 按标识加载友好轨迹，不读取可能较大的结果表格。
    async def load_trace(
        self,
        query_id: str,
    ) -> AgentQueryTraceResponse | None:
        raw = await self._run(self._load_json_sync, query_id, "trace_json")
        return AgentQueryTraceResponse.model_validate_json(raw) if raw else None

    # 按标识加载完整表格结果，仅在结果接口明确请求时读取。
    async def load_result(
        self,
        query_id: str,
    ) -> AgentQueryResultResponse | None:
        raw = await self._run(self._load_json_sync, query_id, "result_json")
        return AgentQueryResultResponse.model_validate_json(raw) if raw else None

    # 返回持久化成功查询的最近标识，调用方负责与内存热缓存去重。
    async def list_query_ids(self, limit: int) -> list[str]:
        return await self._run(self._list_query_ids_sync, limit)
