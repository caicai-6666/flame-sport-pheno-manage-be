import unittest
from decimal import Decimal
from types import TracebackType
from unittest.mock import AsyncMock, call, patch

from fastapi.testclient import TestClient

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.repositories.proofs import (
    ProofBackfillCandidate,
    ProofForFinalReview,
    SeasonUserProjectProgress,
    fetch_locked_backfill_candidates,
)
from app.router.dependencies import require_admin_token
from app.services.proofs import (
    FinalReviewResult,
    ProofFinalReviewConflictError,
    ProofForFinalReviewNotFoundError,
    ProofProgressConsistencyError,
    record_proof_final_review,
)


class FakeMappingsResult:
    # 保存回补候选行并模拟 SQLAlchemy 的映射结果调用链。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    # 返回自身以支持仓储调用 mappings().all()。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回预设候选行，顺序代表数据库排序结果。
    def all(self) -> list[dict[str, object]]:
        return self.rows


class FakeRepositorySession:
    # 保存回补候选结果，并记录 SQL 与绑定参数供断言使用。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.result = FakeMappingsResult(rows)
        self.statement = None
        self.params = None

    # 模拟一次参数化异步查询，隔离真实开发数据库。
    async def exec(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> FakeMappingsResult:
        self.statement = statement
        self.params = params
        return self.result


class FakeTransactionContext:
    # 初始化事务状态并保留退出时收到的异常类型。
    def __init__(self) -> None:
        self.entered = False
        self.exited = False
        self.exception_type: type[BaseException] | None = None

    # 标记终审服务已进入事务。
    async def __aenter__(self) -> "FakeTransactionContext":
        self.entered = True
        return self

    # 标记事务退出且不吞掉异常，以模拟真实自动提交或回滚语义。
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
    # 为每个终审服务测试提供独立的可观察事务。
    def __init__(self) -> None:
        self.transaction = FakeTransactionContext()

    # 返回终审用例唯一的事务上下文。
    def begin(self) -> FakeTransactionContext:
        return self.transaction


# 构造统一的待终审凭证，允许测试按需覆盖实际进度贡献。
def build_pending_proof(increase: str = "0.4000") -> ProofForFinalReview:
    return ProofForFinalReview(
        id=501,
        season_user_id=101,
        project_id=5,
        review_status="preliminary_approved",
        progress_delta=Decimal("0.5000"),
        increase=Decimal(increase),
        status=1,
    )


class ProofFinalReviewRepositoryTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证回补查询限定同一用户项目，并将终审通过凭证置于待终审凭证之前。
    async def test_backfill_candidates_use_required_scope_and_priority(
        self,
    ) -> None:
        session = FakeRepositorySession(
            [
                {
                    "id": 601,
                    "review_status": "approved",
                    "progress_delta": Decimal("0.3000"),
                    "increase": Decimal("0.1000"),
                },
                {
                    "id": 602,
                    "review_status": "preliminary_approved",
                    "progress_delta": Decimal("0.4000"),
                    "increase": Decimal("0.2000"),
                },
            ]
        )

        candidates = await fetch_locked_backfill_candidates(  # type: ignore[arg-type]
            session,
            season_user_id=101,
            project_id=5,
            excluded_proof_record_id=501,
        )

        self.assertEqual(
            candidates,
            (
                ProofBackfillCandidate(
                    id=601,
                    review_status="approved",
                    progress_delta=Decimal("0.3000"),
                    increase=Decimal("0.1000"),
                ),
                ProofBackfillCandidate(
                    id=602,
                    review_status="preliminary_approved",
                    progress_delta=Decimal("0.4000"),
                    increase=Decimal("0.2000"),
                ),
            ),
        )
        self.assertEqual(
            session.params,
            {
                "season_user_id": 101,
                "project_id": 5,
                "excluded_proof_record_id": 501,
            },
        )
        sql = str(session.statement)
        self.assertIn("proof_record.status = 1", sql)
        self.assertIn("'approved'", sql)
        self.assertIn("'preliminary_approved'", sql)
        self.assertIn("progress_delta > proof_record.increase", sql)
        self.assertIn("WHEN 'approved' THEN 0", sql)
        self.assertIn("FOR UPDATE", sql)


class ProofFinalReviewServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证终审通过只覆盖审核字段，不调整凭证贡献或项目进度。
    async def test_approve_updates_review_without_progress_changes(self) -> None:
        session = FakeServiceSession()
        proof = build_pending_proof()
        fetch_proof_mock = AsyncMock(side_effect=[proof, proof])
        update_review_mock = AsyncMock()

        with (
            patch(
                "app.services.proofs.fetch_proof_for_final_review",
                new=fetch_proof_mock,
            ),
            patch(
                "app.services.proofs.update_proof_final_review",
                new=update_review_mock,
            ),
            patch(
                "app.services.proofs.fetch_locked_project_progress",
                new=AsyncMock(),
            ) as project_mock,
        ):
            result = await record_proof_final_review(  # type: ignore[arg-type]
                session,
                proof_record_id=501,
                review_comment="凭证符合要求",
                decision="approved",
            )

        self.assertEqual(result.review_status, "approved")
        self.assertEqual(result.rolled_back_progress, Decimal("0.0000"))
        self.assertIsNone(result.completion_progress)
        update_review_mock.assert_awaited_once_with(
            session,
            501,
            "approved",
            "凭证符合要求",
        )
        project_mock.assert_not_awaited()
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)

    # 验证拒绝后先使用终审通过候选，再使用待终审候选补满释放进度。
    async def test_reject_rolls_back_and_backfills_in_priority_order(
        self,
    ) -> None:
        session = FakeServiceSession()
        proof = build_pending_proof()
        candidates = (
            ProofBackfillCandidate(
                id=601,
                review_status="approved",
                progress_delta=Decimal("0.3000"),
                increase=Decimal("0.1000"),
            ),
            ProofBackfillCandidate(
                id=602,
                review_status="preliminary_approved",
                progress_delta=Decimal("0.5000"),
                increase=Decimal("0.1000"),
            ),
        )
        update_increase_mock = AsyncMock()
        update_progress_mock = AsyncMock()

        with (
            patch(
                "app.services.proofs.fetch_proof_for_final_review",
                new=AsyncMock(side_effect=[proof, proof]),
            ),
            patch(
                "app.services.proofs.fetch_locked_project_progress",
                new=AsyncMock(
                    return_value=SeasonUserProjectProgress(
                        id=801,
                        completion_progress=Decimal("0.9000"),
                    )
                ),
            ),
            patch(
                "app.services.proofs.fetch_locked_backfill_candidates",
                new=AsyncMock(return_value=candidates),
            ),
            patch(
                "app.services.proofs.update_proof_final_review",
                new=AsyncMock(),
            ) as update_review_mock,
            patch(
                "app.services.proofs.update_proof_increase",
                new=update_increase_mock,
            ),
            patch(
                "app.services.proofs.update_project_completion_progress",
                new=update_progress_mock,
            ),
        ):
            result = await record_proof_final_review(  # type: ignore[arg-type]
                session,
                proof_record_id=501,
                review_comment="图片日期无法确认",
                decision="rejected",
            )

        update_review_mock.assert_awaited_once_with(
            session,
            501,
            "rejected",
            "图片日期无法确认",
            Decimal("0.0000"),
        )
        self.assertEqual(
            update_increase_mock.await_args_list,
            [
                call(session, 601, Decimal("0.3000")),
                call(session, 602, Decimal("0.3000")),
            ],
        )
        update_progress_mock.assert_awaited_once_with(
            session,
            801,
            Decimal("0.9000"),
        )
        self.assertEqual(result.rolled_back_progress, Decimal("0.4000"))
        self.assertEqual(result.backfilled_progress, Decimal("0.4000"))
        self.assertEqual(result.completion_progress, Decimal("0.9000"))

    # 验证候选贡献不足时项目进度只回补实际可用部分，不伪造满进度。
    async def test_reject_keeps_remaining_progress_reduction(self) -> None:
        session = FakeServiceSession()
        proof = build_pending_proof()
        candidate = ProofBackfillCandidate(
            id=601,
            review_status="approved",
            progress_delta=Decimal("0.2000"),
            increase=Decimal("0.1000"),
        )
        update_progress_mock = AsyncMock()

        with (
            patch(
                "app.services.proofs.fetch_proof_for_final_review",
                new=AsyncMock(side_effect=[proof, proof]),
            ),
            patch(
                "app.services.proofs.fetch_locked_project_progress",
                new=AsyncMock(
                    return_value=SeasonUserProjectProgress(
                        id=801,
                        completion_progress=Decimal("0.9000"),
                    )
                ),
            ),
            patch(
                "app.services.proofs.fetch_locked_backfill_candidates",
                new=AsyncMock(return_value=(candidate,)),
            ),
            patch(
                "app.services.proofs.update_proof_final_review",
                new=AsyncMock(),
            ),
            patch(
                "app.services.proofs.update_proof_increase",
                new=AsyncMock(),
            ),
            patch(
                "app.services.proofs.update_project_completion_progress",
                new=update_progress_mock,
            ),
        ):
            result = await record_proof_final_review(  # type: ignore[arg-type]
                session,
                proof_record_id=501,
                review_comment="凭证不符合要求",
                decision="rejected",
            )

        self.assertEqual(result.backfilled_progress, Decimal("0.1000"))
        self.assertEqual(result.completion_progress, Decimal("0.6000"))
        update_progress_mock.assert_awaited_once_with(
            session,
            801,
            Decimal("0.6000"),
        )

    # 验证已终审凭证被拒绝重复操作，并让事务按异常路径退出回滚。
    async def test_repeated_final_review_is_rejected(self) -> None:
        session = FakeServiceSession()
        reviewed_proof = ProofForFinalReview(
            id=501,
            season_user_id=101,
            project_id=5,
            review_status="approved",
            progress_delta=Decimal("0.5000"),
            increase=Decimal("0.4000"),
            status=1,
        )

        with patch(
            "app.services.proofs.fetch_proof_for_final_review",
            new=AsyncMock(return_value=reviewed_proof),
        ):
            with self.assertRaises(ProofFinalReviewConflictError):
                await record_proof_final_review(  # type: ignore[arg-type]
                    session,
                    proof_record_id=501,
                    review_comment="重复终审",
                    decision="approved",
                )

        self.assertIs(
            session.transaction.exception_type,
            ProofFinalReviewConflictError,
        )

    # 验证拒绝时缺少有效用户项目进度会中止事务，避免产生部分更新。
    async def test_reject_stops_on_missing_project_progress(self) -> None:
        session = FakeServiceSession()
        proof = build_pending_proof()

        with (
            patch(
                "app.services.proofs.fetch_proof_for_final_review",
                new=AsyncMock(return_value=proof),
            ),
            patch(
                "app.services.proofs.fetch_locked_project_progress",
                new=AsyncMock(return_value=None),
            ),
        ):
            with self.assertRaises(ProofProgressConsistencyError):
                await record_proof_final_review(  # type: ignore[arg-type]
                    session,
                    proof_record_id=501,
                    review_comment="拒绝凭证",
                    decision="rejected",
                )

        self.assertIs(
            session.transaction.exception_type,
            ProofProgressConsistencyError,
        )


