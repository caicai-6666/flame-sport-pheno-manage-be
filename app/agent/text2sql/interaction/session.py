"""管理单进程内可订阅、可暂停和可恢复的 Text-to-SQL 查询会话。"""

import asyncio
import threading
import time
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Literal
from uuid import uuid4

from app.agent.text2sql.pipeline import AgentQueryPipelineResult
from app.agent.text2sql.events.models import (
    AgentEventStatus,
    AgentQueryStage,
    AgentQueryTraceEntry,
    AgentQueryTraceEntryType,
    AgentProgressEvent,
    AgentProgressUpdate,
    now_in_shanghai,
)
from app.agent.text2sql.interaction.models import (
    AgentInteraction,
    AgentInteractionType,
    AgentQuerySessionSnapshot,
    AgentQueryStatus,
)


TERMINAL_QUERY_STATUSES = frozenset(
    {"completed", "abandoned", "failed", "cancelled"}
)
INTERACTION_TIMEOUT_SECONDS = 5 * 60
PLAN_REVIEW_OPTIONS = ("确认并继续", "修正查询")


class AgentQueryCancelled(RuntimeError):
    """用户取消或应用关闭时中断仍在等待交互的查询。"""


class AgentQueryInteractionTimeout(RuntimeError):
    """操作员未在固定时限内回答交互，查询应作为失败而非取消结束。"""


