"""定义积分与奖品 Text-to-SQL 业务域的查询计划和业务对齐约束。"""

import re
from typing import Final

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from app.agent.text2sql.domains.base import (
    AlignmentLogicalConstraintView,
    AlignmentPolicyIssue,
    QueryPlanPolicyIssue,
)
from app.agent.text2sql.subgraphs.planning.tools.query_plan import (
    NaturalLanguageQueryPlan,
    QueryPlanBlock,
    QueryPlanQuantifiedCondition,
)


BALANCE_TERMS: Final[tuple[str, ...]] = (
    "积分余额",
    "当前积分",
    "剩余积分",
    "可用积分",
    "还有多少积分",
)
POINT_HISTORY_TERMS: Final[tuple[str, ...]] = (
    "积分流水",
    "积分明细",
    "积分记录",
    "积分变动",
)
EXCHANGE_TERMS: Final[tuple[str, ...]] = (
    "兑换记录",
    "兑换流水",
    "兑换了什么",
    "商品兑换",
    "商品履约",
    "商品兑换履约记录",
    "待处理商品兑换",
    "待发放奖品",
    "已发放奖品",
    "奖品发放",
)
SEASON_POINTS_TERMS: Final[tuple[str, ...]] = (
    "赛季积分",
    "赛季结算积分",
    "最终积分",
    "赛季奖励",
)
PRODUCT_TERMS: Final[tuple[str, ...]] = (
    "商品目录",
    "积分商城商品",
    "当前可兑换商品",
    "上架商品",
    "下架商品",
)
PENDING_GIFT_TERMS: Final[tuple[str, ...]] = (
    "待发放奖品",
    "待处理商品兑换",
)
POINT_DETAIL_TERMS: Final[tuple[str, ...]] = POINT_HISTORY_TERMS + (
    "积分变动明细",
    "兑换记录",
    "兑换流水",
    "商品兑换履约记录",
    "待处理商品兑换",
    "待发放奖品",
    "已发放奖品",
)
BALANCE_SPECIFIC_TERMS: Final[tuple[str, ...]] = (
    *BALANCE_TERMS,
    *POINT_HISTORY_TERMS,
    *EXCHANGE_TERMS,
    *SEASON_POINTS_TERMS,
    *PRODUCT_TERMS,
    "兑换",
    "发放",
    "奖励",
)
GIFT_SPECIFIC_TERMS: Final[tuple[str, ...]] = (
    *EXCHANGE_TERMS,
    *PRODUCT_TERMS,
    "奖品目录",
    "奖品兑换",
    "奖品履约",
)


# 判断用户是否只说了“积分”而没有指定余额、流水、赛季结算或兑换语义。
def _has_ambiguous_point_scope(question: str) -> bool:
    return "积分" in question and not any(
        term in question for term in BALANCE_SPECIFIC_TERMS
    )


# 判断用户是否只说了“奖品”而没有区分商品目录、兑换记录或履约状态。
def _has_ambiguous_gift_scope(question: str) -> bool:
    return "奖品" in question and not any(
        term in question for term in GIFT_SPECIFIC_TERMS
    )


# 判断查询是否是商品目录查询，避免把商品目录和用户兑换履约混为一谈。
def _requests_product_catalog(planning_input: str) -> bool:
    return any(term in planning_input for term in PRODUCT_TERMS)


# 将积分域计划表达式中的合法别名还原为真实表名，同时保留条件解析所需的空格。
def _replace_rewards_plan_aliases(
    expression: str,
    query_plan: NaturalLanguageQueryPlan,
) -> str:
    normalized = expression.replace("`", "").lower()
    for block in query_plan.query_blocks:
        for alias in block.aliases:
            normalized = re.sub(
                rf"\b{re.escape(alias.alias.lower())}\.",
                f"{alias.source_table.lower()}.",
                normalized,
            )
    return normalized


# 还原积分域表达式来源并移除排版差异，供单字段来源做精确比较。
def _normalize_rewards_plan_expression(
    expression: str,
    query_plan: NaturalLanguageQueryPlan,
) -> str:
    return re.sub(
        r"\s+",
        "",
        _replace_rewards_plan_aliases(expression, query_plan),
    )


