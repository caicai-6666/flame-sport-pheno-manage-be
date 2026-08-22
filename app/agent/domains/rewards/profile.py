"""声明积分与奖品查询业务域及其资源边界。"""

from pathlib import Path

from app.agent.domains.base import QueryDomainProfile, freeze_table_labels
from app.agent.domains.rewards.planning_policy import (
    validate_rewards_alignment,
    validate_rewards_query_plan,
)


REWARDS_DOMAIN_ROOT = Path(__file__).resolve().parent


REWARDS_QUERY_PROFILE = QueryDomainProfile(
    key="rewards",
    display_name="积分与奖品数据",
    query_scope="用户、部门、赛季参与、赛季积分、积分流水、商品和奖品履约的只读查询",
    root_directory=REWARDS_DOMAIN_ROOT,
    allowed_tables=(
        "department",
        "user",
        "season",
        "season_user",
        "product",
        "point_record",
    ),
    table_context_files=(
        "department.txt",
        "user.txt",
        "season.txt",
        "season-user.txt",
        "product.txt",
        "point-record.txt",
    ),
    table_labels=freeze_table_labels(
        {
            "department": "部门信息",
            "user": "用户信息",
            "season": "赛季信息",
            "season_user": "赛季参与与结算",
            "product": "积分商品",
            "point_record": "积分变动流水",
        }
    ),
    protected_database_identifiers=frozenset(
        {
            "department",
            "user",
            "season",
            "season_user",
            "product",
            "point_record",
            "id",
            "name",
            "department_id",
            "season_id",
            "user_id",
            "product_id",
            "final_points",
            "points_issued",
            "change_type",
            "change_points",
            "points_after",
            "gift_distribution_status",
            "points_required",
            "created_at",
            "status",
        }
    ),
    query_plan_validator=validate_rewards_query_plan,
    alignment_validator=validate_rewards_alignment,
    alignment_prompt_instructions=(
        "对齐需求必须明确积分或奖品查询的主体。‘积分余额’主体是用户当前可用积分；"
        "‘积分流水’或‘积分明细’主体是逐条积分变动；‘兑换记录’或‘待发放奖品’主体是逐条商品兑换履约记录；"
        "‘商品’主体是商品目录。主体标识是定位结果的辅助信息，不能替代用户要求的主体内容。\n\n"
        "‘积分’单独出现时不能自行假设它表示余额、流水、赛季结算积分或兑换消耗；"
        "这些含义会改变查询结果，必须根据上下文确定，无法确定时调用 ask_user。"
    ),
    planning_prompt_instructions=(
        "生成计划前必须先确定查询主体，并在 query_goal 与 row_granularity 中明确表达。"
        "一行必须代表用户真正要查询的对象：用户余额、积分变动记录、商品兑换履约记录、商品目录，"
        "或用户参与赛季的结算记录。主体的唯一 ID 是用于定位该对象的辅助字段，不能让返回内容退化为只有 ID。\n\n"
        "查询当前可用积分时，应以用户最新有效积分流水的余额事实为准，不能把历史余额字段相加。"
        "查询待发放奖品时，必须同时确认这是有效的商品兑换记录，而不是商品目录；礼品履约状态只对商品兑换记录有业务意义。\n\n"
        "当前积分流水没有结构化赛季关联。可以查询用户在某赛季的结算积分和发放状态，"
        "但不能仅凭赛季奖励类型把全局积分流水归属于某个具体赛季。"
    ),
)
