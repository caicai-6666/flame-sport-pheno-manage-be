"""集中暴露结果审计子图当前使用的提示词构建器。"""

from app.agent.text2sql.subgraphs.audit.node import (
    _build_audit_messages,
    _build_audit_system_prompt,
)

build_audit_messages = _build_audit_messages
build_audit_system_prompt = _build_audit_system_prompt
