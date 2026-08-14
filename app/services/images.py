"""编排客户端后端图片读取、项目图标上传与商品图片替换。"""

from dataclasses import dataclass
from typing import NoReturn

import httpx

from app.clients.client_backend import ClientBackendClient

ALLOWED_IMAGE_MEDIA_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif"}
)


class ImageServiceError(RuntimeError):
    """图片中转用例的可预期失败基类。"""


class EmptyImageAddressError(ImageServiceError):
    """图片地址为空或只有空白。"""


class InvalidImagePathError(ImageServiceError):
    """客户端后端拒绝了非法图片路径。"""


class ImageNotFoundError(ImageServiceError):
    """客户端后端未找到目标图片。"""


class ImageBackendUnavailableError(ImageServiceError):
    """客户端后端网络连接失败或超时。"""


class ImageBackendResponseError(ImageServiceError):
    """客户端后端返回未约定的异常状态。"""


class InvalidImageContentError(ImageServiceError):
    """客户端后端返回的内容不是允许的图片格式。"""


class ProjectIconUploadError(ImageServiceError):
    """项目图标上传失败的可预期异常基类。"""


class InvalidProjectIconUploadError(ProjectIconUploadError):
    """客户端后端拒绝了项目图标内容或存储参数。"""


class ProjectIconUploadTooLargeError(ProjectIconUploadError):
    """项目图标超过客户端后端允许的大小。"""


class ProjectIconUploadBackendUnavailableError(ProjectIconUploadError):
    """客户端后端项目图标上传服务连接失败或超时。"""


class ProjectIconUploadBackendResponseError(ProjectIconUploadError):
    """客户端后端返回了未约定的图标上传响应。"""


class ProductImageReplacementError(ImageServiceError):
    """客户端后端未能完成商品图片替换。"""


class ProductImageReplacementBackendUnavailableError(
    ProductImageReplacementError
):
    """客户端后端商品图片替换服务连接失败或超时。"""


@dataclass(frozen=True, slots=True)
class ProxiedImage:
    content: bytes
    media_type: str
    cache_seconds: int


@dataclass(frozen=True, slots=True)
class ImageResourceDefinition:
    empty_detail: str
    invalid_path_detail: str
    not_found_detail: str
    unavailable_detail: str
    response_error_detail: str
    invalid_content_detail: str


@dataclass(frozen=True, slots=True)
class UploadedProjectIcon:
    icon_url: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ReplacedProductImage:
    image_url: str
    size_bytes: int
    old_image_removed: bool


AVATAR_RESOURCE = ImageResourceDefinition(
    empty_detail="头像地址不能为空",
    invalid_path_detail="头像路径非法",
    not_found_detail="头像文件不存在",
    unavailable_detail="客户端后端头像服务不可用",
    response_error_detail="客户端后端头像服务响应异常",
    invalid_content_detail="客户端后端返回了无效的头像内容",
)

PROJECT_ICON_RESOURCE = ImageResourceDefinition(
    empty_detail="项目图标路径不能为空",
    invalid_path_detail="项目图标路径非法",
    not_found_detail="项目图标文件不存在",
    unavailable_detail="客户端后端项目图标服务不可用",
    response_error_detail="客户端后端项目图标服务响应异常",
    invalid_content_detail="客户端后端返回了无效的项目图标内容",
)

PRODUCT_IMAGE_RESOURCE = ImageResourceDefinition(
    empty_detail="商品图片路径不能为空",
    invalid_path_detail="商品图片路径非法",
    not_found_detail="商品图片文件不存在",
    unavailable_detail="客户端后端商品图片服务不可用",
    response_error_detail="客户端后端商品图片服务响应异常",
    invalid_content_detail="客户端后端返回了无效的商品图片内容",
)

PROOF_RECORD_RESOURCE = ImageResourceDefinition(
    empty_detail="凭证记录 ID 无效",
    invalid_path_detail="凭证图片路径非法",
    not_found_detail="凭证不存在",
    unavailable_detail="客户端后端凭证图片服务不可用",
    response_error_detail="客户端后端凭证图片服务响应异常",
    invalid_content_detail="客户端后端返回了无效的凭证图片内容",
)


