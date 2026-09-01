"""提供管理端运动项目查询、创建与可见状态修改接口。"""

from typing import Annotated

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    UploadFile,
    status,
)
from app.db.session import DatabaseSession
from app.repositories.projects import (
    ProjectRuleCreation,
    ProjectUploadConfigurationCreation,
)
from app.router.dependencies import ClientBackend
from app.router.support.forms import parse_json_form_field
from app.router.support.uploads import read_limited_upload
from app.schemas.project import (
    PROJECT_FORM_ADAPTER,
    PROJECT_RULES_FORM_ADAPTER,
    PROJECT_UPLOAD_CONFIGS_FORM_ADAPTER,
    ProjectInformationResponse,
    ProjectRuleResponse,
    UpdateProjectNameRequest,
    UpdateProjectVisibilityStatusRequest,
)
from app.services.projects import (
    InvalidProjectIconContentError,
    InvalidProjectIconMediaTypeError,
    MAX_PROJECT_ICON_SIZE_BYTES,
    ProjectCreation,
    ProjectIconDimensionsExceededError,
    ProjectIconSizeExceededError,
    ProjectNameConflictError,
    ProjectNotFoundError,
    ProjectRuleLevelCoverageError,
    ProjectRuleMetricLabelsInconsistentError,
    ProjectRuleNotFoundError,
    create_project as create_project_service,
    get_project_rule_content as get_project_rule_content_service,
    list_projects as list_projects_service,
    update_project_name as update_project_name_service,
    update_project_visibility_status as update_project_visibility_status_service,
)
from app.services.configuration_guard import (
    ActiveSeasonConfigurationWindowClosedError,
    MultipleActiveSeasonsForConfigurationError,
)
from app.services.images import (
    InvalidProjectIconUploadError,
    ProjectIconUploadBackendResponseError,
    ProjectIconUploadBackendUnavailableError,
    ProjectIconUploadTooLargeError,
)

router = APIRouter(prefix="/project", tags=["project"])


# 接收全部项目列表请求并返回可见状态，供管理前端自行过滤隐藏项目。
@router.get(
    "/list",
    response_model=list[ProjectInformationResponse],
    summary="获取全部项目列表",
)
async def list_projects_route(
    session: DatabaseSession,
) -> list[ProjectInformationResponse]:
    projects = await list_projects_service(session)
    return [
        ProjectInformationResponse.model_validate(
            project,
            from_attributes=True,
        )
        for project in projects
    ]


# 接收项目与等级标识并返回完整展示规则，缺失组合使用明确的不存在响应。
@router.get(
    "/rule",
    response_model=ProjectRuleResponse,
    summary="获取特定项目的挑战规则",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "未找到对应的项目规则"
        },
    },
)
async def get_project_rule_route(
    session: DatabaseSession,
    project_id: Annotated[
        int,
        Query(gt=0, description="运动项目 ID"),
    ],
    level_id: Annotated[
        int,
        Query(gt=0, description="挑战等级 ID"),
    ],
) -> ProjectRuleResponse:
    try:
        project_rule = await get_project_rule_content_service(
            session,
            project_id,
            level_id,
        )
    except ProjectRuleNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到对应的项目规则",
        ) from error
    return ProjectRuleResponse.model_validate(
        project_rule,
        from_attributes=True,
    )


# 按项目主键修改可见状态，并映射项目不存在与配置时间窗口冲突。
@router.patch(
    "/{project_id}/status",
    response_model=ProjectInformationResponse,
    summary="修改项目可见状态",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "运动项目不存在"},
        status.HTTP_409_CONFLICT: {
            "description": "激活赛季配置窗口已关闭或赛季数据冲突"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "项目主键或可见状态不符合字段约束"
        },
    },
)
async def update_project_visibility_status(
    session: DatabaseSession,
    project_id: Annotated[int, Path(gt=0, description="运动项目 ID")],
    request: UpdateProjectVisibilityStatusRequest,
) -> ProjectInformationResponse:
    try:
        project = await update_project_visibility_status_service(
            session,
            project_id,
            request.status,
        )
    except ProjectNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="运动项目不存在",
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
    return ProjectInformationResponse.model_validate(
        project,
        from_attributes=True,
    )