# 沿查询块输出字段反向追溯真实来源，使领域校验兼容别名和前置查询块。
def _resolve_rewards_field_origin(
    expression: str,
    query_plan: NaturalLanguageQueryPlan,
    visited_outputs: frozenset[tuple[str, str]] = frozenset(),
) -> str | None:
    normalized_expression = _normalize_rewards_plan_expression(expression, query_plan)
    matched_field = re.fullmatch(
        r"([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)",
        normalized_expression,
    )
    if matched_field is None:
        return None
    qualifier, field_name = matched_field.groups()
    blocks_by_id = {
        block.block_id.lower(): block for block in query_plan.query_blocks
    }
    source_block = blocks_by_id.get(qualifier)
    if source_block is None:
        return normalized_expression
    output_key = (qualifier, field_name)
    if output_key in visited_outputs:
        return None
    selected_field = next(
        (
            item
            for item in source_block.select_fields
            if item.result_field.lower() == field_name
        ),
        None,
    )
    if selected_field is None:
        return None
    return _resolve_rewards_field_origin(
        selected_field.field,
        query_plan,
        visited_outputs | {output_key},
    )


# 判断根结果是否返回指定真实字段，不受查询块或表别名影响。
def _selects_origin_field(
    query_plan: NaturalLanguageQueryPlan,
    table_name: str,
    field_name: str,
) -> bool:
    expected_origin = f"{table_name.lower()}.{field_name.lower()}"
    return any(
        _resolve_rewards_field_origin(select_field.field, query_plan)
        == expected_origin
        for select_field in query_plan.select_fields
    )


# 判断计划是否已经声明一条具体的表记录身份，避免明细查询退化为只有业务名称或金额。
def _has_subject_identifier(
    query_plan: NaturalLanguageQueryPlan,
    subject_table: str,
) -> bool:
    return _selects_origin_field(query_plan, subject_table, "id")


# 判断查询计划是否包含聚合；汇总结果不强制返回单条明细记录的主键。
def _has_aggregation(query_plan: NaturalLanguageQueryPlan) -> bool:
    return any(block.aggregations or block.group_by for block in query_plan.query_blocks)


# 移除只影响排版的括号节点，便于按真实布尔结构检查条件。
def _unwrap_parentheses(expression: exp.Expression) -> exp.Expression:
    while isinstance(expression, exp.Paren):
        expression = expression.this
    return expression


# 将条件拆成最外层 AND 原子；OR 内部条件不能冒充必然成立的业务筛选。
def _flatten_conjunctive_atoms(expression: exp.Expression) -> list[exp.Expression]:
    expression = _unwrap_parentheses(expression)
    if isinstance(expression, exp.And):
        return _flatten_conjunctive_atoms(expression.this) + _flatten_conjunctive_atoms(
            expression.expression
        )
    return [expression]


# 解析计划条件；无效表达式交由通用计划或 SQL 校验处理，领域校验只接受可证明的条件。
def _parse_condition_atoms(condition: str) -> list[exp.Expression]:
    try:
        parsed = parse_one(condition, read="mysql")
    except ParseError:
        return []
    return _flatten_conjunctive_atoms(parsed)


# 规范化条件原子，便于对字段、运算符和值进行精确匹配。
def _normalize_condition_atom(expression: exp.Expression) -> str:
    return re.sub(
        r"\s+",
        "",
        expression.sql(dialect="mysql").replace("`", "").lower(),
    )


# 返回查询块外层实际可用的积分流水角色，避免把不同查询块的条件拼成一个伪口径。
def _point_record_outer_qualifiers(block: QueryPlanBlock) -> tuple[str, ...]:
    alias_qualifiers = tuple(
        alias.alias.lower()
        for alias in block.aliases
        if alias.source_table.lower() == "point_record"
    )
    if alias_qualifiers:
        return alias_qualifiers
    if "point_record" in {table_name.lower() for table_name in block.source_tables}:
        return ("point_record",)
    return ()


