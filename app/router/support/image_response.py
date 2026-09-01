"""统一把图片应用服务结果转换为安全 HTTP 响应。"""

from collections.abc import Awaitable

from fastapi import HTTPException, Response, status

from app.services.images import (
    EmptyImageAddressError,
    ImageBackendResponseError,
    ImageBackendUnavailableError,
    ImageNotFoundError,
    InvalidImageContentError,
    InvalidImagePathError,
    ProxiedImage,
)


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