# 提取客户端后端约定的安全错误提示，异常响应格式使用固定兜底文案。
def get_upstream_error_detail(
    response: httpx.Response,
    fallback_detail: str,
) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return fallback_detail
    return detail if isinstance(detail, str) and detail else fallback_detail


# 按固定资源定义转换上游状态异常，保留安全业务提示并隐藏未知响应内容。
def raise_image_upstream_error(
    error: httpx.HTTPStatusError,
    resource: ImageResourceDefinition,
) -> NoReturn:
    if error.response.status_code == 400:
        raise InvalidImagePathError(
            get_upstream_error_detail(
                error.response,
                resource.invalid_path_detail,
            )
        ) from error
    if error.response.status_code == 404:
        raise ImageNotFoundError(
            get_upstream_error_detail(
                error.response,
                resource.not_found_detail,
            )
        ) from error
    raise ImageBackendResponseError(resource.response_error_detail) from error


# 请求一个由服务层确定的客户端图片路径，统一执行状态与媒体类型校验。
async def request_proxied_image(
    client_backend: ClientBackendClient,
    upstream_path: str,
    cache_seconds: int,
    resource: ImageResourceDefinition,
    params: dict[str, str] | None = None,
) -> ProxiedImage:
    try:
        upstream_response = await client_backend.request(
            "GET",
            upstream_path,
            params=params,
            headers={"Accept": "image/*"},
        )
    except httpx.HTTPStatusError as error:
        raise_image_upstream_error(error, resource)
    except httpx.RequestError as error:
        raise ImageBackendUnavailableError(resource.unavailable_detail) from error

    content_type = upstream_response.headers.get("content-type", "")
    media_type = content_type.partition(";")[0].strip().lower()
    if media_type not in ALLOWED_IMAGE_MEDIA_TYPES:
        raise InvalidImageContentError(resource.invalid_content_detail)

    return ProxiedImage(
        content=upstream_response.content,
        media_type=media_type,
        cache_seconds=cache_seconds,
    )


# 校验图片地址后向固定客户端接口中转，调用方不能通过地址改变上游路径。
async def get_proxied_image(
    client_backend: ClientBackendClient,
    image_url: str,
    cache_seconds: int,
    resource: ImageResourceDefinition,
    upstream_path: str,
    query_parameter: str,
) -> ProxiedImage:
    normalized_image_url = image_url.strip()
    if not normalized_image_url:
        raise EmptyImageAddressError(resource.empty_detail)

    return await request_proxied_image(
        client_backend,
        upstream_path,
        cache_seconds,
        resource,
        params={query_parameter: normalized_image_url},
    )


# 从固定客户端接口读取头像，校验安全媒体类型并返回带统一缓存时效的图片结果。
async def get_avatar_image(
    client_backend: ClientBackendClient,
    avatar_url: str,
    cache_seconds: int,
) -> ProxiedImage:
    return await get_proxied_image(
        client_backend,
        avatar_url,
        cache_seconds,
        AVATAR_RESOURCE,
        "/avator",
        "avatar_url",
    )


# 从固定客户端接口读取项目图标，并应用与其他图片一致的安全校验和缓存时效。
async def get_project_icon_image(
    client_backend: ClientBackendClient,
    icon_url: str,
    cache_seconds: int,
) -> ProxiedImage:
    return await get_proxied_image(
        client_backend,
        icon_url,
        cache_seconds,
        PROJECT_ICON_RESOURCE,
        "/project_icon",
        "icon_url",
    )