# 汇总一个查询块中必然成立的外层筛选原子，保留角色别名以校验条件作用于同一行。
def _block_filter_atoms(block: QueryPlanBlock) -> tuple[str, ...]:
    return tuple(
        _normalize_condition_atom(atom)
        for query_filter in block.filters
        for atom in _parse_condition_atoms(query_filter.condition)
    )


# 判断任一积分流水角色是否在同一查询块中被明确限定为商品兑换。
def _has_exchange_role_filter(query_plan: NaturalLanguageQueryPlan) -> bool:
    for block in query_plan.query_blocks:
        filter_atoms = set(_block_filter_atoms(block))
        if any(
            f"{qualifier}.change_type='exchange'" in filter_atoms
            for qualifier in _point_record_outer_qualifiers(block)
        ):
            return True
    return False


# 找出同一积分流水角色缺失的待发放四项条件，禁止跨块或跨别名拼接筛选。
def _find_pending_gift_filter_gaps(
    query_plan: NaturalLanguageQueryPlan,
) -> tuple[str, ...]:
    best_missing: tuple[str, ...] | None = None
    for block in query_plan.query_blocks:
        filter_atoms = set(_block_filter_atoms(block))
        for qualifier in _point_record_outer_qualifiers(block):
            required_filters = (
                (f"{qualifier}.change_type='exchange'", "change_type = 'exchange'"),
                (f"not{qualifier}.product_idisnull", "product_id IS NOT NULL"),
                (f"{qualifier}.status=1", "status = 1"),
                (
                    f"{qualifier}.gift_distribution_status='pending'",
                    "gift_distribution_status = 'pending'",
                ),
            )
            missing = tuple(
                display_condition
                for normalized_condition, display_condition in required_filters
                if normalized_condition not in filter_atoms
            )
            if not missing:
                return ()
            if best_missing is None or len(missing) < len(best_missing):
                best_missing = missing
    return best_missing or (
        "change_type = 'exchange'",
        "product_id IS NOT NULL",
        "status = 1",
        "gift_distribution_status = 'pending'",
    )


# 根据用户问题确定逐条结果真正的业务主体，避免任意关联表 ID 冒充主体标识。
def _requested_detail_subject_table(planning_input: str) -> str | None:
    if any(term in planning_input for term in POINT_DETAIL_TERMS):
        return "point_record"
    if any(term in planning_input for term in SEASON_POINTS_TERMS):
        return "season_user"
    if _requests_product_catalog(planning_input):
        return "product"
    return None


# 取得查询块内限定符的真实来源表；别名不存在时限定符本身就是来源名。
def _source_table_for_qualifier(block: QueryPlanBlock, qualifier: str) -> str:
    for alias in block.aliases:
        if alias.alias.lower() == qualifier.lower():
            return alias.source_table.lower()
    return qualifier.lower()


# 判断表达式是否是两个指定字段的等值比较，左右顺序不影响结果。
def _is_field_equality(
    expression: exp.Expression,
    first_qualifier: str,
    first_field: str,
    second_qualifier: str,
    second_field: str,
) -> bool:
    if not isinstance(expression, exp.EQ):
        return False
    left = expression.this
    right = expression.expression
    if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
        return False
    actual = {
        (left.table.lower(), left.name.lower()),
        (right.table.lower(), right.name.lower()),
    }
    expected = {
        (first_qualifier.lower(), first_field.lower()),
        (second_qualifier.lower(), second_field.lower()),
    }
    return actual == expected


# 判断表达式是否表示左侧记录字段严格晚于右侧，兼容等价的反向小于写法。
def _is_field_later_than(
    expression: exp.Expression,
    later_qualifier: str,
    later_field: str,
    earlier_qualifier: str,
    earlier_field: str,
) -> bool:
    left = expression.this
    right = expression.expression
    if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
        return False
    left_key = (left.table.lower(), left.name.lower())
    right_key = (right.table.lower(), right.name.lower())
    later_key = (later_qualifier.lower(), later_field.lower())
    earlier_key = (earlier_qualifier.lower(), earlier_field.lower())
    return (
        isinstance(expression, exp.GT)
        and left_key == later_key
        and right_key == earlier_key
    ) or (
        isinstance(expression, exp.LT)
        and left_key == earlier_key
        and right_key == later_key
    )


