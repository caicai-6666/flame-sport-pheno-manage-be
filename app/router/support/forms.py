"""提供 multipart 表单中的 JSON 解析与请求模型校验。"""

from typing import TypeVar

from fastapi import HTTPException, status
from pydantic import TypeAdapter, ValidationError

from app.schemas.product import (
    CreateProductRequest,
    UpdateProductBasicInformationRequest,
)


ParsedFormValue = TypeVar("ParsedFormValue")


# 将 multipart 中的 JSON 字符串解析为强类型对象，并返回字段级安全校验提示。
def parse_json_form_field(
    raw_value: str,
    adapter: TypeAdapter[ParsedFormValue],
    field_name: str,
) -> ParsedFormValue:
    try:
        return adapter.validate_json(raw_value)
    except (ValidationError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field_name} 必须是符合接口定义的 JSON 字符串",
        ) from error


# 解析 multipart 中的商品 JSON 补丁，并把格式错误转换为稳定的字段级响应。
def parse_product_update_form(
    raw_product: str | None,
) -> UpdateProductBasicInformationRequest:
    if raw_product is None:
        return UpdateProductBasicInformationRequest()
    try:
        return UpdateProductBasicInformationRequest.model_validate_json(
            raw_product
        )
    except (ValidationError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="product 必须是符合接口定义的 JSON 字符串",
        ) from error


# 校验奖品新增表单的标量字段，并把纯空白描述统一转换为空值。
def validate_product_creation_form(
    name: str,
    points_required: int,
    description: str | None,
) -> CreateProductRequest:
    try:
        return CreateProductRequest.model_validate(
            {
                "name": name,
                "points_required": points_required,
                "description": description,
            }
        )
    except ValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="奖品名称、兑换积分或描述不符合字段约束",
        ) from error

