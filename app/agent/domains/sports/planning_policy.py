"""定义运动业务域的查询计划硬约束。"""

import re
from typing import Final

from app.agent.domains.base import AlignmentPolicyIssue, QueryPlanPolicyIssue
from app.agent.tools.query_plan import NaturalLanguageQueryPlan, ResultShapePlan


PROOF_IMAGE_TERMS: Final[tuple[str, ...]] = ("图片", "图像", "影像", "照片", "截图")
PROOF_IMAGE_RETURN_TERMS: Final[tuple[str, ...]] = (
    "返回",
    "查看",
    "展示",
    "显示",
    "提供",
    "带上",
    "包含",
    "下载",
    "导出",
    "打开",
    "预览",
)
PROOF_IMAGE_COMPOUND_TERMS: Final[tuple[str, ...]] = ("带图", "附图")
PROOF_RECORD_TERMS: Final[tuple[str, ...]] = (
    "运动记录",
    "运动凭证",
    "打卡记录",
    "凭证",
)
PROOF_DETAIL_TERMS: Final[tuple[str, ...]] = (
    "运动记录",
    "运动凭证明细",
    "凭证明细",
    "打卡记录",
)
PROOF_IMAGE_IDENTIFIER_TERMS: Final[tuple[str, ...]] = (
    "凭证的唯一标识",
    "凭证唯一标识",
    "凭证标识",
    "凭证编号",
    "记录唯一标识",
    "记录标识",
)
PROOF_RECORD_RESULT_FIELD: Final[str] = "proof_record_id"
PROOF_RECORD_DISPLAY_LABEL: Final[str] = "凭证记录 ID"


# 判断对齐后的需求是否要查看运动凭证图片，同时避免把头像或项目图标误判为凭证图片。
def _requests_proof_record_image(planning_input: str) -> bool:
    has_proof_record_term = any(
        term in planning_input for term in PROOF_RECORD_TERMS
    )
    explicitly_returns_image = (
        any(term in planning_input for term in PROOF_IMAGE_TERMS)
        and any(term in planning_input for term in PROOF_IMAGE_RETURN_TERMS)
    ) or any(term in planning_input for term in PROOF_IMAGE_COMPOUND_TERMS)
    return has_proof_record_term and explicitly_returns_image


# 判断对齐需求是否把逐条运动凭证明细作为查询主体，而不是只查询凭证标识。
def _requests_proof_record_detail(planning_input: str) -> bool:
    return any(term in planning_input for term in PROOF_DETAIL_TERMS)


# 判断规划是否只返回凭证主键；该判断不限制主体应返回哪些具体明细字段。
def _returns_only_proof_record_id(query_plan: NaturalLanguageQueryPlan) -> bool:
    return bool(query_plan.select_fields) and all(
        _resolve_sports_field_origin(select_field.field, query_plan)
        == "proof_record.id"
        for select_field in query_plan.select_fields
    )


# 校验业务对齐是否保留凭证图片需求的可执行交付方式，避免只保留“查看图片”而丢失凭证定位信息。
def validate_sports_alignment(
    original_question: str,
    aligned_question: str,
    business_constraints: tuple[str, ...],
) -> tuple[AlignmentPolicyIssue, ...]:
    if not _requests_proof_record_image(original_question):
        return ()

    aligned_text = "\n".join((aligned_question, *business_constraints))
    if any(term in aligned_text for term in PROOF_IMAGE_IDENTIFIER_TERMS):
        return ()

    return (
        AlignmentPolicyIssue(
            field_path="aligned_request.aligned_question",
            message=(
                "用户要求查看运动凭证关联图片，但对齐结果没有明确保留"
                "每条凭证的唯一标识。"
            ),
            repair_action=(
                "在 aligned_question 或 business_constraints 中明确写入："
                "保留每条凭证的唯一标识，后续根据该标识查看图片；"
                "不得写成返回图片内容或图片地址。"
            ),
        ),
    )


# 识别返回字段表达式是否引用指定原始字段，兼容后续 SQL 生成所需的别名描述。
def _references_field(expression: str, table_name: str, field_name: str) -> bool:
    return (
        re.search(
            rf"\b{re.escape(table_name)}\s*\.\s*{re.escape(field_name)}\b",
            expression,
            flags=re.IGNORECASE,
        )
        is not None
    )


