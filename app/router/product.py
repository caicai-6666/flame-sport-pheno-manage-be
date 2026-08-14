"""提供管理端积分商城商品查询、资料维护与礼品履约操作接口。"""

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
from pydantic import ValidationError

from app.db.session import DatabaseSession
from app.router.dependencies import ClientBackend
from app.schemas.product import (
    CreateProductRequest,
    GiftDistributionRequest,
    GiftDistributionResponse,
    PendingGiftDistributionResponse,
    ProductDetailsResponse,
    ProductInformationResponse,
    UpdateProductBasicInformationRequest,
    UpdateProductVisibilityStatusRequest,
)
from app.services.configuration_guard import (
    ActiveSeasonConfigurationWindowClosedError,
    MultipleActiveSeasonsForConfigurationError,
)
from app.services.images import ProductImageReplacementError
from app.services.products import (
    GiftDistributionNotFoundError,
    GiftDistributionStatusConflictError,
    InvalidGiftDistributionRecordError,
    PointBalanceConsistencyError,
    ProductBasicInformationPatch,
    ProductCreation,
    ProductImageSizeExceededError,
    ProductImageUpload,
    ProductNotFoundError,
    InvalidProductImageContentError,
    InvalidProductImageMediaTypeError,
    create_product as create_product_service,
    get_product_information,
    list_products,
    list_pending_gift_distributions,
    process_gift_distribution,
    update_product_basic_information as update_product_basic_information_service,
    update_product_visibility_status as update_product_visibility_status_service,
)

router = APIRouter(prefix="/product", tags=["product"])
MAX_PRODUCT_IMAGE_SIZE_BYTES = 5 * 1024 * 1024


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


# 限量读取并关闭奖品图片，供必填新增与可选修改场景安全复用。
async def read_product_image_file(image: UploadFile) -> bytes:
    try:
        return await image.read(MAX_PRODUCT_IMAGE_SIZE_BYTES + 1)
    finally:
        await image.close()


# 接收待发放礼品列表请求，并返回管理端履约所需字段与兑换时间。
@router.get(
    "/pending-distributions",
    response_model=list[PendingGiftDistributionResponse],
    summary="查询待发放礼品",
)
async def get_pending_gift_distributions(
    session: DatabaseSession,
) -> list[PendingGiftDistributionResponse]:
    distributions = await list_pending_gift_distributions(session)
    return [
        PendingGiftDistributionResponse.model_validate(
            distribution,
            from_attributes=True,
        )
        for distribution in distributions
    ]


# 返回商品表全部字段和上下架状态，管理前端可据此展示或筛选完整商品目录。
@router.get(
    "/list",
    response_model=list[ProductDetailsResponse],
    summary="获取全部商品列表",
)
async def get_product_list(
    session: DatabaseSession,
) -> list[ProductDetailsResponse]:
    products = await list_products(session)
    return [
        ProductDetailsResponse.model_validate(
            product,
            from_attributes=True,
        )
        for product in products
    ]


# 创建默认上架的奖品，并在数据库提交后通过客户端后端落盘 WebP 图片。
@router.post(
    "/create",
    response_model=ProductDetailsResponse,
    status_code=status.HTTP_201_CREATED,
    summary="新增奖品",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "奖品图片不是有效的 WebP 文件"
        },
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": "奖品图片超过 5 MiB"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "表单字段缺失或不符合约束"
        },
        status.HTTP_502_BAD_GATEWAY: {
            "description": "商品已创建，但客户端后端图片存储失败"
        },
    },
)
async def create_product(
    session: DatabaseSession,
    client_backend: ClientBackend,
    name: Annotated[str, Form(max_length=128)],
    points_required: Annotated[
        int,
        Form(ge=0, le=4_294_967_295),
    ],
    image: Annotated[
        UploadFile,
        File(
            media_type="image/webp",
            description="仅接受最大 5 MiB 的 WebP 奖品图片",
            json_schema_extra={"contentMediaType": "image/webp"},
        ),
    ],
    description: Annotated[str | None, Form(max_length=255)] = None,
) -> ProductDetailsResponse:
    request = validate_product_creation_form(
        name,
        points_required,
        description,
    )
    image_media_type = image.content_type
    creation = ProductCreation(
        name=request.name,
        points_required=request.points_required,
        description=request.description,
        image=ProductImageUpload(
            content=await read_product_image_file(image),
            media_type=image_media_type,
        ),
    )
    try:
        created_product = await create_product_service(
            session,
            client_backend,
            creation,
        )
    except InvalidProductImageMediaTypeError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="奖品图片只支持 WebP 格式",
        ) from error
    except InvalidProductImageContentError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="上传内容不是有效的奖品图片",
        ) from error
    except ProductImageSizeExceededError as error:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="奖品图片不能超过 5 MiB",
        ) from error
    except ProductImageReplacementError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"奖品已创建，但图片存储失败：{error}",
        ) from error
    return ProductDetailsResponse.model_validate(
        created_product,
        from_attributes=True,
    )


