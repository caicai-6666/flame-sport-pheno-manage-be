"""定义 SQL 子图专属输入模型，避免后续塑形配置进入查询上下文。"""

from pydantic import BaseModel, ConfigDict, Field


class MaterialSqlQueryPlan(BaseModel):
    """只包含 SQL 原料查询职责所需的指导和真实表范围。"""

    model_config = ConfigDict(extra="forbid")

    guidance: str = Field(
        min_length=1,
        description="查询主体、业务范围、资格条件和必须返回的全部原料",
    )
    required_tables: list[str] = Field(
        min_length=1,
        description="筛选、关联和返回原料所需的全部真实数据库表名",
    )
