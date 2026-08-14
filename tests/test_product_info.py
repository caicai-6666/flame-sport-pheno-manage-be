import unittest
from types import TracebackType
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.repositories.products import (
    ProductInformation,
    fetch_product_information,
)
from app.router.dependencies import require_admin_token
from app.services.products import (
    ProductNotFoundError,
    get_product_information,
)


class FakeMappingsResult:
    # 保存奖品查询的唯一预设行，以模拟 SQLAlchemy 映射结果。
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    # 返回自身以支持仓储使用 mappings().first() 调用链。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回预设奖品行，空值表示指定奖品不存在。
    def first(self) -> dict[str, object] | None:
        return self.row


class FakeRepositorySession:
    # 保存奖品查询结果，并捕获参数化 SQL 与绑定参数。
    def __init__(self, row: dict[str, object] | None) -> None:
        self.result = FakeMappingsResult(row)
        self.statement = None
        self.params = None

    # 模拟奖品信息的单次异步参数化查询。
    async def exec(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> FakeMappingsResult:
        self.statement = statement
        self.params = params
        return self.result


class FakeTransactionContext:
    # 初始化可观察的奖品查询事务状态。
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    # 标记奖品信息查询已经进入事务。
    async def __aenter__(self) -> "FakeTransactionContext":
        self.entered = True
        return self

    # 标记事务退出且保持底层异常继续传播。
    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.exited = True
        return False


class FakeServiceSession:
    # 为奖品信息服务测试提供单一可观察事务。
    def __init__(self) -> None:
        self.transaction = FakeTransactionContext()

    # 返回当前奖品查询用例的只读事务上下文。
    def begin(self) -> FakeTransactionContext:
        return self.transaction


class ProductInformationRepositoryTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证仓储按主键映射奖品信息，并保留可空描述与图片地址。
    async def test_repository_maps_product_information(self) -> None:
        session = FakeRepositorySession(
            {
                "name": "运动水杯",
                "description": None,
                "image_url": "/products/bottle.png",
            }
        )

        product = await fetch_product_information(  # type: ignore[arg-type]
            session,
            product_id=5,
        )

        self.assertEqual(
            product,
            ProductInformation(
                name="运动水杯",
                description=None,
                image_url="/products/bottle.png",
            ),
        )
        self.assertEqual(session.params, {"product_id": 5})
        sql = str(session.statement)
        self.assertIn("WHERE product.id = :product_id", sql)
        self.assertNotIn("product.status", sql)

    # 验证指定奖品不存在时仓储返回空结果。
    async def test_repository_returns_none_for_missing_product(self) -> None:
        session = FakeRepositorySession(None)

        product = await fetch_product_information(  # type: ignore[arg-type]
            session,
            product_id=999,
        )

        self.assertIsNone(product)


class ProductInformationServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证奖品信息服务在只读事务中返回仓储结果。
    async def test_service_returns_product_in_transaction(self) -> None:
        session = FakeServiceSession()
        expected = ProductInformation("运动水杯", "运动补水", None)

        with patch(
            "app.services.products.fetch_product_information",
            new=AsyncMock(return_value=expected),
        ):
            product = await get_product_information(  # type: ignore[arg-type]
                session,
                product_id=5,
            )

        self.assertEqual(product, expected)
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)

    # 验证奖品不存在时服务在事务结束后抛出稳定应用异常。
    async def test_service_reports_missing_product(self) -> None:
        session = FakeServiceSession()

        with patch(
            "app.services.products.fetch_product_information",
            new=AsyncMock(return_value=None),
        ):
            with self.assertRaises(ProductNotFoundError):
                await get_product_information(  # type: ignore[arg-type]
                    session,
                    product_id=999,
                )


class ProductInformationRouteTestCase(unittest.TestCase):
    # 为奖品信息路由注入隔离会话，并绕过已单独覆盖的认证逻辑。
    def setUp(self) -> None:
        self.session = object()

        # 返回当前奖品信息接口测试专用会话。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session

    # 清理依赖覆盖，防止奖品信息测试影响其他路由。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证接口按奖品 ID 返回名称、描述和图片地址。
    def test_route_returns_product_information(self) -> None:
        service_mock = AsyncMock(
            return_value=ProductInformation(
                name="运动水杯",
                description="运动补水",
                image_url="/products/bottle.png",
            )
        )

        with patch(
            "app.router.product.get_product_information",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/product/info",
                    params={"product_id": 5},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "name": "运动水杯",
                "description": "运动补水",
                "image_url": "/products/bottle.png",
            },
        )
        service_mock.assert_awaited_once_with(self.session, 5)

    # 验证奖品不存在时接口返回明确的 404 响应。
    def test_route_reports_missing_product(self) -> None:
        with patch(
            "app.router.product.get_product_information",
            new=AsyncMock(side_effect=ProductNotFoundError),
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/product/info",
                    params={"product_id": 999},
                )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "奖品不存在"})

    # 验证奖品 ID 缺失或非正整数时由请求边界拒绝。
    def test_route_validates_product_id(self) -> None:
        with TestClient(app) as client:
            missing_response = client.get("/flame/admin/api/product/info")
            invalid_response = client.get(
                "/flame/admin/api/product/info",
                params={"product_id": 0},
            )

        self.assertEqual(missing_response.status_code, 422)
        self.assertEqual(invalid_response.status_code, 422)

    # 验证奖品信息接口继承管理员认证，未登录时不会执行查询服务。
    def test_route_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.product.get_product_information",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/product/info",
                    params={"product_id": 5},
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()
