"""定义 SQL 数据获取计划、结果塑形计划及其 Pydantic 工具模型。"""

import json
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


class QueryPlanAlias(BaseModel):
    """查询表达式中一个显式别名及其来源，供复杂子查询和 CTE 安全引用。"""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="查询表达式使用的别名，例如 active_project",
    )
    source_table: str | None = Field(
        description="直接来源的真实表名；CTE 或派生表别名传 null"
    )
    source_description: str = Field(description="该别名代表的数据集合")


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
    result_field: str = Field(
        default="",
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description=(
            "SQL 必须使用的稳定结果列别名，例如 user_name；"
            "结果塑形计划只能通过该名称引用 SQL 输出"
        ),
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


class QueryPlanQuantifiedCondition(BaseModel):
    """描述针对一组关联对象的全称、存在或数量约束。"""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(description="被筛选的主体，例如正式参与用户")
    collection: str = Field(description="需要量化判断的关联集合，例如全部有效锁定项目")
    quantifier: Literal[
        "all", "any", "none", "exactly", "at_least", "at_most"
    ] = Field(description="集合约束：全部、任一、没有、恰好、至少或至多")
    predicate: str = Field(
        description="集合成员必须满足的原始字段条件，使用表名或 aliases 中声明的别名"
    )
    correlation_condition: str | None = Field(
        default=None,
        description=(
            "EXISTS、NOT EXISTS 或相关子查询中把成员集合关联到外层主体的原始字段条件；"
            "HAVING 或无需相关子查询时传 null"
        ),
    )
    collection_filters: list[str] = Field(
        default_factory=list,
        description=(
            "只限定被量化成员集合范围的原始字段条件，例如有效状态；"
            "无额外集合范围时传空列表"
        ),
    )
    count: int | None = Field(
        default=None,
        ge=0,
        description="数量型约束的目标数量；all、any、none 时传 null",
    )
    implementation_hint: Literal[
        "having", "exists", "not_exists", "subquery", "cte"
    ] = Field(description="SQL 层实现该量化约束的建议方式")
    reason: str = Field(description="该量化条件对应的业务规则")

    # 拒绝把多个实现方式拼成一个枚举值，并给出选择单一值的明确判据。
    @field_validator("implementation_hint", mode="before")
    @classmethod
    def validate_single_implementation_hint(cls, value: object) -> object:
        if isinstance(value, str) and any(separator in value for separator in ("/", "、", ",")):
            raise ValueError(
                "implementation_hint 只能精确填写一个值：使用 WITH 命名资格集合时填 cte；"
                "使用括号内 SELECT 时填 subquery；使用相关反例排除时填 not_exists；"
                "不得提交 cte/subquery 等组合值"
            )
        return value

    # 保证数量型量词携带数量，集合型量词不混入无意义数字。
    @model_validator(mode="after")
    def validate_quantifier_count(self) -> "QueryPlanQuantifiedCondition":
        count_quantifiers = {"exactly", "at_least", "at_most"}
        if self.quantifier in count_quantifiers and self.count is None:
            raise ValueError(f"量词 {self.quantifier} 必须提供 count")
        if self.quantifier not in count_quantifiers and self.count is not None:
            raise ValueError(f"量词 {self.quantifier} 的 count 必须为 null")
        if (
            self.implementation_hint in {"exists", "not_exists"}
            and not self.correlation_condition
        ):
            raise ValueError(
                "implementation_hint 为 exists 或 not_exists 时 "
                "correlation_condition 不能为空"
            )
        if (
            self.implementation_hint not in {"exists", "not_exists", "subquery"}
            and self.correlation_condition is not None
        ):
            raise ValueError(
                "只有 exists、not_exists 或相关 subquery 可以提供 "
                "correlation_condition"
            )
        return self


class QueryPlanBusinessRuleImplementation(BaseModel):
    """把一条核心业务规则关联到实际承载它的查询计划组件。"""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(description="业务对齐层采用的 core rules.rule 稳定标识")
    plan_references: list[str] = Field(
        min_length=1,
        description=(
            "落实该规则的计划组件路径，例如 filters[1]、filters[2] "
            "或 quantified_conditions[0]"
        ),
    )
    reason: str = Field(description="这些计划组件如何共同实现该业务规则")


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


class ResultShapePlan(BaseModel):
    """SQL 和状态翻译完成后，由本地程序执行的确定性展示塑形计划。"""

    model_config = ConfigDict(extra="forbid")

    shape_type: Literal["passthrough", "pivot"] = Field(
        default="passthrough",
        description="原样表格使用 passthrough；按组动态转列使用 pivot",
    )
    result_row_granularity: str = Field(
        default="与 SQL 查询结果一致",
        description="塑形后最终结果中一行代表的业务对象",
    )
    group_fields: list[str] = Field(
        default_factory=list,
        description="pivot 时用于确定同一结果行的稳定 result_field 列表",
    )
    passthrough_fields: list[str] = Field(
        default_factory=list,
        description="最终按原值保留并展示一次的 result_field 列表",
    )
    pivot_value_field: str | None = Field(
        default=None,
        description="pivot 时依次填入动态列的 result_field；passthrough 时传 null",
    )
    pivot_order_field: str | None = Field(
        default=None,
        description="pivot 组内排序使用的 result_field；无需额外排序时传 null",
    )
    column_key_prefix: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="动态列机器键前缀，例如 project；passthrough 时传 null",
    )
    column_label_pattern: str | None = Field(
        default=None,
        description="动态列中文标题模板，必须包含 {index}，例如 运动项目{index}",
    )
    expected_pivot_columns: int | None = Field(
        default=None,
        ge=1,
        description="业务已确定的动态列数量；无法提前确定时传 null",
    )
    hidden_fields: list[str] = Field(
        default_factory=list,
        description="仅供分组或排序使用、不在最终表格展示的 result_field 列表",
    )

    # 约束透传和转列两类计划的专属字段，避免塑形层猜测缺失配置。
    @model_validator(mode="after")
    def validate_shape_configuration(self) -> "ResultShapePlan":
        for field_name, field_values in (
            ("group_fields", self.group_fields),
            ("passthrough_fields", self.passthrough_fields),
            ("hidden_fields", self.hidden_fields),
        ):
            if len(field_values) != len(set(field_values)):
                raise ValueError(f"{field_name} 不能包含重复字段")
        visible_hidden_overlap = sorted(
            set(self.passthrough_fields) & set(self.hidden_fields)
        )
        if visible_hidden_overlap:
            raise ValueError(
                "passthrough_fields 与 hidden_fields 不能包含同一字段："
                + ", ".join(visible_hidden_overlap)
            )
        pivot_only_values = (
            self.pivot_value_field,
            self.pivot_order_field,
            self.column_key_prefix,
            self.column_label_pattern,
        )
        if self.shape_type == "passthrough":
            if any(value is not None for value in pivot_only_values):
                raise ValueError(
                    "shape_type 为 passthrough 时 pivot_value_field、"
                    "pivot_order_field、column_key_prefix 和 column_label_pattern 必须为 null"
                )
            if self.group_fields or self.hidden_fields or self.expected_pivot_columns:
                raise ValueError(
                    "shape_type 为 passthrough 时 group_fields、hidden_fields 必须为空，"
                    "expected_pivot_columns 必须为 null"
                )
            return self

        if not self.group_fields:
            raise ValueError("shape_type 为 pivot 时 group_fields 不能为空")
        if any(value is None for value in pivot_only_values):
            raise ValueError(
                "shape_type 为 pivot 时 pivot_value_field、column_key_prefix "
                "和 column_label_pattern 均不能为空"
            )
        allowed_hidden_fields = set(self.group_fields)
        if self.pivot_order_field is not None:
            allowed_hidden_fields.add(self.pivot_order_field)
        unused_hidden_fields = sorted(
            set(self.hidden_fields) - allowed_hidden_fields
        )
        if unused_hidden_fields:
            raise ValueError(
                "hidden_fields 只能包含 group_fields 或 pivot_order_field 中实际使用的字段："
                + ", ".join(unused_hidden_fields)
            )
        assert self.column_label_pattern is not None
        if "{index}" not in self.column_label_pattern:
            raise ValueError("column_label_pattern 必须包含 {index} 占位符")
        return self


