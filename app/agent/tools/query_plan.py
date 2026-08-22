"""定义结构化联合查询计划及其 Pydantic 工具模型。"""

import re
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent.tools.strict_schema import build_strict_tool_definition


NATURAL_LANGUAGE_QUERY_TOOL_NAME: Final[str] = "execute_natural_language_query"
ABANDON_QUERY_PLANNING_TOOL_NAME: Final[str] = "abandon_query_planning"


class QueryPlanTable(BaseModel):
    """查询计划中单张参与表的原始名称和业务职责。"""

    model_config = ConfigDict(extra="forbid")

    table_name: str = Field(description="数据库原始表名")
    role: str = Field(description="该表在本次查询中的职责")


class QueryPlanJoin(BaseModel):
    """查询计划中一条跨表关联的原始字段表达式。"""

    model_config = ConfigDict(extra="forbid")

    condition: str = Field(
        description="关联条件，必须使用表名.原始字段名，例如 detail.parent_id = parent.id"
    )
    reason: str = Field(description="采用该关联条件的业务原因")


class QueryPlanFilter(BaseModel):
    """查询计划中一条筛选条件及其业务口径。"""

    model_config = ConfigDict(extra="forbid")

    condition: str = Field(
        description="筛选表达式，必须使用表名.原始字段名，例如 entity.status = 1"
    )
    reason: str = Field(description="该筛选条件的业务含义")


class QueryPlanSelectField(BaseModel):
    """查询计划中一个返回字段及其面向调用方的含义。"""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(
        description="返回字段或聚合表达式，必须保留表名.原始字段名，例如 entity.name"
    )
    purpose: str = Field(
        min_length=1,
        max_length=24,
        description=(
            "该字段是最终结果中给用户看的简短表头标签，建议使用 2～12 个字的名词短语；"
            "只能写字段名称或展示内容，不得写查询策略、定位用途、前端行为或业务规则；"
            "例如使用‘积分流水 ID’，不要写‘用于定位最新的积分流水’"
        ),
    )

    # 明确表头字段只服务于用户展示，避免模型把查询策略和内部处理说明混入结果列名称。
    @field_validator("purpose")
    @classmethod
    def validate_concise_display_purpose(cls, value: str) -> str:
        normalized = value.strip()
        forbidden_terms = (
            "用于",
            "供",
            "以便",
            "通过",
            "方便",
            "因为",
            "代表",
            "表示",
            "判断",
            "定位",
            "后续",
            "调用",
            "查询",
            "筛选",
            "统计",
            "区分",
        )
        if any(term in normalized for term in forbidden_terms):
            raise ValueError(
                "select_fields[].purpose 只用于生成最终结果中面向用户展示的表头，"
                "当前内容包含查询策略、定位用途、前端行为或业务规则，"
                "这些内容不应出现在用户表头中。"
                "请根据当前 field 的实际业务含义，删除用途说明，"
                "只保留用户应看到的字段名称或展示内容；系统不会替你指定一个固定名称。"
                "查询、筛选、定位和业务口径请分别放入 query_goal、filters 或 business_caliber。"
            )
        return normalized


class QueryPlanAggregation(BaseModel):
    """查询计划中一个聚合表达式及其计算目的。"""

    model_config = ConfigDict(extra="forbid")

    expression: str = Field(
        description="聚合表达式，必须保留涉及的表名.原始字段名，例如 COUNT(entity.id)"
    )
    purpose: str = Field(description="该聚合的业务含义")


class QueryPlanOrderBy(BaseModel):
    """查询计划中一个排序字段与方向。"""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(description="排序字段，必须使用表名.原始字段名")
    direction: Literal["ASC", "DESC"] = Field(description="排序方向")


class QueryPlanPagination(BaseModel):
    """规划层为本次查询确定的结果范围，未指定时完整执行符合筛选条件的查询。"""

    model_config = ConfigDict(extra="forbid")

    limit: int | None = Field(
        description=(
            "规划层为本次查询确定的最大返回行数；无需用户明确提出也可按查询目标设置。"
            "null 表示必须完整返回当前筛选结果，SQL 不添加 LIMIT"
        ),
        ge=1,
    )
    offset: int = Field(default=0, description="结果偏移量；未分页时为 0", ge=0)


