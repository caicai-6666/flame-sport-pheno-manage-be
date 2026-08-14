"""封装管理端运动项目与项目规则的数据库查询和配置写入。"""

import json
from dataclasses import dataclass
from typing import cast

from pydantic import JsonValue
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession


@dataclass(frozen=True, slots=True)
class ProjectInformation:
    project_id: int
    project_name: str
    description: str | None
    icon_url: str | None
    status: int


@dataclass(frozen=True, slots=True)
class ProjectRuleContent:
    sub_desc: str | None
    rule_content: JsonValue
    rule_note: str | None


@dataclass(frozen=True, slots=True)
class ProjectRuleConfiguration:
    project_id: int
    level_id: int
    sub_desc: str | None
    rule_content: JsonValue
    rule_note: str | None


@dataclass(frozen=True, slots=True)
class ProjectRuleMetricSource:
    project_id: int
    level_id: int
    rule_content: JsonValue


@dataclass(frozen=True, slots=True)
class ProjectRuleMetricSnapshot:
    project_ids: tuple[int, ...]
    sources: tuple[ProjectRuleMetricSource, ...]


@dataclass(frozen=True, slots=True)
class ProjectRuleInitialization:
    project_id: int
    rule_content: JsonValue


@dataclass(frozen=True, slots=True)
class ProjectRuleCreation:
    level_id: int
    sub_desc: str | None
    rule_content: JsonValue
    rule_note: str | None
    status: int


@dataclass(frozen=True, slots=True)
class ProjectUploadConfigurationCreation:
    record_type: str
    upload_hint: str
    note_example: str | None
    sort_order: int
    status: int


# 将异步 MySQL 返回的 JSON 文本还原为 JSON 值，避免向 HTTP 层泄漏序列化细节。
def decode_rule_content(raw_rule_content: object) -> JsonValue:
    if isinstance(raw_rule_content, (str, bytes, bytearray)):
        return cast(JsonValue, json.loads(raw_rule_content))
    return cast(JsonValue, raw_rule_content)


# 查询全部项目的基础展示信息与可见状态，并按项目 ID 返回稳定顺序。
async def fetch_all_projects(
    session: AsyncSession,
) -> tuple[ProjectInformation, ...]:
    result = await session.exec(
        text(
            """
            SELECT
                project.id AS project_id,
                project.name AS project_name,
                project.description,
                project.icon_url,
                project.status
            FROM project
            ORDER BY project.id ASC
            """
        )
    )
    return tuple(
        ProjectInformation(
            project_id=int(row["project_id"]),
            project_name=str(row["project_name"]),
            description=(
                str(row["description"])
                if row["description"] is not None
                else None
            ),
            icon_url=(
                str(row["icon_url"])
                if row["icon_url"] is not None
                else None
            ),
            status=int(row["status"]),
        )
        for row in result.mappings().all()
    )


# 共享锁定全部挑战等级，为新项目规则矩阵提供一致且完整的等级快照。
async def lock_all_project_level_ids(
    session: AsyncSession,
) -> tuple[int, ...]:
    result = await session.exec(
        text(
            """
            SELECT project_level.id
            FROM project_level
            ORDER BY project_level.id ASC
            FOR SHARE
            """
        )
    )
    return tuple(int(row["id"]) for row in result.mappings().all())


# 写入包含唯一图标地址的新项目，并返回数据库生成的项目主键。
async def insert_project(
    session: AsyncSession,
    name: str,
    description: str | None,
    icon_url: str,
    project_status: int,
) -> ProjectInformation:
    result = await session.exec(
        text(
            """
            INSERT INTO project (
                name,
                description,
                icon_url,
                status
            ) VALUES (
                :name,
                :description,
                :icon_url,
                :project_status
            )
            """
        ),
        params={
            "name": name,
            "description": description,
            "icon_url": icon_url,
            "project_status": project_status,
        },
    )
    return ProjectInformation(
        project_id=int(result.lastrowid),
        project_name=name,
        description=description,
        icon_url=icon_url,
        status=project_status,
    )


