"""公开不依赖 Pipeline 的 Text-to-SQL 用户交互模型。"""

from app.agent.text2sql.interaction.models import (
    AgentInteraction,
    AgentQuerySessionSnapshot,
    UserInteraction,
)
__all__ = [
    "AgentInteraction",
    "AgentQuerySessionSnapshot",
    "UserInteraction",
]