class NaturalLanguageQueryPlan(BaseModel):
    """后续 SQL 生成器可直接消费的完整只读联合查询计划。"""

    model_config = ConfigDict(extra="forbid")

    query_goal: str = Field(description="本次查询要回答的业务问题")
    row_granularity: str = Field(description="结果中一行数据代表的业务对象")
    tables: list[QueryPlanTable] = Field(
        description="涉及的全部数据表及其职责，至少包含一张表", min_length=1
    )
    joins: list[QueryPlanJoin] = Field(
        description="全部跨表关联；单表查询时传空列表"
    )
    filters: list[QueryPlanFilter] = Field(
        description="全部筛选条件；无筛选时传空列表"
    )
    select_fields: list[QueryPlanSelectField] = Field(
        description="返回字段，至少包含一个字段", min_length=1
    )
    group_by: list[str] = Field(
        description="分组字段，必须使用表名.原始字段名；不分组时传空列表"
    )
    aggregations: list[QueryPlanAggregation] = Field(
        description="聚合表达式；不聚合时传空列表"
    )
    order_by: list[QueryPlanOrderBy] = Field(
        description="排序规则；不排序时传空列表"
    )
    pagination: QueryPlanPagination = Field(description="分页或返回数量限制")
    query_strategy: Literal["join", "subquery", "cte", "mixed"] = Field(
        description="主要查询策略；普通关联查询优先使用 join"
    )
    strategy_reason: str = Field(description="选择该查询策略的原因")
    business_caliber: list[str] = Field(
        description="有效状态、去重、时间范围和空值等业务口径；无额外口径时传空列表"
    )
    assumptions: list[str] = Field(
        description="无法从现有事实确认但影响查询的假设；无假设时传空列表"
    )

    # 校验计划中的跨字段引用全部落在已声明表范围内，避免不完整计划把错误推迟到 SQL 阶段。
    @model_validator(mode="after")
    def validate_table_reference_integrity(self) -> "NaturalLanguageQueryPlan":
        declared_table_names = {table.table_name for table in self.tables}
        referenced_expressions = (
            [join.condition for join in self.joins]
            + [query_filter.condition for query_filter in self.filters]
            + [select_field.field for select_field in self.select_fields]
            + self.group_by
            + [aggregation.expression for aggregation in self.aggregations]
            + [order_by.field for order_by in self.order_by]
        )
        references_by_table: dict[str, set[str]] = {}
        for expression in referenced_expressions:
            for table_name, field_name in re.findall(
                r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b",
                expression,
            ):
                references_by_table.setdefault(table_name, set()).add(field_name)

        missing_table_names = sorted(
            set(references_by_table).difference(declared_table_names)
        )
        if missing_table_names:
            missing_references = ", ".join(
                f"{table_name}.{field_name}"
                for table_name in missing_table_names
                for field_name in sorted(references_by_table[table_name])
            )
            raise ValueError(
                "查询计划引用了 tables 未声明的表："
                + ", ".join(missing_table_names)
                + f"；相关引用：{missing_references}。"
                "请将缺少的表加入 tables，并补充必要的关联关系。"
            )
        return self


class NaturalLanguageQueryToolArguments(BaseModel):
    """执行联合查询前必须提供的完整结构化查询计划。"""

    model_config = ConfigDict(extra="forbid")

    query_plan: NaturalLanguageQueryPlan = Field(
        description="完整查询计划，所有字段均必须提供；不适用的列表字段传空列表"
    )


class QueryPlanningAbandonment(BaseModel):
    """查询规划无法产生可靠 SQL 计划时面向用户的结构化放弃说明。"""

    model_config = ConfigDict(extra="forbid")

    reason_type: Literal[
        "entity_not_found", "insufficient_information", "unsupported_scope"
    ] = Field(description="规划放弃原因分类")
    user_message: str = Field(description="向用户展示的简洁原因与下一步建议")
    confirmed_facts: list[str] = Field(
        description="已经通过表结构、实体检索或用户澄清确认的事实；无时传空列表"
    )


class AbandonQueryPlanningArguments(BaseModel):
    """规划模型主动终止当前查询时使用的函数调用参数。"""

    model_config = ConfigDict(extra="forbid")

    reason_type: Literal[
        "entity_not_found", "insufficient_information", "unsupported_scope"
    ] = Field(description="放弃原因分类")
    reason: str = Field(
        description="必须说明无法生成可靠查询计划的具体理由，并给出用户可采取的下一步"
    )
    confirmed_facts: list[str] = Field(
        description="已经通过表结构、实体检索或用户澄清确认的事实；无时传空列表"
    )


# 基于 Pydantic 参数模型生成函数调用定义，强制模型提供可验证的完整查询计划。
def build_natural_language_query_tool_definition() -> dict[str, object]:
    return build_strict_tool_definition(
        tool_name=NATURAL_LANGUAGE_QUERY_TOOL_NAME,
        description=(
            "提交结构化的只读联合查询计划。"
            "所有关联、筛选、返回字段、分组和排序均须保留表名.原始字段名；"
            "pagination.limit 是规划层确定的结果行上限；"
            "为 null 时后续 SQL 将按筛选条件完整执行。"
        ),
        arguments_model=NaturalLanguageQueryToolArguments,
    )


