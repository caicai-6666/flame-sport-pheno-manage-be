"""定义积分与奖品业务域的查询计划和业务对齐约束。"""

import re
from typing import Final

from app.agent.domains.base import (
    AlignmentLogicalConstraintView,
    AlignmentPolicyIssue,
    QueryPlanPolicyIssue,
)
from app.agent.tools.query_plan import NaturalLanguageQueryPlan


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


# 判断用户是否只说了“积分”而没有指定余额、流水、赛季结算或兑换语义。
def _has_ambiguous_point_scope(question: str) -> bool:
    return "积分" in question and not any(
        term in question for term in BALANCE_SPECIFIC_TERMS
    )


# 判断查询需求是否明确要求逐条积分或奖品业务记录，而不是统计汇总。
def _requests_point_detail(planning_input: str) -> bool:
    return any(term in planning_input for term in POINT_DETAIL_TERMS)


# 判断查询是否是商品目录查询，避免把商品目录和用户兑换履约混为一谈。
def _requests_product_catalog(planning_input: str) -> bool:
    return any(term in planning_input for term in PRODUCT_TERMS)


# 判断计划表达式是否引用指定表的原始字段，兼容 SQL 生成阶段使用别名说明。
def _references_field(expression: str, table_name: str, field_name: str) -> bool:
    return (
        re.search(
            rf"\b{re.escape(table_name)}\s*\.\s*{re.escape(field_name)}\b",
            expression,
            flags=re.IGNORECASE,
        )
        is not None
    )


# 判断计划是否已经声明一条具体的表记录身份，避免明细查询退化为只有业务名称或金额。
def _has_record_identifier(query_plan: NaturalLanguageQueryPlan) -> bool:
    return any(
        _references_field(select_field.field, "point_record", "id")
        or _references_field(select_field.field, "product", "id")
        or _references_field(select_field.field, "season_user", "id")
        for select_field in query_plan.select_fields
    )


# 判断查询计划是否包含聚合；汇总结果不强制返回单条明细记录的主键。
def _has_aggregation(query_plan: NaturalLanguageQueryPlan) -> bool:
    return any(block.aggregations or block.group_by for block in query_plan.query_blocks)


# 判断过滤条件是否已经限定为有效的商品兑换流水。
def _has_exchange_filter(query_plan: NaturalLanguageQueryPlan) -> bool:
    filter_text = "\n".join(
        query_filter.condition for _, _, query_filter in query_plan.iter_filters()
    )
    normalized = filter_text.lower()
    return "change_type" in normalized and "exchange" in normalized


# 校验积分与奖品业务对齐结果是否明确了积分含义，避免模糊需求直接进入字段规划。
def validate_rewards_alignment(
    original_question: str,
    aligned_question: str,
    business_constraints: tuple[str, ...],
    applied_business_rules: tuple[str, ...] = (),
    logical_constraints: tuple[AlignmentLogicalConstraintView, ...] = (),
) -> tuple[AlignmentPolicyIssue, ...]:
    del applied_business_rules, logical_constraints
    if not _has_ambiguous_point_scope(original_question):
        return ()

    aligned_text = "\n".join((aligned_question, *business_constraints))
    if any(term in aligned_text for term in BALANCE_SPECIFIC_TERMS):
        return ()

    return (
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
        ),
    )


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

    issues: list[QueryPlanPolicyIssue] = []
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

    if any(term in planning_input for term in BALANCE_TERMS) and not _references_field(
        selected_text, "point_record", "points_after"
    ):
        issues.append(
            QueryPlanPolicyIssue(
                field_path=root_select_field_path,
                message=(
                    "用户查询的是当前积分余额，但返回字段没有包含能够表示当前余额的"
                    " point_record.points_after。"
                ),
                repair_action=(
                    "保留用户要求的主体和其他返回内容，并添加 point_record.points_after；"
                    "当前余额应取用户最新有效积分流水的余额事实，不能将历史余额相加。"
                ),
            )
        )

    if any(term in planning_input for term in SEASON_POINTS_TERMS) and not (
        _references_field(selected_text, "season_user", "final_points")
        or _references_field(selected_text, "season_user", "points_issued")
    ):
        issues.append(
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
        )

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
        if "商品" in planning_input or "奖品" in planning_input:
            if "product" not in declared_tables:
                issues.append(
                    QueryPlanPolicyIssue(
                        field_path="query_plan.tables",
                        message="用户需要知道兑换的商品，但计划没有 product。",
                        repair_action=(
                            "将 product 加入 tables，并通过 point_record.product_id 关联商品。"
                        ),
                    )
                )

    if "gift_distribution_status" in full_plan_text and not _has_exchange_filter(query_plan):
        issues.append(
            QueryPlanPolicyIssue(
                field_path="query_plan.query_blocks[].filters",
                message=(
                    "计划使用了奖品履约状态，但没有限定为商品兑换流水；"
                    "该状态对赛季奖励和人工积分调整没有业务意义。"
                ),
                repair_action=(
                    "在 filters 中添加 point_record.change_type = 'exchange'，"
                    "并保留 product_id 非空和有效记录条件；不要仅凭履约状态筛选所有积分流水。"
                ),
            )
        )

    if _requests_product_catalog(planning_input) and "gift_distribution_status" in full_plan_text:
        issues.append(
            QueryPlanPolicyIssue(
                field_path="query_plan.query_blocks[].filters",
                message="用户查询的是商品目录，但计划使用了兑换记录的奖品履约状态。",
                repair_action=(
                    "删除 gift_distribution_status 相关条件，改为根据 product 的商品状态和兑换积分查询。"
                ),
            )
        )

    if _requests_point_detail(planning_input) and not _has_aggregation(query_plan):
        if not _has_record_identifier(query_plan):
            issues.append(
                QueryPlanPolicyIssue(
                    field_path=root_select_field_path,
                    message=(
                        "查询主体是逐条积分、兑换或奖品记录，但计划没有返回能够定位该记录的主键。"
                    ),
                    repair_action=(
                        "保留用户要求的业务字段，并添加对应主体的原始 ID；"
                        "ID 只能作为记录定位字段，不能替代用户要求的主体内容。"
                    ),
                )
            )

    return tuple(issues)