class ProofFinalReviewRouteTestCase(unittest.TestCase):
    # 为终审路由注入隔离会话并绕过已单独验证的管理员认证。
    def setUp(self) -> None:
        self.session = object()

        # 返回当前终审接口测试使用的会话替身。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session

    # 清理依赖覆盖，防止终审接口测试影响其他路由。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证终审拒绝接口返回回退、回补和最终项目进度。
    def test_route_returns_final_review_result(self) -> None:
        service_mock = AsyncMock(
            return_value=FinalReviewResult(
                proof_record_id=501,
                review_status="rejected",
                review_comment="凭证不符合要求",
                rolled_back_progress=Decimal("0.4000"),
                backfilled_progress=Decimal("0.2500"),
                completion_progress=Decimal("0.7500"),
            )
        )

        with patch(
            "app.router.proof.record_proof_final_review",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/flame/admin/api/proof/final-review",
                    json={
                        "proof_record_id": 501,
                        "review_comment": "凭证不符合要求",
                        "decision": "rejected",
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "proof_record_id": 501,
                "review_status": "rejected",
                "review_comment": "凭证不符合要求",
                "rolled_back_progress": 0.4,
                "backfilled_progress": 0.25,
                "completion_progress": 0.75,
            },
        )
        service_mock.assert_awaited_once_with(
            self.session,
            501,
            "凭证不符合要求",
            "rejected",
        )

    # 验证凭证不存在、重复终审和进度异常具有独立安全错误语义。
    def test_route_maps_final_review_errors(self) -> None:
        cases = (
            (ProofForFinalReviewNotFoundError(), 404, "凭证不存在或已失效"),
            (
                ProofFinalReviewConflictError(),
                409,
                "凭证已完成终审或当前状态不允许终审",
            ),
            (
                ProofProgressConsistencyError(),
                409,
                "凭证贡献与项目进度不一致，无法完成终审",
            ),
        )
        for exception, status_code, detail in cases:
            with self.subTest(exception=type(exception).__name__):
                with patch(
                    "app.router.proof.record_proof_final_review",
                    new=AsyncMock(side_effect=exception),
                ):
                    with TestClient(app) as client:
                        response = client.post(
                            "/flame/admin/api/proof/final-review",
                            json={
                                "proof_record_id": 501,
                                "review_comment": "终审意见",
                                "decision": "rejected",
                            },
                        )

                self.assertEqual(response.status_code, status_code)
                self.assertEqual(response.json(), {"detail": detail})

    # 验证记录 ID、终审评语和决定不合法时在 HTTP 边界返回校验错误。
    def test_route_validates_final_review_request(self) -> None:
        invalid_payloads = (
            {
                "proof_record_id": 0,
                "review_comment": "终审意见",
                "decision": "approved",
            },
            {
                "proof_record_id": 501,
                "review_comment": "   ",
                "decision": "approved",
            },
            {
                "proof_record_id": 501,
                "review_comment": "终审意见",
                "decision": "pending",
            },
        )
        with TestClient(app) as client:
            responses = [
                client.post(
                    "/flame/admin/api/proof/final-review",
                    json=payload,
                )
                for payload in invalid_payloads
            ]

        self.assertTrue(all(response.status_code == 422 for response in responses))

    # 验证终审评语字段允许显式为空，兼容通过时无需补充说明的业务口径。
    def test_route_allows_null_review_comment(self) -> None:
        service_mock = AsyncMock(
            return_value=FinalReviewResult(
                proof_record_id=501,
                review_status="approved",
                review_comment=None,
                rolled_back_progress=Decimal("0.0000"),
                backfilled_progress=Decimal("0.0000"),
                completion_progress=None,
            )
        )

        with patch(
            "app.router.proof.record_proof_final_review",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/flame/admin/api/proof/final-review",
                    json={
                        "proof_record_id": 501,
                        "review_comment": None,
                        "decision": "approved",
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["review_comment"])
        service_mock.assert_awaited_once_with(
            self.session,
            501,
            None,
            "approved",
        )

    # 验证终审写接口继承统一认证，未登录时不会执行写用例。
    def test_route_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.proof.record_proof_final_review",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/flame/admin/api/proof/final-review",
                    json={
                        "proof_record_id": 501,
                        "review_comment": "终审意见",
                        "decision": "approved",
                    },
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()
