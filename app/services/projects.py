"""编排管理端运动项目查询、创建与可见状态修改用例。"""

from dataclasses import dataclass
from io import BytesIO
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from pydantic import JsonValue
from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession

from app.clients.client_backend import ClientBackendClient
from app.core.config import get_settings
from app.repositories.projects import (
    ProjectInformation,
    ProjectRuleContent,
    ProjectRuleCreation,
    ProjectUploadConfigurationCreation,
    fetch_all_projects,
    fetch_project_rule_content,
    insert_project,
    insert_project_rules,
    insert_project_upload_configurations,
    lock_all_project_level_ids,
    update_project_name as update_project_name_repository,
    update_project_visibility_status as update_project_visibility_status_repository,
)
from app.services.configuration_guard import (
    ensure_active_season_configuration_editable,
)
from app.services.images import upload_project_icon

settings = get_settings()
MAX_PROJECT_ICON_SIZE_BYTES = 5 * 1024 * 1024
MAX_PROJECT_ICON_DIMENSION = 1600


class ProjectRuleNotFoundError(RuntimeError):
    """指定项目与挑战等级没有对应规则。"""


class ProjectNotFoundError(LookupError):
    """请求修改可见状态的运动项目不存在。"""


class ProjectNameConflictError(ValueError):
    """新项目名称违反数据库唯一约束。"""


class ProjectRuleLevelCoverageError(ValueError):
    """新项目规则没有恰好覆盖当前全部挑战等级。"""


class ProjectRuleMetricLabelsInconsistentError(ValueError):
    """新项目在不同等级下使用了不一致的指标标签。"""


class InvalidProjectIconMediaTypeError(ValueError):
    """上传文件声明的媒体类型不是 WebP。"""


class InvalidProjectIconContentError(ValueError):
    """上传文件的实际内容不是有效 WebP。"""


class ProjectIconDimensionsExceededError(ValueError):
    """项目图标任一边超过允许的像素尺寸。"""


class ProjectIconSizeExceededError(ValueError):
    """项目图标字节数超过允许上限。"""


@dataclass(frozen=True, slots=True)
class ProjectCreation:
    name: str
    description: str | None
    status: int
    rules: tuple[ProjectRuleCreation, ...]
    upload_configurations: tuple[ProjectUploadConfigurationCreation, ...]
    icon_content: bytes
    icon_media_type: str | None


# 识别 MySQL 重复键错误，只将项目名称唯一约束冲突转换为业务异常。
def is_duplicate_key_error(error: IntegrityError) -> bool:
    original_arguments = getattr(error.orig, "args", ())
    return bool(original_arguments and original_arguments[0] == 1062)


# 为每次新项目生成不复用的 WebP 相对地址，避免浏览器继续命中历史图标缓存。
def generate_project_icon_url() -> str:
    return f"/project-{uuid4().hex}.webp"


# 校验 WebP 类型、大小、真实格式与像素尺寸，避免伪装文件占用事务和上游资源。
def validate_project_icon(
    icon_content: bytes,
    icon_media_type: str | None,
) -> None:
    normalized_media_type = (icon_media_type or "").partition(";")[0]
    if normalized_media_type.strip().lower() != "image/webp":
        raise InvalidProjectIconMediaTypeError
    if len(icon_content) > MAX_PROJECT_ICON_SIZE_BYTES:
        raise ProjectIconSizeExceededError
    try:
        with Image.open(BytesIO(icon_content)) as image:
            if image.format != "WEBP":
                raise InvalidProjectIconContentError
            width, height = image.size
            if max(width, height) > MAX_PROJECT_ICON_DIMENSION:
                raise ProjectIconDimensionsExceededError
            image.verify()
    except ProjectIconDimensionsExceededError:
        raise
    except Image.DecompressionBombError as error:
        raise ProjectIconDimensionsExceededError from error
    except (UnidentifiedImageError, OSError, SyntaxError) as error:
        raise InvalidProjectIconContentError from error


# 提取规则中的有序标签并拒绝非标准、空标签或重复标签结构。
def extract_project_rule_metric_labels(
    rule_content: JsonValue,
) -> tuple[str, ...]:
    if not isinstance(rule_content, list) or not rule_content:
        raise ProjectRuleMetricLabelsInconsistentError

    labels: list[str] = []
    for metric in rule_content:
        if not isinstance(metric, dict):
            raise ProjectRuleMetricLabelsInconsistentError
        label = metric.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ProjectRuleMetricLabelsInconsistentError
        if label in labels:
            raise ProjectRuleMetricLabelsInconsistentError
        labels.append(label)
    return tuple(labels)


