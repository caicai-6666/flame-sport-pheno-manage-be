"""编排管理端商品查询维护、礼品发放审核与拒绝退款用例。"""

from dataclasses import dataclass
from io import BytesIO
from typing import Literal
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from sqlmodel.ext.asyncio.session import AsyncSession

from app.clients.client_backend import ClientBackendClient
from app.core.config import get_settings
from app.repositories.products import (
    GiftDistributionRecord,
    LatestPointRecord,
    PendingGiftDistribution,
    ProductDetails,
    ProductInformation,
    fetch_all_products,
    fetch_gift_distribution_for_update,
    fetch_gift_distribution_user_id,
    fetch_latest_point_record_for_update,
    fetch_pending_gift_distributions,
    fetch_product_information,
    insert_product,
    is_product_image_referenced_by_other_products,
    insert_exchange_refund_point_record,
    lock_user_for_point_update,
    reject_gift_distribution,
    update_product_basic_information as update_product_basic_information_repository,
    update_product_visibility_status as update_product_status_repository,
    update_gift_distribution_status,
)
from app.services.configuration_guard import (
    ensure_active_season_configuration_editable,
)
from app.services.images import replace_product_image

settings = get_settings()

GiftDistributionDecision = Literal["distributed", "rejected"]
REJECTED_DISTRIBUTION_DESCRIPTION = "发放失败，请联系管理员"
EXCHANGE_REFUND_DESCRIPTION = "礼品拒绝发放，退还兑换积分"


class ProductNotFoundError(RuntimeError):
    """指定奖品不存在。"""


class GiftDistributionNotFoundError(RuntimeError):
    """指定积分流水不存在。"""


class InvalidGiftDistributionRecordError(RuntimeError):
    """积分流水不是允许发放礼品的有效商品兑换记录。"""


class GiftDistributionStatusConflictError(RuntimeError):
    """礼品发放状态不属于已知状态，不能安全覆盖。"""


class PointBalanceConsistencyError(RuntimeError):
    """兑换扣分或用户最新积分流水不完整，拒绝退款必须回滚。"""


class InvalidProductImageMediaTypeError(ValueError):
    """奖品图片声明的媒体类型不是允许的 WebP。"""


class InvalidProductImageContentError(ValueError):
    """奖品图片为空或真实内容无法解码为 WebP。"""


class ProductImageSizeExceededError(ValueError):
    """奖品图片超过客户端后端允许的五 MiB 上限。"""


@dataclass(frozen=True, slots=True)
class GiftDistributionResult:
    id: int
    gift_distribution_status: GiftDistributionDecision


@dataclass(frozen=True, slots=True)
class ProductBasicInformationPatch:
    update_name: bool = False
    name: str | None = None
    update_description: bool = False
    description: str | None = None
    update_points_required: bool = False
    points_required: int | None = None
    image: "ProductImageUpload | None" = None


@dataclass(frozen=True, slots=True)
class ProductImageUpload:
    content: bytes
    media_type: str | None


@dataclass(frozen=True, slots=True)
class ProductCreation:
    name: str
    points_required: int
    description: str | None
    image: ProductImageUpload


PRODUCT_IMAGE_FORMAT = "WEBP"
PRODUCT_IMAGE_MEDIA_TYPE = "image/webp"
PRODUCT_IMAGE_EXTENSION = "webp"
MAX_PRODUCT_IMAGE_SIZE_BYTES = 5 * 1024 * 1024