# 校验“更晚流水”同时按创建时间和同时间主键确定稳定先后关系。
def _is_stable_later_record_predicate(
    predicate: str,
    later_qualifier: str,
    current_qualifier: str,
) -> bool:
    parsed_atoms = _parse_condition_atoms(predicate)
    if len(parsed_atoms) != 1:
        return False
    predicate_expression = _unwrap_parentheses(parsed_atoms[0])
    if not isinstance(predicate_expression, exp.Or):
        return False
    branches = [
        _unwrap_parentheses(predicate_expression.this),
        _unwrap_parentheses(predicate_expression.expression),
    ]
    for timestamp_branch, tied_branch in (branches, branches[::-1]):
        if not _is_field_later_than(
            timestamp_branch,
            later_qualifier,
            "created_at",
            current_qualifier,
            "created_at",
        ):
            continue
        tied_atoms = _flatten_conjunctive_atoms(tied_branch)
        if len(tied_atoms) != 2:
            continue
        has_equal_timestamp = any(
            _is_field_equality(
                atom,
                later_qualifier,
                "created_at",
                current_qualifier,
                "created_at",
            )
            for atom in tied_atoms
        )
        has_later_id = any(
            _is_field_later_than(
                atom,
                later_qualifier,
                "id",
                current_qualifier,
                "id",
            )
            for atom in tied_atoms
        )
        if has_equal_timestamp and has_later_id:
            return True
    return False


# 定位余额计划中“同一用户不存在更晚有效流水”的量词，并返回缺失的契约部分。
def _find_balance_latest_contract_gaps(
    query_plan: NaturalLanguageQueryPlan,
) -> tuple[str, ...]:
    candidate_conditions: list[
        tuple[QueryPlanBlock, QueryPlanQuantifiedCondition]
    ] = []
    for block, _, condition in query_plan.iter_quantified_conditions():
        if condition.quantifier != "none" or condition.collection_base_source is None:
            continue
        collection_source = condition.collection_base_source
        if _source_table_for_qualifier(block, collection_source) != "point_record":
            continue
        candidate_conditions.append((block, condition))
    if not candidate_conditions:
        return ("缺少 none 量词排除同一用户更晚的有效积分流水",)

    best_gaps: tuple[str, ...] | None = None
    for block, condition in candidate_conditions:
        assert condition.collection_base_source is not None
        later_qualifier = condition.collection_base_source
        correlation_atoms = _parse_condition_atoms(
            condition.correlation_condition or ""
        )
        current_qualifier: str | None = None
        for atom in correlation_atoms:
            if not isinstance(atom, exp.EQ):
                continue
            columns = [atom.this, atom.expression]
            if not all(isinstance(column, exp.Column) for column in columns):
                continue
            typed_columns = [column for column in columns if isinstance(column, exp.Column)]
            if {column.name.lower() for column in typed_columns} != {"user_id"}:
                continue
            qualifiers = [column.table.lower() for column in typed_columns]
            if later_qualifier.lower() not in qualifiers:
                continue
            current_qualifier = next(
                qualifier
                for qualifier in qualifiers
                if qualifier != later_qualifier.lower()
            )
            break

        gaps: list[str] = []
        if current_qualifier is None or _source_table_for_qualifier(
            block, current_qualifier
        ) != "point_record":
            gaps.append("correlation_condition 未按 user_id 关联当前流水与候选更晚流水")
        else:
            outer_filter_atoms = [
                _normalize_condition_atom(atom)
                for query_filter in block.filters
                for atom in _parse_condition_atoms(query_filter.condition)
            ]
            if f"{current_qualifier}.status=1" not in outer_filter_atoms:
                gaps.append("当前流水缺少 status = 1 有效条件")
            has_stable_later_correlation = any(
                _is_stable_later_record_predicate(
                    atom.sql(dialect="mysql"),
                    later_qualifier,
                    current_qualifier,
                )
                for atom in correlation_atoms
            )
            if not has_stable_later_correlation:
                gaps.append("更晚流水未按 created_at、同时间再按 id 严格判定")

        member_filter_atoms = [
            _normalize_condition_atom(atom)
            for member_condition in (
                condition.predicate,
                *condition.collection_filters,
            )
            for atom in _parse_condition_atoms(member_condition)
        ]
        if f"{later_qualifier.lower()}.status=1" not in member_filter_atoms:
            gaps.append("候选更晚流水缺少 status = 1 有效条件")
        if condition.collection_joins:
            gaps.append("最新流水判断不应增加无关的集合内部关联")
        if condition.require_non_empty:
            gaps.append("none 量词的 require_non_empty 必须为 false")

        gap_tuple = tuple(gaps)
        if not gap_tuple:
            return ()
        if best_gaps is None or len(gap_tuple) < len(best_gaps):
            best_gaps = gap_tuple
    return best_gaps or ("最新有效流水契约不完整",)