# 按项目主键修改展示名称，并统一映射不存在、重名和配置窗口冲突。
@router.patch(
    "/{project_id}/name",
    response_model=ProjectInformationResponse,
    summary="修改项目名称",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "运动项目不存在"},
        status.HTTP_409_CONFLICT: {
            "description": "项目名称重复、配置窗口已关闭或赛季数据冲突"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "项目主键或项目名称不符合字段约束"
        },
    },
)
async def update_project_name(
    session: DatabaseSession,
    project_id: Annotated[int, Path(gt=0, description="运动项目 ID")],
    request: UpdateProjectNameRequest,
) -> ProjectInformationResponse:
    try:
        project = await update_project_name_service(
            session,
            project_id,
            request.name,
        )
    except ProjectNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="运动项目不存在",
        ) from error
    except ProjectNameConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="运动项目名称已存在",
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
    return ProjectInformationResponse.model_validate(
        project,
        from_attributes=True,
    )


# 解析 multipart 项目配置并编排数据库写入与客户端 WebP 图标上传。
@router.post(
    "/create",
    response_model=ProjectInformationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建运动项目",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "上传文件不是有效且尺寸合规的 WebP"
        },
        status.HTTP_409_CONFLICT: {
            "description": "名称、规则矩阵或配置时间窗口冲突"
        },
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": "项目图标超过 5 MiB"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "表单字段缺失或 JSON 配置不符合约束"
        },
        status.HTTP_502_BAD_GATEWAY: {
            "description": "客户端后端项目图标上传服务异常"
        },
    },
)
async def create_project(
    session: DatabaseSession,
    client_backend: ClientBackend,
    project: Annotated[str, Form(max_length=2048)],
    project_rules: Annotated[str, Form(max_length=262144)],
    project_upload_configs: Annotated[str, Form(max_length=131072)],
    icon_file: Annotated[
        UploadFile,
        File(
            media_type="image/webp",
            description="仅接受最大 5 MiB、最长边 1600 像素的 WebP 图标",
            json_schema_extra={"contentMediaType": "image/webp"},
        ),
    ],
) -> ProjectInformationResponse:
    project_request = parse_json_form_field(
        project,
        PROJECT_FORM_ADAPTER,
        "project",
    )
    project_rule_requests = parse_json_form_field(
        project_rules,
        PROJECT_RULES_FORM_ADAPTER,
        "project_rules",
    )
    upload_configuration_requests = parse_json_form_field(
        project_upload_configs,
        PROJECT_UPLOAD_CONFIGS_FORM_ADAPTER,
        "project_upload_configs",
    )
    record_types = [
        configuration.record_type
        for configuration in upload_configuration_requests
    ]
    if len(record_types) != len(set(record_types)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="project_upload_configs 不能包含重复 record_type",
        )

    icon_media_type = icon_file.content_type
    icon_content = await read_limited_upload(
        icon_file,
        MAX_PROJECT_ICON_SIZE_BYTES,
    )
    creation = ProjectCreation(
        name=project_request.name,
        description=project_request.description,
        status=project_request.status,
        rules=tuple(
            ProjectRuleCreation(
                level_id=rule.level_id,
                sub_desc=rule.sub_desc,
                rule_content=[
                    metric.model_dump()
                    for metric in rule.rule_content
                ],
                rule_note=rule.rule_note,
                status=rule.status,
            )
            for rule in project_rule_requests
        ),
        upload_configurations=tuple(
            ProjectUploadConfigurationCreation(
                record_type=configuration.record_type,
                upload_hint=configuration.upload_hint,
                note_example=configuration.note_example,
                sort_order=configuration.sort_order,
                status=configuration.status,
            )
            for configuration in upload_configuration_requests
        ),
        icon_content=icon_content,
        icon_media_type=icon_media_type,
    )
    try:
        created_project = await create_project_service(
            session,
            client_backend,
            creation,
        )
    except ProjectNameConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="运动项目名称已存在",
        ) from error
    except ProjectRuleLevelCoverageError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="项目规则必须覆盖当前全部挑战等级",
        ) from error
    except ProjectRuleMetricLabelsInconsistentError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="项目各等级的评估指标标签必须一致",
        ) from error
    except InvalidProjectIconMediaTypeError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="仅支持上传 WebP 项目图标",
        ) from error
    except InvalidProjectIconContentError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="上传内容不是有效的 WebP 图片",
        ) from error
    except ProjectIconDimensionsExceededError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="项目图标最长边不能超过 1600 像素",
        ) from error
    except ProjectIconSizeExceededError as error:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="项目图标不能超过 5 MiB",
        ) from error
    except InvalidProjectIconUploadError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error
    except ProjectIconUploadTooLargeError as error:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(error),
        ) from error
    except (
        ProjectIconUploadBackendUnavailableError,
        ProjectIconUploadBackendResponseError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(error),
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
    return ProjectInformationResponse.model_validate(
        created_project,
        from_attributes=True,
    )