# 基于 Pydantic 放弃模型生成函数调用定义，区分业务无法继续和模型、数据库等技术失败。
def build_abandon_query_planning_tool_definition(
    query_scope: str = "当前只读数据查询范围",
) -> dict[str, object]:
    return build_strict_tool_definition(
        tool_name=ABANDON_QUERY_PLANNING_TOOL_NAME,
        description=(
            "当实体检索已确认用户指定的业务实体不存在，"
            f"关键事实在向用户询问后仍不足，或请求超出{query_scope}时，"
            "提交放弃原因并结束规划。不得把最终 SQL 返回空行的正常查询结果当作放弃。"
        ),
        arguments_model=AbandonQueryPlanningArguments,
    )


# 将模型返回的函数参数 JSON 按 Pydantic 模型校验为后续 SQL 执行器可消费的查询计划。
def parse_natural_language_query_tool_arguments(
    arguments_json: str,
) -> NaturalLanguageQueryToolArguments:
    return NaturalLanguageQueryToolArguments.model_validate_json(arguments_json)


# 将放弃工具参数 JSON 按 Pydantic 校验为可供 Pipeline 正常停止的规划结果。
def parse_abandon_query_planning_arguments(
    arguments_json: str,
) -> QueryPlanningAbandonment:
    arguments = AbandonQueryPlanningArguments.model_validate_json(arguments_json)
    return QueryPlanningAbandonment(
        reason_type=arguments.reason_type,
        user_message=arguments.reason,
        confirmed_facts=arguments.confirmed_facts,
    )


# 将结构化查询计划渲染为统一层次的 Markdown，便于人工复核并保留原始数据库标识符。
def render_natural_language_query_plan(
    query_plan: NaturalLanguageQueryPlan,
) -> str:
    """将查询计划及规划层确定的结果范围渲染为人工可复核的说明。"""
    table_lines = [f"- `{table.table_name}`：{table.role}" for table in query_plan.tables]
    join_lines = [
        f"- `{join.condition}`：{join.reason}" for join in query_plan.joins
    ] or ["- 无跨表关联。"]
    filter_lines = [
        f"- `{query_filter.condition}`：{query_filter.reason}"
        for query_filter in query_plan.filters
    ] or ["- 无筛选条件。"]
    select_lines = [
        f"- `{select_field.field}`：{select_field.purpose}"
        for select_field in query_plan.select_fields
    ]
    group_by_lines = [f"- `{field}`" for field in query_plan.group_by] or ["- 无分组。"]
    aggregation_lines = [
        f"- `{aggregation.expression}`：{aggregation.purpose}"
        for aggregation in query_plan.aggregations
    ] or ["- 无聚合。"]
    order_by_lines = [
        f"- `{order_by.field} {order_by.direction}`" for order_by in query_plan.order_by
    ] or ["- 无排序。"]
    pagination = (
        f"规划最多返回 `{query_plan.pagination.limit}` 行，偏移量 `{query_plan.pagination.offset}`。"
        if query_plan.pagination.limit is not None
        else "规划未设置行数上限；SQL 将完整执行当前筛选条件，偏移量为 0。"
    )
    business_caliber_lines = [
        f"- {business_caliber}" for business_caliber in query_plan.business_caliber
    ] or ["- 无额外业务口径。"]
    assumption_lines = [f"- {assumption}" for assumption in query_plan.assumptions] or [
        "- 无额外假设。"
    ]
    return "\n\n".join(
        (
            "## 查询目标\n\n" + query_plan.query_goal,
            "## 结果行粒度\n\n" + query_plan.row_granularity,
            "## 涉及表\n\n" + "\n".join(table_lines),
            "## 关联关系\n\n" + "\n".join(join_lines),
            "## 筛选条件\n\n" + "\n".join(filter_lines),
            "## 返回字段\n\n" + "\n".join(select_lines),
            "## 分组与聚合\n\n"
            + "### 分组\n\n"
            + "\n".join(group_by_lines)
            + "\n\n### 聚合\n\n"
            + "\n".join(aggregation_lines),
            "## 排序与分页\n\n"
            + "### 排序\n\n"
            + "\n".join(order_by_lines)
            + "\n\n### 分页\n\n"
            + pagination,
            "## 查询策略\n\n"
            + f"- 策略：`{query_plan.query_strategy}`\n"
            + f"- 原因：{query_plan.strategy_reason}",
            "## 业务口径\n\n" + "\n".join(business_caliber_lines),
            "## 假设与待确认项\n\n" + "\n".join(assumption_lines),
        )
    )