class NaturalLanguageQueryPlan(BaseModel):
    """后续 SQL 生成器单独消费的完整只读数据获取计划。"""

    model_config = ConfigDict(extra="forbid")

    query_goal: str = Field(description="本次查询要回答的业务问题")
    row_granularity: str = Field(description="SQL 原始结果中一行数据代表的业务对象")
    tables: list[QueryPlanTable] = Field(
        description="涉及的全部数据表及其职责，至少包含一张表", min_length=1
    )
    aliases: list[QueryPlanAlias] = Field(
        default_factory=list,
        description="查询表达式使用的全部别名；不使用别名时传空列表",
    )
    joins: list[QueryPlanJoin] = Field(
        description="全部跨表关联；单表查询时传空列表"
    )
    filters: list[QueryPlanFilter] = Field(
        description="全部筛选条件；无筛选时传空列表"
    )
    quantified_conditions: list[QueryPlanQuantifiedCondition] = Field(
        default_factory=list,
        description="针对关联集合的全部、任一、没有或数量约束；无时传空列表",
    )
    implemented_business_rules: list[QueryPlanBusinessRuleImplementation] = Field(
        default_factory=list,
        description="本次 SQL 计划落实的全部 core rules.rule 及其正式计划组件引用",
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
    having: list[QueryPlanFilter] = Field(
        default_factory=list,
        description="聚合完成后的筛选条件；无 HAVING 时传空列表",
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

    # 为旧版内部调用补齐稳定结果列名；远端 strict 工具仍要求模型显式提交 result_field。
    @model_validator(mode="after")
    def populate_legacy_result_fields(self) -> "NaturalLanguageQueryPlan":
        used_result_fields: set[str] = set()
        for index, select_field in enumerate(self.select_fields, start=1):
            if not select_field.result_field:
                candidates = re.findall(
                    r"[A-Za-z_][A-Za-z0-9_]*",
                    select_field.field,
                )
                base_name = candidates[-1].lower() if candidates else f"field_{index}"
                candidate = base_name
                duplicate_index = 2
                while candidate in used_result_fields:
                    candidate = f"{base_name}_{duplicate_index}"
                    duplicate_index += 1
                select_field.result_field = candidate
            if select_field.result_field in used_result_fields:
                raise ValueError(
                    "select_fields[].result_field 不能重复："
                    f"{select_field.result_field}"
                )
            used_result_fields.add(select_field.result_field)
        return self

    # 校验别名来源和跨字段引用都落在已声明范围内，避免把可定位问题推迟到 SQL 阶段。
    @model_validator(mode="after")
    def validate_table_reference_integrity(self) -> "NaturalLanguageQueryPlan":
        declared_table_names = {table.table_name for table in self.tables}
        implemented_rule_ids = [
            implementation.rule_id
            for implementation in self.implemented_business_rules
        ]
        if len(implemented_rule_ids) != len(set(implemented_rule_ids)):
            raise ValueError("implemented_business_rules[].rule_id 不能重复")
        component_sizes = {
            "joins": len(self.joins),
            "filters": len(self.filters),
            "quantified_conditions": len(self.quantified_conditions),
            "select_fields": len(self.select_fields),
            "group_by": len(self.group_by),
            "aggregations": len(self.aggregations),
            "having": len(self.having),
            "order_by": len(self.order_by),
        }
        for implementation in self.implemented_business_rules:
            if len(implementation.plan_references) != len(
                set(implementation.plan_references)
            ):
                raise ValueError(
                    "implemented_business_rules[].plan_references 不能重复"
                )
            for reference in implementation.plan_references:
                matched_reference = re.fullmatch(
                    r"(joins|filters|quantified_conditions|select_fields|"
                    r"group_by|aggregations|having|order_by)\[(\d+)\]",
                    reference,
                )
                if matched_reference is None:
                    raise ValueError(
                        "implemented_business_rules[].plan_references 只能引用"
                        "已有计划组件，例如 filters[0] 或 quantified_conditions[0]"
                    )
                component_name, raw_index = matched_reference.groups()
                if int(raw_index) >= component_sizes[component_name]:
                    raise ValueError(
                        f"计划组件引用越界：{reference}，"
                        f"{component_name} 只有 {component_sizes[component_name]} 项"
                    )
        declared_alias_names = {alias.alias for alias in self.aliases}
        if len(declared_alias_names) != len(self.aliases):
            raise ValueError("aliases[].alias 不能重复")
        shadowed_table_names = sorted(declared_alias_names & declared_table_names)
        if shadowed_table_names:
            raise ValueError(
                "aliases[].alias 不能与真实表名相同："
                + ", ".join(shadowed_table_names)
            )
        invalid_alias_sources = sorted(
            {
                alias.source_table
                for alias in self.aliases
                if alias.source_table is not None
                and alias.source_table not in declared_table_names
            }
        )
        if invalid_alias_sources:
            raise ValueError(
                "aliases[].source_table 引用了 tables 未声明的表："
                + ", ".join(invalid_alias_sources)
            )
        referenced_expressions = (
            [join.condition for join in self.joins]
            + [query_filter.condition for query_filter in self.filters]
            + [condition.predicate for condition in self.quantified_conditions]
            + [
                condition.correlation_condition
                for condition in self.quantified_conditions
                if condition.correlation_condition is not None
            ]
            + [
                collection_filter
                for condition in self.quantified_conditions
                for collection_filter in condition.collection_filters
            ]
            + [select_field.field for select_field in self.select_fields]
            + self.group_by
            + [aggregation.expression for aggregation in self.aggregations]
            + [having.condition for having in self.having]
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
            set(references_by_table).difference(
                declared_table_names | declared_alias_names
            )
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
    """规划结束时同时提交相互独立的数据获取计划和结果塑形计划。"""

    model_config = ConfigDict(extra="forbid")

    query_plan: NaturalLanguageQueryPlan = Field(
        description=(
            "只供 SQL 查询执行层消费的数据获取计划；"
            "所有字段均必须提供，不适用的列表字段传空列表"
        )
    )
    result_shape_plan: ResultShapePlan = Field(
        default_factory=ResultShapePlan,
        description=(
            "只供翻译后的确定性结果塑形层消费；"
            "普通结果使用 passthrough，需要动态按列展开时使用 pivot"
        ),
    )

    # 校验塑形字段全部来自 SQL 计划声明的稳定结果列，防止两份计划拆分后失去依赖闭包。
    @model_validator(mode="after")
    def validate_shape_dependencies(self) -> "NaturalLanguageQueryToolArguments":
        available_fields = {
            select_field.result_field for select_field in self.query_plan.select_fields
        }
        shape_plan = self.result_shape_plan
        referenced_fields = set(shape_plan.group_fields)
        referenced_fields.update(shape_plan.passthrough_fields)
        referenced_fields.update(shape_plan.hidden_fields)
        if shape_plan.pivot_value_field is not None:
            referenced_fields.add(shape_plan.pivot_value_field)
        if shape_plan.pivot_order_field is not None:
            referenced_fields.add(shape_plan.pivot_order_field)
        unknown_fields = sorted(referenced_fields - available_fields)
        if unknown_fields:
            raise ValueError(
                "result_shape_plan 引用了 query_plan.select_fields 未声明的 "
                "result_field：" + ", ".join(unknown_fields)
            )
        if shape_plan.shape_type == "passthrough" and not shape_plan.passthrough_fields:
            shape_plan.passthrough_fields = [
                select_field.result_field for select_field in self.query_plan.select_fields
            ]
        return self


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
            "同时提交结构化的只读 SQL 数据获取计划和确定性结果塑形计划。"
            "所有关联、筛选、返回字段、分组和排序均须保留表名.原始字段名；"
            "query_plan 只供 SQL 查询执行层使用，result_shape_plan 不得改变查询口径；"
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
    try:
        arguments_payload = json.loads(arguments_json)
    except json.JSONDecodeError:
        return NaturalLanguageQueryToolArguments.model_validate_json(arguments_json)
    if (
        isinstance(arguments_payload, dict)
        and "query_plan" in arguments_payload
        and "result_shape_plan" not in arguments_payload
    ):
        arguments_payload["result_shape_plan"] = ResultShapePlan().model_dump()
    return NaturalLanguageQueryToolArguments.model_validate(arguments_payload)


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
    result_shape_plan: ResultShapePlan | None = None,
) -> str:
    """将 SQL 数据获取计划及独立塑形计划渲染为人工可复核的说明。"""
    table_lines = [f"- `{table.table_name}`：{table.role}" for table in query_plan.tables]
    join_lines = [
        f"- `{join.condition}`：{join.reason}" for join in query_plan.joins
    ] or ["- 无跨表关联。"]
    filter_lines = [
        f"- `{query_filter.condition}`：{query_filter.reason}"
        for query_filter in query_plan.filters
    ] or ["- 无筛选条件。"]
    quantified_lines = [
        (
            f"- `{condition.quantifier}` {condition.collection}："
            f"成员条件 `{condition.predicate}`；"
            f"主体关联 `{condition.correlation_condition or '不适用'}`；"
            "集合范围 "
            + (
                "、".join(f"`{item}`" for item in condition.collection_filters)
                or "无额外条件"
            )
            + f"（{condition.reason}）"
        )
        for condition in query_plan.quantified_conditions
    ] or ["- 无集合量化条件。"]
    implemented_rule_lines = [
        (
            f"- `{implementation.rule_id}`："
            + "、".join(implementation.plan_references)
        )
        for implementation in query_plan.implemented_business_rules
    ] or ["- 无核心规则实现。"]
    select_lines = [
        (
            f"- `{select_field.field}` AS `{select_field.result_field}`："
            f"{select_field.purpose}"
        )
        for select_field in query_plan.select_fields
    ]
    group_by_lines = [f"- `{field}`" for field in query_plan.group_by] or ["- 无分组。"]
    aggregation_lines = [
        f"- `{aggregation.expression}`：{aggregation.purpose}"
        for aggregation in query_plan.aggregations
    ] or ["- 无聚合。"]
    having_lines = [
        f"- `{having.condition}`：{having.reason}" for having in query_plan.having
    ] or ["- 无聚合后筛选。"]
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
    resolved_shape_plan = result_shape_plan or ResultShapePlan(
        passthrough_fields=[
            select_field.result_field for select_field in query_plan.select_fields
        ]
    )
    shape_lines = [
        f"- 类型：`{resolved_shape_plan.shape_type}`",
        f"- 最终行粒度：{resolved_shape_plan.result_row_granularity}",
        "- 透传字段："
        + (", ".join(resolved_shape_plan.passthrough_fields) or "无"),
    ]
    if resolved_shape_plan.shape_type == "pivot":
        shape_lines.extend(
            (
                "- 分组字段：" + ", ".join(resolved_shape_plan.group_fields),
                f"- 动态列取值字段：{resolved_shape_plan.pivot_value_field}",
                f"- 动态列标题：{resolved_shape_plan.column_label_pattern}",
            )
        )
    return "\n\n".join(
        (
            "# SQL 数据获取计划",
            "## 查询目标\n\n" + query_plan.query_goal,
            "## SQL 原始行粒度\n\n" + query_plan.row_granularity,
            "## 涉及表\n\n" + "\n".join(table_lines),
            "## 关联关系\n\n" + "\n".join(join_lines),
            "## 筛选条件\n\n" + "\n".join(filter_lines),
            "## 集合量化条件\n\n" + "\n".join(quantified_lines),
            "## 核心规则实现\n\n" + "\n".join(implemented_rule_lines),
            "## 返回字段\n\n" + "\n".join(select_lines),
            "## 分组与聚合\n\n"
            + "### 分组\n\n"
            + "\n".join(group_by_lines)
            + "\n\n### 聚合\n\n"
            + "\n".join(aggregation_lines),
            "## 聚合后筛选\n\n" + "\n".join(having_lines),
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
            "# 结果塑形计划\n\n" + "\n".join(shape_lines),
        )
    )
