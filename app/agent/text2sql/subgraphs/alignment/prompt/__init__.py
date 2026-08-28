"""向业务对齐子图公开稳定 Prompt 构建入口。"""

from app.agent.text2sql.subgraphs.alignment.prompt.prompt import (
    build_business_alignment_prompt,
)

__all__ = ["build_business_alignment_prompt"]