# 批量写入新项目的全部等级规则，避免逐等级数据库往返和部分规则暴露。
async def insert_project_rules(
    session: AsyncSession,
    project_id: int,
    rules: tuple[ProjectRuleCreation, ...],
) -> None:
    if not rules:
        return

    values: list[str] = []
    params: dict[str, object] = {"project_id": project_id}
    for index, rule in enumerate(rules):
        values.append(
            "("
            f":project_id, :level_id_{index}, :sub_desc_{index}, "
            f":rule_content_{index}, :rule_note_{index}, :status_{index}"
            ")"
        )
        params[f"level_id_{index}"] = rule.level_id
        params[f"sub_desc_{index}"] = rule.sub_desc
        params[f"rule_content_{index}"] = json.dumps(
            rule.rule_content,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        params[f"rule_note_{index}"] = rule.rule_note
        params[f"status_{index}"] = rule.status

    await session.exec(
        text(
            """
            INSERT INTO project_rule (
                project_id,
                level_id,
                sub_desc,
                rule_content,
                rule_note,
                status
            ) VALUES
            """
            + ",\n".join(values)
        ),
        params=params,
    )


# 批量写入新项目的凭证上传配置，并保留请求给出的展示顺序和状态。
async def insert_project_upload_configurations(
    session: AsyncSession,
    project_id: int,
    configurations: tuple[ProjectUploadConfigurationCreation, ...],
) -> None:
    if not configurations:
        return

    values: list[str] = []
    params: dict[str, object] = {"project_id": project_id}
    for index, configuration in enumerate(configurations):
        values.append(
            "("
            f":project_id, :record_type_{index}, :upload_hint_{index}, "
            f":note_example_{index}, :sort_order_{index}, :status_{index}"
            ")"
        )
        params[f"record_type_{index}"] = configuration.record_type
        params[f"upload_hint_{index}"] = configuration.upload_hint
        params[f"note_example_{index}"] = configuration.note_example
        params[f"sort_order_{index}"] = configuration.sort_order
        params[f"status_{index}"] = configuration.status

    await session.exec(
        text(
            """
            INSERT INTO project_upload_config (
                project_id,
                record_type,
                upload_hint,
                note_example,
                sort_order,
                status
            ) VALUES
            """
            + ",\n".join(values)
        ),
        params=params,
    )


# 锁定目标项目后覆盖可见状态，项目不存在时不写入并返回空结果。
async def update_project_visibility_status(
    session: AsyncSession,
    project_id: int,
    visibility_status: int,
) -> ProjectInformation | None:
    result = await session.exec(
        text(
            """
            SELECT
                project.id AS project_id,
                project.name AS project_name,
                project.description,
                project.icon_url
            FROM project
            WHERE project.id = :project_id
            FOR UPDATE
            """
        ),
        params={"project_id": project_id},
    )
    row = result.mappings().first()
    if row is None:
        return None

    await session.exec(
        text(
            """
            UPDATE project
            SET status = :visibility_status
            WHERE id = :project_id
            """
        ),
        params={
            "project_id": project_id,
            "visibility_status": visibility_status,
        },
    )
    return ProjectInformation(
        project_id=int(row["project_id"]),
        project_name=str(row["project_name"]),
        description=(
            str(row["description"])
            if row["description"] is not None
            else None
        ),
        icon_url=(
            str(row["icon_url"])
            if row["icon_url"] is not None
            else None
        ),
        status=visibility_status,
    )


# 共享锁定当前启用项目集合并返回数量，防止创建赛季校验期间项目被停用。
async def lock_visible_project_count(session: AsyncSession) -> int:
    result = await session.exec(
        text(
            """
            SELECT project.id
            FROM project
            WHERE project.status = 1
            ORDER BY project.id ASC
            FOR SHARE
            """
        )
    )
    return len(result.mappings().all())


# 共享锁定全部项目及已有规则，提供新等级初始化所需的一致指标快照。
async def lock_project_rule_metric_snapshot(
    session: AsyncSession,
) -> ProjectRuleMetricSnapshot:
    project_result = await session.exec(
        text(
            """
            SELECT project.id
            FROM project
            ORDER BY project.id ASC
            FOR SHARE
            """
        )
    )
    rule_result = await session.exec(
        text(
            """
            SELECT
                project_rule.project_id,
                project_rule.level_id,
                project_rule.rule_content
            FROM project_rule
            ORDER BY
                project_rule.project_id ASC,
                project_rule.level_id ASC
            FOR SHARE
            """
        )
    )
    return ProjectRuleMetricSnapshot(
        project_ids=tuple(
            int(row["id"])
            for row in project_result.mappings().all()
        ),
        sources=tuple(
            ProjectRuleMetricSource(
                project_id=int(row["project_id"]),
                level_id=int(row["level_id"]),
                rule_content=decode_rule_content(row["rule_content"]),
            )
            for row in rule_result.mappings().all()
        ),
    )


# 批量写入新等级的项目规则，指标值保持 JSON null，副描述和备注留空。
async def insert_initialized_project_rules(
    session: AsyncSession,
    level_id: int,
    rules: tuple[ProjectRuleInitialization, ...],
) -> None:
    if not rules:
        return

    values: list[str] = []
    params: dict[str, object] = {"level_id": level_id}
    for index, rule in enumerate(rules):
        project_key = f"project_id_{index}"
        content_key = f"rule_content_{index}"
        values.append(
            f"(:{project_key}, :level_id, NULL, :{content_key}, NULL, 1)"
        )
        params[project_key] = rule.project_id
        params[content_key] = json.dumps(
            rule.rule_content,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    await session.exec(
        text(
            """
            INSERT INTO project_rule (
                project_id,
                level_id,
                sub_desc,
                rule_content,
                rule_note,
                status
            ) VALUES
            """
            + ",\n".join(values)
        ),
        params=params,
    )


# 使用联合唯一条件锁定项目规则，保证配置校验与覆盖写入基于同一版本。
async def lock_project_rule_configuration(
    session: AsyncSession,
    project_id: int,
    level_id: int,
) -> ProjectRuleConfiguration | None:
    result = await session.exec(
        text(
            """
            SELECT
                project_rule.project_id,
                project_rule.level_id,
                project_rule.sub_desc,
                project_rule.rule_content,
                project_rule.rule_note
            FROM project_rule
            WHERE project_rule.project_id = :project_id
                AND project_rule.level_id = :level_id
            FOR UPDATE
            """
        ),
        params={
            "project_id": project_id,
            "level_id": level_id,
        },
    )
    row = result.mappings().first()
    if row is None:
        return None
    return ProjectRuleConfiguration(
        project_id=int(row["project_id"]),
        level_id=int(row["level_id"]),
        sub_desc=(
            str(row["sub_desc"])
            if row["sub_desc"] is not None
            else None
        ),
        rule_content=decode_rule_content(row["rule_content"]),
        rule_note=(
            str(row["rule_note"])
            if row["rule_note"] is not None
            else None
        ),
    )


# 覆盖已经校验的完整规则配置，并返回事务内可直接响应的最终结果。
async def update_project_rule_configuration(
    session: AsyncSession,
    project_id: int,
    level_id: int,
    sub_desc: str | None,
    rule_content: JsonValue,
    rule_note: str | None,
) -> ProjectRuleConfiguration:
    await session.exec(
        text(
            """
            UPDATE project_rule
            SET
                sub_desc = :sub_desc,
                rule_content = :rule_content,
                rule_note = :rule_note
            WHERE project_id = :project_id
                AND level_id = :level_id
            """
        ),
        params={
            "project_id": project_id,
            "level_id": level_id,
            "sub_desc": sub_desc,
            "rule_content": json.dumps(
                rule_content,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "rule_note": rule_note,
        },
    )
    return ProjectRuleConfiguration(
        project_id=project_id,
        level_id=level_id,
        sub_desc=sub_desc,
        rule_content=rule_content,
        rule_note=rule_note,
    )


# 使用项目与等级联合唯一条件查询完整展示规则，保留历史记录的管理端可读性。
async def fetch_project_rule_content(
    session: AsyncSession,
    project_id: int,
    level_id: int,
) -> ProjectRuleContent | None:
    result = await session.exec(
        text(
            """
            SELECT
                project_rule.sub_desc,
                project_rule.rule_content,
                project_rule.rule_note
            FROM project_rule
            WHERE project_rule.project_id = :project_id
                AND project_rule.level_id = :level_id
            LIMIT 1
            """
        ),
        params={
            "project_id": project_id,
            "level_id": level_id,
        },
    )
    row = result.mappings().first()
    if row is None:
        return None
    return ProjectRuleContent(
        sub_desc=(
            str(row["sub_desc"])
            if row["sub_desc"] is not None
            else None
        ),
        rule_content=decode_rule_content(row["rule_content"]),
        rule_note=(
            str(row["rule_note"])
            if row["rule_note"] is not None
            else None
        ),
    )