# 校验图片大小、声明媒体类型与真实格式均为 WebP，拒绝仅修改扩展名的伪装文件。
def validate_product_image_upload(upload: ProductImageUpload) -> str:
    if not upload.content:
        raise InvalidProductImageContentError
    if len(upload.content) > MAX_PRODUCT_IMAGE_SIZE_BYTES:
        raise ProductImageSizeExceededError
    normalized_media_type = (
        upload.media_type or ""
    ).partition(";")[0].strip().lower()
    if normalized_media_type != PRODUCT_IMAGE_MEDIA_TYPE:
        raise InvalidProductImageMediaTypeError
    try:
        with Image.open(BytesIO(upload.content)) as image:
            image_format = image.format
            if image_format != PRODUCT_IMAGE_FORMAT:
                raise InvalidProductImageContentError
            image.verify()
    except (InvalidProductImageContentError, InvalidProductImageMediaTypeError):
        raise
    except Image.DecompressionBombError as error:
        raise InvalidProductImageContentError from error
    except (UnidentifiedImageError, OSError, SyntaxError) as error:
        raise InvalidProductImageContentError from error
    return PRODUCT_IMAGE_EXTENSION


# 为每次奖品图片上传生成不复用的相对地址，避免浏览器继续命中旧图片缓存。
def generate_product_image_url(extension: str) -> str:
    return f"/product-{uuid4().hex}.{extension}"


# 先提交默认上架的奖品记录，再复用客户端覆盖接口落盘 WebP 图片。
async def create_product(
    session: AsyncSession,
    client_backend: ClientBackendClient,
    creation: ProductCreation,
) -> ProductDetails:
    image_extension = validate_product_image_upload(creation.image)
    image_url = generate_product_image_url(image_extension)

    async with session.begin():
        product = await insert_product(
            session,
            creation.name,
            creation.description,
            creation.points_required,
            image_url,
        )

    await replace_product_image(
        client_backend,
        None,
        image_url,
        creation.image.content,
        PRODUCT_IMAGE_MEDIA_TYPE,
    )
    return product


# 在显式只读事务中查询待发放礼品，保持事务边界与 HTTP 协议解耦。
async def list_pending_gift_distributions(
    session: AsyncSession,
) -> tuple[PendingGiftDistribution, ...]:
    async with session.begin():
        return await fetch_pending_gift_distributions(session)


# 在只读事务中查询奖品信息，并把空结果转换为稳定的应用异常。
async def get_product_information(
    session: AsyncSession,
    product_id: int,
) -> ProductInformation:
    async with session.begin():
        product = await fetch_product_information(session, product_id)
    if product is None:
        raise ProductNotFoundError
    return product


# 在显式只读事务中获取全部商品及完整字段，不按上下架状态隐藏记录。
async def list_products(
    session: AsyncSession,
) -> tuple[ProductDetails, ...]:
    async with session.begin():
        return await fetch_all_products(session)


# 在商品行锁保护下切换上下架状态，重复提交相同状态保持幂等成功。
async def update_product_visibility_status(
    session: AsyncSession,
    product_id: int,
    visibility_status: int,
) -> ProductDetails:
    async with session.begin():
        product = await update_product_status_repository(
            session,
            product_id,
            visibility_status,
        )
        if product is None:
            raise ProductNotFoundError
        return product


# 先提交商品字段更新，再上传 WebP 新图并清理旧图；积分字段单独受配置窗口保护。
async def update_product_basic_information(
    session: AsyncSession,
    client_backend: ClientBackendClient,
    product_id: int,
    patch: ProductBasicInformationPatch,
    edit_window_hours: int = (
        settings.active_season_config_edit_window_hours
    ),
) -> ProductDetails:
    new_image_url = None
    if patch.image is not None:
        image_extension = validate_product_image_upload(patch.image)
        new_image_url = generate_product_image_url(image_extension)

    async with session.begin():
        if patch.update_points_required:
            await ensure_active_season_configuration_editable(
                session,
                edit_window_hours,
            )
        update_result = await update_product_basic_information_repository(
            session,
            product_id,
            update_name=patch.update_name,
            name=patch.name,
            update_description=patch.update_description,
            description=patch.description,
            update_points_required=patch.update_points_required,
            points_required=patch.points_required,
            update_image_url=patch.image is not None,
            image_url=new_image_url,
        )
        if update_result is None:
            raise ProductNotFoundError

    if patch.image is None or not update_result.image_changed:
        return update_result.product

    old_image_url_for_replacement = update_result.previous_image_url
    if old_image_url_for_replacement is not None:
        async with session.begin():
            old_image_is_shared = (
                await is_product_image_referenced_by_other_products(
                    session,
                    product_id,
                    old_image_url_for_replacement,
                )
            )
        if old_image_is_shared:
            old_image_url_for_replacement = None

    new_image_url = update_result.product.image_url
    if new_image_url is None:
        return update_result.product
    await replace_product_image(
        client_backend,
        old_image_url_for_replacement,
        new_image_url,
        patch.image.content,
        PRODUCT_IMAGE_MEDIA_TYPE,
    )
    return update_result.product


