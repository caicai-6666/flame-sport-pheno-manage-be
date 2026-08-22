"""定义运动业务域的查询计划硬约束。"""

import re
from typing import Final

from app.agent.domains.base import AlignmentPolicyIssue, QueryPlanPolicyIssue
from app.agent.tools.query_plan import NaturalLanguageQueryPlan


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
        _references_field(select_field.field, "proof_record", "id")
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


# 禁止暴露凭证内部图片路径；需要图片时强制返回可供安全中转接口使用的凭证主键。
def validate_sports_query_plan(
    planning_input: str,
    query_plan: object,
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
    has_proof_record_id = any(
        _references_field(select_field.field, "proof_record", "id")
        for select_field in query_plan.select_fields
    )
    has_proof_record_image_url = any(
        _references_field(select_field.field, "proof_record", "image_url")
        for select_field in query_plan.select_fields
    )

    if has_proof_record_image_url:
        issues.append(
            QueryPlanPolicyIssue(
                field_path="query_plan.select_fields",
                message=(
                    "proof_record.image_url 是凭证内部存储路径，"
                    "不得作为查询结果字段返回。"
                ),
                repair_action=(
                    "从 query_plan.select_fields 删除引用 "
                    "proof_record.image_url 的字段。"
                ),
            )
        )

    if _requests_proof_record_image(planning_input) and not has_proof_record_id:
        issues.append(
            QueryPlanPolicyIssue(
                field_path="query_plan.select_fields",
                message=(
                    "用户要求查看运动凭证图片，"
                    "但返回字段缺少 proof_record.id。"
                ),
                repair_action=(
                    "在 query_plan.select_fields 中添加 field 为 proof_record.id "
                    "的返回字段，供前端调用凭证图片安全中转接口。"
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
                field_path="query_plan.select_fields",
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

    return tuple(issues)
