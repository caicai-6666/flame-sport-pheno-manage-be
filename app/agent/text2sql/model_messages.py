"""在统一模型调用边界收集真实请求上下文与模型响应。"""

from collections.abc import Callable, Mapping, Sequence
from threading import Lock
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelMessageTraceEntry(BaseModel):
    """模型消息队列中的一条有序请求、响应或请求失败记录。"""

    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(description="当前查询内单调递增的消息轨迹序号")
    node: str = Field(description="发起模型请求的节点名称")
    direction: Literal["request", "response", "error"] = Field(
        description="消息流向：发送给模型、模型返回或请求失败"
    )
    payload: Any = Field(description="该次请求上下文、模型 assistant 消息或失败类型")


ModelMessageTraceWriter = Callable[[ModelMessageTraceEntry], None]


# 将 SDK 消息对象递归转换为可复制、可写入 JSON 日志的基础类型。
def _normalize_message_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _normalize_message_payload(
            value.model_dump(mode="json", exclude_none=True)
        )
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_message_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_normalize_message_payload(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "__dict__"):
        return _normalize_message_payload(
            {
                key: item
                for key, item in vars(value).items()
                if not key.startswith("_")
            }
        )
    return str(value)


class ModelMessageTraceQueue:
    """按真实发生顺序保存模型请求上下文和响应，不解析任何业务语义。"""

    # 初始化查询级队列；可选写出器用于在入队时同步写入受控诊断日志。
    def __init__(
        self,
        writer: ModelMessageTraceWriter | None = None,
        enabled: bool = True,
    ) -> None:
        self._writer = writer
        self._enabled = enabled
        self._entries: list[ModelMessageTraceEntry] = []
        self._lock = Lock()

    # 原子追加一条消息轨迹，确保翻译字段并发调用时仍能获得唯一递增序号。
    def append(
        self,
        node: str,
        direction: Literal["request", "response", "error"],
        payload: Any,
    ) -> None:
        if not self._enabled:
            return
        with self._lock:
            entry = ModelMessageTraceEntry(
                sequence=len(self._entries) + 1,
                node=node,
                direction=direction,
                payload=_normalize_message_payload(payload),
            )
            self._entries.append(entry)
        if self._writer is not None:
            self._writer(entry)

    # 返回不可变快照，供测试和受控排障读取，调用方不能修改队列内部记录。
    def snapshot(self) -> tuple[ModelMessageTraceEntry, ...]:
        with self._lock:
            return tuple(entry.model_copy(deep=True) for entry in self._entries)


# 在唯一模型调用边界记录实际请求消息和 assistant 响应，业务节点无需另行埋点。
async def create_traced_chat_completion(
    *,
    client: Any,
    message_queue: ModelMessageTraceQueue | None,
    node: str,
    messages: Sequence[Any],
    **request_options: Any,
) -> Any:
    request_messages = list(messages)
    if message_queue is not None:
        message_queue.append(
            node,
            "request",
            {"messages": request_messages},
        )
    try:
        response = await client.chat.completions.create(
            messages=request_messages,
            **request_options,
        )
    except Exception as error:
        if message_queue is not None:
            message_queue.append(
                node,
                "error",
                {"exception_type": type(error).__name__},
            )
        raise
    if message_queue is not None:
        message_queue.append(
            node,
            "response",
            response.choices[0].message,
        )
    return response
