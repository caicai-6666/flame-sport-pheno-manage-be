import unittest
from types import TracebackType
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.repositories.products import ProductDetails, fetch_all_products
from app.router.dependencies import require_admin_token
from app.services.products import list_products


class FakeMappingsResult:
    # 保存商品列表查询的预设映射行，支持仓储一次性读取完整结果。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    # 返回自身以支持仓储使用 mappings().all() 调用链。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回全部预设商品行，并保持数据库查询给出的稳定顺序。
    def all(self) -> list[dict[str, object]]:
        return self.rows


class FakeRepositorySession:
    # 保存商品查询结果，并捕获仓储执行的 SQL 供筛选口径断言。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.result = FakeMappingsResult(rows)
        self.statement = None

    # 模拟商品列表的单次异步查询，避免测试访问开发数据库。
    async def exec(self, statement: object) -> FakeMappingsResult:
        self.statement = statement
        return self.result


class FakeTransactionContext:
    # 初始化可观察的商品列表只读事务状态。
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    # 标记商品列表服务已经进入事务。
    async def __aenter__(self) -> "FakeTransactionContext":
        self.entered = True
        return self

    # 标记事务正常退出，并让潜在异常继续向上层传播。
    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.exited = True
        return False


class FakeServiceSession:
    # 为商品列表服务提供单一且可观察的事务上下文。
    def __init__(self) -> None:
        self.transaction = FakeTransactionContext()

    # 返回商品列表用例应使用的只读事务。
    def begin(self) -> FakeTransactionContext:
        return self.transaction


class ProductListRepositoryTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证仓储返回商品表全部字段，且不会过滤已下架商品。
    async def test_repository_maps_all_product_fields(self) -> None:
        session = FakeRepositorySession(
            [
                {
                    "id": 1,
                    "name": "运动水杯",
                    "description": "运动补水",
                    "points_required": 50,
                    "image_url": "/运动水杯.jpg",
                    "status": 1,
                },
                {
                    "id": 2,
                    "name": "旧款跳绳",
                    "description": None,
                    "points_required": 30,
                    "image_url": None,
                    "status": 0,
                },
            ]
        )

        products = await fetch_all_products(  # type: ignore[arg-type]
            session
        )

        self.assertEqual(
            products,
            (
                ProductDetails(
                    id=1,
                    name="运动水杯",
                    description="运动补水",
                    points_required=50,
                    image_url="/运动水杯.jpg",
                    status=1,
                ),
                ProductDetails(
                    id=2,
                    name="旧款跳绳",
                    description=None,
                    points_required=30,
                    image_url=None,
                    status=0,
                ),
            ),
        )
        sql = str(session.statement)
        self.assertIn("product.id", sql)
        self.assertIn("product.points_required", sql)
        self.assertIn("product.status", sql)
        self.assertNotIn("WHERE", sql)
        self.assertIn("ORDER BY product.id ASC", sql)

    # 验证商品表为空时仓储返回空集合，不构造虚假占位商品。
    async def test_repository_returns_empty_products(self) -> None:
        session = FakeRepositorySession([])

        products = await fetch_all_products(  # type: ignore[arg-type]
            session
        )

        self.assertEqual(products, ())


class ProductListServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证商品列表服务在显式只读事务中调用仓储。
    async def test_service_manages_read_transaction(self) -> None:
        session = FakeServiceSession()
        expected = (
            ProductDetails(
                id=1,
                name="运动水杯",
                description=None,
                points_required=50,
                image_url=None,
                status=1,
            ),
        )
        repository_mock = AsyncMock(return_value=expected)

        with patch(
            "app.services.products.fetch_all_products",
            new=repository_mock,
        ):
            products = await list_products(  # type: ignore[arg-type]
                session
            )

        self.assertEqual(products, expected)
        repository_mock.assert_awaited_once_with(session)
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)


class ProductListRouteTestCase(unittest.TestCase):
    # 为商品列表路由注入隔离会话，并绕过已单独覆盖的认证实现。
    def setUp(self) -> None:
        self.session = object()

        # 返回商品列表接口测试专用的数据库会话替身。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session

    # 清理依赖覆盖，避免商品列表测试污染其他路由。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证接口返回每个商品的全部字段，包括主键、积分和上下架状态。
    def test_route_returns_complete_product_list(self) -> None:
        service_mock = AsyncMock(
            return_value=(
                ProductDetails(
                    id=1,
                    name="运动水杯",
                    description="运动补水",
                    points_required=50,
                    image_url="/运动水杯.jpg",
                    status=1,
                ),
                ProductDetails(
                    id=2,
                    name="旧款跳绳",
                    description=None,
                    points_required=30,
                    image_url=None,
                    status=0,
                ),
            )
        )

        with patch(
            "app.router.product.list_products",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get("/flame/admin/api/product/list")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [
                {
                    "id": 1,
                    "name": "运动水杯",
                    "description": "运动补水",
                    "points_required": 50,
                    "image_url": "/运动水杯.jpg",
                    "status": 1,
                },
                {
                    "id": 2,
                    "name": "旧款跳绳",
                    "description": None,
                    "points_required": 30,
                    "image_url": None,
                    "status": 0,
                },
            ],
        )
        service_mock.assert_awaited_once_with(self.session)

    # 验证没有商品时接口返回成功空数组。
    def test_route_returns_empty_array(self) -> None:
        with patch(
            "app.router.product.list_products",
            new=AsyncMock(return_value=()),
        ):
            with TestClient(app) as client:
                response = client.get("/flame/admin/api/product/list")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    # 验证商品列表继承管理员认证，未登录请求不会进入查询服务。
    def test_route_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.product.list_products",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/product/list",
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()
