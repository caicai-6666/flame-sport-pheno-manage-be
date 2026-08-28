"""将 Text-to-SQL 查询进度事件编码为标准 SSE 文本帧。"""

from app.agent.text2sql.events.models import AgentProgressEvent


# 以事件序号支持浏览器断线续传，并保持每条 data 为单行 JSON。
def encode_sse_event(event: AgentProgressEvent) -> str:
    return (
        f"id: {event.sequence}\n"
        f"event: {event.event_type}\n"
        f"data: {event.model_dump_json()}\n\n"
    )


# 使用 SSE 注释帧维持代理和浏览器长连接，不产生业务事件序号。
def encode_sse_heartbeat() -> str:
    return ": heartbeat\n\n"
