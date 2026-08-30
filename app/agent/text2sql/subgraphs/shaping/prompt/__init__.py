"""公开原料查询后结果塑形提示词。"""

from app.agent.text2sql.subgraphs.shaping.prompt.prompt import (
    MATERIAL_SHAPING_SYSTEM_PROMPT,
    build_material_shaping_messages,
)

__all__ = ["MATERIAL_SHAPING_SYSTEM_PROMPT", "build_material_shaping_messages"]
