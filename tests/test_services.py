import unittest
from datetime import date, datetime
from decimal import Decimal
from types import TracebackType
from unittest.mock import AsyncMock, Mock, patch

from tests.support import configure_test_environment

configure_test_environment()

from app.repositories.projects import ProjectInformation
from app.repositories.proofs import PendingFinalReviewProof
from app.repositories.season_statistics import (
    CurrentSeasonProjectParticipant,
    CurrentSeasonStatistics,
    MultipleActiveSeasonsError,
)
from app.repositories.seasons import SeasonRecord
from app.repositories.users import UserBasicInformation
from app.services.admin_auth import InvalidAdminKeyError, issue_admin_token
from app.services.projects import list_projects
from app.services.proofs import list_pending_final_review_proofs
from app.services.season_statistics import (
    CurrentSeasonConflictError,
    CurrentSeasonNotFoundError,
    get_current_season_project_participants,
    get_current_season_statistics,
)
from app.services.seasons import (
    SeasonListItem,
    UnknownSeasonStatusError,
    list_seasons,
)
from app.services.users import get_user_basic_information


class FakeTransactionContext:
    # 记录服务层是否完整进入事务，并保留退出时收到的异常类型。
    def __init__(self) -> None:
        self.entered = False
        self.exited = False
        self.exception_type: type[BaseException] | None = None

    # 标记服务用例已经进入请求会话的事务边界。
    async def __aenter__(self) -> "FakeTransactionContext":
        self.entered = True
        return self

    # 标记事务退出并让原异常继续传播，以模拟真实会话回滚语义。
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
    # 为每个服务测试提供独立且可观察的事务上下文。
    def __init__(self) -> None:
        self.transaction = FakeTransactionContext()

    # 返回当前用例唯一的事务上下文，避免连接真实数据库。
    def begin(self) -> FakeTransactionContext:
        return self.transaction


class AdminAuthenticationServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证登录服务只暴露签发结果，不把缓存内部对象泄漏给路由。
    async def test_issue_admin_token_returns_stable_result(self) -> None:
        token_cache = Mock()
        token_cache.issue = AsyncMock(return_value="test-token")
        token_cache.ttl_seconds = 900

        issued_token = await issue_admin_token(token_cache, "test-key")

        self.assertEqual(issued_token.access_token, "test-token")
        self.assertEqual(issued_token.expires_in, 900)
        token_cache.issue.assert_awaited_once_with("test-key")

    # 验证缓存拒绝管理员密钥时转换为稳定的应用服务异常。
    async def test_issue_admin_token_rejects_invalid_key(self) -> None:
        token_cache = Mock()
        token_cache.issue = AsyncMock(return_value=None)

        with self.assertRaises(InvalidAdminKeyError):
            await issue_admin_token(token_cache, "invalid-key")


class ProjectServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证全部项目查询由服务层开启事务并在事务中调用仓储。
    async def test_list_projects_manages_transaction(self) -> None:
        session = FakeServiceSession()
        expected_projects = (
            ProjectInformation(
                1,
                "跑步",
                "累计跑步里程，提升心肺能力",
                "/running.png",
                1,
            ),
        )
        repository_mock = AsyncMock(return_value=expected_projects)

        with patch(
            "app.services.projects.fetch_all_projects",
            new=repository_mock,
        ):
            projects = await list_projects(session)  # type: ignore[arg-type]

        self.assertEqual(projects, expected_projects)
        repository_mock.assert_awaited_once_with(session)
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)


class SeasonServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证全部赛季查询在服务层开启并完整退出只读事务。
    async def test_list_seasons_manages_transaction(self) -> None:
        session = FakeServiceSession()
        repository_seasons = (
            SeasonRecord(
                id=2,
                name="2026年9月赛季",
                start_date=date(2026, 9, 1),
                end_date=date(2026, 9, 30),
                status=2,
            ),
        )
        repository_mock = AsyncMock(return_value=repository_seasons)

        with patch(
            "app.services.seasons.fetch_all_seasons",
            new=repository_mock,
        ):
            seasons = await list_seasons(session)  # type: ignore[arg-type]

        self.assertEqual(
            seasons,
            (
                SeasonListItem(
                    id=2,
                    name="2026年9月赛季",
                    start_date=date(2026, 9, 1),
                    end_date=date(2026, 9, 30),
                    status=2,
                    status_name="结算中",
                ),
            ),
        )
        repository_mock.assert_awaited_once_with(session)
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)

    # 验证数据库出现未定义状态时服务拒绝生成误导性的中文含义。
    async def test_list_seasons_rejects_unknown_status(self) -> None:
        session = FakeServiceSession()
        repository_mock = AsyncMock(
            return_value=(
                SeasonRecord(
                    id=2,
                    name="异常赛季",
                    start_date=date(2026, 9, 1),
                    end_date=date(2026, 9, 30),
                    status=9,
                ),
            )
        )

        with patch(
            "app.services.seasons.fetch_all_seasons",
            new=repository_mock,
        ):
            with self.assertRaises(UnknownSeasonStatusError):
                await list_seasons(session)  # type: ignore[arg-type]

        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)


class ProofServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证待终审查询服务在只读事务中把参赛记录 ID 传给凭证仓储。
    async def test_list_pending_final_review_proofs_manages_transaction(
        self,
    ) -> None:
        session = FakeServiceSession()
        expected_proofs = (
            PendingFinalReviewProof(
                id=501,
                project_id=5,
                image_url="/proofs/501.jpg",
                created_at=datetime(2026, 8, 12, 10, 30, 45),
                proof_date=date(2026, 8, 11),
                note="晚间跑步 5 公里",
                review_comment="距离满足单次要求",
            ),
        )
        repository_mock = AsyncMock(return_value=expected_proofs)

        with patch(
            "app.services.proofs.fetch_pending_final_review_proofs",
            new=repository_mock,
        ):
            proofs = await list_pending_final_review_proofs(  # type: ignore[arg-type]
                session,
                season_user_id=101,
            )

        self.assertEqual(proofs, expected_proofs)
        repository_mock.assert_awaited_once_with(session, 101)
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)


class UserServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证用户查询先按首次出现顺序去重，再在服务事务中调用仓储。
    async def test_get_user_information_deduplicates_ids_in_transaction(
        self,
    ) -> None:
        session = FakeServiceSession()
        expected_users = (
            UserBasicInformation("user-2", "李四", "产品部", None),
        )
        repository_mock = AsyncMock(return_value=expected_users)

        with patch(
            "app.services.users.fetch_user_basic_information",
            new=repository_mock,
        ):
            users = await get_user_basic_information(  # type: ignore[arg-type]
                session,
                ["user-2", "user-1", "user-2"],
            )

        self.assertEqual(users, expected_users)
        repository_mock.assert_awaited_once_with(
            session,
            ("user-2", "user-1"),
        )
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)


class SeasonStatisticsServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证项目参赛进度服务在同一只读事务中向仓储传递两个业务标识。
    async def test_get_project_participants_manages_transaction(self) -> None:
        session = FakeServiceSession()
        expected_participants = (
            CurrentSeasonProjectParticipant(
                user_id="user-1",
                completion_progress=Decimal("0.7500"),
            ),
        )
        repository_mock = AsyncMock(return_value=expected_participants)

        with patch(
            "app.services.season_statistics."
            "fetch_current_season_project_participants",
            new=repository_mock,
        ):
            participants = await get_current_season_project_participants(  # type: ignore[arg-type]
                session,
                season_user_id=101,
                project_id=5,
            )

        self.assertEqual(participants, expected_participants)
        repository_mock.assert_awaited_once_with(session, 101, 5)
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)

    # 验证当前赛季查询在服务事务中返回唯一激活赛季。
    async def test_get_current_season_returns_repository_result(self) -> None:
        session = FakeServiceSession()
        expected_season = CurrentSeasonStatistics(
            id=7,
            name="2026年8月赛季",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            required_project_count=3,
            status=1,
            participants=(),
        )
        repository_mock = AsyncMock(return_value=expected_season)

        with patch(
            "app.services.season_statistics.fetch_current_season_statistics",
            new=repository_mock,
        ):
            current_season = await get_current_season_statistics(  # type: ignore[arg-type]
                session
            )

        self.assertEqual(current_season, expected_season)
        repository_mock.assert_awaited_once_with(session)
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)

    # 验证没有激活赛季时服务返回明确的用例级不存在异常。
    async def test_get_current_season_reports_not_found(self) -> None:
        session = FakeServiceSession()

        with patch(
            "app.services.season_statistics.fetch_current_season_statistics",
            new=AsyncMock(return_value=None),
        ):
            with self.assertRaises(CurrentSeasonNotFoundError):
                await get_current_season_statistics(session)  # type: ignore[arg-type]

        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)

    # 验证多个激活赛季时服务隔离仓储异常并保留事务回滚路径。
    async def test_get_current_season_maps_consistency_conflict(self) -> None:
        session = FakeServiceSession()

        with patch(
            "app.services.season_statistics.fetch_current_season_statistics",
            new=AsyncMock(side_effect=MultipleActiveSeasonsError),
        ):
            with self.assertRaises(CurrentSeasonConflictError):
                await get_current_season_statistics(session)  # type: ignore[arg-type]

        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)
        self.assertIs(
            session.transaction.exception_type,
            MultipleActiveSeasonsError,
        )
