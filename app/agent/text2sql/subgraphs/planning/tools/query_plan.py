"""定义查询规划子图的 SQL 数据获取计划与结果塑形工具模型。"""

import re
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent.text2sql.shared.tools.argument_compatibility import (
    validate_tool_arguments_with_embedded_json_fallback,
)
from app.agent.text2sql.shared.tools.pydantic_schema import (
    build_pydantic_tool_definition,
)


NATURAL_LANGUAGE_QUERY_TOOL_NAME: Final[str] = "execute_natural_language_query"
ABANDON_QUERY_PLANNING_TOOL_NAME: Final[str] = "abandon_query_planning"


class QueryPlanTable(BaseModel):
    """查询计划中单张参与表的原始名称和业务职责。"""

    model_config = ConfigDict(extra="forbid")

    table_name: str = Field(description="数据库原始表名")
    role: str = Field(description="该表在本次查询中的职责")


class QueryPlanJoin(BaseModel):
    """查询计划中一条方向、保留语义和基数均明确的跨数据源关联。"""

    model_config = ConfigDict(extra="forbid")

    right_source: str = Field(
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description=(
            "按声明顺序关联到当前左侧结果集的数据源；"
            "必须是本查询块的真实表、前置块或显式角色别名"
        ),
    )
    join_type: Literal["inner", "left"] = Field(
        description=(
            "inner 只保留匹配行；left 保留加入该数据源前的全部左侧行。"
            "不得让 SQL 层根据 reason 猜测"
        )
    )
    cardinality: Literal[
        "one_to_one", "many_to_one", "one_to_many", "many_to_many"
    ] = Field(
        description=(
            "相对于加入 right_source 前每一条左侧结果行的关联基数；"
            "one_to_many 和 many_to_many 可能放大左侧行数"
        )
    )
    right_key: list[str] = Field(
        min_length=1,
        description=(
            "唯一标识 right_source 一行的原始字段键；每项必须使用 "
            "right_source.字段名，用于校验一对多关联后的最终行粒度"
        ),
    )
    condition: str = Field(
        description=(
            "完整 JOIN ON 条件，必须同时引用 right_source 和已经进入左侧结果集的数据源；"
            "必须使用原始字段名，不得拆到 WHERE 或 HAVING"
        )
    )
    reason: str = Field(description="采用该关联条件的业务原因")


