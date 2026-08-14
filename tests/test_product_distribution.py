import unittest
from types import TracebackType
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.repositories.products import (
    GiftDistributionRecord,
    LatestPointRecord,
    fetch_gift_distribution_for_update,
    fetch_gift_distribution_user_id,
    fetch_latest_point_record_for_update,
    insert_exchange_refund_point_record,
    lock_user_for_point_update,
    reject_gift_distribution,
    update_gift_distribution_status,
)
from app.router.dependencies import require_admin_token
from app.services.products import (
    EXCHANGE_REFUND_DESCRIPTION,
    REJECTED_DISTRIBUTION_DESCRIPTION,
    GiftDistributionNotFoundError,
    GiftDistributionResult,
    GiftDistributionStatusConflictError,
    InvalidGiftDistributionRecordError,
    PointBalanceConsistencyError,
    process_gift_distribution,
)


class FakeMappingsResult:
    # 保存单条预设映射行，以模拟 SQLAlchemy 查询结果。
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    # 返回自身以支持仓储使用 mappings().first() 调用链。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回预设行，空值表示目标记录不存在。
    def first(self) -> dict[str, object] | None:
        return self.row


class FakeRepositorySession:
    # 保存预设查询结果，并捕获仓储执行的 SQL 与参数。
    def __init__(self, row: dict[str, object] | None) -> None:
        self.result = FakeMappingsResult(row)
        self.statement = None
        self.params = None

    # 模拟参数化异步数据库操作，避免访问开发数据库。
    async def exec(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> FakeMappingsResult:
        self.statement = statement
        self.params = params
        return self.result


class FakeTransactionContext:
    # 初始化可观察事务，并记录异常退出类型以验证回滚路径。
    def __init__(self) -> None:
        self.entered = False
        self.exited = False
        self.exception_type: type[BaseException] | None = None

    # 标记礼品审核用例已经进入事务。
    async def __aenter__(self) -> "FakeTransactionContext":
        self.entered = True
        return self

    # 标记事务退出并保留异常传播，以模拟真实提交或回滚行为。
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
    # 为礼品审核服务测试提供单一可观察事务。
    def __init__(self) -> None:
        self.transaction = FakeTransactionContext()

    # 返回当前审核用例唯一的事务上下文。
    def begin(self) -> FakeTransactionContext:
        return self.transaction


# 构造有效商品兑换流水，允许测试只覆盖当前关心的状态或积分字段。
def build_distribution_record(
    gift_distribution_status: str = "pending",
    *,
    change_type: str = "exchange",
    product_id: int | None = 5,
    change_points: int = -30,
    record_status: int = 1,
) -> GiftDistributionRecord:
    return GiftDistributionRecord(
        id=31,
        user_id="user-1",
        change_type=change_type,
        product_id=product_id,
        change_points=change_points,
        status=record_status,
        gift_distribution_status=gift_distribution_status,
    )


class GiftDistributionRepositoryTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证仓储按流水主键加行锁读取审核和退款所需全部字段。
    async def test_repository_locks_distribution_record(self) -> None:
        session = FakeRepositorySession(
            {
                "id": 31,
                "user_id": "user-1",
                "change_type": "exchange",
                "product_id": 5,
                "change_points": -30,
                "status": 1,
                "gift_distribution_status": "pending",
            }
        )

        distribution = await fetch_gift_distribution_for_update(  # type: ignore[arg-type]
            session,
            point_record_id=31,
        )

        self.assertEqual(distribution, build_distribution_record())
        self.assertEqual(session.params, {"point_record_id": 31})
        sql = str(session.statement)
        self.assertIn("point_record.change_points", sql)
        self.assertIn("WHERE point_record.id = :point_record_id", sql)
        self.assertIn("FOR UPDATE", sql)

    # 验证拒绝分支可以先读取所属用户，再按统一顺序取得用户级锁。
    async def test_repository_reads_owner_and_locks_user(self) -> None:
        owner_session = FakeRepositorySession({"user_id": "user-1"})
        user_id = await fetch_gift_distribution_user_id(  # type: ignore[arg-type]
            owner_session,
            point_record_id=31,
        )

        lock_session = FakeRepositorySession({"id": "user-1"})
        locked = await lock_user_for_point_update(  # type: ignore[arg-type]
            lock_session,
            user_id="user-1",
        )

        self.assertEqual(user_id, "user-1")
        self.assertTrue(locked)
        self.assertNotIn("FOR UPDATE", str(owner_session.statement))
        self.assertIn("FOR UPDATE", str(lock_session.statement))
        self.assertEqual(lock_session.params, {"user_id": "user-1"})

    # 验证最新有效积分流水按时间与主键倒序锁定并映射当前余额。
    async def test_repository_locks_latest_point_record(self) -> None:
        session = FakeRepositorySession({"id": 40, "points_after": 70})

        latest = await fetch_latest_point_record_for_update(  # type: ignore[arg-type]
            session,
            user_id="user-1",
        )

        self.assertEqual(latest, LatestPointRecord(40, 70))
        sql = str(session.statement)
        self.assertIn("point_record.status = 1", sql)
        self.assertIn("point_record.created_at DESC", sql)
        self.assertIn("point_record.id DESC", sql)
        self.assertIn("FOR UPDATE", sql)

    # 验证确认发放只修改礼品状态，不触及积分与描述字段。
    async def test_repository_only_updates_distributed_status(self) -> None:
        session = FakeRepositorySession(None)

        await update_gift_distribution_status(  # type: ignore[arg-type]
            session,
            point_record_id=31,
        )

        sql = str(session.statement)
        self.assertIn("SET gift_distribution_status = 'distributed'", sql)
        self.assertNotIn("change_points", sql)
        self.assertNotIn("points_after", sql)
        self.assertNotIn("description", sql)

    # 验证拒绝发放同时写入拒绝状态和固定用户提示。
    async def test_repository_rejects_distribution_with_description(self) -> None:
        session = FakeRepositorySession(None)

        await reject_gift_distribution(  # type: ignore[arg-type]
            session,
            point_record_id=31,
            description=REJECTED_DISTRIBUTION_DESCRIPTION,
        )

        sql = str(session.statement)
        self.assertIn("gift_distribution_status = 'rejected'", sql)
        self.assertIn("description = :description", sql)
        self.assertEqual(
            session.params,
            {
                "point_record_id": 31,
                "description": "发放失败，请联系管理员",
            },
        )

    # 验证退款流水使用独立类型、正向积分和最新余额，且不产生待发放礼品。
    async def test_repository_inserts_exchange_refund(self) -> None:
        session = FakeRepositorySession(None)

        await insert_exchange_refund_point_record(  # type: ignore[arg-type]
            session,
            user_id="user-1",
            product_id=5,
            refund_points=30,
            points_after=100,
            description=EXCHANGE_REFUND_DESCRIPTION,
        )

        sql = str(session.statement)
        self.assertIn("INSERT INTO point_record", sql)
        self.assertIn("'exchange_refund'", sql)
        self.assertIn("'pending'", sql)
        self.assertEqual(
            session.params,
            {
                "user_id": "user-1",
                "product_id": 5,
                "refund_points": 30,
                "points_after": 100,
                "description": "礼品拒绝发放，退还兑换积分",
            },
        )


class GiftDistributionServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证发放动作继续沿用原有行锁与状态更新逻辑。
    async def test_pending_distribution_is_marked_distributed(self) -> None:
        session = FakeServiceSession()
        update_mock = AsyncMock()

        with (
            patch(
                "app.services.products.fetch_gift_distribution_for_update",
                new=AsyncMock(return_value=build_distribution_record()),
            ),
            patch(
                "app.services.products.update_gift_distribution_status",
                new=update_mock,
            ),
        ):
            result = await process_gift_distribution(  # type: ignore[arg-type]
                session,
                point_record_id=31,
                decision="distributed",
            )

        self.assertEqual(result, GiftDistributionResult(31, "distributed"))
        update_mock.assert_awaited_once_with(session, 31)
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)

    # 验证拒绝动作覆盖原描述，并按用户最新余额新增等额退款流水。
    async def test_rejected_distribution_refunds_latest_balance(self) -> None:
        session = FakeServiceSession()
        reject_mock = AsyncMock()
        refund_mock = AsyncMock()

        with (
            patch(
                "app.services.products.fetch_gift_distribution_user_id",
                new=AsyncMock(return_value="user-1"),
            ),
            patch(
                "app.services.products.lock_user_for_point_update",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.services.products.fetch_gift_distribution_for_update",
                new=AsyncMock(return_value=build_distribution_record()),
            ),
            patch(
                "app.services.products.fetch_latest_point_record_for_update",
                new=AsyncMock(return_value=LatestPointRecord(40, 70)),
            ),
            patch(
                "app.services.products.reject_gift_distribution",
                new=reject_mock,
            ),
            patch(
                "app.services.products.insert_exchange_refund_point_record",
                new=refund_mock,
            ),
        ):
            result = await process_gift_distribution(  # type: ignore[arg-type]
                session,
                point_record_id=31,
                decision="rejected",
            )

        self.assertEqual(result, GiftDistributionResult(31, "rejected"))
        reject_mock.assert_awaited_once_with(
            session,
            31,
            "发放失败，请联系管理员",
        )
        refund_mock.assert_awaited_once_with(
            session,
            "user-1",
            5,
            30,
            100,
            "礼品拒绝发放，退还兑换积分",
        )

    # 验证历史兑换扣分记录为零时允许拒绝，但退款流水不得凭空增加余额。
    async def test_zero_deduction_rejection_keeps_latest_balance(self) -> None:
        session = FakeServiceSession()
        refund_mock = AsyncMock()

        with (
            patch(
                "app.services.products.fetch_gift_distribution_user_id",
                new=AsyncMock(return_value="user-1"),
            ),
            patch(
                "app.services.products.lock_user_for_point_update",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.services.products.fetch_gift_distribution_for_update",
                new=AsyncMock(
                    return_value=build_distribution_record(change_points=0)
                ),
            ),
            patch(
                "app.services.products.fetch_latest_point_record_for_update",
                new=AsyncMock(return_value=LatestPointRecord(40, 70)),
            ),
            patch(
                "app.services.products.reject_gift_distribution",
                new=AsyncMock(),
            ),
            patch(
                "app.services.products.insert_exchange_refund_point_record",
                new=refund_mock,
            ),
        ):
            result = await process_gift_distribution(  # type: ignore[arg-type]
                session,
                31,
                "rejected",
            )

        self.assertEqual(result.gift_distribution_status, "rejected")
        refund_mock.assert_awaited_once_with(
            session,
            "user-1",
            5,
            0,
            70,
            "礼品拒绝发放，退还兑换积分",
        )

    # 验证重复提交相同终态按幂等成功，不重复写状态或退还积分。
    async def test_same_terminal_decision_is_idempotent(self) -> None:
        for decision in ("distributed", "rejected"):
            with self.subTest(decision=decision):
                session = FakeServiceSession()
                update_mock = AsyncMock()
                refund_mock = AsyncMock()
                patches = [
                    patch(
                        "app.services.products.fetch_gift_distribution_for_update",
                        new=AsyncMock(
                            return_value=build_distribution_record(decision)
                        ),
                    ),
                    patch(
                        "app.services.products.update_gift_distribution_status",
                        new=update_mock,
                    ),
                    patch(
                        "app.services.products.insert_exchange_refund_point_record",
                        new=refund_mock,
                    ),
                ]
                if decision == "rejected":
                    patches.extend(
                        [
                            patch(
                                "app.services.products.fetch_gift_distribution_user_id",
                                new=AsyncMock(return_value="user-1"),
                            ),
                            patch(
                                "app.services.products.lock_user_for_point_update",
                                new=AsyncMock(return_value=True),
                            ),
                        ]
                    )
                with patches[0], patches[1], patches[2]:
                    if decision == "rejected":
                        with patches[3], patches[4]:
                            result = await process_gift_distribution(  # type: ignore[arg-type]
                                session,
                                31,
                                decision,
                            )
                    else:
                        result = await process_gift_distribution(  # type: ignore[arg-type]
                            session,
                            31,
                            decision,
                        )

                self.assertEqual(result.gift_distribution_status, decision)
                update_mock.assert_not_awaited()
                refund_mock.assert_not_awaited()

    # 验证两个终态不能互相覆盖，冲突时事务整体回滚。
    async def test_different_terminal_decision_conflicts(self) -> None:
        cases = (("distributed", "rejected"), ("rejected", "distributed"))
        for current_status, decision in cases:
            with self.subTest(current_status=current_status, decision=decision):
                session = FakeServiceSession()
                with (
                    patch(
                        "app.services.products.fetch_gift_distribution_user_id",
                        new=AsyncMock(return_value="user-1"),
                    ),
                    patch(
                        "app.services.products.lock_user_for_point_update",
                        new=AsyncMock(return_value=True),
                    ),
                    patch(
                        "app.services.products.fetch_gift_distribution_for_update",
                        new=AsyncMock(
                            return_value=build_distribution_record(current_status)
                        ),
                    ),
                ):
                    with self.assertRaises(GiftDistributionStatusConflictError):
                        await process_gift_distribution(  # type: ignore[arg-type]
                            session,
                            31,
                            decision,
                        )

    # 验证非兑换、无商品关联或已作废流水均不能进入礼品审核终态。
    async def test_invalid_distribution_record_is_rejected(self) -> None:
        invalid_records = (
            build_distribution_record(change_type="season_reward"),
            build_distribution_record(product_id=None),
            build_distribution_record(record_status=0),
        )
        for distribution in invalid_records:
            with self.subTest(distribution=distribution):
                session = FakeServiceSession()
                with patch(
                    "app.services.products.fetch_gift_distribution_for_update",
                    new=AsyncMock(return_value=distribution),
                ):
                    with self.assertRaises(InvalidGiftDistributionRecordError):
                        await process_gift_distribution(  # type: ignore[arg-type]
                            session,
                            31,
                            "distributed",
                        )

    # 验证兑换扣分异常或最新积分流水缺失时拒绝分支不会生成退款。
    async def test_rejection_requires_consistent_point_balance(self) -> None:
        cases = (
            (build_distribution_record(change_points=30), LatestPointRecord(40, 70)),
            (build_distribution_record(), None),
        )
        for distribution, latest in cases:
            with self.subTest(distribution=distribution, latest=latest):
                session = FakeServiceSession()
                with (
                    patch(
                        "app.services.products.fetch_gift_distribution_user_id",
                        new=AsyncMock(return_value="user-1"),
                    ),
                    patch(
                        "app.services.products.lock_user_for_point_update",
                        new=AsyncMock(return_value=True),
                    ),
                    patch(
                        "app.services.products.fetch_gift_distribution_for_update",
                        new=AsyncMock(return_value=distribution),
                    ),
                    patch(
                        "app.services.products.fetch_latest_point_record_for_update",
                        new=AsyncMock(return_value=latest),
                    ),
                ):
                    with self.assertRaises(PointBalanceConsistencyError):
                        await process_gift_distribution(  # type: ignore[arg-type]
                            session,
                            31,
                            "rejected",
                        )

                self.assertIs(
                    session.transaction.exception_type,
                    PointBalanceConsistencyError,
                )

    # 验证退款流水写入失败会使原拒绝状态随同事务一起回滚。
    async def test_refund_insert_failure_rolls_back_rejection(self) -> None:
        session = FakeServiceSession()

        with (
            patch(
                "app.services.products.fetch_gift_distribution_user_id",
                new=AsyncMock(return_value="user-1"),
            ),
            patch(
                "app.services.products.lock_user_for_point_update",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.services.products.fetch_gift_distribution_for_update",
                new=AsyncMock(return_value=build_distribution_record()),
            ),
            patch(
                "app.services.products.fetch_latest_point_record_for_update",
                new=AsyncMock(return_value=LatestPointRecord(40, 70)),
            ),
            patch(
                "app.services.products.reject_gift_distribution",
                new=AsyncMock(),
            ),
            patch(
                "app.services.products.insert_exchange_refund_point_record",
                new=AsyncMock(side_effect=RuntimeError("insert failed")),
            ),
        ):
            with self.assertRaises(RuntimeError):
                await process_gift_distribution(  # type: ignore[arg-type]
                    session,
                    31,
                    "rejected",
                )

        self.assertIs(session.transaction.exception_type, RuntimeError)


