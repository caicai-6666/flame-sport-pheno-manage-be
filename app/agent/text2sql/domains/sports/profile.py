"""声明企业运动 Text-to-SQL 查询业务域。"""

from pathlib import Path

from app.agent.text2sql.domains.base import QueryDomainProfile, freeze_table_labels
from app.agent.text2sql.domains.sports.planning_policy import (
    validate_sports_alignment,
    validate_sports_query_plan,
)


SPORTS_DOMAIN_ROOT = Path(__file__).resolve().parent

SPORTS_QUERY_PROFILE = QueryDomainProfile(
    key="sports",
    display_name="运动数据",
    query_scope="企业运动赛季、报名、项目、进度、凭证和排行榜的只读查询",
    root_directory=SPORTS_DOMAIN_ROOT,
    allowed_tables=(
        "department",
        "leaderboard_snapshot",
        "project",
        "project_level",
        "project_rule",
        "proof_record",
        "season",
        "season_supplement_eligibility",
        "season_user",
        "season_user_project",
        "user",
    ),
    table_context_files=(
        "department.txt",
        "user.txt",
        "season.txt",
        "project.txt",
        "project-level.txt",
        "project-rule.txt",
        "season-user.txt",
        "season-user-project.txt",
        "proof-record.txt",
        "season-supplement-eligibility.txt",
        "leaderboard-snapshot.txt",
    ),
    table_labels=freeze_table_labels(
        {
            "department": "部门信息",
            "leaderboard_snapshot": "排行榜快照",
            "project": "运动项目",
            "project_level": "挑战等级",
            "project_rule": "项目挑战规则",
            "proof_record": "运动记录",
            "season": "赛季信息",
            "season_supplement_eligibility": "赛季补传资格",
            "season_user": "赛季参与信息",
            "season_user_project": "项目完成进度",
            "user": "用户信息",
        }
    ),
    protected_database_identifiers=frozenset(
        {
            "department",
            "leaderboard_snapshot",
            "project",
            "project_level",
            "project_rule",
            "proof_record",
            "season",
            "season_supplement_eligibility",
            "season_user",
            "season_user_project",
            "user",
            "id",
            "status",
            "season_id",
            "user_id",
            "level_id",
            "completion_progress",
            "participated_at",
            "final_points",
            "points_issued",
        }
    ),
    query_plan_validator=validate_sports_query_plan,
    alignment_validator=validate_sports_alignment,
    alignment_prompt_instructions=(
        "当用户要求查看运动凭证、运动记录或打卡记录关联的图片时，"
        "必须保留“查看图片”的业务意图，但不得把它改写成“返回图片内容”或“返回图片地址”。"
        "对齐需求应表达为查看凭证关联图片，只需保留每条凭证的唯一标识，"
        "后续根据标识查看图片；这只是业务交付规则，不代表查询结果中存在图片内容字段。\n\n"
        "对齐需求必须明确查询主体。用户询问运动记录、运动凭证或打卡记录时，"
        "主体是逐条运动凭证明细；图片查看和凭证标识只是附加要求，不能替代该主体。"
        "只有用户明确表示只需要凭证标识时，才将查询主体对齐为凭证标识。\n\n"
        "当用户要求正式参与用户锁定 N 项且全部完成时，N 表示赛季要求的锁定项目数量，"
        "必须写入 business_constraints，不得再生成 exactly(count=N)；"
        "针对项目完成语义，logical_constraints 只生成一条全部有效锁定项目完成的 all 约束，"
        "其他独立业务约束保持不变。"
        "只有用户单独要求‘恰好完成 N 项’而不是‘赛季要求 N 项且全部完成’时，"
        "才生成 exactly(count=N)。"
    ),
    planning_prompt_instructions=(
        "只要根查询块的一行代表一条运动凭证，即 grain_fields 可追溯到 "
        "`proof_record.id`，无论用户是否明确要求图片，都必须在根块 select_fields "
        "中返回 `proof_record.id`，并固定使用 `result_field=proof_record_id`、"
        "`purpose=凭证记录 ID`；passthrough 结果还必须把 proof_record_id 作为可见列，"
        "不能放入 hidden_fields。该稳定键供前端定位凭证和按需读取图片。\n\n"
        "当对齐需求包含查看凭证关联图片时，凭证唯一标识是对原有返回内容的附加要求，"
        "不是替代项。必须保留用户原问题和对齐需求中已经要求的其他返回内容；"
        "只有对齐需求明确只要求凭证标识时，才可以生成只返回标识的查询计划。\n\n"
        "生成计划前必须先确定查询主体，并在 `query_goal` 与 `row_granularity` 中明确表达。"
        "一行应描述用户真正要查询的主体；如果主体是逐条运动凭证明细，"
        "`proof_record.id` 只能作为辅助定位字段，不能让返回字段退化为 ID 清单。\n\n"

        "涉及全部有效锁定项目完成时，quantifier 必须为 all；该量词固定编译为 "
        "not_exists，成员 predicate 中的"
        "完成边界统一写为 `season_user_project.completion_progress >= 1`；"
        "不要改写为等号条件，SQL 层会用 `< 1` 作为可验证的反例条件。"
        "承载该 all 约束的资格块不得使用 group_by、aggregations 或 having；"
        "如最终结果还需要聚合，应由读取资格块的后续查询块单独完成。"
        "正式参与和全部项目完成都是 season_user 粒度时，优先在同一个资格查询块中"
        "完成筛选，不要创建职责重复的多个资格块。"
        "正式参与已由 `season_user.status = season.required_project_count` 与 "
        "`season_user.level_id IS NOT NULL` 判定，不要再用 HAVING 重复统计有效锁定项目数；"
        "用户明确要求赛季配置数量为 N 时，在同一资格块增加 "
        "`season.required_project_count = N`，不要把 N 改写为第二个项目数量资格块。"
        "同一量词的 correlation_condition 必须把成员侧 "
        "`season_user_project.season_user_id` 关联到当前主体。"
        "主体直接来自真实表时使用 `season_user.id`；主体来自前置查询块时，"
        "使用该块中可追溯到 `season_user.id` 的实际输出字段，并保留 input_blocks 依赖，"
        "不得为了满足关联格式而复制真实表、重复资格条件或留下未被根块引用的查询块。"
        "使用 exists、not_exists 或相关 subquery 判断全部完成时，"
        "season_user_project 只属于量词内部成员集合：不得同时出现在该资格块的外层 "
        "joins 或 filters；source_tables 仍须声明该内部子查询读取了此真实表。"
        "collection_filters 必须且只能包含 `season_user_project.status = 1`，"
        "从而只检查当前参与记录下的全部有效锁定项目。"
    ),
)