class QueryPlanDeduplication(BaseModel):
    """声明查询块是否通过 SELECT DISTINCT 消除一对多关联产生的重复主体行。"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["none", "distinct"] = Field(
        description=(
            "none 表示不执行结果去重；distinct 表示本查询块必须使用 SELECT DISTINCT。"
            "按 grain_fields 分组的查询块必须使用 none"
        )
    )
    reason: str = Field(description="选择该去重方式的业务原因")


class QueryPlanAlias(BaseModel):
    """查询表达式中一个显式别名及其来源，供复杂子查询和 CTE 安全引用。"""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="查询表达式使用的别名，例如 active_project",
    )
    source_table: str = Field(description="该角色别名直接来源的真实表名")
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
    subject_key: list[str] = Field(
        min_length=1,
        description=(
            "被筛选主体在当前查询块中的稳定原始字段键，例如 season_user.id；"
            "用于校验量词资格与查询块行粒度是否一致"
        ),
    )
    collection: str = Field(description="需要量化判断的关联集合，例如全部有效锁定项目")
    collection_base_source: str | None = Field(
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description=(
            "all、any、none 对应相关子查询内部 FROM 的唯一起始数据源；"
            "数量型量词固定使用 HAVING，必须传 null"
        ),
    )
    collection_joins: list[QueryPlanJoin] = Field(
        description=(
            "all、any、none 对应相关子查询内部按顺序执行的完整 JOIN；"
            "数量型量词固定使用 HAVING，必须传空列表"
        )
    )
    quantifier: Literal[
        "all", "any", "none", "exactly", "at_least", "at_most"
    ] = Field(description="集合约束：全部、任一、没有、恰好、至少或至多")
    predicate: str = Field(
        description="集合成员必须满足的原始字段条件，使用表名或 aliases 中声明的别名"
    )
    member_key: list[str] = Field(
        default_factory=list,
        max_length=1,
        description=(
            "数量型量词用于 COUNT(DISTINCT ...) 的单一集合成员稳定键，"
            "例如 season_user_project.id；"
            "all、any、none 时传空列表"
        ),
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
    require_non_empty: bool = Field(
        description=(
            "仅 all 或 none 使用：true 表示成员集合为空时主体不匹配，false 表示按"
            "标准空集语义判断；any 和数量型量词必须传 false"
        )
    )
    reason: str = Field(description="该量化条件对应的业务规则")

    # 保证数量型量词携带数量，集合型量词不混入无意义数字。
    @model_validator(mode="after")
    def validate_quantifier_count(self) -> "QueryPlanQuantifiedCondition":
        count_quantifiers = {"exactly", "at_least", "at_most"}
        if self.quantifier in count_quantifiers and self.count is None:
            raise ValueError(f"量词 {self.quantifier} 必须提供 count")
        if self.quantifier in count_quantifiers and not self.member_key:
            raise ValueError(f"量词 {self.quantifier} 必须提供 member_key")
        if self.quantifier not in count_quantifiers and self.count is not None:
            raise ValueError(f"量词 {self.quantifier} 的 count 必须为 null")
        if self.quantifier not in count_quantifiers and self.member_key:
            raise ValueError(f"量词 {self.quantifier} 的 member_key 必须为空列表")
        if self.quantifier in {"all", "any", "none"} and not self.correlation_condition:
            raise ValueError(
                f"量词 {self.quantifier} 使用相关集合判断，correlation_condition 不能为空"
            )
        if self.quantifier in {"all", "any", "none"}:
            correlation_qualifiers = {
                qualifier
                for qualifier, _ in re.findall(
                    r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b",
                    self.correlation_condition or "",
                )
            }
            if len(correlation_qualifiers) < 2:
                raise ValueError(
                    f"量词 {self.quantifier} 的 correlation_condition 必须显式关联"
                    "成员侧与外层主体侧两个不同数据源；"
                    "同一真实表承担两个角色时必须先在 aliases 声明不同别名"
                )
        if self.quantifier in {"all", "any", "none"} and not self.collection_base_source:
            raise ValueError(
                f"量词 {self.quantifier} 必须提供 collection_base_source，"
                "用于唯一确定相关子查询内部 FROM"
            )
        if self.quantifier in count_quantifiers and self.collection_base_source is not None:
            raise ValueError(
                "数量型量词固定在主体粒度查询块中按 HAVING 计算，"
                "collection_base_source 必须为 null"
            )
        if self.quantifier in count_quantifiers and self.collection_joins:
            raise ValueError(
                "数量型量词固定在主体粒度查询块中按 HAVING 计算，"
                "collection_joins 必须为空列表"
            )
        if self.quantifier in count_quantifiers and self.correlation_condition is not None:
            raise ValueError(
                "数量型量词固定在主体粒度查询块中按 HAVING 计算，"
                "correlation_condition 必须为 null"
            )
        if self.quantifier not in {"all", "none"} and self.require_non_empty:
            raise ValueError(
                "require_non_empty 只适用于 all 或 none；any 和数量型量词必须传 false"
            )
        return self

    # 根据量词唯一确定 SQL 关系运算，禁止规划模型同时维护一份可能冲突的实现提示。
    @property
    def implementation(self) -> Literal["having", "exists", "not_exists"]:
        if self.quantifier in {"exactly", "at_least", "at_most"}:
            return "having"
        if self.quantifier == "any":
            return "exists"
        return "not_exists"


class QueryPlanBusinessRuleImplementation(BaseModel):
    """把一条核心业务规则关联到实际承载它的查询计划组件。"""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(description="业务对齐层采用的 core rules.rule 稳定标识")
    plan_references: list[str] = Field(
        min_length=1,
        description=(
            "落实该规则的带查询块作用域组件路径，例如 "
            "query_blocks[qualified_users].filters[0]"
        ),
    )
    reason: str = Field(description="这些计划组件如何共同实现该业务规则")


class QueryPlanBusinessCaliber(BaseModel):
    """把一条面向人的业务口径绑定到唯一的正式查询计划组件。"""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(description="有效状态、时间范围、空值或去重等业务口径")
    plan_references: list[str] = Field(
        min_length=1,
        description=(
            "实际落实该口径的带查询块作用域组件路径；"
            "不能只写自然语言而不引用正式筛选、关联、量词、聚合或去重组件"
        ),
    )


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


class QueryPlanBlock(BaseModel):
    """具有独立行粒度和条件作用域的查询块。"""

    model_config = ConfigDict(extra="forbid")

    block_id: str = Field(
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="查询块稳定标识；非根块将作为同名 CTE 输出",
    )
    role: Literal["qualification", "aggregation", "detail", "result"] = Field(
        description="查询块职责：资格筛选、聚合、明细或最终结果"
    )
    row_granularity: str = Field(description="该查询块中一行代表的业务对象")
    grain_fields: list[str] = Field(
        description=(
            "唯一确定该查询块一行的原始字段；全局单行聚合可传空列表，"
            "其余查询块至少提供一个字段"
        )
    )
    source_tables: list[str] = Field(
        description=(
            "该查询块外层关系和 quantified_conditions 集合关系读取的全部真实表名；"
            "不读真实表时传空列表，其他子查询应拆成独立 query_block"
        )
    )
    input_blocks: list[str] = Field(
        description="该查询块直接读取的前置查询块 block_id；无时传空列表"
    )
    base_source: str = Field(
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description=(
            "该查询块外层 FROM 的唯一起始数据源；必须是 source_tables、input_blocks "
            "或 aliases 中可直接引用的一个名称"
        ),
    )
    aliases: list[QueryPlanAlias] = Field(
        default_factory=list,
        description="仅在本查询块内生效的真实表别名；不使用时传空列表",
    )
    joins: list[QueryPlanJoin] = Field(description="仅在本查询块内执行的关联")
    deduplication: QueryPlanDeduplication = Field(
        description="本查询块唯一的 SELECT DISTINCT 语义来源"
    )
    filters: list[QueryPlanFilter] = Field(description="仅在本查询块内执行的行筛选")
    quantified_conditions: list[QueryPlanQuantifiedCondition] = Field(
        default_factory=list,
        description="仅在本查询块粒度上判定的集合量词条件",
    )
    select_fields: list[QueryPlanSelectField] = Field(
        min_length=1,
        description="该查询块向后续查询块或最终结果暴露的字段",
    )
    group_by: list[str] = Field(description="本查询块的分组键；不分组时传空列表")
    aggregations: list[QueryPlanAggregation] = Field(
        description="本查询块的聚合表达式；不聚合时传空列表"
    )
    having: list[QueryPlanFilter] = Field(
        default_factory=list,
        description="仅筛选本查询块聚合结果的条件；无时传空列表",
    )
    order_by: list[QueryPlanOrderBy] = Field(
        description="本查询块内部的排序；不排序时传空列表"
    )
    strategy: Literal["join", "subquery", "aggregate", "mixed"] = Field(
        description=(
            "由正式计划组件派生的人工审阅标签：只有外层关联为 join，"
            "只有 EXISTS/NOT EXISTS 量词为 subquery，分组、聚合或数量量词为 "
            "aggregate，两类及以上并存为 mixed；不作为第二份 SQL 指令"
        )
    )
    strategy_reason: str = Field(
        description="面向人说明已声明组件的组合，不得增加未在正式组件中出现的查询口径"
    )

    # 为每个查询块补齐稳定输出名，并拒绝同一查询块内重复的机器列名。
    @model_validator(mode="after")
    def populate_result_fields(self) -> "QueryPlanBlock":
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
                    "select_fields[].result_field 不能在同一 query_block 中重复："
                    f"{select_field.result_field}"
                )
            used_result_fields.add(select_field.result_field)
        return self

    # 将分组粒度与量词主体显式对齐，阻止在用户粒度分组后直接选择凭证明细。
    @model_validator(mode="after")
    def validate_block_granularity(self) -> "QueryPlanBlock":
        if not self.grain_fields and not self.aggregations:
            raise ValueError(
                "只有全局单行聚合 query_block 可以把 grain_fields 设为空列表"
            )
        count_quantifiers = {"exactly", "at_least", "at_most"}
        numeric_conditions = [
            condition
            for condition in self.quantified_conditions
            if condition.quantifier in count_quantifiers
        ]
        if self.having and not self.aggregations:
            raise ValueError("query_block.having 非空时 aggregations 不能为空")
        if numeric_conditions and set(self.group_by) != set(self.grain_fields):
            raise ValueError(
                "数量型 quantified_conditions 必须在主体 grain_fields 粒度分组；"
                "query_block.group_by 必须与 grain_fields 完全一致"
            )
        if numeric_conditions and self.having:
            raise ValueError(
                "数量型 quantified_conditions 已唯一声明计数键、比较方式和数量，"
                "同一查询块不得再用 having 重复表达；其他聚合后筛选请拆到后续查询块"
            )
        if self.group_by and set(self.grain_fields) != set(self.group_by):
            raise ValueError(
                "query_block.group_by 必须与 grain_fields 完全一致；"
                "需要在不同粒度返回明细时，应先建立资格查询块，再由结果查询块引用"
            )
        if self.group_by:
            normalized_group_fields = {
                re.sub(r"\s+", "", field.replace("`", "").lower())
                for field in self.group_by
            }
            normalized_aggregations = {
                re.sub(r"\s+", "", item.expression.replace("`", "").lower())
                for item in self.aggregations
            }
            invalid_select_fields = [
                item.field
                for item in self.select_fields
                if re.sub(r"\s+", "", item.field.replace("`", "").lower())
                not in normalized_group_fields | normalized_aggregations
            ]
            if invalid_select_fields:
                raise ValueError(
                    "分组 query_block 的 select_fields 只能包含 group_by 字段或已声明"
                    "聚合表达式；以下明细字段与当前粒度冲突："
                    + ", ".join(invalid_select_fields)
                    + "。请先建立主体资格块，再由结果块返回这些明细"
                )
        if self.aggregations and not self.group_by and self.grain_fields:
            raise ValueError(
                "包含普通聚合且保留业务行粒度的 query_block 必须提供 group_by；"
                "全局单行聚合应将 grain_fields 设为空列表"
            )
        if self.aggregations and not self.grain_fields:
            normalized_aggregations = {
                re.sub(r"\s+", "", item.expression.replace("`", "").lower())
                for item in self.aggregations
            }
            invalid_global_outputs = [
                item.field
                for item in self.select_fields
                if re.sub(r"\s+", "", item.field.replace("`", "").lower())
                not in normalized_aggregations
            ]
            if invalid_global_outputs:
                raise ValueError(
                    "全局单行聚合 query_block 的 select_fields 只能包含已声明聚合表达式："
                    + ", ".join(invalid_global_outputs)
                )
        for index, condition in enumerate(self.quantified_conditions):
            if (
                condition.implementation == "having"
                and set(condition.subject_key) != set(self.grain_fields)
            ):
                raise ValueError(
                    f"quantified_conditions[{index}] 的 subject_key 必须与当前查询块 "
                    "grain_fields 完全一致；请把该量词放入主体粒度的独立资格查询块"
                )
        if self.group_by and self.deduplication.mode != "none":
            raise ValueError(
                "已经按 grain_fields 分组的 query_block 不得再声明 DISTINCT；"
                "将 deduplication.mode 改为 none"
            )
        normalized_grain_fields = {
            re.sub(r"\s+", "", field.replace("`", "").lower())
            for field in self.grain_fields
        }
        multiplying_joins = [
            query_join
            for query_join in self.joins
            if query_join.cardinality in {"one_to_many", "many_to_many"}
            and not {
                re.sub(r"\s+", "", key.replace("`", "").lower())
                for key in query_join.right_key
            }.issubset(normalized_grain_fields)
        ]
        if multiplying_joins and not self.group_by:
            if self.deduplication.mode != "distinct":
                raise ValueError(
                    "一对多或多对多关联会放大当前主体行，且 grain_fields 未包含右侧稳定键；"
                    "必须使用 deduplication.mode=distinct，或把明细右侧键加入 grain_fields"
                )
            multiplying_sources = {
                query_join.right_source for query_join in multiplying_joins
            }
            ambiguous_detail_fields = [
                select_field.field
                for select_field in self.select_fields
                if any(
                    re.search(
                        rf"\b{re.escape(source)}\.",
                        select_field.field,
                        flags=re.IGNORECASE,
                    )
                    for source in multiplying_sources
                )
            ]
            if ambiguous_detail_fields:
                raise ValueError(
                    "DISTINCT 只能消除仅返回左侧主体字段的重复行；"
                    "当前查询同时返回一对多右侧明细字段，无法保证声明的主体粒度："
                    + ", ".join(ambiguous_detail_fields)
                    + "。请把右侧稳定键加入 grain_fields，或先在独立查询块聚合"
                )
        strategy_component_count = sum(
            (
                bool(self.joins),
                any(
                    condition.implementation in {"exists", "not_exists"}
                    for condition in self.quantified_conditions
                ),
                bool(
                    self.group_by
                    or self.aggregations
                    or self.having
                    or numeric_conditions
                ),
            )
        )
        if strategy_component_count >= 2:
            expected_strategy = "mixed"
        elif self.group_by or self.aggregations or self.having or numeric_conditions:
            expected_strategy = "aggregate"
        elif any(
            condition.implementation in {"exists", "not_exists"}
            for condition in self.quantified_conditions
        ):
            expected_strategy = "subquery"
        else:
            expected_strategy = "join"
        if self.strategy != expected_strategy:
            raise ValueError(
                f"query_block.strategy 必须由正式计划组件派生为 "
                f"{expected_strategy}，当前为 {self.strategy}；"
                "不得用 strategy 表达未声明的 SQL 实现"
            )
        return self


class NaturalLanguageQueryPlan(BaseModel):
    """由拓扑有序查询块组成的完整只读数据获取计划。"""

    model_config = ConfigDict(extra="forbid")

    query_goal: str = Field(description="本次查询要回答的业务问题")
    tables: list[QueryPlanTable] = Field(
        min_length=1,
        description="整个查询图允许读取的真实数据表及职责",
    )
    root_block_id: str = Field(description="最终输出 SQL 结果的根查询块 block_id")
    query_blocks: list[QueryPlanBlock] = Field(
        min_length=1,
        description="按依赖拓扑顺序排列的查询块；根查询块必须位于最后",
    )
    implemented_business_rules: list[QueryPlanBusinessRuleImplementation] = Field(
        default_factory=list,
        description="本次计划落实的 core rule 及其带查询块作用域的组件引用",
    )
    pagination: QueryPlanPagination = Field(description="仅应用于根查询块的结果范围")
    business_caliber: list[QueryPlanBusinessCaliber] = Field(
        description=(
            "有效状态、去重、时间范围和空值等业务口径及其正式计划引用；"
            "无时传空列表"
        )
    )
    assumptions: list[str] = Field(
        description=(
            "必须始终传空列表；影响结果的不确定事实必须先询问用户，"
            "确认后写入正式计划组件和 business_caliber"
        )
    )

    # 校验查询块拓扑、字段作用域和业务规则引用，使计划本身不存在跨粒度歧义。
    @model_validator(mode="after")
    def validate_query_block_graph(self) -> "NaturalLanguageQueryPlan":
        table_names = [table.table_name for table in self.tables]
        if len(table_names) != len(set(table_names)):
            raise ValueError("tables[].table_name 不能重复")
        declared_table_names = set(table_names)
        block_ids = [block.block_id for block in self.query_blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("query_blocks[].block_id 不能重复")
        colliding_block_ids = sorted(set(block_ids) & declared_table_names)
        if colliding_block_ids:
            raise ValueError(
                "query_blocks[].block_id 不能与真实表名相同："
                + ", ".join(colliding_block_ids)
            )
        if self.root_block_id not in block_ids:
            raise ValueError("root_block_id 必须引用已声明的 query_block")
        if self.query_blocks[-1].block_id != self.root_block_id:
            raise ValueError("根 query_block 必须位于 query_blocks 最后")
        if self.query_blocks[-1].role != "result":
            raise ValueError("根 query_block 的 role 必须为 result")

        blocks_by_id: dict[str, QueryPlanBlock] = {}
        for block in self.query_blocks:
            self._validate_block_declarations(
                block=block,
                declared_table_names=declared_table_names,
                declared_block_ids=set(block_ids),
                previous_blocks=blocks_by_id,
            )
            blocks_by_id[block.block_id] = block

        reachable_blocks: set[str] = set()

        # 从根块反向遍历依赖，拒绝不会参与最终结果的悬空查询块。
        def collect_dependencies(block_id: str) -> None:
            if block_id in reachable_blocks:
                return
            reachable_blocks.add(block_id)
            for input_block in blocks_by_id[block_id].input_blocks:
                collect_dependencies(input_block)

        collect_dependencies(self.root_block_id)
        unreachable_blocks = sorted(set(block_ids) - reachable_blocks)
        if unreachable_blocks:
            raise ValueError(
                "存在未被根查询块引用的 query_blocks：" + ", ".join(unreachable_blocks)
            )
        if self.assumptions:
            raise ValueError(
                "query_plan.assumptions 必须为空；影响结果的事实不能作为隐藏假设，"
                "请先调用 ask_user 确认，再把结论写入正式计划组件和 business_caliber"
            )
        self._validate_business_rule_references(blocks_by_id)
        return self

    # 校验单个查询块的表、依赖、别名和字段限定符都位于显式作用域内。
    @staticmethod
    def _validate_block_declarations(
        *,
        block: QueryPlanBlock,
        declared_table_names: set[str],
        declared_block_ids: set[str],
        previous_blocks: dict[str, QueryPlanBlock],
    ) -> None:
        if len(block.source_tables) != len(set(block.source_tables)):
            raise ValueError(f"查询块 {block.block_id} 的 source_tables 不能重复")
        unknown_tables = sorted(set(block.source_tables) - declared_table_names)
        if unknown_tables:
            raise ValueError(
                f"查询块 {block.block_id} 引用了 tables 未声明的表："
                + ", ".join(unknown_tables)
            )
        if len(block.input_blocks) != len(set(block.input_blocks)):
            raise ValueError(f"查询块 {block.block_id} 的 input_blocks 不能重复")
        unknown_inputs = [
            input_block
            for input_block in block.input_blocks
            if input_block not in previous_blocks
        ]
        if unknown_inputs:
            raise ValueError(
                f"查询块 {block.block_id} 只能引用排在它前面的 input_blocks："
                + ", ".join(unknown_inputs)
            )
        alias_names = [alias.alias for alias in block.aliases]
        if len(alias_names) != len(set(alias_names)):
            raise ValueError(f"查询块 {block.block_id} 的 aliases[].alias 不能重复")
        shadowed_names = sorted(
            set(alias_names) & (declared_table_names | declared_block_ids)
        )
        if shadowed_names:
            raise ValueError(
                f"查询块 {block.block_id} 的 alias 不能与真实表或查询块同名："
                + ", ".join(shadowed_names)
            )
        invalid_alias_sources = sorted(
            {
                alias.source_table
                for alias in block.aliases
                if alias.source_table not in block.source_tables
            }
        )
        if invalid_alias_sources:
            raise ValueError(
                f"查询块 {block.block_id} 的 alias 必须直接来源于 source_tables："
                + ", ".join(invalid_alias_sources)
            )
        available_outer_sources = (
            set(block.source_tables) | set(block.input_blocks) | set(alias_names)
        ) - {alias.source_table for alias in block.aliases}
        outer_relation_sources = NaturalLanguageQueryPlan._validate_relation_chain(
            block_id=block.block_id,
            component_path="joins",
            base_source=block.base_source,
            joins=block.joins,
            available_sources=available_outer_sources,
        )
        for condition_index, condition in enumerate(block.quantified_conditions):
            if condition.implementation == "having":
                continue
            assert condition.collection_base_source is not None
            collection_sources = NaturalLanguageQueryPlan._validate_relation_chain(
                block_id=block.block_id,
                component_path=(
                    f"quantified_conditions[{condition_index}].collection_joins"
                ),
                base_source=condition.collection_base_source,
                joins=condition.collection_joins,
                available_sources=available_outer_sources,
            )
            overlapping_sources = sorted(outer_relation_sources & collection_sources)
            if overlapping_sources:
                raise ValueError(
                    f"查询块 {block.block_id} 的 quantified_conditions"
                    f"[{condition_index}] 内外层数据源名称不能相同："
                    + ", ".join(overlapping_sources)
                    + "；同一真实表承担不同角色时必须声明不同 aliases"
                )
            member_expressions = [condition.predicate] + condition.collection_filters
            member_qualifiers = {
                qualifier
                for expression in member_expressions
                for qualifier, _ in re.findall(
                    r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b",
                    expression,
                )
            }
            invalid_member_qualifiers = sorted(member_qualifiers - collection_sources)
            if invalid_member_qualifiers:
                raise ValueError(
                    f"查询块 {block.block_id} 的 quantified_conditions"
                    f"[{condition_index}] 成员条件只能引用 collection_base_source "
                    "和 collection_joins："
                    + ", ".join(invalid_member_qualifiers)
                )
            correlation_qualifiers = {
                qualifier
                for qualifier, _ in re.findall(
                    r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b",
                    condition.correlation_condition or "",
                )
            }
            if not correlation_qualifiers.intersection(collection_sources) or not (
                correlation_qualifiers.intersection(outer_relation_sources)
            ):
                raise ValueError(
                    f"查询块 {block.block_id} 的 quantified_conditions"
                    f"[{condition_index}].correlation_condition 必须同时引用"
                    "集合子查询数据源和外层主体数据源"
                )
        outer_component_expressions = (
            block.grain_fields
            + [query_filter.condition for query_filter in block.filters]
            + [
                key
                for condition in block.quantified_conditions
                for key in condition.subject_key
            ]
            + [
                expression
                for condition in block.quantified_conditions
                if condition.implementation == "having"
                for expression in (
                    condition.member_key
                    + condition.collection_filters
                    + [condition.predicate]
                )
            ]
            + [select_field.field for select_field in block.select_fields]
            + block.group_by
            + [aggregation.expression for aggregation in block.aggregations]
            + [having.condition for having in block.having]
            + [order_by.field for order_by in block.order_by]
        )
        outer_component_qualifiers = {
            qualifier
            for expression in outer_component_expressions
            for qualifier, _ in re.findall(
                r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b",
                expression,
            )
        }
        invalid_outer_qualifiers = sorted(
            outer_component_qualifiers - outer_relation_sources
        )
        if invalid_outer_qualifiers:
            raise ValueError(
                f"查询块 {block.block_id} 的粒度、普通筛选、输出、聚合或排序只能引用"
                "外层 base_source 和 joins："
                + ", ".join(invalid_outer_qualifiers)
                + "；需要额外关系时请声明 JOIN 或拆分 query_block"
            )
        NaturalLanguageQueryPlan._validate_block_expressions(
            block=block,
            previous_blocks=previous_blocks,
            alias_names=set(alias_names),
        )

    # 校验一条外层或集合子查询关系链的起点、顺序、ON 作用域和右侧稳定键。
    @staticmethod
    def _validate_relation_chain(
        *,
        block_id: str,
        component_path: str,
        base_source: str,
        joins: list[QueryPlanJoin],
        available_sources: set[str],
    ) -> set[str]:
        if base_source not in available_sources:
            raise ValueError(
                f"查询块 {block_id} 的 {component_path} 起始数据源不属于"
                f"可直接引用的数据源：{base_source}"
            )
        joined_sources: set[str] = {base_source}
        for join_index, query_join in enumerate(joins):
            if query_join.right_source not in available_sources:
                raise ValueError(
                    f"查询块 {block_id} 的 {component_path}[{join_index}].right_source "
                    f"不属于可直接引用的数据源：{query_join.right_source}"
                )
            if query_join.right_source in joined_sources:
                raise ValueError(
                    f"查询块 {block_id} 的 {component_path} 重复关联数据源："
                    f"{query_join.right_source}"
                )
            condition_qualifiers = {
                qualifier
                for qualifier, _ in re.findall(
                    r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b",
                    query_join.condition,
                )
            }
            if query_join.right_source not in condition_qualifiers:
                raise ValueError(
                    f"查询块 {block_id} 的 {component_path}[{join_index}].condition "
                    f"必须引用 right_source {query_join.right_source}"
                )
            if not condition_qualifiers.intersection(joined_sources):
                raise ValueError(
                    f"查询块 {block_id} 的 {component_path}[{join_index}].condition "
                    "必须关联到已经进入左侧结果集的数据源"
                )
            unavailable_qualifiers = sorted(
                condition_qualifiers - joined_sources - {query_join.right_source}
            )
            if unavailable_qualifiers:
                raise ValueError(
                    f"查询块 {block_id} 的 {component_path}[{join_index}].condition "
                    "引用了尚未进入关联链的数据源："
                    + ", ".join(unavailable_qualifiers)
                )
            expected_key_pattern = re.compile(
                rf"{re.escape(query_join.right_source)}\."
                r"[A-Za-z_][A-Za-z0-9_]*",
                flags=re.IGNORECASE,
            )
            if any(
                expected_key_pattern.fullmatch(key) is None
                for key in query_join.right_key
            ):
                raise ValueError(
                    f"查询块 {block_id} 的 {component_path}[{join_index}].right_key "
                    f"必须全部使用 {query_join.right_source}.原始字段名"
                )
            if len(query_join.right_key) != len(set(query_join.right_key)):
                raise ValueError(
                    f"查询块 {block_id} 的 {component_path}[{join_index}].right_key "
                    "不能重复"
                )
            joined_sources.add(query_join.right_source)
        return joined_sources

    # 校验表达式限定符，并保证输入块只暴露其显式选择的稳定输出列。
    @staticmethod
    def _validate_block_expressions(
        *,
        block: QueryPlanBlock,
        previous_blocks: dict[str, QueryPlanBlock],
        alias_names: set[str],
    ) -> None:
        expressions = (
            block.grain_fields
            + [join.condition for join in block.joins]
            + [key for join in block.joins for key in join.right_key]
            + [query_filter.condition for query_filter in block.filters]
            + [condition.predicate for condition in block.quantified_conditions]
            + [
                condition.correlation_condition
                for condition in block.quantified_conditions
                if condition.correlation_condition is not None
            ]
            + [
                collection_filter
                for condition in block.quantified_conditions
                for collection_filter in condition.collection_filters
            ]
            + [key for condition in block.quantified_conditions for key in condition.subject_key]
            + [key for condition in block.quantified_conditions for key in condition.member_key]
            + [select_field.field for select_field in block.select_fields]
            + block.group_by
            + [aggregation.expression for aggregation in block.aggregations]
            + [having.condition for having in block.having]
            + [order_by.field for order_by in block.order_by]
        )
        references_by_qualifier: dict[str, set[str]] = {}
        for expression in expressions:
            for qualifier, field_name in re.findall(
                r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b",
                expression,
            ):
                references_by_qualifier.setdefault(qualifier, set()).add(field_name)
        allowed_qualifiers = (
            set(block.source_tables) | alias_names | set(block.input_blocks)
        )
        invalid_qualifiers = sorted(set(references_by_qualifier) - allowed_qualifiers)
        if invalid_qualifiers:
            raise ValueError(
                f"查询块 {block.block_id} 引用了不属于该作用域的限定符："
                + ", ".join(invalid_qualifiers)
            )
        aliased_source_tables = {
            alias.source_table for alias in block.aliases
        }
        raw_aliased_references = sorted(
            set(references_by_qualifier) & aliased_source_tables
        )
        if raw_aliased_references:
            raise ValueError(
                f"查询块 {block.block_id} 已为真实表声明别名，表达式不得再用真实表名限定："
                + ", ".join(raw_aliased_references)
                + "；请始终使用 aliases 中对应的角色别名"
            )
        for input_block_id in block.input_blocks:
            available_fields = {
                field.result_field for field in previous_blocks[input_block_id].select_fields
            }
            invalid_fields = sorted(
                references_by_qualifier.get(input_block_id, set()) - available_fields
            )
            if invalid_fields:
                raise ValueError(
                    f"查询块 {block.block_id} 引用了输入块 {input_block_id} 未输出的字段："
                    + ", ".join(invalid_fields)
                )

    # 校验业务规则引用必须精确定位到查询块及其中一个现存计划组件。
    def _validate_business_rule_references(
        self,
        blocks_by_id: dict[str, QueryPlanBlock],
    ) -> None:
        rule_ids = [item.rule_id for item in self.implemented_business_rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("implemented_business_rules[].rule_id 不能重复")
        for implementation in self.implemented_business_rules:
            self._validate_plan_references(
                implementation.plan_references,
                blocks_by_id,
                "implemented_business_rules[].plan_references",
            )
        caliber_descriptions = [item.description for item in self.business_caliber]
        if len(caliber_descriptions) != len(set(caliber_descriptions)):
            raise ValueError("business_caliber[].description 不能重复")
        for caliber_index, caliber in enumerate(self.business_caliber):
            self._validate_plan_references(
                caliber.plan_references,
                blocks_by_id,
                f"business_caliber[{caliber_index}].plan_references",
            )

    # 统一验证自然语言规则和业务口径只能引用已存在的正式列表项或块级关系语义。
    @staticmethod
    def _validate_plan_references(
        references: list[str],
        blocks_by_id: dict[str, QueryPlanBlock],
        field_path: str,
    ) -> None:
        if len(references) != len(set(references)):
            raise ValueError(f"{field_path} 不能重复")
        for reference in references:
            indexed_reference = re.fullmatch(
                r"query_blocks\[([A-Za-z_][A-Za-z0-9_]*)\]\."
                r"(joins|filters|quantified_conditions|select_fields|"
                r"group_by|aggregations|having|order_by)\[(\d+)\]",
                reference,
            )
            scalar_reference = re.fullmatch(
                r"query_blocks\[([A-Za-z_][A-Za-z0-9_]*)\]\."
                r"(base_source|deduplication)",
                reference,
            )
            if indexed_reference is None and scalar_reference is None:
                raise ValueError(
                    "plan_references 必须带查询块作用域，例如 "
                    "query_blocks[qualified_users].filters[0] 或 "
                    "query_blocks[result_rows].deduplication"
                )
            if indexed_reference is not None:
                block_id, component_name, raw_index = indexed_reference.groups()
            else:
                assert scalar_reference is not None
                block_id, _ = scalar_reference.groups()
                component_name = ""
                raw_index = "0"
            if block_id not in blocks_by_id:
                raise ValueError(f"计划组件引用了不存在的查询块：{reference}")
            if indexed_reference is None:
                continue
            component_size = len(getattr(blocks_by_id[block_id], component_name))
            if int(raw_index) >= component_size:
                raise ValueError(
                    f"计划组件引用越界：{reference}，"
                    f"{component_name} 只有 {component_size} 项"
                )

    # 返回最终结果查询块，供展示、审计和现有领域策略读取根作用域。
    @property
    def root_block(self) -> QueryPlanBlock:
        return next(
            block for block in self.query_blocks if block.block_id == self.root_block_id
        )

    # 兼容读取最终 SQL 行粒度，实际来源始终是根查询块。
    @property
    def row_granularity(self) -> str:
        return self.root_block.row_granularity

    # 兼容读取根查询块别名。
    @property
    def aliases(self) -> list[QueryPlanAlias]:
        return self.root_block.aliases

    # 兼容读取根查询块关联。
    @property
    def joins(self) -> list[QueryPlanJoin]:
        return self.root_block.joins

    # 兼容读取根查询块筛选。
    @property
    def filters(self) -> list[QueryPlanFilter]:
        return self.root_block.filters

    # 兼容读取根查询块量词条件。
    @property
    def quantified_conditions(self) -> list[QueryPlanQuantifiedCondition]:
        return self.root_block.quantified_conditions

    # 兼容读取根查询块输出字段。
    @property
    def select_fields(self) -> list[QueryPlanSelectField]:
        return self.root_block.select_fields

    # 兼容读取根查询块分组键。
    @property
    def group_by(self) -> list[str]:
        return self.root_block.group_by

    # 兼容读取根查询块聚合。
    @property
    def aggregations(self) -> list[QueryPlanAggregation]:
        return self.root_block.aggregations

    # 兼容读取根查询块 HAVING 条件。
    @property
    def having(self) -> list[QueryPlanFilter]:
        return self.root_block.having

    # 兼容读取根查询块排序。
    @property
    def order_by(self) -> list[QueryPlanOrderBy]:
        return self.root_block.order_by

    # 兼容读取根查询块策略名称。
    @property
    def query_strategy(self) -> str:
        return self.root_block.strategy

    # 兼容读取根查询块策略原因。
    @property
    def strategy_reason(self) -> str:
        return self.root_block.strategy_reason

    # 汇总所有查询块的量词条件，供跨作用域业务规则校验使用。
    def iter_quantified_conditions(
        self,
    ) -> list[tuple[QueryPlanBlock, int, QueryPlanQuantifiedCondition]]:
        return [
            (block, index, condition)
            for block in self.query_blocks
            for index, condition in enumerate(block.quantified_conditions)
        ]

    # 汇总所有查询块的筛选条件，供领域规则校验完整查询口径。
    def iter_filters(self) -> list[tuple[QueryPlanBlock, int, QueryPlanFilter]]:
        return [
            (block, index, query_filter)
            for block in self.query_blocks
            for index, query_filter in enumerate(block.filters)
        ]


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
        "entity_not_found",
        "insufficient_information",
        "unsupported_scope",
        "user_cancelled",
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
    return build_pydantic_tool_definition(
        tool_name=NATURAL_LANGUAGE_QUERY_TOOL_NAME,
        description=(
            "同时提交结构化的只读 SQL 数据获取计划和确定性结果塑形计划。"
            "query_plan 必须由拓扑有序 query_blocks 组成，并由 root_block_id 指向根块；"
            "资格判断与最终明细粒度不同时必须拆成独立查询块。"
            "每块必须明确 FROM 起点、有序 JOIN 右侧/类型/基数/右键、完整 ON "
            "与 DISTINCT 口径；所有关联、筛选、返回字段、分组和排序"
            "均须保留原始标识符；"
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
    return build_pydantic_tool_definition(
        tool_name=ABANDON_QUERY_PLANNING_TOOL_NAME,
        description=(
            "当实体检索已确认用户指定的业务实体不存在，"
            f"关键事实在向用户询问后仍不足，或请求超出{query_scope}时，"
            "提交放弃原因并结束规划。不得把最终 SQL 返回空行的正常查询结果当作放弃。"
        ),
        arguments_model=AbandonQueryPlanningArguments,
    )


# 严格校验失败后递归还原兼容接口偶发的嵌套 JSON 字符串，不放宽任何查询计划约束。
def parse_natural_language_query_tool_arguments(
    arguments_json: str,
) -> NaturalLanguageQueryToolArguments:
    return validate_tool_arguments_with_embedded_json_fallback(
        arguments_json,
        NaturalLanguageQueryToolArguments,
    )


# 将放弃工具参数 JSON 按 Pydantic 校验为可供 Pipeline 正常停止的规划结果。
def parse_abandon_query_planning_arguments(
    arguments_json: str,
) -> QueryPlanningAbandonment:
    arguments = validate_tool_arguments_with_embedded_json_fallback(
        arguments_json,
        AbandonQueryPlanningArguments,
    )
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
    block_sections: list[str] = []
    for block in query_plan.query_blocks:
        block_lines = [
            f"### `{block.block_id}`（{block.role}）",
            f"- 行粒度：{block.row_granularity}",
            "- 粒度键：" + ("、".join(f"`{item}`" for item in block.grain_fields) or "全局单行"),
            "- 真实表：" + ("、".join(f"`{item}`" for item in block.source_tables) or "无"),
            "- 前置块：" + ("、".join(f"`{item}`" for item in block.input_blocks) or "无"),
            f"- 起始数据源：`{block.base_source}`",
            "- 关联："
            + (
                "；".join(
                    f"`{item.join_type}` `{item.right_source}` "
                    f"({item.cardinality}) ON `{item.condition}`"
                    for item in block.joins
                )
                or "无"
            ),
            f"- 去重：`{block.deduplication.mode}`（{block.deduplication.reason}）",
            "- 筛选："
            + ("；".join(f"`{item.condition}`" for item in block.filters) or "无"),
            "- 量词："
            + (
                "；".join(
                    f"`{item.quantifier}` {item.collection}（主体键 "
                    + "、".join(f"`{key}`" for key in item.subject_key)
                    + f"；成员条件 `{item.predicate}`；实现 `{item.implementation}`"
                    + (
                        f"；集合 FROM `{item.collection_base_source}`"
                        + (
                            "，JOIN "
                            + "、".join(
                                f"`{query_join.right_source}`"
                                for query_join in item.collection_joins
                            )
                            if item.collection_joins
                            else ""
                        )
                        if item.collection_base_source is not None
                        else ""
                    )
                    + "）"
                    for item in block.quantified_conditions
                )
                or "无"
            ),
            "- 输出："
            + "；".join(
                f"`{item.field}` AS `{item.result_field}`（{item.purpose}）"
                for item in block.select_fields
            ),
            "- 分组：" + ("、".join(f"`{item}`" for item in block.group_by) or "无"),
            "- 聚合后筛选："
            + ("；".join(f"`{item.condition}`" for item in block.having) or "无"),
            f"- 策略：`{block.strategy}`（{block.strategy_reason}）",
        ]
        block_sections.append("\n".join(block_lines))
    implemented_rule_lines = [
        (
            f"- `{implementation.rule_id}`："
            + "、".join(implementation.plan_references)
        )
        for implementation in query_plan.implemented_business_rules
    ] or ["- 无核心规则实现。"]
    order_by_lines = [
        f"- `{order_by.field} {order_by.direction}`" for order_by in query_plan.order_by
    ] or ["- 无排序。"]
    pagination = (
        f"规划最多返回 `{query_plan.pagination.limit}` 行，偏移量 `{query_plan.pagination.offset}`。"
        if query_plan.pagination.limit is not None
        else "规划未设置行数上限；SQL 将完整执行当前筛选条件，偏移量为 0。"
    )
    business_caliber_lines = [
        f"- {business_caliber.description}："
        + "、".join(business_caliber.plan_references)
        for business_caliber in query_plan.business_caliber
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
            "## 涉及表\n\n" + "\n".join(table_lines),
            "## 查询块与作用域\n\n" + "\n\n".join(block_sections),
            "## 核心规则实现\n\n" + "\n".join(implemented_rule_lines),
            "## 排序与分页\n\n"
            + "### 排序\n\n"
            + "\n".join(order_by_lines)
            + "\n\n### 分页\n\n"
            + pagination,
            "## 业务口径\n\n" + "\n".join(business_caliber_lines),
            "## 假设与待确认项\n\n" + "\n".join(assumption_lines),
            "# 结果塑形计划\n\n" + "\n".join(shape_lines),
        )
    )