class GiftDistributionRouteTestCase(unittest.TestCase):
    # 为礼品审核路由注入隔离会话，并绕过已单独验证的认证逻辑。
    def setUp(self) -> None:
        self.session = object()

        # 返回当前礼品审核接口测试专用的数据库会话替身。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session

    # 清理依赖覆盖，防止礼品审核测试影响其他路由。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证旧请求省略结论时仍默认按确认发放处理。
    def test_route_defaults_to_distributed(self) -> None:
        service_mock = AsyncMock(
            return_value=GiftDistributionResult(31, "distributed")
        )

        with patch(
            "app.router.product.process_gift_distribution",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/flame/admin/api/product/distribute",
                    json={"id": 31},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"id": 31, "gift_distribution_status": "distributed"},
        )
        service_mock.assert_awaited_once_with(self.session, 31, "distributed")

    # 验证接口接收拒绝结论并返回最终拒绝发放状态。
    def test_route_rejects_gift_distribution(self) -> None:
        service_mock = AsyncMock(
            return_value=GiftDistributionResult(31, "rejected")
        )

        with patch(
            "app.router.product.process_gift_distribution",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/flame/admin/api/product/distribute",
                    json={"id": 31, "decision": "rejected"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"id": 31, "gift_distribution_status": "rejected"},
        )
        service_mock.assert_awaited_once_with(self.session, 31, "rejected")

    # 验证不存在、无效兑换、状态冲突和积分异常映射为稳定 HTTP 错误。
    def test_route_maps_distribution_errors(self) -> None:
        cases = (
            (GiftDistributionNotFoundError(), 404, "兑换流水不存在"),
            (
                InvalidGiftDistributionRecordError(),
                409,
                "该流水不是有效的商品兑换记录",
            ),
            (
                GiftDistributionStatusConflictError(),
                409,
                "礼品发放状态异常，无法更新",
            ),
            (
                PointBalanceConsistencyError(),
                409,
                "用户积分流水不完整，无法拒绝发放",
            ),
        )
        for exception, expected_status, expected_detail in cases:
            with self.subTest(exception=type(exception).__name__):
                with patch(
                    "app.router.product.process_gift_distribution",
                    new=AsyncMock(side_effect=exception),
                ):
                    with TestClient(app) as client:
                        response = client.post(
                            "/flame/admin/api/product/distribute",
                            json={"id": 31, "decision": "rejected"},
                        )

                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.json(), {"detail": expected_detail})

    # 验证流水 ID 或审核结论不合法时由请求边界拒绝。
    def test_route_validates_distribution_request(self) -> None:
        with TestClient(app) as client:
            missing_id_response = client.post(
                "/flame/admin/api/product/distribute",
                json={"decision": "rejected"},
            )
            invalid_id_response = client.post(
                "/flame/admin/api/product/distribute",
                json={"id": 0},
            )
            invalid_decision_response = client.post(
                "/flame/admin/api/product/distribute",
                json={"id": 31, "decision": "pending"},
            )

        self.assertEqual(missing_id_response.status_code, 422)
        self.assertEqual(invalid_id_response.status_code, 422)
        self.assertEqual(invalid_decision_response.status_code, 422)

    # 验证礼品审核接口继承管理员认证，未登录时不会执行写服务。
    def test_route_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.product.process_gift_distribution",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/flame/admin/api/product/distribute",
                    json={"id": 31, "decision": "rejected"},
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()