# 按商品主键切换上下架状态，商品不存在时返回明确的资源错误。
@router.patch(
    "/{product_id}/status",
    response_model=ProductDetailsResponse,
    summary="修改商品可见状态",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "奖品不存在"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "商品主键或可见状态不符合字段约束"
        },
    },
)
async def update_product_visibility_status(
    session: DatabaseSession,
    product_id: Annotated[int, Path(gt=0, description="奖品 ID")],
    request: UpdateProductVisibilityStatusRequest,
) -> ProductDetailsResponse:
    try:
        product = await update_product_visibility_status_service(
            session,
            product_id,
            request.status,
        )
    except ProductNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="奖品不存在",
        ) from error
    return ProductDetailsResponse.model_validate(
        product,
        from_attributes=True,
    )


# 按商品主键局部修改基础资料，并将图片替换安排在数据库事务提交后的最后一步。
@router.patch(
    "/{product_id}",
    response_model=ProductDetailsResponse,
    summary="修改奖品基本信息",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "奖品图片不是有效的 WebP 文件"
        },
        status.HTTP_404_NOT_FOUND: {"description": "奖品不存在"},
        status.HTTP_409_CONFLICT: {
            "description": "积分修改超过配置窗口或激活赛季数据冲突"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "商品主键、补丁字段或字段值不符合约束"
        },
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": "奖品图片超过 5 MiB"
        },
        status.HTTP_502_BAD_GATEWAY: {
            "description": "数据库已更新，但客户端后端图片替换失败"
        },
    },
)
async def update_product_basic_information(
    session: DatabaseSession,
    client_backend: ClientBackend,
    product_id: Annotated[int, Path(gt=0, description="奖品 ID")],
    product_payload: Annotated[
        str | None,
        Form(
            alias="product",
            max_length=4096,
            description="可选商品字段组成的 JSON 字符串",
        ),
    ] = None,
    image: Annotated[
        UploadFile | None,
        File(
            media_type="image/webp",
            description="仅接受最大 5 MiB 的 WebP 奖品图片",
        ),
    ] = None,
) -> ProductDetailsResponse:
    request = parse_product_update_form(product_payload)
    submitted_fields = request.model_fields_set
    if not submitted_fields and image is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="至少提交一个可修改字段或奖品图片",
        )
    image_upload = None
    if image is not None:
        image_media_type = image.content_type
        image_upload = ProductImageUpload(
            content=await read_product_image_file(image),
            media_type=image_media_type,
        )
    patch = ProductBasicInformationPatch(
        update_name="name" in submitted_fields,
        name=request.name,
        update_description="description" in submitted_fields,
        description=request.description,
        update_points_required="points_required" in submitted_fields,
        points_required=request.points_required,
        image=image_upload,
    )
    try:
        updated_product = await update_product_basic_information_service(
            session,
            client_backend,
            product_id,
            patch,
        )
    except ProductNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="奖品不存在",
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
    except InvalidProductImageMediaTypeError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="奖品图片只支持 WebP 格式",
        ) from error
    except InvalidProductImageContentError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="上传内容不是有效的奖品图片",
        ) from error
    except ProductImageSizeExceededError as error:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="奖品图片不能超过 5 MiB",
        ) from error
    except ProductImageReplacementError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"奖品基本信息已更新，但图片替换失败：{error}",
        ) from error
    return ProductDetailsResponse.model_validate(
        updated_product,
        from_attributes=True,
    )


# 根据正整数奖品 ID 返回展示信息，并将不存在结果映射为明确的 HTTP 响应。
@router.get(
    "/info",
    response_model=ProductInformationResponse,
    summary="获取奖品信息",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "奖品不存在"},
    },
)
async def get_product_information_route(
    session: DatabaseSession,
    product_id: Annotated[int, Query(gt=0, description="奖品 ID")],
) -> ProductInformationResponse:
    try:
        product = await get_product_information(session, product_id)
    except ProductNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="奖品不存在",
        ) from error
    return ProductInformationResponse.model_validate(
        product,
        from_attributes=True,
    )


# 接收积分兑换流水 ID 与审核结论，并将状态冲突和积分一致性异常映射为明确响应。
@router.post(
    "/distribute",
    response_model=GiftDistributionResponse,
    summary="处理兑换礼品发放审核",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "兑换流水不存在"},
        status.HTTP_409_CONFLICT: {
            "description": "流水状态或积分数据不允许完成审核"
        },
    },
)
async def distribute_gift(
    session: DatabaseSession,
    request: GiftDistributionRequest,
) -> GiftDistributionResponse:
    try:
        result = await process_gift_distribution(
            session,
            request.id,
            request.decision,
        )
    except GiftDistributionNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="兑换流水不存在",
        ) from error
    except InvalidGiftDistributionRecordError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该流水不是有效的商品兑换记录",
        ) from error
    except GiftDistributionStatusConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="礼品发放状态异常，无法更新",
        ) from error
    except PointBalanceConsistencyError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="用户积分流水不完整，无法拒绝发放",
        ) from error
    return GiftDistributionResponse.model_validate(
        result,
        from_attributes=True,
    )
