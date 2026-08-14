"""编排管理端挑战等级查询、创建、积分与项目规则配置修改用例。"""

from dataclasses import dataclass
from typing import cast

from pydantic import JsonValue
from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import get_settings
from app.repositories.project_levels import (
    ProjectLevelInformation,
    fetch_all_project_levels,
    insert_project_level,
    update_project_level_reward as update_project_level_reward_repository,
)
from app.repositories.projects import (
    ProjectRuleConfiguration,
    ProjectRuleInitialization,
    ProjectRuleMetricSnapshot,
    insert_initialized_project_rules,
    lock_project_rule_configuration,
    lock_project_rule_metric_snapshot,
    update_project_rule_configuration as update_project_rule_configuration_repository,
)
from app.services.configuration_guard import (
    ensure_active_season_configuration_editable,
)

settings = get_settings()


class ProjectLevelNameConflictError(ValueError):
    """挑战等级名称违反数据库唯一约束。"""


class ProjectRuleMetricTemplateMissingError(ValueError):
    """至少一个项目没有可用于新等级初始化的评估指标。"""


class ProjectRuleMetricTemplateInconsistentError(ValueError):
    """至少一个项目在不同等级下使用了不一致的评估指标。"""


class ProjectLevelNotFoundError(LookupError):
    """请求修改的挑战等级不存在。"""


class ProjectRuleConfigurationNotFoundError(LookupError):
    """项目与挑战等级没有对应的规则配置。"""


class ProjectRuleMetricLabelMismatchError(ValueError):
    """请求修改了不存在的指标标签或重复提交了同一标签。"""


class ProjectRuleStoredContentInvalidError(ValueError):
    """数据库中的既有规则指标不是可安全更新的标准结构。"""


@dataclass(frozen=True, slots=True)
class ProjectRuleMetricValueUpdate:
    label: str
    value: JsonValue


@dataclass(frozen=True, slots=True)
class ProjectRuleConfigurationPatch:
    metric_values: tuple[ProjectRuleMetricValueUpdate, ...]
    update_sub_desc: bool
    sub_desc: str | None
    update_rule_note: bool
    rule_note: str | None


# 识别 MySQL 重复键错误，只把明确的名称唯一约束冲突转换为业务异常。
def is_duplicate_key_error(error: IntegrityError) -> bool:
    original_arguments = getattr(error.orig, "args", ())
    return bool(original_arguments and original_arguments[0] == 1062)


# 从规则 JSON 中提取有序且唯一的指标名称，拒绝空模板和非法结构。
def extract_metric_labels(rule_content: object) -> tuple[str, ...]:
    if not isinstance(rule_content, list) or not rule_content:
        raise ProjectRuleMetricTemplateMissingError

    labels: list[str] = []
    for metric in rule_content:
        if not isinstance(metric, dict):
            raise ProjectRuleMetricTemplateInconsistentError
        label = metric.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ProjectRuleMetricTemplateInconsistentError
        normalized_label = label.strip()
        if normalized_label in labels:
            raise ProjectRuleMetricTemplateInconsistentError
        labels.append(normalized_label)
    return tuple(labels)


# 校验每个项目跨等级指标一致，并生成值为 JSON null 的新等级规则。
def build_project_rule_initializations(
    snapshot: ProjectRuleMetricSnapshot,
) -> tuple[ProjectRuleInitialization, ...]:
    labels_by_project: dict[int, tuple[str, ...]] = {}
    project_id_set = set(snapshot.project_ids)

    for source in snapshot.sources:
        if source.project_id not in project_id_set:
            raise ProjectRuleMetricTemplateInconsistentError
        labels = extract_metric_labels(source.rule_content)
        existing_labels = labels_by_project.get(source.project_id)
        if existing_labels is not None and existing_labels != labels:
            raise ProjectRuleMetricTemplateInconsistentError
        labels_by_project[source.project_id] = labels

    initializations: list[ProjectRuleInitialization] = []
    for project_id in snapshot.project_ids:
        labels = labels_by_project.get(project_id)
        if labels is None:
            raise ProjectRuleMetricTemplateMissingError
        initializations.append(
            ProjectRuleInitialization(
                project_id=project_id,
                rule_content=[
                    {"label": label, "value": None}
                    for label in labels
                ],
            )
        )
    return tuple(initializations)