# 校验规则恰好覆盖锁定的全部等级，并确保各等级使用相同有序指标标签。
def validate_project_rule_matrix(
    level_ids: tuple[int, ...],
    rules: tuple[ProjectRuleCreation, ...],
) -> None:
    requested_level_ids = tuple(rule.level_id for rule in rules)
    if (
        len(requested_level_ids) != len(set(requested_level_ids))
        or set(requested_level_ids) != set(level_ids)
    ):
        raise ProjectRuleLevelCoverageError

    expected_labels: tuple[str, ...] | None = None
    for rule in rules:
        labels = extract_project_rule_metric_labels(rule.rule_content)
        if expected_labels is None:
            expected_labels = labels
            continue
        if labels != expected_labels:
            raise ProjectRuleMetricLabelsInconsistentError


# 在明确的只读事务中查询全部项目，前端依据状态决定是否展示隐藏项目。
async def list_projects(
    session: AsyncSession,
) -> tuple[ProjectInformation, ...]:
    async with session.begin():
        return await fetch_all_projects(session)


# 在只读事务中查询项目等级规则，并把空结果转换为稳定的应用异常。
async def get_project_rule_content(
    session: AsyncSession,
    project_id: int,
    level_id: int,
) -> ProjectRuleContent:
    async with session.begin():
        project_rule = await fetch_project_rule_content(
            session,
            project_id,
            level_id,
        )
    if project_rule is None:
        raise ProjectRuleNotFoundError
    return project_rule


# 在统一配置窗口和项目行锁保护下修改可见状态，重复提交相同状态保持成功。
async def update_project_visibility_status(
    session: AsyncSession,
    project_id: int,
    visibility_status: int,
    edit_window_hours: int = (
        settings.active_season_config_edit_window_hours
    ),
) -> ProjectInformation:
    async with session.begin():
        await ensure_active_season_configuration_editable(
            session,
            edit_window_hours,
        )
        project = await update_project_visibility_status_repository(
            session,
            project_id,
            visibility_status,
        )
        if project is None:
            raise ProjectNotFoundError
        return project


# 在统一配置窗口和项目行锁保护下修改名称，并将唯一键冲突转换为业务错误。
async def update_project_name(
    session: AsyncSession,
    project_id: int,
    project_name: str,
    edit_window_hours: int = (
        settings.active_season_config_edit_window_hours
    ),
) -> ProjectInformation:
    async with session.begin():
        await ensure_active_season_configuration_editable(
            session,
            edit_window_hours,
        )
        try:
            project = await update_project_name_repository(
                session,
                project_id,
                project_name,
            )
        except IntegrityError as error:
            if is_duplicate_key_error(error):
                raise ProjectNameConflictError from error
            raise
        if project is None:
            raise ProjectNotFoundError
        return project


# 在配置窗口内原子写入项目、完整规则矩阵与上传配置，并在提交前上传唯一 WebP 图标。
async def create_project(
    session: AsyncSession,
    client_backend: ClientBackendClient,
    creation: ProjectCreation,
    edit_window_hours: int = (
        settings.active_season_config_edit_window_hours
    ),
) -> ProjectInformation:
    validate_project_icon(
        creation.icon_content,
        creation.icon_media_type,
    )
    icon_url = generate_project_icon_url()

    async with session.begin():
        await ensure_active_season_configuration_editable(
            session,
            edit_window_hours,
        )
        level_ids = await lock_all_project_level_ids(session)
        validate_project_rule_matrix(level_ids, creation.rules)
        try:
            project = await insert_project(
                session,
                creation.name,
                creation.description,
                icon_url,
                creation.status,
            )
        except IntegrityError as error:
            if is_duplicate_key_error(error):
                raise ProjectNameConflictError from error
            raise
        await insert_project_rules(
            session,
            project.project_id,
            creation.rules,
        )
        await insert_project_upload_configurations(
            session,
            project.project_id,
            creation.upload_configurations,
        )
        await upload_project_icon(
            client_backend,
            icon_url,
            creation.icon_content,
        )
        return project
