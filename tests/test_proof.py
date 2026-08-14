import unittest
from datetime import date, datetime
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.repositories.proofs import (
    PendingFinalReviewProof,
    fetch_pending_final_review_proofs,
)
from app.router.dependencies import require_admin_token


class FakeMappingsResult:
    # 保存待终审查询的预设数据库行，模拟 SQLAlchemy 映射结果。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    # 返回自身以支持 result.mappings().all() 查询链。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回全部预设行并保持数据库排序结果。
    def all(self) -> list[dict[str, object]]:
        return self.rows


class FakeRepositorySession:
    # 保存预设结果，并记录仓储执行的 SQL 与绑定参数。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.result = FakeMappingsResult(rows)
        self.statement = None
        self.params = None

    # 模拟异步参数化查询，避免测试访问开发数据库。
    async def exec(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> FakeMappingsResult:
        self.statement = statement
        self.params = params
        return self.result


class PendingFinalReviewProofRepositoryTestCase(
    unittest.IsolatedAsyncioTestCase
):
    # 验证仓储映射待终审所需字段，并严格筛选初审通过的有效记录。
    async def test_repository_maps_pending_final_review_proofs(self) -> None:
        uploaded_at = datetime(2026, 8, 12, 10, 30, 45)
        session = FakeRepositorySession(
            [
                {
                    "id": 501,
                    "project_id": 5,
                    "image_url": "/proofs/501.jpg",
                    "created_at": uploaded_at,
                    "proof_date": date(2026, 8, 11),
                    "note": "晚间跑步 5 公里",
                    "review_comment": "距离满足单次要求",
                },
                {
                    "id": 500,
                    "project_id": 2,
                    "image_url": "/proofs/500.png",
                    "created_at": datetime(2026, 8, 10, 8, 0),
                    "proof_date": date(2026, 8, 10),
                    "note": None,
                    "review_comment": None,
                },
            ]
        )

        proofs = await fetch_pending_final_review_proofs(  # type: ignore[arg-type]
            session,
            season_user_id=101,
        )

        self.assertEqual(
            proofs,
            (
                PendingFinalReviewProof(
                    id=501,
                    project_id=5,
                    image_url="/proofs/501.jpg",
                    created_at=uploaded_at,
                    proof_date=date(2026, 8, 11),
                    note="晚间跑步 5 公里",
                    review_comment="距离满足单次要求",
                ),
                PendingFinalReviewProof(
                    id=500,
                    project_id=2,
                    image_url="/proofs/500.png",
                    created_at=datetime(2026, 8, 10, 8, 0),
                    proof_date=date(2026, 8, 10),
                    note=None,
                    review_comment=None,
                ),
            ),
        )
        self.assertEqual(session.params, {"season_user_id": 101})
        sql = str(session.statement)
        self.assertIn("FROM proof_record", sql)
        self.assertIn(
            "proof_record.review_status = 'preliminary_approved'",
            sql,
        )
        self.assertIn("proof_record.status = 1", sql)
        self.assertIn("proof_record.proof_date DESC", sql)
        self.assertNotIn("review_status = 'approved'", sql)

    # 验证指定参赛记录没有待终审凭证时返回空集合。
    async def test_repository_returns_empty_without_pending_proofs(self) -> None:
        session = FakeRepositorySession([])

        proofs = await fetch_pending_final_review_proofs(  # type: ignore[arg-type]
            session,
            season_user_id=101,
        )

        self.assertEqual(proofs, ())


class PendingFinalReviewProofRouteTestCase(unittest.TestCase):
    # 为待终审路由绕过已单独验证的认证，并注入不连接数据库的会话。
    def setUp(self) -> None:
        self.session = object()

        # 返回当前路由测试专用会话，数据访问由服务替身隔离。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session

    # 清理依赖覆盖，防止待终审测试影响其他接口。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证接口完整序列化待终审记录及其可空备注和初审意见。
    def test_pending_final_review_returns_proofs(self) -> None:
        service_mock = AsyncMock(
            return_value=(
                PendingFinalReviewProof(
                    id=501,
                    project_id=5,
                    image_url="/proofs/501.jpg",
                    created_at=datetime(2026, 8, 12, 10, 30, 45),
                    proof_date=date(2026, 8, 11),
                    note="晚间跑步 5 公里",
                    review_comment="距离满足单次要求",
                ),
                PendingFinalReviewProof(
                    id=500,
                    project_id=2,
                    image_url="/proofs/500.png",
                    created_at=datetime(2026, 8, 10, 8, 0),
                    proof_date=date(2026, 8, 10),
                    note=None,
                    review_comment=None,
                ),
            )
        )

        with patch(
            "app.router.proof.list_pending_final_review_proofs",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/proof/pending-final-review",
                    params={"season_user_id": 101},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [
                {
                    "id": 501,
                    "project_id": 5,
                    "image_url": "/proofs/501.jpg",
                    "created_at": "2026-08-12T10:30:45",
                    "proof_date": "2026-08-11",
                    "note": "晚间跑步 5 公里",
                    "review_comment": "距离满足单次要求",
                },
                {
                    "id": 500,
                    "project_id": 2,
                    "image_url": "/proofs/500.png",
                    "created_at": "2026-08-10T08:00:00",
                    "proof_date": "2026-08-10",
                    "note": None,
                    "review_comment": None,
                },
            ],
        )
        service_mock.assert_awaited_once_with(self.session, 101)

    # 验证没有待终审记录时返回空数组和成功状态。
    def test_pending_final_review_returns_empty_array(self) -> None:
        with patch(
            "app.router.proof.list_pending_final_review_proofs",
            new=AsyncMock(return_value=()),
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/proof/pending-final-review",
                    params={"season_user_id": 101},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    # 验证缺少参赛记录 ID 或传入非正整数时返回参数校验错误。
    def test_pending_final_review_validates_season_user_id(self) -> None:
        with TestClient(app) as client:
            missing_response = client.get(
                "/flame/admin/api/proof/pending-final-review"
            )
            invalid_response = client.get(
                "/flame/admin/api/proof/pending-final-review",
                params={"season_user_id": 0},
            )

        self.assertEqual(missing_response.status_code, 422)
        self.assertEqual(invalid_response.status_code, 422)

    # 验证待终审接口继承管理员认证，未登录时不会查询凭证。
    def test_pending_final_review_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.proof.list_pending_final_review_proofs",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/proof/pending-final-review",
                    params={"season_user_id": 101},
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()
