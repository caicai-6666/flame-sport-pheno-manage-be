"""封装管理端商品与礼品查询、审核加锁、状态更新及退款写入。"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession


@dataclass(frozen=True, slots=True)
class PendingGiftDistribution:
    id: int
    user_id: str
    product_id: int
    description: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ProductInformation:
    name: str
    description: str | None
    image_url: str | None


@dataclass(frozen=True, slots=True)
class ProductDetails:
    id: int
    name: str
    description: str | None
    points_required: int
    image_url: str | None
    status: int


@dataclass(frozen=True, slots=True)
class ProductBasicInformationUpdate:
    product: ProductDetails
    previous_image_url: str | None
    image_changed: bool


@dataclass(frozen=True, slots=True)
class GiftDistributionRecord:
    id: int
    user_id: str
    change_type: str
    product_id: int | None
    change_points: int
    status: int
    gift_distribution_status: str


@dataclass(frozen=True, slots=True)
class LatestPointRecord:
    id: int
    points_after: int


# 新增默认上架的奖品记录，并返回数据库生成主键及全部基础字段。
async def insert_product(
    session: AsyncSession,
    name: str,
    description: str | None,
    points_required: int,
    image_url: str,
) -> ProductDetails:
    result = await session.exec(
        text(
            """
            INSERT INTO product (
                name,
                description,
                points_required,
                image_url,
                status
            ) VALUES (
                :name,
                :description,
                :points_required,
                :image_url,
                1
            )
            """
        ),
        params={
            "name": name,
            "description": description,
            "points_required": points_required,
            "image_url": image_url,
        },
    )
    return ProductDetails(
        id=int(result.lastrowid),
        name=name,
        description=description,
        points_required=points_required,
        image_url=image_url,
        status=1,
    )


# 查询有效且商品关联完整的待发放兑换流水，并按兑换时间和主键稳定返回。
async def fetch_pending_gift_distributions(
    session: AsyncSession,
) -> tuple[PendingGiftDistribution, ...]:
    result = await session.exec(
        text(
            """
            SELECT
                point_record.id,
                point_record.user_id,
                point_record.product_id,
                point_record.description,
                point_record.created_at
            FROM point_record
            WHERE point_record.change_type = 'exchange'
              AND point_record.gift_distribution_status = 'pending'
              AND point_record.status = 1
              AND point_record.product_id IS NOT NULL
            ORDER BY
                point_record.created_at ASC,
                point_record.id ASC
            """
        )
    )
    return tuple(
        PendingGiftDistribution(
            id=int(row["id"]),
            user_id=str(row["user_id"]),
            product_id=int(row["product_id"]),
            description=(
                str(row["description"])
                if row["description"] is not None
                else None
            ),
            created_at=row["created_at"],
        )
        for row in result.mappings().all()
    )


# 按奖品主键查询管理端展示信息，不按上下架状态过滤以保留历史兑换可读性。
async def fetch_product_information(
    session: AsyncSession,
    product_id: int,
) -> ProductInformation | None:
    result = await session.exec(
        text(
            """
            SELECT
                product.name,
                product.description,
                product.image_url
            FROM product
            WHERE product.id = :product_id
            LIMIT 1
            """
        ),
        params={"product_id": product_id},
    )
    row = result.mappings().first()
    if row is None:
        return None
    return ProductInformation(
        name=str(row["name"]),
        description=(
            str(row["description"])
            if row["description"] is not None
            else None
        ),
        image_url=(
            str(row["image_url"])
            if row["image_url"] is not None
            else None
        ),
    )


# 查询全部商品字段并保留上下架状态，按主键返回稳定的管理端列表。
async def fetch_all_products(
    session: AsyncSession,
) -> tuple[ProductDetails, ...]:
    result = await session.exec(
        text(
            """
            SELECT
                product.id,
                product.name,
                product.description,
                product.points_required,
                product.image_url,
                product.status
            FROM product
            ORDER BY product.id ASC
            """
        )
    )
    return tuple(
        ProductDetails(
            id=int(row["id"]),
            name=str(row["name"]),
            description=(
                str(row["description"])
                if row["description"] is not None
                else None
            ),
            points_required=int(row["points_required"]),
            image_url=(
                str(row["image_url"])
                if row["image_url"] is not None
                else None
            ),
            status=int(row["status"]),
        )
        for row in result.mappings().all()
    )


# 锁定目标商品后覆盖上下架状态，并返回更新后的完整商品信息。
async def update_product_visibility_status(
    session: AsyncSession,
    product_id: int,
    visibility_status: int,
) -> ProductDetails | None:
    result = await session.exec(
        text(
            """
            SELECT
                product.id,
                product.name,
                product.description,
                product.points_required,
                product.image_url
            FROM product
            WHERE product.id = :product_id
            FOR UPDATE
            """
        ),
        params={"product_id": product_id},
    )
    row = result.mappings().first()
    if row is None:
        return None

    await session.exec(
        text(
            """
            UPDATE product
            SET status = :visibility_status
            WHERE id = :product_id
            """
        ),
        params={
            "product_id": product_id,
            "visibility_status": visibility_status,
        },
    )
    return ProductDetails(
        id=int(row["id"]),
        name=str(row["name"]),
        description=(
            str(row["description"])
            if row["description"] is not None
            else None
        ),
        points_required=int(row["points_required"]),
        image_url=(
            str(row["image_url"])
            if row["image_url"] is not None
            else None
        ),
        status=visibility_status,
    )


# 锁定目标商品并按显式字段标记局部更新基础信息，同时保留旧图片地址供事务提交后清理。
async def update_product_basic_information(
    session: AsyncSession,
    product_id: int,
    *,
    update_name: bool,
    name: str | None,
    update_description: bool,
    description: str | None,
    update_points_required: bool,
    points_required: int | None,
    update_image_url: bool,
    image_url: str | None,
) -> ProductBasicInformationUpdate | None:
    result = await session.exec(
        text(
            """
            SELECT
                product.id,
                product.name,
                product.description,
                product.points_required,
                product.image_url,
                product.status
            FROM product
            WHERE product.id = :product_id
            FOR UPDATE
            """
        ),
        params={"product_id": product_id},
    )
    row = result.mappings().first()
    if row is None:
        return None

    previous_image_url = (
        str(row["image_url"])
        if row["image_url"] is not None
        else None
    )
    effective_name = name if update_name else str(row["name"])
    effective_description = (
        description
        if update_description
        else (
            str(row["description"])
            if row["description"] is not None
            else None
        )
    )
    effective_points_required = (
        points_required
        if update_points_required
        else int(row["points_required"])
    )
    effective_image_url = image_url if update_image_url else previous_image_url

    await session.exec(
        text(
            """
            UPDATE product
            SET
                name = :name,
                description = :description,
                points_required = :points_required,
                image_url = :image_url
            WHERE id = :product_id
            """
        ),
        params={
            "product_id": product_id,
            "name": effective_name,
            "description": effective_description,
            "points_required": effective_points_required,
            "image_url": effective_image_url,
        },
    )
    product = ProductDetails(
        id=int(row["id"]),
        name=str(effective_name),
        description=effective_description,
        points_required=int(effective_points_required),
        image_url=effective_image_url,
        status=int(row["status"]),
    )
    return ProductBasicInformationUpdate(
        product=product,
        previous_image_url=previous_image_url,
        image_changed=(
            update_image_url and effective_image_url != previous_image_url
        ),
    )


# 检查旧图片是否仍被其他商品引用，避免替换流程误删共享的客户端图片文件。
async def is_product_image_referenced_by_other_products(
    session: AsyncSession,
    product_id: int,
    image_url: str,
) -> bool:
    result = await session.exec(
        text(
            """
            SELECT product.id
            FROM product
            WHERE product.id <> :product_id
              AND product.image_url = :image_url
            LIMIT 1
            """
        ),
        params={
            "product_id": product_id,
            "image_url": image_url,
        },
    )
    return result.mappings().first() is not None


# 锁定指定积分流水并读取兑换有效性与发放状态，防止并发操作重复改变履约结果。
async def fetch_gift_distribution_for_update(
    session: AsyncSession,
    point_record_id: int,
) -> GiftDistributionRecord | None:
    result = await session.exec(
        text(
            """
            SELECT
                point_record.id,
                point_record.user_id,
                point_record.change_type,
                point_record.product_id,
                point_record.change_points,
                point_record.status,
                point_record.gift_distribution_status
            FROM point_record
            WHERE point_record.id = :point_record_id
            FOR UPDATE
            """
        ),
        params={"point_record_id": point_record_id},
    )
    row = result.mappings().first()
    if row is None:
        return None
    return GiftDistributionRecord(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        change_type=str(row["change_type"]),
        product_id=(
            int(row["product_id"])
            if row["product_id"] is not None
            else None
        ),
        change_points=int(row["change_points"]),
        status=int(row["status"]),
        gift_distribution_status=str(row["gift_distribution_status"]),
    )


# 在目标流水行锁保护下将礼品标记为已发放，禁止修改任何积分相关字段。
async def update_gift_distribution_status(
    session: AsyncSession,
    point_record_id: int,
) -> None:
    await session.exec(
        text(
            """
            UPDATE point_record
            SET gift_distribution_status = 'distributed'
            WHERE id = :point_record_id
            """
        ),
        params={"point_record_id": point_record_id},
    )


# 读取目标兑换流水所属用户，为拒绝分支建立统一的用户级积分写入锁顺序。
async def fetch_gift_distribution_user_id(
    session: AsyncSession,
    point_record_id: int,
) -> str | None:
    result = await session.exec(
        text(
            """
            SELECT point_record.user_id
            FROM point_record
            WHERE point_record.id = :point_record_id
            LIMIT 1
            """
        ),
        params={"point_record_id": point_record_id},
    )
    row = result.mappings().first()
    return str(row["user_id"]) if row is not None else None


# 锁定用户主记录以串行化同一用户的拒绝退款，避免多条兑换并发退款覆盖余额。
async def lock_user_for_point_update(
    session: AsyncSession,
    user_id: str,
) -> bool:
    result = await session.exec(
        text(
            """
            SELECT user_account.id
            FROM `user` AS user_account
            WHERE user_account.id = :user_id
            FOR UPDATE
            """
        ),
        params={"user_id": user_id},
    )
    return result.mappings().first() is not None


# 锁定用户最新有效积分流水，以其余额作为本次拒绝退款后的计算基准。
async def fetch_latest_point_record_for_update(
    session: AsyncSession,
    user_id: str,
) -> LatestPointRecord | None:
    result = await session.exec(
        text(
            """
            SELECT
                point_record.id,
                point_record.points_after
            FROM point_record
            WHERE point_record.user_id = :user_id
              AND point_record.status = 1
            ORDER BY
                point_record.created_at DESC,
                point_record.id DESC
            LIMIT 1
            FOR UPDATE
            """
        ),
        params={"user_id": user_id},
    )
    row = result.mappings().first()
    if row is None:
        return None
    return LatestPointRecord(
        id=int(row["id"]),
        points_after=int(row["points_after"]),
    )


# 将原兑换流水标记为拒绝并覆盖用户提示，积分退还由同一事务中的新流水表达。
async def reject_gift_distribution(
    session: AsyncSession,
    point_record_id: int,
    description: str,
) -> None:
    await session.exec(
        text(
            """
            UPDATE point_record
            SET
                gift_distribution_status = 'rejected',
                description = :description
            WHERE id = :point_record_id
            """
        ),
        params={
            "point_record_id": point_record_id,
            "description": description,
        },
    )


# 新增可审计的兑换退款流水，正向补回原兑换扣除的积分并保存最新余额。
async def insert_exchange_refund_point_record(
    session: AsyncSession,
    user_id: str,
    product_id: int,
    refund_points: int,
    points_after: int,
    description: str,
) -> None:
    await session.exec(
        text(
            """
            INSERT INTO point_record (
                user_id,
                product_id,
                change_type,
                change_points,
                points_after,
                description,
                status,
                gift_distribution_status
            ) VALUES (
                :user_id,
                :product_id,
                'exchange_refund',
                :refund_points,
                :points_after,
                :description,
                1,
                'pending'
            )
            """
        ),
        params={
            "user_id": user_id,
            "product_id": product_id,
            "refund_points": refund_points,
            "points_after": points_after,
            "description": description,
        },
    )
