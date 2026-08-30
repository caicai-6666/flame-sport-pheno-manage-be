"""定义最终查询与塑形结果供审计使用的兼容计划投影。"""

from pydantic import BaseModel, ConfigDict, Field

from app.agent.text2sql.shared.yaml_context import render_yaml_context

class MaterialQueryPlan(BaseModel):
    """把最终选中的查询参数和塑形指导投影为下游兼容审计输入。"""

    model_config = ConfigDict(extra="forbid")

    guidance: str = Field(
        min_length=1,
        description=(
            "使用 Markdown bullet 说明查询主体、业务范围、资格条件和必须取得的业务原料；"
            "所有数据库筛选和资格判断必须在此说明，并且必须包含塑形指导引用的每个展示、"
            "分组、排序和动态列原料及其来源表；不得包含 SQL、结构化查询 AST，或猜测"
            "尚未通过真实表结构确认的字段名"
        ),
    )
    required_tables: list[str] = Field(
        min_length=1,
        description=(
            "SQL 查询原料需要读取的全部真实数据库表名，按依赖顺序排列；必须覆盖每个"
            "筛选条件、关联、返回业务值、分组值、动态列值和排序值的实际来源表"
        ),
    )
    raw_material_shaping_guidance: str = Field(
        min_length=1,
        description=(
            "使用 Markdown bullet 描述查询成功后如何把原料组织为目标表格，必须分别说明"
            "SQL 原料输入行粒度与塑形后最终行粒度，并说明普通展示字段、稳定分组、组内排序、"
            "动态列和隐藏技术原料；必须用唯一 bullet 声明动态列数量为‘固定 N 列’、"
            "‘由完整结果决定’或‘不适用’；每项引用都必须在 guidance 的必取原料中出现；"
            "不得包含 SQL、数据库筛选、聚合资格条件或结构化塑形对象"
        ),
    )


# 将最终计划投影渲染为稳定、易读的 YAML，供诊断日志和下游审计共享。
def render_material_query_plan(plan: MaterialQueryPlan) -> str:
    return render_yaml_context(
        {
            "原料查询指导": plan.guidance,
            "所需数据表": plan.required_tables,
            "原料塑形指导": plan.raw_material_shaping_guidance,
        }
    )


__all__ = [
    "MaterialQueryPlan",
    "render_material_query_plan",
]
