"""装配 Text-to-SQL 流水线并管理后台任务、会话容量和生命周期。"""

import asyncio
import logging
from collections.abc import AsyncIterator
from uuid import uuid4

from app.agent.text2sql.diagnostics import AgentQueryDiagnosticLogger
from app.agent.text2sql.domains.registry import get_query_domain_profile
from app.agent.text2sql.pipeline import AgentQueryPipeline, AgentQueryPipelineResult
from app.agent.text2sql.events.models import AgentProgressEvent, AgentProgressUpdate
from app.agent.text2sql.interaction.session import (
    AgentQueryCancelled,
    AgentQueryInteractionTimeout,
    AgentQuerySession,
    TERMINAL_QUERY_STATUSES,
)
from app.agent.text2sql.shared.table_schema_cache import CachingTableSchemaReader
from app.agent.text2sql.shared.table_schema_reader import InformationSchemaTableSchemaReader
from app.agent.text2sql.shared.model_options import resolve_model_provider_connection
from app.core.config import Settings


logger = logging.getLogger(__name__)

DEFAULT_MAX_ACTIVE_SESSIONS = 20
DEFAULT_EVENT_HISTORY_SIZE = 200
DEFAULT_SESSION_TTL_SECONDS = 3600
DEFAULT_SSE_HEARTBEAT_SECONDS = 15


class AgentQueryCapacityError(RuntimeError):
    """当前运行中或等待交互的查询数量已经达到配置上限。"""


class AgentQueryNotFoundError(LookupError):
    """指定查询会话不存在或已经过期清理。"""


# 裁剪会话中的模型原文、SQL、结构和数据行；失败时仅保留安全错误码与尝试次数供结果接口诊断。
def _build_session_safe_result(
    result: AgentQueryPipelineResult,
) -> AgentQueryPipelineResult:
    safe_alignment_result = (
        result.alignment_result.model_copy(
            update={
                "thoughts": [],
                "user_interactions": [],
                "raw_responses": [],
            }
        )
        if result.alignment_result is not None
        else None
    )
    safe_audit_result = (
        result.audit_result.model_copy(update={"raw_model_response": None})
        if result.audit_result is not None
        else None
    )
    safe_sql_result = (
        result.sql_result.model_copy(
            update={
                "schema_results": [],
                "draft": None,
                "sql": None,
                "analysis_sql": None,
                "result_columns": [],
                "rows": [],
                "raw_model_response": None,
            }
        )
        if result.status == "failure" and result.sql_result is not None
        else None
    )
    return result.model_copy(
        update={
            "alignment_result": safe_alignment_result,
            "planning_result": None,
            "sql_result": safe_sql_result,
            "translation_result": None,
            "shaping_result": None,
            "audit_result": safe_audit_result,
        }
    )


