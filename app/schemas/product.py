"""定义积分商城商品与礼品履约接口的数据结构。"""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


class PendingGiftDistributionResponse(BaseModel):
    id: int
    user_id: str
    product_id: int
    description: str | None
    created_at: datetime


class ProductInformationResponse(BaseModel):
    name: str
    description: str | None
    image_url: str | None


class ProductDetailsResponse(BaseModel):
    id: int
    name: str
    description: str | None
    points_required: int
    image_url: str | None
    status: Literal[0, 1]


class CreateProductRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
    ]
    points_required: int = Field(
        strict=True,
        ge=0,
        le=4_294_967_295,
    )
    description: Annotated[
        str,
        StringConstraints(strip_whitespace=True, max_length=255),
    ] | None = None

    # 将缺省、空字符串和纯空白商品描述统一保存为空值，避免产生无意义文本。
    @field_validator("description", mode="before")
    @classmethod
    def normalize_empty_description(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class UpdateProductVisibilityStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: int = Field(strict=True, ge=0, le=1)


class UpdateProductBasicInformationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
    ] | None = None
    description: Annotated[
        str,
        StringConstraints(strip_whitespace=True, max_length=255),
    ] | None = None
    points_required: int | None = Field(
        default=None,
        strict=True,
        ge=0,
        le=4_294_967_295,
    )
    # 区分字段缺省与显式清空描述，并拒绝名称和积分显式传入 null。
    @model_validator(mode="after")
    def validate_patch_fields(self) -> Self:
        submitted_fields = self.model_fields_set
        for field_name in ("name", "points_required"):
            if (
                field_name in submitted_fields
                and getattr(self, field_name) is None
            ):
                raise ValueError(f"{field_name} 不能为 null")
        return self


class GiftDistributionRequest(BaseModel):
    id: int = Field(gt=0, description="待审核的积分兑换流水 ID")
    decision: Literal["distributed", "rejected"] = "distributed"


class GiftDistributionResponse(BaseModel):
    id: int
    gift_distribution_status: Literal["distributed", "rejected"]
