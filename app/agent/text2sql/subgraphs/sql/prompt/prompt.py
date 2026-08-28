"""集中暴露 SQL 子图当前使用的生成提示词。"""

from app.agent.text2sql.subgraphs.sql.node import (
    _build_sql_generation_messages,
    _build_sql_generation_system_prompt,
)

build_sql_generation_messages = _build_sql_generation_messages
build_sql_generation_system_prompt = _build_sql_generation_system_prompt