# 按既有标签定位并仅替换 value，保留指标顺序、label 和其他扩展字段。
def apply_project_rule_metric_value_updates(
    rule_content: JsonValue,
    updates: tuple[ProjectRuleMetricValueUpdate, ...],
) -> JsonValue:
    if not isinstance(rule_content, list):
        raise ProjectRuleStoredContentInvalidError

    metrics_by_label: dict[str, dict[str, JsonValue]] = {}
    for metric in rule_content:
        if not isinstance(metric, dict):
            raise ProjectRuleStoredContentInvalidError
        label = metric.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ProjectRuleStoredContentInvalidError
        if label in metrics_by_label:
            raise ProjectRuleStoredContentInvalidError
        metrics_by_label[label] = cast(dict[str, JsonValue], metric)

    updates_by_label: dict[str, JsonValue] = {}
    for update in updates:
        if update.label in updates_by_label:
            raise ProjectRuleMetricLabelMismatchError
        if update.label not in metrics_by_label:
            raise ProjectRuleMetricLabelMismatchError
        updates_by_label[update.label] = update.value

    updated_metrics: list[JsonValue] = []
    for metric in rule_content:
        typed_metric = cast(dict[str, JsonValue], metric)
        label = cast(str, typed_metric["label"])
        if label not in updates_by_label:
            updated_metrics.append(dict(typed_metric))
            continue
        updated_metric = dict(typed_metric)
        updated_metric["value"] = updates_by_label[label]
        updated_metrics.append(updated_metric)
    return updated_metrics


# 在明确的只读事务中查询全部等级，保持启用和停用等级均可供管理端查看。
async def list_project_levels(
    session: AsyncSession,
) -> tuple[ProjectLevelInformation, ...]:
    async with session.begin():
        return await fetch_all_project_levels(session)


# 在配置窗口内原子创建等级及全部项目空值规则，并拒绝不完整的指标模板。
async def create_project_level(
    session: AsyncSession,
    name: str,
    reward: int,
    edit_window_hours: int = (
        settings.active_season_config_edit_window_hours
    ),
) -> ProjectLevelInformation:
    async with session.begin():
        await ensure_active_season_configuration_editable(
            session,
            edit_window_hours,
        )
        metric_snapshot = await lock_project_rule_metric_snapshot(session)
        initializations = build_project_rule_initializations(
            metric_snapshot
        )
        try:
            project_level = await insert_project_level(
                session,
                name,
                reward,
            )
        except IntegrityError as error:
            if is_duplicate_key_error(error):
                raise ProjectLevelNameConflictError from error
            raise
        await insert_initialized_project_rules(
            session,
            project_level.id,
            initializations,
        )
        return project_level


# 在赛季配置窗口保护和等级行锁下更新奖励积分，重复写入相同值保持成功。
async def update_project_level_reward(
    session: AsyncSession,
    level_id: int,
    reward: int,
    edit_window_hours: int = (
        settings.active_season_config_edit_window_hours
    ),
) -> ProjectLevelInformation:
    async with session.begin():
        await ensure_active_season_configuration_editable(
            session,
            edit_window_hours,
        )
        project_level = await update_project_level_reward_repository(
            session,
            level_id,
            reward,
        )
        if project_level is None:
            raise ProjectLevelNotFoundError
        return project_level


# 在配置时间窗口和规则行锁保护下局部更新指标值、描述与备注。
async def update_project_rule_configuration(
    session: AsyncSession,
    project_id: int,
    level_id: int,
    patch: ProjectRuleConfigurationPatch,
    edit_window_hours: int = (
        settings.active_season_config_edit_window_hours
    ),
) -> ProjectRuleConfiguration:
    async with session.begin():
        await ensure_active_season_configuration_editable(
            session,
            edit_window_hours,
        )
        current_rule = await lock_project_rule_configuration(
            session,
            project_id,
            level_id,
        )
        if current_rule is None:
            raise ProjectRuleConfigurationNotFoundError

        rule_content = current_rule.rule_content
        if patch.metric_values:
            rule_content = apply_project_rule_metric_value_updates(
                rule_content,
                patch.metric_values,
            )
        sub_desc = (
            patch.sub_desc
            if patch.update_sub_desc
            else current_rule.sub_desc
        )
        rule_note = (
            patch.rule_note
            if patch.update_rule_note
            else current_rule.rule_note
        )
        return await update_project_rule_configuration_repository(
            session,
            project_id,
            level_id,
            sub_desc,
            rule_content,
            rule_note,
        )
