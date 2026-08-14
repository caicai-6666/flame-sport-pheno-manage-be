"""提供管理端挑战等级、奖励积分与项目规则配置接口。"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, status

from app.db.session import DatabaseSession
from app.schemas.project_level import (
    CreateProjectLevelRequest,
    ProjectLevelResponse,
    ProjectRuleConfigurationResponse,
    UpdateProjectLevelRewardRequest,
    UpdateProjectRuleRequest,
)
from app.services.project_levels import (
    ProjectLevelNameConflictError,
    ProjectLevelNotFoundError,
    ProjectRuleConfigurationNotFoundError,
    ProjectRuleConfigurationPatch,
    ProjectRuleMetricLabelMismatchError,
    ProjectRuleMetricValueUpdate,
    ProjectRuleMetricTemplateInconsistentError,
    ProjectRuleMetricTemplateMissingError,
    ProjectRuleStoredContentInvalidError,
    create_project_level as create_project_level_service,
    list_project_levels as list_project_levels_service,
    update_project_level_reward as update_project_level_reward_service,
    update_project_rule_configuration as update_project_rule_configuration_service,
)
from app.services.configuration_guard import (
    ActiveSeasonConfigurationWindowClosedError,
    MultipleActiveSeasonsForConfigurationError,
)

router = APIRouter(prefix="/project-level", tags=["project-level"])


# 接收全部挑战等级列表请求，并序列化等级主键、名称与奖励积分。
@router.get(
    "/list",
    response_model=list[ProjectLevelResponse],
    summary="获取全部挑战等级列表",
)
async def get_project_level_list(
    session: DatabaseSession,
) -> list[ProjectLevelResponse]:
    project_levels = await list_project_levels_service(session)
    return [
        ProjectLevelResponse.model_validate(
            project_level,
            from_attributes=True,
        )
        for project_level in project_levels
    ]


# 在赛季配置窗口内创建默认启用等级，并映射名称、指标模板和窗口冲突。
@router.post(
    "/create",
    response_model=ProjectLevelResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建挑战等级",
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "等级名称重复或项目指标模板无法完成初始化"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "名称或奖励积分不符合字段约束"
        },
    },
)
async def create_project_level(
    session: DatabaseSession,
    request: CreateProjectLevelRequest,
) -> ProjectLevelResponse:
    try:
        project_level = await create_project_level_service(
            session,
            request.name,
            request.reward,
        )
    except ProjectLevelNameConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="挑战等级名称已存在",
        ) from error
    except ProjectRuleMetricTemplateMissingError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="存在未配置评估指标的项目，无法创建挑战等级",
        ) from error
    except ProjectRuleMetricTemplateInconsistentError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="项目评估指标配置不一致，无法创建挑战等级",
        ) from error
    except ActiveSeasonConfigurationWindowClosedError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="当前激活赛季的配置修改窗口已关闭",
        ) from error
    except MultipleActiveSeasonsForConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="存在多个激活赛季，无法判断配置修改窗口",
        ) from error
    return ProjectLevelResponse.model_validate(
        project_level,
        from_attributes=True,
    )


# 接收等级主键与新积分值，并映射不存在及赛季配置窗口冲突。
@router.patch(
    "/{level_id}/reward",
    response_model=ProjectLevelResponse,
    summary="修改挑战等级奖励积分",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "挑战等级不存在"},
        status.HTTP_409_CONFLICT: {
            "description": "激活赛季配置窗口已关闭或赛季数据冲突"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "等级主键或奖励积分不符合字段约束"
        },
    },
)
async def update_project_level_reward(
    session: DatabaseSession,
    level_id: Annotated[int, Path(gt=0, description="挑战等级 ID")],
    request: UpdateProjectLevelRewardRequest,
) -> ProjectLevelResponse:
    try:
        project_level = await update_project_level_reward_service(
            session,
            level_id,
            request.reward,
        )
    except ProjectLevelNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="挑战等级不存在",
        ) from error
    except ActiveSeasonConfigurationWindowClosedError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="当前激活赛季的配置修改窗口已关闭",
        ) from error
    except MultipleActiveSeasonsForConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="存在多个激活赛季，无法判断配置修改窗口",
        ) from error
    return ProjectLevelResponse.model_validate(
        project_level,
        from_attributes=True,
    )


# 按项目与等级局部修改规则值和展示文案，并映射窗口、标签及数据异常。
@router.patch(
    "/{level_id}/project/{project_id}/rule",
    response_model=ProjectRuleConfigurationResponse,
    summary="修改项目挑战规则配置",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "项目规则不存在"},
        status.HTTP_409_CONFLICT: {
            "description": "配置窗口关闭、赛季冲突或规则标签不匹配"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "标识或规则配置不符合字段约束"
        },
    },
)
async def update_project_rule_configuration(
    session: DatabaseSession,
    level_id: Annotated[int, Path(gt=0, description="挑战等级 ID")],
    project_id: Annotated[int, Path(gt=0, description="运动项目 ID")],
    request: UpdateProjectRuleRequest,
) -> ProjectRuleConfigurationResponse:
    patch = ProjectRuleConfigurationPatch(
        metric_values=tuple(
            ProjectRuleMetricValueUpdate(metric.label, metric.value)
            for metric in request.rule_content or ()
        ),
        update_sub_desc="sub_desc" in request.model_fields_set,
        sub_desc=request.sub_desc,
        update_rule_note="rule_note" in request.model_fields_set,
        rule_note=request.rule_note,
    )
    try:
        project_rule = await update_project_rule_configuration_service(
            session,
            project_id,
            level_id,
            patch,
        )
    except ProjectRuleConfigurationNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到对应的项目规则",
        ) from error
    except ProjectRuleMetricLabelMismatchError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="规则指标标签与现有配置不一致",
        ) from error
    except ProjectRuleStoredContentInvalidError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="现有项目规则指标格式异常，无法修改",
        ) from error
    except ActiveSeasonConfigurationWindowClosedError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="当前激活赛季的配置修改窗口已关闭",
        ) from error
    except MultipleActiveSeasonsForConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="存在多个激活赛季，无法判断配置修改窗口",
        ) from error
    return ProjectRuleConfigurationResponse.model_validate(
        project_rule,
        from_attributes=True,
    )