class AgentQuerySession:
    """隔离单次查询的事件历史、订阅者、交互等待条件和最终结果。"""

    # 创建绑定当前事件循环的会话；模型线程只能通过线程安全方法发布和等待。
    def __init__(
        self,
        query_id: str,
        domain_key: str,
        question: str,
        event_history_size: int,
        session_ttl_seconds: int,
        event_loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.query_id = query_id
        self.domain_key = domain_key
        self.question = question
        self._event_history: deque[AgentProgressEvent] = deque(
            maxlen=event_history_size
        )
        self._trace_history: deque[AgentQueryTraceEntry] = deque(
            maxlen=event_history_size
        )
        self._session_ttl_seconds = session_ttl_seconds
        self._event_loop = event_loop
        self._lock = threading.RLock()
        self._interaction_condition = threading.Condition(self._lock)
        self._subscribers: set[asyncio.Queue[AgentProgressEvent]] = set()
        self._sequence = 0
        self._trace_sequence = 0
        self._status: AgentQueryStatus = "running"
        self._pending_interaction: AgentInteraction | None = None
        self._result: AgentQueryPipelineResult | None = None
        self._user_message: str | None = None
        self._cancelled = False
        self._created_at = now_in_shanghai()
        self._updated_at = self._created_at
        self._last_activity_monotonic = time.monotonic()
        self._append_trace_locked(
            entry_type="question_submitted",
            stage="accepted",
            status="running",
            title="已提交查询问题",
            message=question,
            occurred_at=self._created_at,
        )

    # 在持锁状态下写入独立友好轨迹，与 SSE 补发队列隔离并保留可展示交互上下文。
    def _append_trace_locked(
        self,
        entry_type: AgentQueryTraceEntryType,
        stage: AgentQueryStage,
        status: AgentEventStatus,
        title: str,
        message: str,
        options: list[str] | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        self._trace_sequence += 1
        self._trace_history.append(
            AgentQueryTraceEntry(
                sequence=self._trace_sequence,
                entry_type=entry_type,
                stage=stage,
                status=status,
                title=title,
                message=message,
                options=options or [],
                occurred_at=occurred_at or now_in_shanghai(),
            )
        )

    # 将已发布的安全进度转换为历史时间线条目，交互请求仅保留可展示问题和选项。
    def _append_progress_trace_locked(self, event: AgentProgressEvent) -> None:
        entry_type: AgentQueryTraceEntryType
        if event.event_type == "interaction_required":
            entry_type = "interaction_requested"
            options = [
                str(option)
                for option in event.payload.get("options", [])
                if isinstance(option, str)
            ]
        else:
            entry_type = "progress"
            options = []
        self._append_trace_locked(
            entry_type=entry_type,
            stage=event.stage,
            status=event.status,
            title=event.title,
            message=event.message,
            options=options,
            occurred_at=event.occurred_at,
        )

    # 在事件循环中向单个订阅者非阻塞投递；队列满时淘汰最旧事件并保留最新状态。
    @staticmethod
    def _deliver_to_subscriber(
        subscriber: asyncio.Queue[AgentProgressEvent],
        event: AgentProgressEvent,
    ) -> None:
        if subscriber.full():
            try:
                subscriber.get_nowait()
            except asyncio.QueueEmpty:
                pass
        subscriber.put_nowait(event)

    # 从模型线程或事件循环发布事件，统一补齐查询标识、序号和时间并广播给所有订阅者。
    def publish(self, update: AgentProgressUpdate) -> AgentProgressEvent:
        with self._lock:
            self._sequence += 1
            event = AgentProgressEvent(
                **update.model_dump(),
                query_id=self.query_id,
                sequence=self._sequence,
            )
            self._event_history.append(event)
            self._append_progress_trace_locked(event)
            self._updated_at = event.occurred_at
            self._last_activity_monotonic = time.monotonic()
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            self._event_loop.call_soon_threadsafe(
                self._deliver_to_subscriber,
                subscriber,
                event,
            )
        return event

    # 创建唯一待回答交互并阻塞当前模型工作线程，五分钟未回答时关闭交互并抛出失败信号。
    def request_interaction(
        self,
        interaction_type: AgentInteractionType,
        question: str,
        options: tuple[str, ...],
    ) -> str:
        with self._interaction_condition:
            if self._cancelled:
                raise AgentQueryCancelled("查询已经取消")
            if self._pending_interaction is not None:
                raise RuntimeError("当前查询已经存在待回答交互")
            interaction = AgentInteraction(
                interaction_id=uuid4().hex,
                interaction_type=interaction_type,
                question=question,
                options=options,
                allow_free_text=interaction_type == "clarification",
            )
            is_plan_review = options == PLAN_REVIEW_OPTIONS
            self._pending_interaction = interaction
            self._status = (
                "waiting_for_confirmation"
                if interaction_type == "confirmation"
                else "waiting_for_clarification"
            )
            self._updated_at = now_in_shanghai()
            self._last_activity_monotonic = time.monotonic()
            self.publish(
                AgentProgressUpdate(
                    stage=(
                        "planning"
                        if is_plan_review
                        else (
                            "confirmation"
                            if interaction_type == "confirmation"
                            else "planning"
                        )
                    ),
                    event_type="interaction_required",
                    status="waiting",
                    title=(
                        "请确认结果字段"
                        if is_plan_review
                        else (
                            "请确认查询需求"
                            if interaction_type == "confirmation"
                            else "需要补充一项信息"
                        )
                    ),
                    message=question,
                    payload={
                        "interaction_id": interaction.interaction_id,
                        "interaction_type": interaction.interaction_type,
                        "options": list(interaction.options),
                        "allow_free_text": interaction.allow_free_text,
                    },
                )
            )
            answered = self._interaction_condition.wait_for(
                lambda: interaction.status != "pending" or self._cancelled,
                timeout=INTERACTION_TIMEOUT_SECONDS,
            )
            if not answered:
                interaction.status = "cancelled"
                self._pending_interaction = None
                self._status = "running"
                self._updated_at = now_in_shanghai()
                self._last_activity_monotonic = time.monotonic()
                raise AgentQueryInteractionTimeout("等待用户回答超过五分钟")
            if self._cancelled or interaction.status == "cancelled":
                self._pending_interaction = None
                self._status = "cancelled"
                raise AgentQueryCancelled("查询已取消")
            assert interaction.answer is not None
            answer = interaction.answer
            self._pending_interaction = None
            self._status = "running"
            self._updated_at = now_in_shanghai()
            self._last_activity_monotonic = time.monotonic()
            return answer

    # 提交当前唯一交互答案并唤醒模型工作线程，重复回答和错误 ID 均明确拒绝。
    def answer_interaction(self, interaction_id: str, answer: str) -> None:
        normalized_answer = answer.strip()
        if not normalized_answer:
            raise ValueError("交互答案不能为空")
        with self._interaction_condition:
            interaction = self._pending_interaction
            if interaction is None or interaction.interaction_id != interaction_id:
                raise ValueError("待回答交互不存在或已经结束")
            if interaction.status != "pending":
                raise ValueError("该交互已经回答，不能重复提交")
            interaction.status = "answered"
            interaction.answer = normalized_answer
            interaction.answered_at = now_in_shanghai()
            is_plan_review = interaction.options == PLAN_REVIEW_OPTIONS
            self._append_trace_locked(
                entry_type="interaction_answered",
                stage=(
                    "planning"
                    if is_plan_review
                    else (
                        "confirmation"
                        if interaction.interaction_type == "confirmation"
                        else "planning"
                    )
                ),
                status="success",
                title=(
                    "已提交查询方案选择"
                    if is_plan_review
                    else (
                        "已提交查询确认"
                        if interaction.interaction_type == "confirmation"
                        else "已补充查询信息"
                    )
                ),
                message=(
                    f"操作员选择：{normalized_answer}"
                    if interaction.interaction_type == "confirmation"
                    else normalized_answer
                ),
                occurred_at=interaction.answered_at,
            )
            self._status = "running"
            self._updated_at = interaction.answered_at
            self._last_activity_monotonic = time.monotonic()
            self._interaction_condition.notify_all()

    # 标记查询终态并保存内部完整结果；HTTP 快照仅暴露是否可读取和用户友好说明。
    def finish(
        self,
        status: Literal["completed", "abandoned", "failed", "cancelled"],
        result: AgentQueryPipelineResult | None,
        user_message: str,
    ) -> None:
        with self._lock:
            self._status = status
            self._result = result
            self._user_message = user_message
            self._updated_at = now_in_shanghai()
            self._last_activity_monotonic = time.monotonic()

    # 取消查询并唤醒可能等待用户输入的工作线程；外部模型调用会在返回后观察终态。
    def cancel(self) -> None:
        with self._interaction_condition:
            self._cancelled = True
            self._status = "cancelled"
            if self._pending_interaction is not None:
                self._pending_interaction.status = "cancelled"
            self._updated_at = now_in_shanghai()
            self._last_activity_monotonic = time.monotonic()
            self._interaction_condition.notify_all()

    # 判断终态会话是否已超过保留时间，运行中或等待中的会话不由普通清理误删。
    def is_expired(self) -> bool:
        with self._lock:
            return (
                self._status in TERMINAL_QUERY_STATUSES
                and time.monotonic() - self._last_activity_monotonic
                >= self._session_ttl_seconds
            )

    # 返回线程安全快照，使路由无需接触可变会话内部结构。
    def snapshot(self) -> AgentQuerySessionSnapshot:
        with self._lock:
            return AgentQuerySessionSnapshot(
                query_id=self.query_id,
                domain_key=self.domain_key,
                question=self.question,
                status=self._status,
                latest_sequence=self._sequence,
                pending_interaction=(
                    self._pending_interaction.model_copy(deep=True)
                    if self._pending_interaction is not None
                    else None
                ),
                result_available=self._result is not None,
                user_message=self._user_message,
                created_at=self._created_at,
                updated_at=self._updated_at,
            )

    # 返回内部查询结果供服务转换为受控 HTTP 响应，不复制模型原始消息到事件流。
    def get_result(self) -> AgentQueryPipelineResult | None:
        with self._lock:
            return self._result

    # 返回独立于 SSE 补发队列的友好历史副本，供历史页面按 query_id 安全展示。
    def get_trace_entries(self) -> list[AgentQueryTraceEntry]:
        with self._lock:
            return [entry.model_copy(deep=True) for entry in self._trace_history]

    # 注册独立订阅队列并先补发指定序号之后的历史，避免多个页面互相消费同一事件。
    async def subscribe(
        self,
        after_sequence: int,
        heartbeat_seconds: int,
    ) -> AsyncIterator[AgentProgressEvent | None]:
        subscriber: asyncio.Queue[AgentProgressEvent] = asyncio.Queue(
            maxsize=self._event_history.maxlen or 200
        )
        with self._lock:
            backlog = [
                event
                for event in self._event_history
                if event.sequence > after_sequence
            ]
            self._subscribers.add(subscriber)
        last_delivered_sequence = after_sequence
        try:
            for event in backlog:
                last_delivered_sequence = event.sequence
                yield event
            while True:
                with self._lock:
                    terminal = self._status in TERMINAL_QUERY_STATUSES
                    terminal_backlog = [
                        event
                        for event in self._event_history
                        if event.sequence > last_delivered_sequence
                    ]
                for event in terminal_backlog:
                    last_delivered_sequence = event.sequence
                    yield event
                if terminal:
                    return
                try:
                    event = await asyncio.wait_for(
                        subscriber.get(),
                        timeout=heartbeat_seconds,
                    )
                except asyncio.TimeoutError:
                    yield None
                    continue
                if event.sequence <= last_delivered_sequence:
                    continue
                last_delivered_sequence = event.sequence
                yield event
        finally:
            with self._lock:
                self._subscribers.discard(subscriber)
