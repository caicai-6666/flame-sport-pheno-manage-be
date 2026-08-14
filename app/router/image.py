"""提供经客户端后端安全中转的图片 HTTP 接口。"""

from collections.abc import Awaitable
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Response, status

from app.core.config import get_settings
from app.router.dependencies import ClientBackend
from app.services.images import (
    EmptyImageAddressError,
    ImageBackendResponseError,
    ImageBackendUnavailableError,
    ImageNotFoundError,
    InvalidImageContentError,
    InvalidImagePathError,
    ProxiedImage,
    get_avatar_image,
    get_product_image,
    get_project_icon_image,
    get_proof_record_image,
)

router = APIRouter(prefix="/image", tags=["image"])
settings = get_settings()


# 将图片服务结果与异常统一映射为安全 HTTP 响应，避免不同图片接口产生协议偏差。
async def build_image_response(
    image_operation: Awaitable[ProxiedImage],
) -> Response:
    try:
        image = await image_operation
    except (EmptyImageAddressError, InvalidImagePathError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error
    except ImageNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        ImageBackendUnavailableError,
        ImageBackendResponseError,
        InvalidImageContentError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(error),
        ) from error

    return Response(
        content=image.content,
        status_code=status.HTTP_200_OK,
        headers={
            "Content-Type": image.media_type,
            "Cache-Control": f"private, max-age={image.cache_seconds}",
            "X-Content-Type-Options": "nosniff",
        },
    )


# 接收头像地址并调用固定头像中转用例，参数不能改变上游接口路径。
@router.get(
    "/avator",
    response_class=Response,
    summary="获取用户头像",
    responses={
        status.HTTP_200_OK: {
            "description": "头像文件",
            "content": {
                "image/jpeg": {},
                "image/png": {},
                "image/webp": {},
                "image/gif": {},
            },
        },
        status.HTTP_400_BAD_REQUEST: {"description": "头像地址为空或非法"},
        status.HTTP_404_NOT_FOUND: {"description": "头像文件不存在"},
        status.HTTP_502_BAD_GATEWAY: {"description": "客户端后端头像服务不可用"},
    },
)
async def get_avatar(
    client_backend: ClientBackend,
    avatar_url: Annotated[
        str,
        Query(
            max_length=255,
            description="头像相对地址，例如 /xxx.jpg",
        ),
    ],
) -> Response:
    return await build_image_response(
        get_avatar_image(
            client_backend,
            avatar_url,
            settings.image_cache_seconds,
        )
    )


# 接收项目图标地址并调用固定项目图标用例，统一应用图片缓存和内容防护。
@router.get(
    "/project_icon",
    response_class=Response,
    summary="获取项目图标",
    responses={
        status.HTTP_200_OK: {
            "description": "项目图标文件",
            "content": {
                "image/jpeg": {},
                "image/png": {},
                "image/webp": {},
                "image/gif": {},
            },
        },
        status.HTTP_400_BAD_REQUEST: {"description": "项目图标地址为空或非法"},
        status.HTTP_404_NOT_FOUND: {"description": "项目图标文件不存在"},
        status.HTTP_502_BAD_GATEWAY: {
            "description": "客户端后端项目图标服务不可用"
        },
    },
)
async def get_project_icon(
    client_backend: ClientBackend,
    icon_url: Annotated[
        str,
        Query(
            max_length=255,
            description="项目图标相对地址，例如 /xxx.png",
        ),
    ],
) -> Response:
    return await build_image_response(
        get_project_icon_image(
            client_backend,
            icon_url,
            settings.image_cache_seconds,
        )
    )


# 接收商品图片地址并调用固定商品图片用例，不允许参数改变上游接口路径。
@router.get(
    "/product",
    response_class=Response,
    summary="获取商品图片",
    responses={
        status.HTTP_200_OK: {
            "description": "商品图片文件",
            "content": {
                "image/jpeg": {},
                "image/png": {},
                "image/webp": {},
                "image/gif": {},
            },
        },
        status.HTTP_400_BAD_REQUEST: {"description": "商品图片地址为空或非法"},
        status.HTTP_404_NOT_FOUND: {"description": "商品图片文件不存在"},
        status.HTTP_502_BAD_GATEWAY: {
            "description": "客户端后端商品图片服务不可用"
        },
    },
)
async def get_product(
    client_backend: ClientBackend,
    image_url: Annotated[
        str,
        Query(
            max_length=255,
            description="商品图片相对地址，例如 /Keep 弹力带.jpg",
        ),
    ],
) -> Response:
    return await build_image_response(
        get_product_image(
            client_backend,
            image_url,
            settings.image_cache_seconds,
        )
    )


# 接收凭证记录主键并中转对应图片，不要求前端提供用户、赛季或文件路径。
@router.get(
    "/proof_record/{proof_record_id}",
    response_class=Response,
    summary="获取运动凭证图片",
    responses={
        status.HTTP_200_OK: {
            "description": "运动凭证图片文件",
            "content": {
                "image/jpeg": {},
                "image/png": {},
                "image/webp": {},
                "image/gif": {},
            },
        },
        status.HTTP_400_BAD_REQUEST: {"description": "凭证图片路径非法"},
        status.HTTP_404_NOT_FOUND: {
            "description": "凭证、所属赛季或图片文件不存在"
        },
        status.HTTP_502_BAD_GATEWAY: {
            "description": "客户端后端凭证图片服务不可用"
        },
    },
)
async def get_proof_record(
    client_backend: ClientBackend,
    proof_record_id: Annotated[
        int,
        Path(gt=0, description="待查看的有效凭证记录 ID"),
    ],
) -> Response:
    return await build_image_response(
        get_proof_record_image(
            client_backend,
            proof_record_id,
            settings.image_cache_seconds,
        )
    )