# 校验积分与奖品业务对齐结果是否明确了积分含义，避免模糊需求直接进入字段规划。
def validate_rewards_alignment(
    original_question: str,
    aligned_question: str,
    business_constraints: tuple[str, ...],
    applied_business_rules: tuple[str, ...] = (),
    logical_constraints: tuple[AlignmentLogicalConstraintView, ...] = (),
) -> tuple[AlignmentPolicyIssue, ...]:
    del applied_business_rules, logical_constraints
    aligned_text = "\n".join((aligned_question, *business_constraints))
    issues: list[AlignmentPolicyIssue] = []
    if _has_ambiguous_point_scope(original_question) and not any(
        term in aligned_text for term in BALANCE_SPECIFIC_TERMS
    ):
        issues.append(
            AlignmentPolicyIssue(
                field_path="aligned_request.aligned_question",
                message=(
                    "用户只提到‘积分’，但对齐结果没有明确它是当前余额、积分流水、"
                    "赛季结算积分、兑换消耗还是其他积分信息。"
                ),
                repair_action=(
                    "不要自行选择积分口径；调用 ask_user 询问用户希望查看当前余额、"
                    "积分变动明细、赛季结算积分还是商品兑换相关积分，然后重新提交完整对齐结果。"
                ),
            )
        )
    if _has_ambiguous_gift_scope(original_question) and not any(
        term in aligned_text for term in GIFT_SPECIFIC_TERMS
    ):
        issues.append(
            AlignmentPolicyIssue(
                field_path="aligned_request.aligned_question",
                message=(
                    "用户只提到‘奖品’，但对齐结果没有明确它是商品目录、"
                    "用户兑换记录还是奖品履约状态。"
                ),
                repair_action=(
                    "不要自行选择奖品口径；调用 ask_user 询问用户希望查看可兑换商品、"
                    "逐条兑换记录还是待发放、已发放或拒绝发放的履约记录，"
                    "然后重新提交完整对齐结果。"
                ),
            )
        )
    return tuple(issues)


