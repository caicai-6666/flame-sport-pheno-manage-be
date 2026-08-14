import unittest
from io import BytesIO
from types import TracebackType
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from PIL import Image

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.repositories.products import ProductDetails, insert_product
from app.router.dependencies import get_client_backend, require_admin_token
from app.services.images import ProductImageReplacementError
from app.services.products import (
    InvalidProductImageContentError,
    InvalidProductImageMediaTypeError,
    ProductCreation,
    ProductImageSizeExceededError,
    ProductImageUpload,
    create_product,
)


# 生成真实的小型 WebP 图片，供奖品新增的格式和 multipart 测试复用。
def build_webp() -> bytes:
    image_buffer = BytesIO()
    Image.new("RGB", (2, 2), color=(23, 45, 67)).save(
        image_buffer,
        format="WEBP",
    )
    return image_buffer.getvalue()


class FakeInsertResult:
    # 保存数据库生成主键，模拟商品 INSERT 的执行结果。
    def __init__(self, lastrowid: int) -> None:
        self.lastrowid = lastrowid


class FakeRepositorySession:
    # 捕获新增商品 SQL 和绑定参数，避免测试访问真实数据库。
    def __init__(self, lastrowid: int) -> None:
        self.lastrowid = lastrowid
        self.statement: object | None = None
        self.params: dict[str, object] | None = None

    # 模拟商品新增语句并返回预设的自增主键。
    async def exec(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> FakeInsertResult:
        self.statement = statement
        self.params = params
        return FakeInsertResult(self.lastrowid)


class FakeTransactionContext:
    # 记录奖品新增事务是否在图片存储前正常退出。
    def __init__(self) -> None:
        self.entered = False
        self.exited = False
        self.exception_type: type[BaseException] | None = None

    # 标记奖品新增事务已经开始。
    async def __aenter__(self) -> "FakeTransactionContext":
        self.entered = True
        return self

    # 记录事务退出结果并保持异常继续传播。
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
    # 为奖品新增服务提供单一且可观察的数据库事务。
    def __init__(self) -> None:
        self.transaction = FakeTransactionContext()

    # 返回奖品新增用例使用的事务上下文。
    def begin(self) -> FakeTransactionContext:
        return self.transaction


class ProductCreationRepositoryTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证仓储写入全部必需字段，并让新奖品默认处于上架状态。
    async def test_repository_inserts_visible_product(self) -> None:
        session = FakeRepositorySession(lastrowid=12)

        product = await insert_product(  # type: ignore[arg-type]
            session,
            "运动毛巾",
            None,
            80,
            "/product-unique.webp",
        )

        self.assertEqual(
            product,
            ProductDetails(
                id=12,
                name="运动毛巾",
                description=None,
                points_required=80,
                image_url="/product-unique.webp",
                status=1,
            ),
        )
        self.assertIn("INSERT INTO product", str(session.statement))
        self.assertIn("status", str(session.statement))
        self.assertEqual(
            session.params,
            {
                "name": "运动毛巾",
                "description": None,
                "points_required": 80,
                "image_url": "/product-unique.webp",
            },
        )


class ProductCreationServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证数据库事务先提交，再通过客户端覆盖接口以空旧地址存储新图片。
    async def test_service_commits_product_before_storing_image(self) -> None:
        session = FakeServiceSession()
        client_backend = object()
        image_content = build_webp()
        creation = ProductCreation(
            name="运动毛巾",
            points_required=80,
            description=None,
            image=ProductImageUpload(image_content, "image/webp"),
        )
        expected_product = ProductDetails(
            12,
            "运动毛巾",
            None,
            80,
            "/product-unique.webp",
            1,
        )
        insert_mock = AsyncMock(return_value=expected_product)
        configuration_guard_mock = AsyncMock()

        # 在图片落盘调用发生时确认数据库事务已经正常提交。
        async def assert_database_committed(
            passed_client_backend: object,
            old_image_url: str | None,
            new_image_url: str,
            passed_image_content: bytes,
            image_media_type: str | None,
        ) -> object:
            self.assertIs(passed_client_backend, client_backend)
            self.assertTrue(session.transaction.exited)
            self.assertIsNone(session.transaction.exception_type)
            self.assertIsNone(old_image_url)
            self.assertEqual(new_image_url, "/product-unique.webp")
            self.assertEqual(passed_image_content, image_content)
            self.assertEqual(image_media_type, "image/webp")
            return object()

        with (
            patch(
                "app.services.products.generate_product_image_url",
                return_value="/product-unique.webp",
            ),
            patch(
                "app.services.products.insert_product",
                new=insert_mock,
            ),
            patch(
                "app.services.products."
                "ensure_active_season_configuration_editable",
                new=configuration_guard_mock,
            ),
            patch(
                "app.services.products.replace_product_image",
                new=AsyncMock(side_effect=assert_database_committed),
            ) as replace_mock,
        ):
            product = await create_product(  # type: ignore[arg-type]
                session,
                client_backend,
                creation,
            )

        self.assertEqual(product, expected_product)
        insert_mock.assert_awaited_once_with(
            session,
            "运动毛巾",
            None,
            80,
            "/product-unique.webp",
        )
        replace_mock.assert_awaited_once()
        configuration_guard_mock.assert_not_awaited()

    # 验证数据库写入失败时不会提前调用客户端图片接口，并由事务统一回滚。
    async def test_service_skips_image_storage_when_database_fails(
        self,
    ) -> None:
        session = FakeServiceSession()
        replacement_mock = AsyncMock()
        creation = ProductCreation(
            "运动毛巾",
            80,
            None,
            ProductImageUpload(build_webp(), "image/webp"),
        )

        with (
            patch(
                "app.services.products.insert_product",
                new=AsyncMock(side_effect=RuntimeError("database failed")),
            ),
            patch(
                "app.services.products.replace_product_image",
                new=replacement_mock,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "database failed"):
                await create_product(  # type: ignore[arg-type]
                    session,
                    object(),
                    creation,
                )

        replacement_mock.assert_not_awaited()
        self.assertIs(session.transaction.exception_type, RuntimeError)

    # 验证客户端图片存储失败时数据库事务已经完成，异常按部分成功继续传播。
    async def test_service_propagates_image_failure_after_commit(self) -> None:
        session = FakeServiceSession()
        creation = ProductCreation(
            "运动毛巾",
            80,
            None,
            ProductImageUpload(build_webp(), "image/webp"),
        )

        with (
            patch(
                "app.services.products.generate_product_image_url",
                return_value="/product-unique.webp",
            ),
            patch(
                "app.services.products.insert_product",
                new=AsyncMock(
                    return_value=ProductDetails(
                        12,
                        "运动毛巾",
                        None,
                        80,
                        "/product-unique.webp",
                        1,
                    )
                ),
            ),
            patch(
                "app.services.products.replace_product_image",
                new=AsyncMock(
                    side_effect=ProductImageReplacementError(
                        "客户端后端商品图片替换服务不可用"
                    )
                ),
            ),
        ):
            with self.assertRaises(ProductImageReplacementError):
                await create_product(  # type: ignore[arg-type]
                    session,
                    object(),
                    creation,
                )

        self.assertTrue(session.transaction.exited)
        self.assertIsNone(session.transaction.exception_type)


class ProductCreationRouteTestCase(unittest.TestCase):
    # 为奖品新增接口注入隔离依赖，并绕过已单独覆盖的管理员认证。
    def setUp(self) -> None:
        self.session = object()
        self.client_backend = object()

        # 返回奖品新增路由测试专用会话，避免连接开发数据库。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[get_client_backend] = (
            lambda: self.client_backend
        )

    # 清理依赖覆盖，避免奖品新增测试污染其他接口。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 构造奖品新增 multipart 请求，允许覆盖描述、图片内容和媒体类型。
    def build_request(
        self,
        *,
        name: str = "运动毛巾",
        points_required: str = "80",
        description: str | None = "训练后快速吸汗",
        image_content: bytes | None = None,
        image_media_type: str = "image/webp",
    ) -> tuple[dict[str, str], dict[str, tuple[str, bytes, str]]]:
        form_data = {
            "name": name,
            "points_required": points_required,
        }
        if description is not None:
            form_data["description"] = description
        files = {
            "image": (
                "towel.webp",
                image_content if image_content is not None else build_webp(),
                image_media_type,
            )
        }
        return form_data, files

    # 验证接口接收完整字段、规范化文本并返回默认上架的新奖品。
    def test_route_creates_product(self) -> None:
        image_content = build_webp()
        expected_product = ProductDetails(
            12,
            "运动毛巾",
            "训练后快速吸汗",
            80,
            "/product-unique.webp",
            1,
        )
        service_mock = AsyncMock(return_value=expected_product)
        form_data, files = self.build_request(
            name="  运动毛巾  ",
            image_content=image_content,
        )

        with patch(
            "app.router.product.create_product_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/flame/admin/api/product/create",
                    data=form_data,
                    files=files,
                )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.json(),
            {
                "id": 12,
                "name": "运动毛巾",
                "description": "训练后快速吸汗",
                "points_required": 80,
                "image_url": "/product-unique.webp",
                "status": 1,
            },
        )
        passed_session, passed_client, creation = service_mock.await_args.args
        self.assertIs(passed_session, self.session)
        self.assertIs(passed_client, self.client_backend)
        self.assertEqual(
            creation,
            ProductCreation(
                "运动毛巾",
                80,
                "训练后快速吸汗",
                ProductImageUpload(image_content, "image/webp"),
            ),
        )

    # 验证描述缺省或纯空白时统一转换为数据库空值。
    def test_route_allows_empty_description(self) -> None:
        service_mock = AsyncMock(
            return_value=ProductDetails(
                12,
                "运动毛巾",
                None,
                80,
                "/product-unique.webp",
                1,
            )
        )
        form_data, files = self.build_request(description="   ")

        with patch(
            "app.router.product.create_product_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/flame/admin/api/product/create",
                    data=form_data,
                    files=files,
                )

        self.assertEqual(response.status_code, 201)
        creation = service_mock.await_args.args[2]
        self.assertIsNone(creation.description)

    # 验证缺少必填项、空名称和非法积分均在业务服务前返回参数错误。
    def test_route_validates_required_product_fields(self) -> None:
        service_mock = AsyncMock()
        valid_data, valid_files = self.build_request()
        invalid_requests = (
            ({"points_required": "80"}, valid_files),
            ({"name": "   ", "points_required": "80"}, valid_files),
            ({"name": "运动毛巾", "points_required": "-1"}, valid_files),
            (valid_data, {}),
        )

        with patch(
            "app.router.product.create_product_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                for form_data, files in invalid_requests:
                    with self.subTest(form_data=form_data, files=files):
                        response = client.post(
                            "/flame/admin/api/product/create",
                            data=form_data,
                            files=files,
                        )
                        self.assertEqual(response.status_code, 422)

        service_mock.assert_not_awaited()

    # 验证 WebP 类型、真实内容、大小和客户端落盘异常映射为稳定响应。
    def test_route_maps_product_image_errors(self) -> None:
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
            (
                ProductImageReplacementError("客户端服务不可用"),
                502,
                "奖品已创建，但图片存储失败：客户端服务不可用",
            ),
        )
        form_data, files = self.build_request()

        with TestClient(app) as client:
            for service_error, expected_status, expected_detail in error_cases:
                with self.subTest(service_error=service_error):
                    with patch(
                        "app.router.product.create_product_service",
                        new=AsyncMock(side_effect=service_error),
                    ):
                        response = client.post(
                            "/flame/admin/api/product/create",
                            data=form_data,
                            files=files,
                        )

                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(
                    response.json(),
                    {"detail": expected_detail},
                )

    # 验证奖品新增接口继承统一管理员认证，未登录时不执行服务。
    def test_route_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()
        form_data, files = self.build_request()

        with patch(
            "app.router.product.create_product_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/flame/admin/api/product/create",
                    data=form_data,
                    files=files,
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