# 将运动计划表达式中的合法别名还原为真实表名并移除排版差异，供领域契约做精确比较。
def _normalize_sports_plan_expression(
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
    return re.sub(r"\s+", "", normalized)


# 沿查询块显式输出字段反向追溯真实来源，使领域规则可以安全识别 CTE 暴露的赛季参与主键。
def _resolve_sports_field_origin(
    expression: str,
    query_plan: NaturalLanguageQueryPlan,
    visited_outputs: frozenset[tuple[str, str]] = frozenset(),
) -> str | None:
    normalized_expression = _normalize_sports_plan_expression(
        expression,
        query_plan,
    )
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
    return _resolve_sports_field_origin(
        selected_field.field,
        query_plan,
        visited_outputs | {output_key},
    )


# 校验全部完成量词把项目成员关联到可追溯为 season_user.id 的当前主体，兼容真实表和前置 CTE 输出。
def _is_valid_completion_correlation(
    correlation_condition: str | None,
    query_plan: NaturalLanguageQueryPlan,
) -> bool:
    if correlation_condition is None:
        return False
    equality_parts = re.split(
        r"(?<![<>!])=(?!=)",
        correlation_condition,
        maxsplit=1,
    )
    if len(equality_parts) != 2:
        return False
    resolved_origins = [
        _resolve_sports_field_origin(part, query_plan)
        for part in equality_parts
    ]
    return set(resolved_origins) == {
        "season_user_project.season_user_id",
        "season_user.id",
    }


# 根据根查询块的粒度字段识别逐条凭证明细，避免依赖用户是否额外说出“图片”。
def _returns_proof_record_rows(query_plan: NaturalLanguageQueryPlan) -> bool:
    return any(
        _resolve_sports_field_origin(grain_field, query_plan)
        == "proof_record.id"
        for grain_field in query_plan.root_block.grain_fields
    )


# 禁止暴露凭证内部图片路径；逐条凭证明细始终返回前端可识别的稳定凭证主键。
def validate_sports_query_plan(
    planning_input: str,
    query_plan: object,
    result_shape_plan: object | None = None,
) -> tuple[QueryPlanPolicyIssue, ...]:
    if not isinstance(query_plan, NaturalLanguageQueryPlan):
        return (
            QueryPlanPolicyIssue(
                field_path="query_plan",
                message="运动业务域收到了无法识别的查询计划类型。",
                repair_action="重新提交符合工具 Schema 的完整 query_plan。",
            ),
        )

    issues: list[QueryPlanPolicyIssue] = []
    root_select_field_path = (
        f"query_plan.query_blocks[{query_plan.root_block_id}].select_fields"
    )
    has_proof_record_id = any(
        _resolve_sports_field_origin(select_field.field, query_plan)
        == "proof_record.id"
        for select_field in query_plan.select_fields
    )
    proof_record_id_fields = [
        (field_index, select_field)
        for field_index, select_field in enumerate(query_plan.select_fields)
        if _resolve_sports_field_origin(select_field.field, query_plan)
        == "proof_record.id"
    ]
    returns_proof_record_rows = _returns_proof_record_rows(query_plan)
    has_proof_record_image_url = any(
        _resolve_sports_field_origin(select_field.field, query_plan)
        == "proof_record.image_url"
        for select_field in query_plan.select_fields
    )

    if has_proof_record_image_url:
        issues.append(
            QueryPlanPolicyIssue(
                field_path=root_select_field_path,
                message=(
                    "proof_record.image_url 是凭证内部存储路径，"
                    "不得作为查询结果字段返回。"
                ),
                repair_action=(
                    f"从查询块 {query_plan.root_block_id} 的 select_fields 删除引用 "
                    "proof_record.image_url 的字段。"
                ),
            )
        )

    if returns_proof_record_rows and not has_proof_record_id:
        issues.append(
            QueryPlanPolicyIssue(
                field_path=root_select_field_path,
                message=(
                    "根查询块以 proof_record.id 作为逐条凭证明细粒度，"
                    "但返回字段缺少 proof_record.id。"
                ),
                repair_action=(
                    "从读取 proof_record 的查询块开始输出 proof_record.id，并沿 "
                    "input_blocks 逐级透传到根查询块；根块字段固定使用 "
                    "result_field=proof_record_id、purpose=凭证记录 ID。逐条凭证查询"
                    "无论用户是否明确要求图片都必须返回该字段。"
                ),
            )
        )
    elif _requests_proof_record_image(planning_input) and not has_proof_record_id:
        issues.append(
            QueryPlanPolicyIssue(
                field_path=root_select_field_path,
                message=(
                    "用户要求查看运动凭证图片，但返回字段缺少 proof_record.id。"
                ),
                repair_action=(
                    f"在查询块 {query_plan.root_block_id} 的 select_fields 中添加 "
                    "field=proof_record.id、result_field=proof_record_id、"
                    "purpose=凭证记录 ID 的返回字段。"
                ),
            )
        )

    for field_index, select_field in proof_record_id_fields:
        select_field_path = f"{root_select_field_path}[{field_index}]"
        if select_field.result_field != PROOF_RECORD_RESULT_FIELD:
            issues.append(
                QueryPlanPolicyIssue(
                    field_path=f"{select_field_path}.result_field",
                    message=(
                        "proof_record.id 的稳定结果键必须为 proof_record_id，"
                        f"当前为 {select_field.result_field}。"
                    ),
                    repair_action=(
                        "将该字段的 result_field 精确改为 proof_record_id，并同步替换 "
                        "result_shape_plan 中对旧结果键的全部引用。"
                    ),
                )
            )
        if select_field.purpose != PROOF_RECORD_DISPLAY_LABEL:
            issues.append(
                QueryPlanPolicyIssue(
                    field_path=f"{select_field_path}.purpose",
                    message=(
                        "proof_record.id 的展示表头必须为“凭证记录 ID”，"
                        f"当前为“{select_field.purpose}”。"
                    ),
                    repair_action="将该字段的 purpose 精确改为“凭证记录 ID”。",
                )
            )

    if (
        returns_proof_record_rows
        and isinstance(result_shape_plan, ResultShapePlan)
        and result_shape_plan.shape_type == "passthrough"
        and PROOF_RECORD_RESULT_FIELD not in result_shape_plan.passthrough_fields
    ):
        issues.append(
            QueryPlanPolicyIssue(
                field_path="result_shape_plan.passthrough_fields",
                message=(
                    "逐条凭证明细的凭证记录 ID 没有进入最终可见结果列。"
                ),
                repair_action=(
                    "将 proof_record_id 加入 result_shape_plan.passthrough_fields，"
                    "并从 hidden_fields 中删除该字段。"
                ),
            )
        )

    if (
        _requests_proof_record_image(planning_input)
        and _requests_proof_record_detail(planning_input)
        and _returns_only_proof_record_id(query_plan)
    ):
        issues.append(
            QueryPlanPolicyIssue(
                field_path=root_select_field_path,
                message=(
                    "查询主体是逐条运动凭证明细，当前计划却只返回凭证 ID；"
                    "图片标识是附加信息，不能替代查询主体本身。"
                ),
                repair_action=(
                    "保留 proof_record.id 供图片查看，并根据对齐需求补充能够描述"
                    "逐条运动凭证明细的返回字段；不要套用固定字段清单。"
                    "如果用户确实只需要凭证 ID，应先让业务对齐结果明确说明主体仅为凭证标识。"
                ),
            )
        )

    for block, condition_index, condition in query_plan.iter_quantified_conditions():
        condition_path = (
            f"query_plan.query_blocks[{block.block_id}]."
            f"quantified_conditions[{condition_index}]"
        )
        normalized_predicate = _normalize_sports_plan_expression(
            condition.predicate,
            query_plan,
        )
        references_completion_progress = (
            "season_user_project.completion_progress" in normalized_predicate
        )
        uses_canonical_completion_boundary = re.fullmatch(
            r"season_user_project\.completion_progress>=1(?:\.0+)?",
            normalized_predicate,
        ) is not None
        if (
            condition.quantifier == "all"
            and references_completion_progress
            and not uses_canonical_completion_boundary
        ):
            issues.append(
                QueryPlanPolicyIssue(
                    field_path=(
                        f"{condition_path}.predicate"
                    ),
                    message=(
                        "全部项目完成的成员谓词没有使用标准完成条件；predicate 表示"
                        "每个集合成员应满足的条件，不能填写未完成反例。"
                    ),
                    repair_action=(
                        "将该 predicate 的完整内容精确改为 "
                        "season_user_project.completion_progress >= 1；"
                        "保持 quantifier=all 和其他集合范围不变。"
                    ),
                )
            )
        if condition.quantifier != "all" or not references_completion_progress:
            continue
        if (
            condition.implementation_hint in {"exists", "not_exists", "subquery"}
            and not _is_valid_completion_correlation(
                condition.correlation_condition,
                query_plan,
            )
        ):
            issues.append(
                QueryPlanPolicyIssue(
                    field_path=(
                        f"{condition_path}.correlation_condition"
                    ),
                    message=(
                        "全部项目完成的量词没有把内层项目集合直接关联到"
                        "当前外层赛季参与记录。"
                    ),
                    repair_action=(
                        "将 correlation_condition 的成员侧设为 "
                        "season_user_project.season_user_id，主体侧使用当前查询块中"
                        "可追溯到 season_user.id 的 subject_key。主体直接来自真实表时可写 "
                        "season_user_project.season_user_id = season_user.id；"
                        "主体来自前置查询块时应使用该块实际输出的 season_user_id，"
                        "并保留对应 input_blocks 依赖。"
                    ),
                )
            )
        if condition.implementation_hint in {"exists", "not_exists", "subquery"}:
            outer_member_join_indexes = [
                join_index
                for join_index, join in enumerate(block.joins)
                if "season_user_project." in _normalize_sports_plan_expression(
                    join.condition,
                    query_plan,
                )
            ]
            if outer_member_join_indexes:
                issues.append(
                    QueryPlanPolicyIssue(
                        field_path=(
                            f"query_plan.query_blocks[{block.block_id}].joins["
                            + ",".join(
                                str(index) for index in outer_member_join_indexes
                            )
                            + "]"
                        ),
                        message=(
                            "全部项目完成已经由相关子查询量化判断，当前资格块又在外层"
                            "关联 season_user_project，会把一名主体展开为多行并要求 SQL "
                            "为同一张表发明第二个角色别名。"
                        ),
                        repair_action=(
                            f"从查询块 {block.block_id} 的 joins 删除上述所有引用 "
                            "season_user_project 的外层关联；保留 source_tables 中的 "
                            "season_user_project，并只在 quantified_conditions 的相关子查询"
                            "中通过 correlation_condition 关联项目集合。"
                        ),
                    )
                )
            outer_member_filter_indexes = [
                filter_index
                for filter_index, query_filter in enumerate(block.filters)
                if "season_user_project." in _normalize_sports_plan_expression(
                    query_filter.condition,
                    query_plan,
                )
            ]
            if outer_member_filter_indexes:
                issues.append(
                    QueryPlanPolicyIssue(
                        field_path=(
                            f"query_plan.query_blocks[{block.block_id}].filters["
                            + ",".join(
                                str(index) for index in outer_member_filter_indexes
                            )
                            + "]"
                        ),
                        message=(
                            "全部项目完成的成员集合条件被重复写入资格块外层 filters，"
                            "会先展开或裁剪成员行，破坏一名主体一行的资格粒度。"
                        ),
                        repair_action=(
                            f"从查询块 {block.block_id} 的 filters 删除上述所有引用 "
                            "season_user_project 的条件；有效锁定项目范围只保留在"
                            "该量词的 collection_filters 中，完成条件只保留在 predicate 中。"
                        ),
                    )
                )
        normalized_collection_filters = {
            _normalize_sports_plan_expression(item, query_plan)
            for item in condition.collection_filters
        }
        if "season_user_project.status=1" not in normalized_collection_filters:
            issues.append(
                QueryPlanPolicyIssue(
                    field_path=(
                        f"{condition_path}.collection_filters"
                    ),
                    message="全部项目完成的量词集合没有限定为有效锁定项目。",
                    repair_action=(
                        "在 collection_filters 中加入 "
                        "season_user_project.status = 1；"
                        "外层 filters 中用于限定返回项目行的同一条件保持不变。"
                    ),
                )
            )
        unexpected_collection_filters = sorted(
            normalized_collection_filters - {"season_user_project.status=1"}
        )
        if unexpected_collection_filters:
            issues.append(
                QueryPlanPolicyIssue(
                    field_path=(
                        f"{condition_path}.collection_filters"
                    ),
                    message=(
                        "全部项目完成的成员集合混入了不属于有效锁定项目范围的条件："
                        + "、".join(unexpected_collection_filters)
                    ),
                    repair_action=(
                        "从 collection_filters 删除上述额外条件，只保留 "
                        "season_user_project.status = 1；"
                        "赛季范围和外层关联继续由对应 query_block 的 filters、joins 与 "
                        "correlation_condition 承载。"
                    ),
                )
            )

    return tuple(issues)