# 校验当前余额字段及“同一用户不存在更晚有效流水”的稳定最新记录契约。
def _validate_balance_plan(
    planning_input: str,
    query_plan: NaturalLanguageQueryPlan,
    root_select_field_path: str,
) -> list[QueryPlanPolicyIssue]:
    if not any(term in planning_input for term in BALANCE_TERMS):
        return []
    issues: list[QueryPlanPolicyIssue] = []
    if not _selects_origin_field(query_plan, "point_record", "points_after"):
        issues.append(
            QueryPlanPolicyIssue(
                field_path=root_select_field_path,
                message=(
                    "用户查询的是当前积分余额，但根结果没有返回来源可追溯到 "
                    "point_record.points_after 的余额字段。"
                ),
                repair_action=(
                    "保留用户要求的主体和其他返回内容，并在根查询块添加来源为 "
                    "point_record.points_after 的字段；不得将多条历史余额相加。"
                ),
            )
        )
    latest_contract_gaps = _find_balance_latest_contract_gaps(query_plan)
    if latest_contract_gaps:
        issues.append(
            QueryPlanPolicyIssue(
                field_path="query_plan.query_blocks[].quantified_conditions",
                message=(
                    "当前余额计划没有完整确定每名用户的最新有效积分流水："
                    + "；".join(latest_contract_gaps)
                    + "。"
                ),
                repair_action=(
                    "在读取当前流水的查询块中，为 point_record 声明当前流水和候选更晚流水"
                    "两个别名；当前流水用 filters 限定 status = 1；新增 quantifier=none、"
                    "require_non_empty=false 的 quantified_condition，predicate 限定候选 "
                    "status = 1；correlation_condition 先按 user_id 关联两个角色，再限定候选 "
                    "created_at 更晚，或 created_at 相同且候选 id 更大。"
                ),
            )
        )
    return issues


# 校验赛季结算问题返回真实结算字段，避免用无法归属赛季的全局流水代替。
def _validate_season_points_plan(
    planning_input: str,
    query_plan: NaturalLanguageQueryPlan,
    root_select_field_path: str,
) -> list[QueryPlanPolicyIssue]:
    if not any(term in planning_input for term in SEASON_POINTS_TERMS):
        return []
    if _selects_origin_field(
        query_plan, "season_user", "final_points"
    ) or _selects_origin_field(query_plan, "season_user", "points_issued"):
        return []
    return [
        QueryPlanPolicyIssue(
            field_path=root_select_field_path,
            message=(
                "用户查询的是赛季结算积分或发放状态，但返回字段没有包含"
                " season_user.final_points 或 season_user.points_issued。"
            ),
            repair_action=(
                "根据用户要求添加 season_user.final_points 和/或 season_user.points_issued；"
                "不要用全局积分流水猜测具体赛季的发放结果。"
            ),
        )
    ]


# 校验兑换和奖品履约的数据主体、状态适用范围及待发放四项必要条件。
def _validate_exchange_plan(
    planning_input: str,
    query_plan: NaturalLanguageQueryPlan,
    full_plan_text: str,
) -> list[QueryPlanPolicyIssue]:
    issues: list[QueryPlanPolicyIssue] = []
    if any(term in planning_input for term in EXCHANGE_TERMS):
        declared_tables = {table.table_name for table in query_plan.tables}
        if "point_record" not in declared_tables:
            issues.append(
                QueryPlanPolicyIssue(
                    field_path="query_plan.tables",
                    message="用户查询的是商品兑换或奖品履约记录，但计划没有 point_record。",
                    repair_action=(
                        "将 point_record 加入 tables，并以逐条商品兑换积分流水作为查询主体。"
                    ),
                )
            )
        if ("商品" in planning_input or "奖品" in planning_input) and (
            "product" not in declared_tables
        ):
            issues.append(
                QueryPlanPolicyIssue(
                    field_path="query_plan.tables",
                    message="用户需要知道兑换的商品，但计划没有 product。",
                    repair_action=(
                        "将 product 加入 tables，并通过 point_record.product_id 关联商品。"
                    ),
                )
            )

    if "gift_distribution_status" in full_plan_text and not _has_exchange_role_filter(
        query_plan
    ):
        issues.append(
            QueryPlanPolicyIssue(
                field_path="query_plan.query_blocks[].filters",
                message=(
                    "计划使用了奖品履约状态，但没有限定为商品兑换流水；"
                    "该状态对赛季奖励和人工积分调整没有业务意义。"
                ),
                repair_action=(
                    "在负责兑换主体的查询块 filters 中新增独立且必然成立的 "
                    "point_record.change_type = 'exchange' 条件；"
                    "不要把该条件放入 OR 分支，也不要仅凭履约状态筛选所有积分流水。"
                ),
            )
        )

    if any(term in planning_input for term in PENDING_GIFT_TERMS):
        missing_pending_filters = _find_pending_gift_filter_gaps(query_plan)
        if missing_pending_filters:
            issues.append(
                QueryPlanPolicyIssue(
                    field_path="query_plan.query_blocks[].filters",
                    message=(
                        "待发放奖品计划缺少必需且独立成立的筛选条件："
                        + "、".join(missing_pending_filters)
                        + "。"
                    ),
                    repair_action=(
                        "在商品兑换流水所属查询块的 filters 中补齐上述每一项条件；"
                        "四项条件必须通过 AND 同时成立，不得放入 OR 分支，"
                        "不得用商品当前上下架状态替代积分流水有效状态。"
                    ),
                )
            )

    if _requests_product_catalog(planning_input) and (
        "gift_distribution_status" in full_plan_text
    ):
        issues.append(
            QueryPlanPolicyIssue(
                field_path="query_plan.query_blocks[].filters",
                message="用户查询的是商品目录，但计划使用了兑换记录的奖品履约状态。",
                repair_action=(
                    "删除 gift_distribution_status 相关条件，改为根据 product 的商品状态和兑换积分查询。"
                ),
            )
        )
    return issues


