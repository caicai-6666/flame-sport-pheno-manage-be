"""封装用户通知写入与消息字段序列化。"""

import json
from dataclasses import dataclass

from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession


@dataclass(frozen=True, slots=True)
class NotificationField:
    key: str
    value: str


# 将业务消息快照写入待发送队列，初始投递状态固定为 pending。
async def insert_notification(
    session: AsyncSession,
    user_id: str,
    message_title: str,
    message_fields: tuple[NotificationField, ...],
) -> int:
    serialized_fields = json.dumps(
        [
            {"key": field.key, "value": field.value}
            for field in message_fields
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    result = await session.exec(
        text(
            """
            INSERT INTO notification (
                user_id,
                message_title,
                message_fields,
                notification_status
            ) VALUES (
                :user_id,
                :message_title,
                :message_fields,
                'pending'
            )
            """
        ),
        params={
            "user_id": user_id,
            "message_title": message_title,
            "message_fields": serialized_fields,
        },
    )
    return int(result.lastrowid)
