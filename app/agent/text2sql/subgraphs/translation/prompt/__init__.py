"""向结果翻译子图公开 Prompt 常量。"""

from app.agent.text2sql.subgraphs.translation.prompt.prompt import (
    TRANSLATION_RULE_SYSTEM_PROMPT,
    TRANSLATION_TARGET_SYSTEM_PROMPT,
)

__all__ = ["TRANSLATION_RULE_SYSTEM_PROMPT", "TRANSLATION_TARGET_SYSTEM_PROMPT"]