# 将 WebP 项目图标上传到固定客户端接口，并校验返回地址与大小。
async def upload_project_icon(
    client_backend: ClientBackendClient,
    icon_url: str,
    image_content: bytes,
) -> UploadedProjectIcon:
    try:
        upstream_response = await client_backend.request(
            "POST",
            "/project_icon",
            data={"icon_url": icon_url},
            files={
                "image": (
                    icon_url.rsplit("/", maxsplit=1)[-1],
                    image_content,
                    "image/webp",
                )
            },
        )
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 400:
            raise InvalidProjectIconUploadError(
                get_upstream_error_detail(
                    error.response,
                    "项目图标内容或存储地址非法",
                )
            ) from error
        if error.response.status_code == 413:
            raise ProjectIconUploadTooLargeError(
                get_upstream_error_detail(
                    error.response,
                    "项目图标不能超过 5 MiB",
                )
            ) from error
        raise ProjectIconUploadBackendResponseError(
            "客户端后端项目图标上传服务响应异常"
        ) from error
    except httpx.RequestError as error:
        raise ProjectIconUploadBackendUnavailableError(
            "客户端后端项目图标上传服务不可用"
        ) from error

    if upstream_response.status_code != 201:
        raise ProjectIconUploadBackendResponseError(
            "客户端后端项目图标上传服务响应异常"
        )
    try:
        response_payload = upstream_response.json()
        uploaded_icon_url = response_payload["icon_url"]
        size_bytes = response_payload["size_bytes"]
    except (ValueError, KeyError, TypeError) as error:
        raise ProjectIconUploadBackendResponseError(
            "客户端后端项目图标上传服务响应异常"
        ) from error
    if (
        uploaded_icon_url != icon_url
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes != len(image_content)
    ):
        raise ProjectIconUploadBackendResponseError(
            "客户端后端项目图标上传服务响应异常"
        )
    return UploadedProjectIcon(
        icon_url=uploaded_icon_url,
        size_bytes=size_bytes,
    )


# 从固定客户端接口读取商品图片，不查询商品表并统一执行内容与缓存校验。
async def get_product_image(
    client_backend: ClientBackendClient,
    image_url: str,
    cache_seconds: int,
) -> ProxiedImage:
    return await get_proxied_image(
        client_backend,
        image_url,
        cache_seconds,
        PRODUCT_IMAGE_RESOURCE,
        "/product",
        "image_url",
    )


# 在数据库修改完成后上传 WebP 新图并按需清理旧图，禁止提前产生外部副作用。
async def replace_product_image(
    client_backend: ClientBackendClient,
    old_image_url: str | None,
    new_image_url: str,
    image_content: bytes,
    image_media_type: str | None,
) -> ReplacedProductImage:
    form_data = {"new_image_url": new_image_url}
    if old_image_url is not None:
        form_data["old_image_url"] = old_image_url
    try:
        upstream_response = await client_backend.request(
            "POST",
            "/product/replace",
            data=form_data,
            files={
                "image": (
                    new_image_url.rsplit("/", maxsplit=1)[-1],
                    image_content,
                    image_media_type or "application/octet-stream",
                )
            },
        )
    except httpx.HTTPStatusError as error:
        detail = get_upstream_error_detail(
            error.response,
            "客户端后端拒绝替换商品图片",
        )
        raise ProductImageReplacementError(detail) from error
    except httpx.RequestError as error:
        raise ProductImageReplacementBackendUnavailableError(
            "客户端后端商品图片替换服务不可用"
        ) from error

    try:
        response_payload = upstream_response.json()
        replaced_image_url = response_payload["image_url"]
        size_bytes = response_payload["size_bytes"]
        old_image_removed = response_payload["old_image_removed"]
    except (ValueError, KeyError, TypeError) as error:
        raise ProductImageReplacementError(
            "客户端后端商品图片替换服务响应异常"
        ) from error
    if (
        replaced_image_url != new_image_url
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes != len(image_content)
        or not isinstance(old_image_removed, bool)
    ):
        raise ProductImageReplacementError(
            "客户端后端商品图片替换服务响应异常"
        )
    return ReplacedProductImage(
        image_url=replaced_image_url,
        size_bytes=size_bytes,
        old_image_removed=old_image_removed,
    )


# 按凭证记录 ID 请求固定客户端路径，由客户端校验记录有效性、赛季关联和文件安全。
async def get_proof_record_image(
    client_backend: ClientBackendClient,
    proof_record_id: int,
    cache_seconds: int,
) -> ProxiedImage:
    return await request_proxied_image(
        client_backend,
        f"/proof_record/{proof_record_id}",
        cache_seconds,
        PROOF_RECORD_RESOURCE,
    )
