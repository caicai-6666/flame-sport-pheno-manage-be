import unittest
from types import TracebackType
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.repositories.products import (
    ProductDetails,
    update_product_visibility_status as update_product_status_repository,
)
from app.router.dependencies import require_admin_token
from app.services.products import (
    ProductNotFoundError,
    update_product_visibility_status as update_product_status_service,
)


class FakeMappingsResult:
    # 保存商品状态仓储的预设行，空集合用于表达目标商品不存在。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    # 返回自身以支持仓储通过 mappings() 读取映射结果。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回首条商品记录，确保状态更新只面向唯一主键。
    def first(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None


class FakeRepositorySession:
    # 按顺序返回商品锁定和状态更新结果，并记录 SQL 与参数。
    def __init__(self, result_rows: list[list[dict[str, object]]]) -> None:
        self.results = [FakeMappingsResult(rows) for rows in result_rows]
        self.statements: list[object] = []
        self.params: list[dict[str, object] | None] = []

    # 模拟商品状态仓储连续执行的异步数据库语句。
    async def exec(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> FakeMappingsResult:
        self.statements.append(statement)
        self.params.append(params)
        if self.results:
            return self.results.pop(0)
        return FakeMappingsResult([])


class FakeTransactionContext:
    # 记录商品状态服务的事务进入、退出与异常回滚语义。
    def __init__(self) -> None:
        self.entered = False
        self.exited = False
        self.exception_type: type[BaseException] | None = None

    # 标记商品状态写事务已经开始。
    async def __aenter__(self) -> "FakeTransactionContext":
        self.entered = True
        return self

    # 记录事务退出异常并保持异常继续传播。
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
    # 为商品状态服务提供唯一且可观察的写事务。
    def __init__(self) -> None:
        self.transaction = FakeTransactionContext()

    # 返回当前商品状态用例使用的事务上下文。
    def begin(self) -> FakeTransactionContext:
        return self.transaction


class ProductStatusRepositoryTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证仓储先锁定商品，再参数化覆盖状态并返回全部商品字段。
    async def test_repository_updates_product_visibility_status(self) -> None:
        session = FakeRepositorySession(
            [
                [
                    {
                        "id": 2,
                        "name": "运动水杯",
                        "description": "运动补水",
                        "points_required": 50,
                        "image_url": "/运动水杯.jpg",
                    }
                ],
                [],
            ]
        )

        product = await update_product_status_repository(  # type: ignore[arg-type]
            session,
            2,
            0,
        )

        self.assertEqual(
            product,
            ProductDetails(
                id=2,
                name="运动水杯",
                description="运动补水",
                points_required=50,
                image_url="/运动水杯.jpg",
                status=0,
            ),
        )
        self.assertIn("FOR UPDATE", str(session.statements[0]))
        self.assertIn("UPDATE product", str(session.statements[1]))
        self.assertEqual(session.params[0], {"product_id": 2})
        self.assertEqual(
            session.params[1],
            {"product_id": 2, "visibility_status": 0},
        )

    # 验证商品不存在时不执行更新，也不伪造商品返回值。
    async def test_repository_reports_missing_product(self) -> None:
        session = FakeRepositorySession([[]])

        product = await update_product_status_repository(  # type: ignore[arg-type]
            session,
            999,
            0,
        )

        self.assertIsNone(product)
        self.assertEqual(len(session.statements), 1)


class ProductStatusServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证服务在单一事务中修改商品状态并返回仓储结果。
    async def test_service_updates_product_status_in_transaction(
        self,
    ) -> None:
        session = FakeServiceSession()
        expected = ProductDetails(
            id=2,
            name="运动水杯",
            description=None,
            points_required=50,
            image_url=None,
            status=0,
        )
        repository_mock = AsyncMock(return_value=expected)

        with patch(
            "app.services.products."
            "update_product_status_repository",
            new=repository_mock,
        ):
            product = await update_product_status_service(  # type: ignore[arg-type]
                session,
                2,
                0,
            )

        self.assertEqual(product, expected)
        repository_mock.assert_awaited_once_with(session, 2, 0)
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)
        self.assertIsNone(session.transaction.exception_type)

    # 验证商品不存在时服务抛出稳定异常并使写事务回滚。
    async def test_service_reports_missing_product(self) -> None:
        session = FakeServiceSession()

        with patch(
            "app.services.products."
            "update_product_status_repository",
            new=AsyncMock(return_value=None),
        ):
            with self.assertRaises(ProductNotFoundError):
                await update_product_status_service(  # type: ignore[arg-type]
                    session,
                    999,
                    0,
                )

        self.assertIs(
            session.transaction.exception_type,
            ProductNotFoundError,
        )


class ProductStatusRouteTestCase(unittest.TestCase):
    # 为商品状态路由注入隔离会话，并绕过已单独覆盖的认证实现。
    def setUp(self) -> None:
        self.session = object()

        # 返回商品状态接口测试专用的数据库会话替身。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session

    # 清理依赖覆盖，避免商品状态测试影响其他路由。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证接口修改商品可见状态并返回更新后的全部商品字段。
    def test_route_updates_product_visibility_status(self) -> None:
        service_mock = AsyncMock(
            return_value=ProductDetails(
                id=2,
                name="运动水杯",
                description="运动补水",
                points_required=50,
                image_url="/运动水杯.jpg",
                status=0,
            )
        )

        with patch(
            "app.router.product.update_product_visibility_status_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.patch(
                    "/flame/admin/api/product/2/status",
                    json={"status": 0},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "id": 2,
                "name": "运动水杯",
                "description": "运动补水",
                "points_required": 50,
                "image_url": "/运动水杯.jpg",
                "status": 0,
            },
        )
        service_mock.assert_awaited_once_with(self.session, 2, 0)

    # 验证商品不存在时接口返回明确的 404 响应。
    def test_route_reports_missing_product(self) -> None:
        with patch(
            "app.router.product.update_product_visibility_status_service",
            new=AsyncMock(side_effect=ProductNotFoundError),
        ):
            with TestClient(app) as client:
                response = client.patch(
                    "/flame/admin/api/product/999/status",
                    json={"status": 0},
                )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "奖品不存在"})

    # 验证状态必须是严格整数 0 或 1，且请求体不能携带额外字段。
    def test_route_validates_product_visibility_status(self) -> None:
        invalid_payloads = (
            {},
            {"status": -1},
            {"status": 2},
            {"status": True},
            {"status": "1"},
            {"status": 1, "visible": True},
        )
        service_mock = AsyncMock()

        with patch(
            "app.router.product.update_product_visibility_status_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                for payload in invalid_payloads:
                    with self.subTest(payload=payload):
                        response = client.patch(
                            "/flame/admin/api/product/2/status",
                            json=payload,
                        )
                        self.assertEqual(response.status_code, 422)

        service_mock.assert_not_awaited()

    # 验证商品 ID 必须为正整数，非法路径不会进入业务服务。
    def test_route_validates_product_id(self) -> None:
        service_mock = AsyncMock()

        with patch(
            "app.router.product.update_product_visibility_status_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                for product_id in ("0", "-1", "invalid"):
                    with self.subTest(product_id=product_id):
                        response = client.patch(
                            f"/flame/admin/api/product/{product_id}/status",
                            json={"status": 0},
                        )
                        self.assertEqual(response.status_code, 422)

        service_mock.assert_not_awaited()

    # 验证商品状态接口继承管理员认证，未登录请求不会进入写服务。
    def test_route_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.product.update_product_visibility_status_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.patch(
                    "/flame/admin/api/product/2/status",
                    json={"status": 0},
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()