# 校验逐条结果返回当前业务主体的主键，拒绝用关联表主键冒充可定位记录。
def _validate_subject_identifier_plan(
    planning_input: str,
    query_plan: NaturalLanguageQueryPlan,
    root_select_field_path: str,
) -> list[QueryPlanPolicyIssue]:
    requested_subject_table = _requested_detail_subject_table(planning_input)
    if (
        requested_subject_table is None
        or _has_aggregation(query_plan)
        or _has_subject_identifier(query_plan, requested_subject_table)
    ):
        return []
    subject_labels = {
        "point_record": "积分流水",
        "season_user": "赛季参与结算记录",
        "product": "商品",
    }
    subject_label = subject_labels[requested_subject_table]
    return [
        QueryPlanPolicyIssue(
            field_path=root_select_field_path,
            message=(
                f"当前逐条查询主体是{subject_label}，但根结果没有返回"
                f" {requested_subject_table}.id；其他关联表的 ID 不能替代主体主键。"
            ),
            repair_action=(
                f"保留用户要求的业务字段，并在根查询块添加来源可追溯到 "
                f"{requested_subject_table}.id 的主体 ID；"
                "不要删除名称、金额、状态等用户要求的内容。"
            ),
        )
    ]


# 校验积分与奖品查询计划，确保余额、赛季结算、兑换履约和明细主体使用正确数据口径。
def validate_rewards_query_plan(
    planning_input: str,
    query_plan: object,
    result_shape_plan: object | None = None,
) -> tuple[QueryPlanPolicyIssue, ...]:
    if not isinstance(query_plan, NaturalLanguageQueryPlan):
        return (
            QueryPlanPolicyIssue(
                field_path="query_plan",
                message="积分与奖品业务域收到了无法识别的查询计划类型。",
                repair_action="重新提交符合工具 Schema 的完整 query_plan。",
            ),
        )

    root_select_field_path = (
        f"query_plan.query_blocks[{query_plan.root_block_id}].select_fields"
    )
    selected_fields = [field.field for field in query_plan.select_fields]
    selected_text = "\n".join(selected_fields)
    filter_text = "\n".join(
        query_filter.condition for _, _, query_filter in query_plan.iter_filters()
    )
    full_plan_text = "\n".join(
        (
            query_plan.query_goal,
            query_plan.row_granularity,
            selected_text,
            filter_text,
            "\n".join(
                business_caliber.description
                for business_caliber in query_plan.business_caliber
            ),
        )
    )

    issues = _validate_balance_plan(
        planning_input,
        query_plan,
        root_select_field_path,
    )
    issues.extend(
        _validate_season_points_plan(
            planning_input,
            query_plan,
            root_select_field_path,
        )
    )
    issues.extend(_validate_exchange_plan(planning_input, query_plan, full_plan_text))
    issues.extend(
        _validate_subject_identifier_plan(
            planning_input,
            query_plan,
            root_select_field_path,
        )
    )
    return tuple(issues)
