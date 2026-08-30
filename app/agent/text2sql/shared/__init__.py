"""存放多个 Text-to-SQL 子图共同依赖的稳定基础能力。"""

from app.agent.text2sql.shared.system_guidance import (
    SystemGuidancePayload,
    build_system_guidance_message,
)

__all__ = ["SystemGuidancePayload", "build_system_guidance_message"]
