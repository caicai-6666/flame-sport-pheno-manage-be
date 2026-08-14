import unittest
from datetime import datetime
from types import TracebackType
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.repositories.products import (
    PendingGiftDistribution,
    fetch_pending_gift_distributions,
)
from app.router.dependencies import require_admin_token
from app.services.products import list_pending_gift_distributions


class FakeMappingsResult:
    # 保存待发放礼品查询的预设映射行，以模拟 SQLAlchemy 查询结果。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    # 返回自身以支持仓储使用 mappings().all() 调用链。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回预设兑换流水，并保持仓储查询给出的发放顺序。
    def all(self) -> list[dict[str, object]]:
        return self.rows


class FakeRepositorySession:
    # 保存预设结果，并捕获待发放礼品仓储执行的 SQL。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.result = FakeMappingsResult(rows)
        self.statement = None

    # 模拟待发放礼品的单次异步查询，避免访问开发数据库。
    async def exec(self, statement: object) -> FakeMappingsResult:
        self.statement = statement
        return self.result


class FakeTransactionContext:
    # 初始化可观察的礼品查询事务状态。
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    # 标记待发放礼品查询已经进入事务。
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
    # 为待发放礼品服务测试提供单一可观察事务。
    def __init__(self) -> None:
        self.transaction = FakeTransactionContext()

    # 返回当前只读用例的事务上下文。
    def begin(self) -> FakeTransactionContext:
        return self.transaction


class PendingGiftDistributionRepositoryTestCase(
    unittest.IsolatedAsyncioTestCase
):
    # 验证仓储映射待发放礼品字段，并包含完整有效性筛选与稳定顺序。
    async def test_repository_maps_pending_distributions(self) -> None:
        session = FakeRepositorySession(
            [
                {
                    "id": 31,
                    "user_id": "user-1",
                    "product_id": 5,
                    "description": "兑换商品：运动水杯",
                    "created_at": datetime(2026, 8, 12, 9, 30, 0),
                },
                {
                    "id": 32,
                    "user_id": "user-2",
                    "product_id": 8,
                    "description": None,
                    "created_at": datetime(2026, 8, 12, 10, 0, 0),
                },
            ]
        )

        distributions = await fetch_pending_gift_distributions(  # type: ignore[arg-type]
            session
        )

        self.assertEqual(
            distributions,
            (
                PendingGiftDistribution(
                    id=31,
                    user_id="user-1",
                    product_id=5,
                    description="兑换商品：运动水杯",
                    created_at=datetime(2026, 8, 12, 9, 30, 0),
                ),
                PendingGiftDistribution(
                    id=32,
                    user_id="user-2",
                    product_id=8,
                    description=None,
                    created_at=datetime(2026, 8, 12, 10, 0, 0),
                ),
            ),
        )
        sql = str(session.statement)
        self.assertIn("point_record.change_type = 'exchange'", sql)
        self.assertIn(
            "point_record.gift_distribution_status = 'pending'",
            sql,
        )
        self.assertIn("point_record.status = 1", sql)
        self.assertIn("point_record.product_id IS NOT NULL", sql)
        self.assertIn("point_record.created_at ASC", sql)

    # 验证没有有效待发放礼品时仓储返回空集合。
    async def test_repository_returns_empty_distributions(self) -> None:
        session = FakeRepositorySession([])

        distributions = await fetch_pending_gift_distributions(  # type: ignore[arg-type]
            session
        )

        self.assertEqual(distributions, ())


class PendingGiftDistributionServiceTestCase(
    unittest.IsolatedAsyncioTestCase
):
    # 验证待发放礼品服务在显式只读事务中调用仓储。
    async def test_service_manages_read_transaction(self) -> None:
        session = FakeServiceSession()
        expected = (
            PendingGiftDistribution(
                id=31,
                user_id="user-1",
                product_id=5,
                description=None,
                created_at=datetime(2026, 8, 12, 9, 30, 0),
            ),
        )
        repository_mock = AsyncMock(return_value=expected)

        with patch(
            "app.services.products.fetch_pending_gift_distributions",
            new=repository_mock,
        ):
            distributions = await list_pending_gift_distributions(  # type: ignore[arg-type]
                session
            )

        self.assertEqual(distributions, expected)
        repository_mock.assert_awaited_once_with(session)
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)


class PendingGiftDistributionRouteTestCase(unittest.TestCase):
    # 为礼品查询路由注入隔离会话，并绕过已单独覆盖的认证逻辑。
    def setUp(self) -> None:
        self.session = object()

        # 返回当前路由测试专用的数据库会话替身。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session

    # 清理依赖覆盖，防止礼品查询测试影响其他路由。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证接口完整返回待发放礼品所需字段和兑换时间。
    def test_route_returns_pending_distributions(self) -> None:
        service_mock = AsyncMock(
            return_value=(
                PendingGiftDistribution(
                    id=31,
                    user_id="user-1",
                    product_id=5,
                    description="兑换商品：运动水杯",
                    created_at=datetime(2026, 8, 12, 9, 30, 0),
                ),
            )
        )

        with patch(
            "app.router.product.list_pending_gift_distributions",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/product/pending-distributions"
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [
                {
                    "id": 31,
                    "user_id": "user-1",
                    "product_id": 5,
                    "description": "兑换商品：运动水杯",
                    "created_at": "2026-08-12T09:30:00",
                }
            ],
        )
        service_mock.assert_awaited_once_with(self.session)

    # 验证没有待发放礼品时接口返回成功空数组。
    def test_route_returns_empty_array(self) -> None:
        with patch(
            "app.router.product.list_pending_gift_distributions",
            new=AsyncMock(return_value=()),
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/product/pending-distributions"
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    # 验证待发放礼品接口继承管理员认证，未登录时不执行数据库查询。
    def test_route_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.product.list_pending_gift_distributions",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/product/pending-distributions",
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()