class AgentQueryManager:
    """在单进程内管理查询后台任务，并为路由提供稳定的会话访问接口。"""

    # 保存资源上限与会话容器；不在构造阶段访问模型或数据库。
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._max_active_sessions = getattr(
            settings,
            "agent_query_max_active_sessions",
            DEFAULT_MAX_ACTIVE_SESSIONS,
        )
        self._event_history_size = getattr(
            settings,
            "agent_query_event_history_size",
            DEFAULT_EVENT_HISTORY_SIZE,
        )
        self._session_ttl_seconds = getattr(
            settings,
            "agent_query_session_ttl_seconds",
            DEFAULT_SESSION_TTL_SECONDS,
        )
        self._sse_heartbeat_seconds = getattr(
            settings,
            "agent_query_sse_heartbeat_seconds",
            DEFAULT_SSE_HEARTBEAT_SECONDS,
        )
        self._sessions: dict[str, AgentQuerySession] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._diagnostics: dict[str, AgentQueryDiagnosticLogger] = {}
        self._schema_caches: dict[str, CachingTableSchemaReader] = {}
        self._lock = asyncio.Lock()

    # 清理超过保留时间的终态会话，运行中和等待用户回答的查询不会被静默删除。
    def _purge_expired_sessions(self) -> None:
        expired_query_ids = [
            query_id
            for query_id, session in self._sessions.items()
            if session.is_expired()
        ]
        for query_id in expired_query_ids:
            self._sessions.pop(query_id, None)
            self._tasks.pop(query_id, None)
            self._diagnostics.pop(query_id, None)

    # 根据当前快照统计占用工作资源的查询，终态历史不计入并发容量。
    def _active_session_count(self) -> int:
        return sum(
            session.snapshot().status not in TERMINAL_QUERY_STATUSES
            for session in self._sessions.values()
        )

    # 为每个业务域创建一个进程级线程安全表结构缓存，成功结构跨查询复用且失败结果允许重试。
    def _get_or_create_schema_cache(
        self,
        domain_key: str,
    ) -> CachingTableSchemaReader:
        cached_reader = self._schema_caches.get(domain_key)
        if cached_reader is not None:
            return cached_reader
        profile = get_query_domain_profile(domain_key)
        cached_reader = CachingTableSchemaReader(
            InformationSchemaTableSchemaReader(
                self._settings,
                profile.allowed_tables,
            ).read
        )
        self._schema_caches[domain_key] = cached_reader
        return cached_reader

    # 创建查询会话并启动异步流水线任务，立即向调用方返回 query_id。
    async def start_query(self, question: str, domain_key: str) -> AgentQuerySession:
        normalized_question = question.strip()
        if not normalized_question:
            raise ValueError("查询问题不能为空")
        resolve_model_provider_connection(self._settings)
        profile = get_query_domain_profile(domain_key)
        event_loop = asyncio.get_running_loop()
        async with self._lock:
            self._purge_expired_sessions()
            if (
                self._active_session_count()
                >= self._max_active_sessions
            ):
                raise AgentQueryCapacityError("当前查询任务较多，请稍后重试")
            query_id = uuid4().hex
            session = AgentQuerySession(
                query_id=query_id,
                domain_key=profile.key,
                question=normalized_question,
                event_history_size=self._event_history_size,
                session_ttl_seconds=self._session_ttl_seconds,
                event_loop=event_loop,
            )
            schema_cache = self._get_or_create_schema_cache(profile.key)
            diagnostics = AgentQueryDiagnosticLogger(
                query_id=query_id,
                domain_key=profile.key,
                enabled=self._settings.agent_query_diagnostic_log_enabled,
                level=self._settings.agent_query_diagnostic_log_level,
            )
            self._sessions[query_id] = session
            self._diagnostics[query_id] = diagnostics
            diagnostics.query_started(len(normalized_question))
            self._publish_progress(
                session,
                diagnostics,
                AgentProgressUpdate(
                    stage="accepted",
                    event_type="query_started",
                    status="running",
                    title="查询任务已创建",
                    message=f"已开始处理{profile.display_name}查询。",
                ),
            )
            task = asyncio.create_task(
                self._run_query(session, schema_cache, diagnostics),
                name=f"agent-query-{query_id}",
            )
            self._tasks[query_id] = task
            return session

    # 直接等待异步 Pipeline；其中尚未异步的子图由 Pipeline 自行在线程中隔离。
    async def _run_query(
        self,
        session: AgentQuerySession,
        schema_cache: CachingTableSchemaReader,
        diagnostics: AgentQueryDiagnosticLogger,
    ) -> None:
        profile = get_query_domain_profile(session.domain_key)

        # 将子图进度同时送入脱敏诊断日志与现有 SSE 会话，不改变子图回调协议。
        def publish_progress(update: AgentProgressUpdate) -> None:
            self._publish_progress(session, diagnostics, update)

        pipeline = AgentQueryPipeline(
            domain_profile=profile,
            interaction_requester=session.request_interaction,
            progress_emitter=publish_progress,
            settings=self._settings,
            trace_writer=None,
            schema_reader=schema_cache.read,
        )
        try:
            result = await pipeline.run(session.question)
            if session.snapshot().status == "cancelled":
                return
            diagnostics.pipeline_finished(result)
            if result.status == "success":
                user_message = result.user_message or "查询已经完成。"
                self._publish_progress(
                    session,
                    diagnostics,
                    AgentProgressUpdate(
                        stage="result",
                        event_type="query_completed",
                        status="success",
                        title="查询完成",
                        message=user_message,
                        payload={"result_available": True},
                    ),
                )
                session.finish(
                    "completed",
                    _build_session_safe_result(result),
                    user_message,
                )
            elif result.status == "abandoned":
                user_message = result.user_message or "本次查询已停止。"
                self._publish_progress(
                    session,
                    diagnostics,
                    AgentProgressUpdate(
                        stage="result",
                        event_type="query_abandoned",
                        status="abandoned",
                        title="查询已停止",
                        message=user_message,
                    ),
                )
                session.finish(
                    "abandoned",
                    _build_session_safe_result(result),
                    user_message,
                )
            else:
                self._finish_failed_query(
                    session,
                    diagnostics,
                    result,
                    "查询生成或执行失败，请稍后重试。",
                )
        except AgentQueryInteractionTimeout:
            diagnostics.query_terminated("failure", "interaction_timeout")
            self._finish_failed_query(
                session,
                diagnostics,
                None,
                "等待您的回答超过 5 分钟，查询已失败。",
            )
        except AgentQueryCancelled:
            cancelled_snapshot = session.snapshot()
            if cancelled_snapshot.user_message is not None:
                return
            if cancelled_snapshot.status != "cancelled":
                session.cancel()
            user_message = "本次查询已取消。"
            diagnostics.query_terminated("cancelled", "user_cancelled")
            self._publish_progress(
                session,
                diagnostics,
                AgentProgressUpdate(
                    stage="result",
                    event_type="query_cancelled",
                    status="cancelled",
                    title="查询已取消",
                    message=user_message,
                ),
            )
            session.finish("cancelled", None, user_message)
        except Exception as error:
            diagnostics.query_failed_with_exception(error)
            logger.error(
                "查询智能体后台任务执行失败：query_id=%s exception_type=%s",
                session.query_id,
                type(error).__name__,
            )
            self._finish_failed_query(
                session,
                diagnostics,
                None,
                "查询处理失败，请稍后重试。",
            )

    # 统一保存失败终态和用户安全提示，内部异常、SQL 与模型响应不会进入 SSE。
    def _finish_failed_query(
        self,
        session: AgentQuerySession,
        diagnostics: AgentQueryDiagnosticLogger,
        result: AgentQueryPipelineResult | None,
        user_message: str,
    ) -> None:
        self._publish_progress(
            session,
            diagnostics,
            AgentProgressUpdate(
                stage="result",
                event_type="query_failed",
                status="failure",
                title="查询未能完成",
                message=user_message,
            ),
        )
        session.finish(
            "failed",
            _build_session_safe_result(result) if result is not None else None,
            user_message,
        )

    # 同时写入脱敏诊断事件和现有会话队列，保证日志与 SSE 阶段顺序一致。
    def _publish_progress(
        self,
        session: AgentQuerySession,
        diagnostics: AgentQueryDiagnosticLogger,
        update: AgentProgressUpdate,
    ) -> None:
        diagnostics.progress(update)
        session.publish(update)

    # 获取现有会话并顺带清理已过期终态；不存在时使用稳定业务异常。
    async def get_session(self, query_id: str) -> AgentQuerySession:
        async with self._lock:
            self._purge_expired_sessions()
            session = self._sessions.get(query_id)
            if session is None:
                raise AgentQueryNotFoundError("查询任务不存在或已经过期")
            return session

    # 返回当前进程内仍在保留期的查询标识，按创建时间倒序供调用方再定向读取轨迹或结果。
    async def list_cached_query_ids(self, limit: int) -> list[str]:
        async with self._lock:
            self._purge_expired_sessions()
            sessions = sorted(
                self._sessions.values(),
                key=lambda session: session.snapshot().created_at,
                reverse=True,
            )
            return [session.query_id for session in sessions[:limit]]

    # 将用户答案提交给当前待处理交互，具体 ID、空值和重复提交由会话原子校验。
    async def answer_interaction(
        self,
        query_id: str,
        interaction_id: str,
        answer: str,
    ) -> AgentQuerySession:
        session = await self.get_session(query_id)
        pending_interaction = session.snapshot().pending_interaction
        session.answer_interaction(interaction_id, answer)
        diagnostics = self._diagnostics.get(query_id)
        if diagnostics is not None:
            diagnostics.interaction_answered(
                pending_interaction.interaction_type
                if pending_interaction is not None
                else "unknown"
            )
        return session

    # 取消指定查询并通知可能等待交互的后台线程，重复取消保持幂等。
    async def cancel_query(self, query_id: str) -> AgentQuerySession:
        session = await self.get_session(query_id)
        if session.snapshot().status not in TERMINAL_QUERY_STATUSES:
            session.cancel()
            user_message = "用户取消了本次查询。"
            diagnostics = self._diagnostics.get(query_id)
            if diagnostics is not None:
                diagnostics.query_terminated("cancelled", "user_cancelled")
                self._publish_progress(
                    session,
                    diagnostics,
                    AgentProgressUpdate(
                        stage="result",
                        event_type="query_cancelled",
                        status="cancelled",
                        title="查询已取消",
                        message=user_message,
                    ),
                )
            else:
                session.publish(
                    AgentProgressUpdate(
                        stage="result",
                        event_type="query_cancelled",
                        status="cancelled",
                        title="查询已取消",
                        message=user_message,
                    )
                )
            session.finish("cancelled", None, user_message)
        return session

    # 以独立订阅队列输出历史补发和实时事件，SSE 心跳用 None 表示且不占业务序号。
    async def subscribe(
        self,
        query_id: str,
        after_sequence: int,
    ) -> AsyncIterator[AgentProgressEvent | None]:
        session = await self.get_session(query_id)
        async for event in session.subscribe(
            after_sequence,
            self._sse_heartbeat_seconds,
        ):
            yield event

    # 应用退出时取消全部活动会话和异步查询任务，并唤醒可能仍在线程中等待回答的交互。
    async def shutdown(self) -> None:
        async with self._lock:
            sessions = tuple(self._sessions.values())
            tasks = tuple(task for task in self._tasks.values() if not task.done())
        for session in sessions:
            if session.snapshot().status not in TERMINAL_QUERY_STATUSES:
                session.cancel()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
