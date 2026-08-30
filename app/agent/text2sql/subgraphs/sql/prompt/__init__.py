"""向 SQL 子图公开 Prompt 构建入口。"""

from app.agent.text2sql.subgraphs.sql.prompt.prompt import (
    build_sql_generation_messages,
    build_sql_generation_system_prompt,
)
from app.agent.text2sql.subgraphs.sql.prompt.material_prompt import (
    MATERIAL_SQL_SYSTEM_PROMPT,
    build_material_sql_generation_messages,
)

__all__ = [
    "MATERIAL_SQL_SYSTEM_PROMPT",
    "build_material_sql_generation_messages",
    "build_sql_generation_messages",
    "build_sql_generation_system_prompt",
]
