import json
import unittest
from io import BytesIO
from types import TracebackType
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.repositories.products import (
    ProductBasicInformationUpdate,
    ProductDetails,
    is_product_image_referenced_by_other_products,
    update_product_basic_information as update_product_repository,
)
from app.router.dependencies import get_client_backend, require_admin_token
from app.services.configuration_guard import (
    ActiveSeasonConfigurationWindowClosedError,
)
from app.services.images import (
    ProductImageReplacementError,
    ReplacedProductImage,
    replace_product_image,
)
from app.services.products import (
    InvalidProductImageContentError,
    InvalidProductImageMediaTypeError,
    ProductBasicInformationPatch,
    ProductImageSizeExceededError,
    ProductImageUpload,
    ProductNotFoundError,
    generate_product_image_url,
    update_product_basic_information as update_product_service,
    validate_product_image_upload,
)


class FakeMappingsResult:
    # 保存商品更新仓储的预设映射行，支持读取首条结果。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    # 返回自身以兼容 SQLAlchemy 映射结果读取方式。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回首条映射；空列表用于表达商品不存在或图片未被引用。
    def first(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None


class FakeRepositorySession:
    # 按调用顺序提供查询结果并记录全部参数化 SQL。
    def __init__(self, result_rows: list[list[dict[str, object]]]) -> None:
        self.results = [FakeMappingsResult(rows) for rows in result_rows]
        self.statements: list[object] = []
        self.params: list[dict[str, object] | None] = []

    # 模拟异步数据库执行，便于验证局部更新和共享图片查询。
    async def exec(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> FakeMappingsResult:
        self.statements.append(statement)
        self.params.append(params)
        return self.results.pop(0) if self.results else FakeMappingsResult([])


class FakeTransactionContext:
    # 记录事务是否已经退出，供图片调用顺序断言使用。
    def __init__(self) -> None:
        self.entered = False
        self.exited = False
        self.exception_type: type[BaseException] | None = None

    # 标记事务开始。
    async def __aenter__(self) -> "FakeTransactionContext":
        self.entered = True
        return self

    # 标记事务完成并保留异常类型以验证提交或回滚语义。
    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.exited = True
        self.exception_type = exception_type
        return False


class FakeServiceSession:
    # 为每次 begin 创建可观察事务，支持先写后读的图片安全检查。
    def __init__(self) -> None:
        self.transactions: list[FakeTransactionContext] = []

    # 返回新的事务上下文并记录调用顺序。
    def begin(self) -> FakeTransactionContext:
        transaction = FakeTransactionContext()
        self.transactions.append(transaction)
        return transaction


class FakeClientBackend:
    # 保存预设客户端响应或网络异常，并记录图片替换请求。
    def __init__(
        self,
        response: httpx.Response | None = None,
        error: httpx.RequestError | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    # 模拟共享客户端请求，并按真实适配器约定抛出 HTTP 状态异常。
    async def request(
        self,
        method: str,
        path: str,
        **kwargs: object,
    ) -> httpx.Response:
        self.calls.append((method, path, kwargs))
        if self.error is not None:
            raise self.error
        assert self.response is not None
        self.response.raise_for_status()
        return self.response


# 创建带请求上下文的客户端响应，确保 raise_for_status 行为与生产一致。
def make_client_response(
    status_code: int,
    payload: dict[str, object],
) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("POST", "http://backend/product/replace"),
    )


# 生成体积很小的真实图片字节，确保格式校验测试不依赖仓库外部文件。
def create_test_image(image_format: str = "WEBP") -> bytes:
    image_buffer = BytesIO()
    Image.new("RGB", (2, 2), color=(12, 34, 56)).save(
        image_buffer,
        format=image_format,
    )
    return image_buffer.getvalue()


class ProductUpdateRepositoryTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证仓储锁定商品后只采用显式补丁值，并保留状态与旧图片地址。
    async def test_repository_updates_selected_product_fields(self) -> None:
        session = FakeRepositorySession(
            [
                [
                    {
                        "id": 7,
                        "name": "旧名称",
                        "description": "旧描述",
                        "points_required": 60,
                        "image_url": "/旧图.jpg",
                        "status": 1,
                    }
                ],
                [],
            ]
        )

        result = await update_product_repository(  # type: ignore[arg-type]
            session,
            7,
            update_name=True,
            name="新名称",
            update_description=True,
            description=None,
            update_points_required=False,
            points_required=None,
            update_image_url=True,
            image_url="/新图.png",
        )

        self.assertEqual(
            result,
            ProductBasicInformationUpdate(
                product=ProductDetails(
                    id=7,
                    name="新名称",
                    description=None,
                    points_required=60,
                    image_url="/新图.png",
                    status=1,
                ),
                previous_image_url="/旧图.jpg",
                image_changed=True,
            ),
        )
        self.assertIn("FOR UPDATE", str(session.statements[0]))
        self.assertIn("UPDATE product", str(session.statements[1]))
        self.assertEqual(
            session.params[1],
            {
                "product_id": 7,
                "name": "新名称",
                "description": None,
                "points_required": 60,
                "image_url": "/新图.png",
            },
        )

    # 验证共享图片检查排除当前商品，并只按旧地址查找其他引用。
    async def test_repository_detects_shared_product_image(self) -> None:
        session = FakeRepositorySession([[{"id": 8}]])

        is_shared = await is_product_image_referenced_by_other_products(  # type: ignore[arg-type]
            session,
            7,
            "/旧图.jpg",
        )

        self.assertTrue(is_shared)
        self.assertEqual(
            session.params[0],
            {"product_id": 7, "image_url": "/旧图.jpg"},
        )


class ProductImageReplacementTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证客户端替换请求严格使用约定路径、字段与响应结构。
    async def test_replaces_product_image_through_client_backend(self) -> None:
        image_content = create_test_image()
        client_backend = FakeClientBackend(
            response=make_client_response(
                200,
                {
                    "image_url": "/新图.webp",
                    "size_bytes": len(image_content),
                    "old_image_removed": True,
                },
            )
        )

        result = await replace_product_image(  # type: ignore[arg-type]
            client_backend,
            "/旧图.jpg",
            "/新图.webp",
            image_content,
            "image/webp",
        )

        self.assertEqual(
            result,
            ReplacedProductImage(
                "/新图.webp",
                len(image_content),
                True,
            ),
        )
        self.assertEqual(
            client_backend.calls,
            [
                (
                    "POST",
                    "/product/replace",
                    {
                        "data": {
                            "old_image_url": "/旧图.jpg",
                            "new_image_url": "/新图.webp",
                        },
                        "files": {
                            "image": (
                                "新图.webp",
                                image_content,
                                "image/webp",
                            )
                        },
                    },
                )
            ],
        )

    # 验证客户端安全错误被转换为商品图片替换异常而不泄露响应体。
    async def test_maps_client_replacement_error(self) -> None:
        client_backend = FakeClientBackend(
            response=make_client_response(
                404,
                {"detail": "新奖品图片文件不存在"},
            )
        )

        with self.assertRaisesRegex(
            ProductImageReplacementError,
            "新奖品图片文件不存在",
        ):
            await replace_product_image(  # type: ignore[arg-type]
                client_backend,
                "/旧图.jpg",
                "/新图.webp",
                create_test_image(),
                "image/webp",
            )


class ProductImageValidationTestCase(unittest.TestCase):
    # 验证真实 WebP 图片通过校验并获得不复用的 WebP 相对地址。
    def test_accepts_webp_and_generates_unique_url(self) -> None:
        upload = ProductImageUpload(create_test_image(), "image/webp")

        extension = validate_product_image_upload(upload)
        first_url = generate_product_image_url(extension)
        second_url = generate_product_image_url(extension)

        self.assertEqual(extension, "webp")
        self.assertRegex(first_url, r"^/product-[0-9a-f]{32}\.webp$")
        self.assertNotEqual(first_url, second_url)

    # 验证 JPEG、PNG 和错误媒体类型全部在数据库写入前被拒绝。
    def test_rejects_non_webp_images(self) -> None:
        invalid_uploads = (
            (
                ProductImageUpload(create_test_image("JPEG"), "image/jpeg"),
                InvalidProductImageMediaTypeError,
            ),
            (
                ProductImageUpload(create_test_image("PNG"), "image/webp"),
                InvalidProductImageContentError,
            ),
            (
                ProductImageUpload(create_test_image(), "image/png"),
                InvalidProductImageMediaTypeError,
            ),
        )

        for upload, expected_error in invalid_uploads:
            with self.subTest(upload=upload, expected_error=expected_error):
                with self.assertRaises(expected_error):
                    validate_product_image_upload(upload)

    # 验证空图片与超过五 MiB 的图片获得可区分的校验异常。
    def test_rejects_empty_and_oversized_images(self) -> None:
        with self.assertRaises(InvalidProductImageContentError):
            validate_product_image_upload(
                ProductImageUpload(b"", "image/webp")
            )
        with self.assertRaises(ProductImageSizeExceededError):
            validate_product_image_upload(
                ProductImageUpload(
                    b"x" * (5 * 1024 * 1024 + 1),
                    "image/webp",
                )
            )


class ProductUpdateServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证积分补丁先经过配置窗口，再在已提交事务之后执行图片替换。
    async def test_service_commits_database_before_replacing_image(self) -> None:
        session = FakeServiceSession()
        client_backend = object()
        image_content = create_test_image()
        update_result = ProductBasicInformationUpdate(
            product=ProductDetails(
                id=7,
                name="新名称",
                description="新描述",
                points_required=80,
                image_url="/新图.webp",
                status=1,
            ),
            previous_image_url="/旧图.jpg",
            image_changed=True,
        )
        guard_mock = AsyncMock()
        repository_mock = AsyncMock(return_value=update_result)
        shared_mock = AsyncMock(return_value=False)

        # 在外部替换发生时断言写事务和共享引用读取事务均已经正常退出。
        async def assert_committed_before_replacement(
            passed_client_backend: object,
            old_image_url: str | None,
            new_image_url: str,
            passed_image_content: bytes,
            image_media_type: str | None,
        ) -> ReplacedProductImage:
            self.assertIs(passed_client_backend, client_backend)
            self.assertTrue(all(item.exited for item in session.transactions))
            self.assertTrue(
                all(item.exception_type is None for item in session.transactions)
            )
            self.assertEqual(old_image_url, "/旧图.jpg")
            self.assertEqual(new_image_url, "/新图.webp")
            self.assertEqual(passed_image_content, image_content)
            self.assertEqual(image_media_type, "image/webp")
            return ReplacedProductImage(
                new_image_url,
                len(passed_image_content),
                True,
            )

        with (
            patch(
                "app.services.products."
                "ensure_active_season_configuration_editable",
                new=guard_mock,
            ),
            patch(
                "app.services.products."
                "update_product_basic_information_repository",
                new=repository_mock,
            ),
            patch(
                "app.services.products."
                "is_product_image_referenced_by_other_products",
                new=shared_mock,
            ),
            patch(
                "app.services.products.replace_product_image",
                new=AsyncMock(side_effect=assert_committed_before_replacement),
            ) as replacement_mock,
            patch(
                "app.services.products.generate_product_image_url",
                return_value="/新图.webp",
            ),
        ):
            product = await update_product_service(  # type: ignore[arg-type]
                session,
                client_backend,
                7,
                ProductBasicInformationPatch(
                    update_points_required=True,
                    points_required=80,
                    image=ProductImageUpload(
                        image_content,
                        "image/webp",
                    ),
                ),
                edit_window_hours=12,
            )

        self.assertEqual(product, update_result.product)
        guard_mock.assert_awaited_once_with(session, 12)
        shared_mock.assert_awaited_once_with(session, 7, "/旧图.jpg")
        replacement_mock.assert_awaited_once()
        self.assertEqual(len(session.transactions), 2)

    # 验证旧图片仍被其他商品使用时，客户端只校验新图而不会删除共享旧图。
    async def test_service_preserves_shared_old_image(self) -> None:
        session = FakeServiceSession()
        image_content = create_test_image()
        update_result = ProductBasicInformationUpdate(
            product=ProductDetails(7, "奖品", None, 50, "/新图.webp", 1),
            previous_image_url="/共享图.jpg",
            image_changed=True,
        )
        replacement_mock = AsyncMock()

        with (
            patch(
                "app.services.products."
                "update_product_basic_information_repository",
                new=AsyncMock(return_value=update_result),
            ),
            patch(
                "app.services.products."
                "is_product_image_referenced_by_other_products",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.services.products.replace_product_image",
                new=replacement_mock,
            ),
            patch(
                "app.services.products.generate_product_image_url",
                return_value="/新图.webp",
            ),
        ):
            await update_product_service(  # type: ignore[arg-type]
                session,
                object(),
                7,
                ProductBasicInformationPatch(
                    image=ProductImageUpload(
                        image_content,
                        "image/webp",
                    ),
                ),
            )

        replacement_mock.assert_awaited_once_with(
            unittest.mock.ANY,
            None,
            "/新图.webp",
            image_content,
            "image/webp",
        )

    # 验证不提交积分字段时不触发配置窗口，且图片未变化时不调用客户端。
    async def test_service_updates_text_without_guard_or_image_call(self) -> None:
        session = FakeServiceSession()
        update_result = ProductBasicInformationUpdate(
            product=ProductDetails(7, "新名称", None, 50, "/图.jpg", 1),
            previous_image_url="/图.jpg",
            image_changed=False,
        )
        guard_mock = AsyncMock()
        replacement_mock = AsyncMock()

        with (
            patch(
                "app.services.products."
                "ensure_active_season_configuration_editable",
                new=guard_mock,
            ),
            patch(
                "app.services.products."
                "update_product_basic_information_repository",
                new=AsyncMock(return_value=update_result),
            ),
            patch(
                "app.services.products.replace_product_image",
                new=replacement_mock,
            ),
        ):
            product = await update_product_service(  # type: ignore[arg-type]
                session,
                object(),
                7,
                ProductBasicInformationPatch(
                    update_name=True,
                    name="新名称",
                ),
            )

        self.assertEqual(product, update_result.product)
        guard_mock.assert_not_awaited()
        replacement_mock.assert_not_awaited()


class ProductUpdateRouteTestCase(unittest.TestCase):
    # 注入隔离会话和客户端后端，并绕过已单独覆盖的管理员认证。
    def setUp(self) -> None:
        self.session = object()
        self.client_backend = object()

        # 为商品更新路由提供测试专用数据库会话。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[get_client_backend] = (
            lambda: self.client_backend
        )

    # 清除依赖覆盖，避免影响其他接口测试。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证接口只把实际提交字段转换为补丁，并返回更新后的完整商品。
    def test_route_updates_partial_product_information(self) -> None:
        image_content = create_test_image()
        service_mock = AsyncMock(
            return_value=ProductDetails(
                7,
                "新名称",
                None,
                50,
                "/新图.webp",
                1,
            )
        )

        with patch(
            "app.router.product.update_product_basic_information_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.patch(
                    "/flame/admin/api/product/7",
                    data={
                        "product": json.dumps(
                            {
                                "name": "  新名称  ",
                                "description": None,
                            },
                            ensure_ascii=False,
                        )
                    },
                    files={
                        "image": (
                            "新图.webp",
                            image_content,
                            "image/webp",
                        )
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "id": 7,
                "name": "新名称",
                "description": None,
                "points_required": 50,
                "image_url": "/新图.webp",
                "status": 1,
            },
        )
        passed_session, passed_client, product_id, product_patch = (
            service_mock.await_args.args
        )
        self.assertIs(passed_session, self.session)
        self.assertIs(passed_client, self.client_backend)
        self.assertEqual(product_id, 7)
        self.assertEqual(
            product_patch,
            ProductBasicInformationPatch(
                update_name=True,
                name="新名称",
                update_description=True,
                description=None,
                image=ProductImageUpload(
                    image_content,
                    "image/webp",
                ),
            ),
        )

    # 验证空补丁、额外字段和非严格积分均在 multipart 接口边界被拒绝。
    def test_route_validates_product_patch(self) -> None:
        invalid_payloads = (
            {},
            {"name": None},
            {"points_required": "50"},
            {"points_required": True},
            {"image_url": "/新图.webp"},
            {"unknown": "value"},
        )
        service_mock = AsyncMock()

        with patch(
            "app.router.product.update_product_basic_information_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                for payload in invalid_payloads:
                    with self.subTest(payload=payload):
                        response = client.patch(
                            "/flame/admin/api/product/7",
                            data={"product": json.dumps(payload)},
                        )
                        self.assertEqual(response.status_code, 422)

        service_mock.assert_not_awaited()

    # 验证配置窗口关闭被映射为冲突响应，不进入不明确的服务端错误。
    def test_route_maps_configuration_window_conflict(self) -> None:
        with patch(
            "app.router.product.update_product_basic_information_service",
            new=AsyncMock(
                side_effect=ActiveSeasonConfigurationWindowClosedError
            ),
        ):
            with TestClient(app) as client:
                response = client.patch(
                    "/flame/admin/api/product/7",
                    data={
                        "product": json.dumps({"points_required": 80})
                    },
                )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {"detail": "当前激活赛季的配置修改窗口已关闭"},
        )

    # 验证 WebP 类型、真实内容和大小异常映射为明确且互不混淆的响应。
    def test_route_maps_product_image_validation_errors(self) -> None:
        error_cases = (
            (
                InvalidProductImageMediaTypeError(),
                400,
                "奖品图片只支持 WebP 格式",
            ),
            (
                InvalidProductImageContentError(),
                400,
                "上传内容不是有效的奖品图片",
            ),
            (
                ProductImageSizeExceededError(),
                413,
                "奖品图片不能超过 5 MiB",
            ),
        )

        with TestClient(app) as client:
            for service_error, expected_status, expected_detail in error_cases:
                with self.subTest(service_error=service_error):
                    with patch(
                        "app.router.product."
                        "update_product_basic_information_service",
                        new=AsyncMock(side_effect=service_error),
                    ):
                        response = client.patch(
                            "/flame/admin/api/product/7",
                            files={
                                "image": (
                                    "新图.webp",
                                    b"image-content",
                                    "image/webp",
                                )
                            },
                        )

                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(
                    response.json(),
                    {"detail": expected_detail},
                )

    # 验证最后一步图片替换失败时明确告知数据库已完成更新。
    def test_route_reports_partial_success_when_image_replacement_fails(
        self,
    ) -> None:
        with patch(
            "app.router.product.update_product_basic_information_service",
            new=AsyncMock(
                side_effect=ProductImageReplacementError(
                    "新奖品图片文件不存在"
                )
            ),
        ):
            with TestClient(app) as client:
                response = client.patch(
                    "/flame/admin/api/product/7",
                    files={
                        "image": (
                            "新图.webp",
                            create_test_image(),
                            "image/webp",
                        )
                    },
                )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json(),
            {
                "detail": (
                    "奖品基本信息已更新，但图片替换失败："
                    "新奖品图片文件不存在"
                )
            },
        )

    # 验证商品不存在时返回稳定的资源错误。
    def test_route_reports_missing_product(self) -> None:
        with patch(
            "app.router.product.update_product_basic_information_service",
            new=AsyncMock(side_effect=ProductNotFoundError),
        ):
            with TestClient(app) as client:
                response = client.patch(
                    "/flame/admin/api/product/999",
                    data={"product": json.dumps({"name": "奖品"})},
                )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "奖品不存在"})

    # 验证新接口继承统一管理员认证，未登录请求不会执行写服务。
    def test_route_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.product.update_product_basic_information_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.patch(
                    "/flame/admin/api/product/7",
                    data={"product": json.dumps({"name": "奖品"})},
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