# 校验目标流水属于有效商品兑换；拒绝分支会继续检查原始扣分值。
def validate_gift_distribution_record(
    distribution: GiftDistributionRecord | None,
) -> GiftDistributionRecord:
    if distribution is None:
        raise GiftDistributionNotFoundError
    if (
        distribution.change_type != "exchange"
        or distribution.product_id is None
        or distribution.status != 1
    ):
        raise InvalidGiftDistributionRecordError
    return distribution


# 根据目标结论校验终态幂等或冲突，禁止已发放与已拒绝之间互相覆盖。
def resolve_terminal_distribution_status(
    distribution: GiftDistributionRecord,
    decision: GiftDistributionDecision,
) -> GiftDistributionResult | None:
    current_status = distribution.gift_distribution_status
    if current_status == decision:
        return GiftDistributionResult(distribution.id, decision)
    if current_status != "pending":
        raise GiftDistributionStatusConflictError
    return None


# 拒绝礼品时按最新余额补回原兑换积分，并在同一事务内保存终态和退款流水。
async def reject_and_refund_gift_distribution(
    session: AsyncSession,
    distribution: GiftDistributionRecord,
    latest_point_record: LatestPointRecord,
) -> GiftDistributionResult:
    refund_points = -distribution.change_points
    product_id = distribution.product_id
    if refund_points < 0 or product_id is None:
        raise PointBalanceConsistencyError
    points_after = latest_point_record.points_after + refund_points
    await reject_gift_distribution(
        session,
        distribution.id,
        REJECTED_DISTRIBUTION_DESCRIPTION,
    )
    await insert_exchange_refund_point_record(
        session,
        distribution.user_id,
        product_id,
        refund_points,
        points_after,
        EXCHANGE_REFUND_DESCRIPTION,
    )
    return GiftDistributionResult(distribution.id, "rejected")


# 在单一事务内处理发放或拒绝；拒绝时按用户级锁顺序生成唯一退款流水。
async def process_gift_distribution(
    session: AsyncSession,
    point_record_id: int,
    decision: GiftDistributionDecision,
) -> GiftDistributionResult:
    async with session.begin():
        if decision == "distributed":
            distribution = validate_gift_distribution_record(
                await fetch_gift_distribution_for_update(
                    session,
                    point_record_id,
                )
            )
            terminal_result = resolve_terminal_distribution_status(
                distribution,
                decision,
            )
            if terminal_result is not None:
                return terminal_result
            await update_gift_distribution_status(session, point_record_id)
            return GiftDistributionResult(point_record_id, "distributed")

        user_id = await fetch_gift_distribution_user_id(
            session,
            point_record_id,
        )
        if user_id is None:
            raise GiftDistributionNotFoundError
        if not await lock_user_for_point_update(session, user_id):
            raise PointBalanceConsistencyError
        distribution = validate_gift_distribution_record(
            await fetch_gift_distribution_for_update(
                session,
                point_record_id,
            )
        )
        if distribution.user_id != user_id:
            raise PointBalanceConsistencyError
        terminal_result = resolve_terminal_distribution_status(
            distribution,
            decision,
        )
        if terminal_result is not None:
            return terminal_result
        latest_point_record = await fetch_latest_point_record_for_update(
            session,
            user_id,
        )
        if latest_point_record is None:
            raise PointBalanceConsistencyError
        return await reject_and_refund_gift_distribution(
            session,
            distribution,
            latest_point_record,
        )
