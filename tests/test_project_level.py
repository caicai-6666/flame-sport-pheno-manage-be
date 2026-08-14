import unittest
from types import TracebackType
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.repositories.project_levels import (
    ProjectLevelInformation,
    fetch_all_project_levels,
    insert_project_level,
    update_project_level_reward as update_project_level_reward_repository,
)
from app.repositories.projects import (
    ProjectRuleInitialization,
    ProjectRuleMetricSnapshot,
    ProjectRuleMetricSource,
    insert_initialized_project_rules,
    lock_project_rule_metric_snapshot,
)
from app.router.dependencies import require_admin_token
from app.services.project_levels import (
    ProjectLevelNameConflictError,
    ProjectLevelNotFoundError,
    ProjectRuleMetricTemplateInconsistentError,
    ProjectRuleMetricTemplateMissingError,
    build_project_rule_initializations,
    create_project_level,
    list_project_levels,
    update_project_level_reward as update_project_level_reward_service,
)
from app.services.configuration_guard import (
    ActiveSeasonConfigurationWindowClosedError,
    MultipleActiveSeasonsForConfigurationError,
)


VALID_METRIC_SNAPSHOT = ProjectRuleMetricSnapshot(
    project_ids=(1, 2),
    sources=(
        ProjectRuleMetricSource(
            1,
            1,
            [{"label": "每日步数", "value": "8000步/天"}],
        ),
        ProjectRuleMetricSource(
            1,
            2,
            [{"label": "每日步数", "value": "10000步/天"}],
        ),
        ProjectRuleMetricSource(
            2,
            1,
            [{"label": "累计距离", "value": "30km"}],
        ),
    ),
)

EXPECTED_INITIALIZATIONS = (
    ProjectRuleInitialization(
        1,
        [{"label": "每日步数", "value": None}],
    ),
    ProjectRuleInitialization(
        2,
        [{"label": "累计距离", "value": None}],
    ),
)


class FakeMappingsResult:
    # 保存等级仓储测试预设的映射行，避免连接开发数据库。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    # 返回自身以模拟 SQLAlchemy 映射结果调用链。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回全部预设等级行并保持数据库查询顺序。
    def all(self) -> list[dict[str, object]]:
        return self.rows

    # 返回第一条预设映射行，模拟带行锁的单记录查询结果。
    def first(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None


class FakeInsertResult:
    # 保存数据库为新等级生成的自增主键。
    def __init__(self, lastrowid: int) -> None:
        self.lastrowid = lastrowid


class FakeRepositorySession:
    # 保存预设查询与插入结果，并捕获等级仓储执行的 SQL 和绑定参数。
    def __init__(
        self,
        rows: list[dict[str, object]],
        lastrowid: int = 4,
    ) -> None:
        self.result = FakeMappingsResult(rows)
        self.insert_result = FakeInsertResult(lastrowid)
        self.statement = None
        self.params: dict[str, object] | None = None

    # 根据 SQL 类型返回查询或插入替身，确保测试不写入开发数据库。
    async def exec(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> FakeMappingsResult | FakeInsertResult:
        self.statement = statement
        self.params = params
        if "INSERT INTO project_level" in str(statement):
            return self.insert_result
        return self.result


class FakeSequentialRepositorySession:
    # 按顺序返回多次仓储调用结果，并保存每条 SQL 与绑定参数。
    def __init__(self, result_rows: list[list[dict[str, object]]]) -> None:
        self.results = [FakeMappingsResult(rows) for rows in result_rows]
        self.statements: list[object] = []
        self.params: list[dict[str, object] | None] = []

    # 模拟项目与规则锁定查询及批量初始化写入的连续数据库调用。
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
    # 记录等级用例是否完整进入事务，并保留退出时收到的异常类型。
    def __init__(self) -> None:
        self.entered = False
        self.exited = False
        self.exception_type: type[BaseException] | None = None

    # 标记只读事务已经开始。
    async def __aenter__(self) -> "FakeTransactionContext":
        self.entered = True
        return self

    # 标记事务已经退出、记录异常类型，并保留异常的正常传播行为。
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
    # 为等级查询服务测试提供可观察的事务上下文。
    def __init__(self) -> None:
        self.transaction = FakeTransactionContext()

    # 返回当前测试唯一的只读事务上下文。
    def begin(self) -> FakeTransactionContext:
        return self.transaction

    # 默认返回“没有激活赛季”，使无关服务测试专注原有等级业务分支。
    async def exec(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> FakeMappingsResult:
        return FakeMappingsResult([])


class ProjectLevelRepositoryTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证仓储返回启用和停用等级，并按奖励积分与主键稳定排序。
    async def test_repository_maps_all_project_levels(self) -> None:
        session = FakeRepositorySession(
            [
                {"id": 1, "name": "青铜", "reward": 100},
                {"id": 2, "name": "白银", "reward": 200},
            ]
        )

        project_levels = await fetch_all_project_levels(  # type: ignore[arg-type]
            session
        )

        self.assertEqual(
            project_levels,
            (
                ProjectLevelInformation(1, "青铜", 100),
                ProjectLevelInformation(2, "白银", 200),
            ),
        )
        sql = str(session.statement)
        self.assertIn("FROM project_level", sql)
        self.assertNotIn("WHERE", sql)
        self.assertIn(
            "ORDER BY project_level.reward ASC, project_level.id ASC",
            sql,
        )

    # 验证数据库没有等级时返回空集合，不伪造默认等级。
    async def test_repository_returns_empty_project_levels(self) -> None:
        session = FakeRepositorySession([])

        project_levels = await fetch_all_project_levels(  # type: ignore[arg-type]
            session
        )

        self.assertEqual(project_levels, ())

    # 验证仓储写入请求名称、积分和默认启用状态，并返回数据库自增主键。
    async def test_repository_inserts_enabled_project_level(self) -> None:
        session = FakeRepositorySession([], lastrowid=4)

        project_level = await insert_project_level(  # type: ignore[arg-type]
            session,
            "铂金",
            400,
        )

        self.assertEqual(
            project_level,
            ProjectLevelInformation(4, "铂金", 400),
        )
        sql = str(session.statement)
        self.assertIn("INSERT INTO project_level", sql)
        self.assertIn("status", sql)
        self.assertEqual(session.params, {"name": "铂金", "reward": 400})

    # 验证积分更新先锁定目标等级，再使用绑定参数覆盖奖励积分并返回最新数据。
    async def test_repository_updates_project_level_reward(self) -> None:
        session = FakeSequentialRepositorySession(
            [[{"id": 2, "name": "白银"}], []]
        )

        project_level = await update_project_level_reward_repository(  # type: ignore[arg-type]
            session,
            2,
            260,
        )

        self.assertEqual(
            project_level,
            ProjectLevelInformation(2, "白银", 260),
        )
        self.assertIn("FOR UPDATE", str(session.statements[0]))
        self.assertIn("UPDATE project_level", str(session.statements[1]))
        self.assertEqual(session.params[0], {"level_id": 2})
        self.assertEqual(
            session.params[1],
            {"level_id": 2, "reward": 260},
        )

    # 验证目标等级不存在时仓储不执行更新语句，也不伪造返回记录。
    async def test_repository_reports_missing_level_for_reward_update(
        self,
    ) -> None:
        session = FakeSequentialRepositorySession([[]])

        project_level = await update_project_level_reward_repository(  # type: ignore[arg-type]
            session,
            99,
            260,
        )

        self.assertIsNone(project_level)
        self.assertEqual(len(session.statements), 1)

    # 验证初始化快照共享锁定所有项目和已有规则，并还原规则 JSON。
    async def test_repository_locks_project_rule_metric_snapshot(self) -> None:
        session = FakeSequentialRepositorySession(
            [
                [{"id": 1}, {"id": 2}],
                [
                    {
                        "project_id": 1,
                        "level_id": 1,
                        "rule_content": (
                            '[{"label":"每日步数","value":"8000步/天"}]'
                        ),
                    }
                ],
            ]
        )

        snapshot = await lock_project_rule_metric_snapshot(  # type: ignore[arg-type]
            session
        )

        self.assertEqual(snapshot.project_ids, (1, 2))
        self.assertEqual(
            snapshot.sources,
            (
                ProjectRuleMetricSource(
                    1,
                    1,
                    [{"label": "每日步数", "value": "8000步/天"}],
                ),
            ),
        )
        self.assertIn("FROM project", str(session.statements[0]))
        self.assertIn("FROM project_rule", str(session.statements[1]))
        self.assertNotIn("WHERE", str(session.statements[0]))
        self.assertIn("FOR SHARE", str(session.statements[0]))
        self.assertIn("FOR SHARE", str(session.statements[1]))

    # 验证新等级规则一次批量写入，指标值、描述和备注均按空值初始化。
    async def test_repository_bulk_inserts_initialized_rules(self) -> None:
        session = FakeSequentialRepositorySession([])

        await insert_initialized_project_rules(  # type: ignore[arg-type]
            session,
            level_id=4,
            rules=EXPECTED_INITIALIZATIONS,
        )

        sql = str(session.statements[0])
        self.assertIn("INSERT INTO project_rule", sql)
        self.assertIn("sub_desc", sql)
        self.assertIn("rule_note", sql)
        self.assertIn("NULL", sql)
        self.assertEqual(session.params[0]["level_id"], 4)  # type: ignore[index]
        self.assertEqual(session.params[0]["project_id_0"], 1)  # type: ignore[index]
        self.assertEqual(
            session.params[0]["rule_content_0"],  # type: ignore[index]
            '[{"label":"每日步数","value":null}]',
        )


class ProjectLevelServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证等级列表由服务层开启事务并在事务中调用仓储。
    async def test_service_manages_read_transaction(self) -> None:
        session = FakeServiceSession()
        expected_levels = (ProjectLevelInformation(1, "青铜", 100),)
        repository_mock = AsyncMock(return_value=expected_levels)

        with patch(
            "app.services.project_levels.fetch_all_project_levels",
            new=repository_mock,
        ):
            project_levels = await list_project_levels(  # type: ignore[arg-type]
                session
            )

        self.assertEqual(project_levels, expected_levels)
        repository_mock.assert_awaited_once_with(session)
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)

    # 验证创建服务在单一事务中写入等级并返回仓储结果。
    async def test_service_creates_project_level_in_transaction(self) -> None:
        session = FakeServiceSession()
        expected_level = ProjectLevelInformation(4, "铂金", 400)
        repository_mock = AsyncMock(return_value=expected_level)
        snapshot_mock = AsyncMock(return_value=VALID_METRIC_SNAPSHOT)
        rules_mock = AsyncMock()

        with (
            patch(
                "app.services.project_levels."
                "lock_project_rule_metric_snapshot",
                new=snapshot_mock,
            ),
            patch(
                "app.services.project_levels.insert_project_level",
                new=repository_mock,
            ),
            patch(
                "app.services.project_levels."
                "insert_initialized_project_rules",
                new=rules_mock,
            ),
        ):
            project_level = await create_project_level(  # type: ignore[arg-type]
                session,
                "铂金",
                400,
            )

        self.assertEqual(project_level, expected_level)
        snapshot_mock.assert_awaited_once_with(session)
        repository_mock.assert_awaited_once_with(session, "铂金", 400)
        rules_mock.assert_awaited_once_with(
            session,
            4,
            EXPECTED_INITIALIZATIONS,
        )
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)

    # 验证 MySQL 重复键错误在事务回滚后转换为稳定的同名冲突异常。
    async def test_service_maps_duplicate_name_conflict(self) -> None:
        session = FakeServiceSession()
        duplicate_error = IntegrityError(
            "INSERT",
            {"name": "青铜"},
            Exception(1062, "Duplicate entry"),
        )

        with (
            patch(
                "app.services.project_levels."
                "lock_project_rule_metric_snapshot",
                new=AsyncMock(return_value=VALID_METRIC_SNAPSHOT),
            ),
            patch(
                "app.services.project_levels.insert_project_level",
                new=AsyncMock(side_effect=duplicate_error),
            ),
        ):
            with self.assertRaises(ProjectLevelNameConflictError):
                await create_project_level(  # type: ignore[arg-type]
                    session,
                    "青铜",
                    100,
                )

        self.assertTrue(session.transaction.exited)
        self.assertIs(
            session.transaction.exception_type,
            ProjectLevelNameConflictError,
        )

    # 验证非重复键完整性错误保持原异常，避免错误报告为名称重复。
    async def test_service_preserves_other_integrity_errors(self) -> None:
        session = FakeServiceSession()
        integrity_error = IntegrityError(
            "INSERT",
            {"reward": 400},
            Exception(1048, "Column cannot be null"),
        )

        with (
            patch(
                "app.services.project_levels."
                "lock_project_rule_metric_snapshot",
                new=AsyncMock(return_value=VALID_METRIC_SNAPSHOT),
            ),
            patch(
                "app.services.project_levels.insert_project_level",
                new=AsyncMock(side_effect=integrity_error),
            ),
        ):
            with self.assertRaises(IntegrityError):
                await create_project_level(  # type: ignore[arg-type]
                    session,
                    "铂金",
                    400,
                )

    # 验证规则批量初始化失败时异常退出同一事务，使已插入等级一并回滚。
    async def test_service_rolls_back_level_when_rule_initialization_fails(
        self,
    ) -> None:
        session = FakeServiceSession()
        expected_level = ProjectLevelInformation(4, "铂金", 400)
        rule_error = RuntimeError("rule initialization failed")

        with (
            patch(
                "app.services.project_levels."
                "lock_project_rule_metric_snapshot",
                new=AsyncMock(return_value=VALID_METRIC_SNAPSHOT),
            ),
            patch(
                "app.services.project_levels.insert_project_level",
                new=AsyncMock(return_value=expected_level),
            ),
            patch(
                "app.services.project_levels."
                "insert_initialized_project_rules",
                new=AsyncMock(side_effect=rule_error),
            ),
        ):
            with self.assertRaises(RuntimeError):
                await create_project_level(  # type: ignore[arg-type]
                    session,
                    "铂金",
                    400,
                )

        self.assertIs(session.transaction.exception_type, RuntimeError)

    # 验证项目缺少指标模板时在写入等级前拒绝并回滚事务。
    async def test_service_rejects_missing_project_metric_template(
        self,
    ) -> None:
        session = FakeServiceSession()
        level_mock = AsyncMock()

        with (
            patch(
                "app.services.project_levels."
                "lock_project_rule_metric_snapshot",
                new=AsyncMock(
                    return_value=ProjectRuleMetricSnapshot(
                        project_ids=(1, 2),
                        sources=(
                            ProjectRuleMetricSource(
                                1,
                                1,
                                [{"label": "每日步数", "value": "8000步"}],
                            ),
                        ),
                    )
                ),
            ),
            patch(
                "app.services.project_levels.insert_project_level",
                new=level_mock,
            ),
        ):
            with self.assertRaises(ProjectRuleMetricTemplateMissingError):
                await create_project_level(  # type: ignore[arg-type]
                    session,
                    "铂金",
                    400,
                )

        level_mock.assert_not_awaited()
        self.assertIs(
            session.transaction.exception_type,
            ProjectRuleMetricTemplateMissingError,
        )

    # 验证积分修改在同一事务内先校验赛季窗口，再锁定并更新目标等级。
    async def test_service_updates_reward_in_configuration_window(
        self,
    ) -> None:
        session = FakeServiceSession()
        expected_level = ProjectLevelInformation(2, "白银", 260)
        guard_mock = AsyncMock()
        repository_mock = AsyncMock(return_value=expected_level)

        with (
            patch(
                "app.services.project_levels."
                "ensure_active_season_configuration_editable",
                new=guard_mock,
            ),
            patch(
                "app.services.project_levels."
                "update_project_level_reward_repository",
                new=repository_mock,
            ),
        ):
            project_level = await update_project_level_reward_service(  # type: ignore[arg-type]
                session,
                2,
                260,
                edit_window_hours=48,
            )

        self.assertEqual(project_level, expected_level)
        guard_mock.assert_awaited_once_with(session, 48)
        repository_mock.assert_awaited_once_with(session, 2, 260)
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)

    # 验证积分修改找不到等级时抛出稳定异常并退出写事务。
    async def test_service_reports_missing_level_for_reward_update(
        self,
    ) -> None:
        session = FakeServiceSession()

        with (
            patch(
                "app.services.project_levels."
                "ensure_active_season_configuration_editable",
                new=AsyncMock(),
            ),
            patch(
                "app.services.project_levels."
                "update_project_level_reward_repository",
                new=AsyncMock(return_value=None),
            ),
        ):
            with self.assertRaises(ProjectLevelNotFoundError):
                await update_project_level_reward_service(  # type: ignore[arg-type]
                    session,
                    99,
                    260,
                )

        self.assertIs(
            session.transaction.exception_type,
            ProjectLevelNotFoundError,
        )

    # 验证同一项目各等级指标不一致时拒绝构造空值初始化规则。
    def test_builder_rejects_inconsistent_project_metrics(self) -> None:
        snapshot = ProjectRuleMetricSnapshot(
            project_ids=(1,),
            sources=(
                ProjectRuleMetricSource(
                    1,
                    1,
                    [{"label": "每日步数", "value": "8000步"}],
                ),
                ProjectRuleMetricSource(
                    1,
                    2,
                    [{"label": "累计天数", "value": "20天"}],
                ),
            ),
        )

        with self.assertRaises(
            ProjectRuleMetricTemplateInconsistentError
        ):
            build_project_rule_initializations(snapshot)


class ProjectLevelRouteTestCase(unittest.TestCase):
    # 为等级路由测试绕过认证并注入不访问开发数据库的会话。
    def setUp(self) -> None:
        self.session = object()

        # 返回当前等级路由测试专用会话。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session

    # 清理依赖覆盖，防止等级路由测试影响其他接口。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证接口返回全部等级的主键、名称和奖励积分。
    def test_route_returns_all_project_levels(self) -> None:
        service_mock = AsyncMock(
            return_value=(
                ProjectLevelInformation(1, "青铜", 100),
                ProjectLevelInformation(2, "白银", 200),
            )
        )

        with patch(
            "app.router.project_level.list_project_levels_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/project-level/list"
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [
                {"id": 1, "name": "青铜", "reward": 100},
                {"id": 2, "name": "白银", "reward": 200},
            ],
        )
        service_mock.assert_awaited_once_with(self.session)

    # 验证没有等级时接口返回空数组和成功状态。
    def test_route_returns_empty_array(self) -> None:
        with patch(
            "app.router.project_level.list_project_levels_service",
            new=AsyncMock(return_value=()),
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/project-level/list"
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    # 验证等级列表继承统一管理员认证，未登录请求不会查询数据库。
    def test_route_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.project_level.list_project_levels_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/project-level/list",
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()

    # 验证合法请求去除名称首尾空白并返回新等级主键、名称和奖励积分。
    def test_create_route_returns_created_project_level(self) -> None:
        service_mock = AsyncMock(
            return_value=ProjectLevelInformation(4, "铂金", 400)
        )

        with patch(
            "app.router.project_level.create_project_level_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/flame/admin/api/project-level/create",
                    json={"name": " 铂金 ", "reward": 400},
                )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.json(),
            {"id": 4, "name": "铂金", "reward": 400},
        )
        service_mock.assert_awaited_once_with(self.session, "铂金", 400)

    # 验证同名等级由创建接口返回明确冲突响应。
    def test_create_route_maps_duplicate_name_conflict(self) -> None:
        with patch(
            "app.router.project_level.create_project_level_service",
            new=AsyncMock(side_effect=ProjectLevelNameConflictError),
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/flame/admin/api/project-level/create",
                    json={"name": "青铜", "reward": 100},
                )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "挑战等级名称已存在"})

    # 验证项目指标模板缺失或不一致时创建接口返回对应冲突提示。
    def test_create_route_maps_metric_template_conflicts(self) -> None:
        cases = (
            (
                ProjectRuleMetricTemplateMissingError,
                "存在未配置评估指标的项目，无法创建挑战等级",
            ),
            (
                ProjectRuleMetricTemplateInconsistentError,
                "项目评估指标配置不一致，无法创建挑战等级",
            ),
        )

        for error_type, detail in cases:
            with self.subTest(error_type=error_type):
                with patch(
                    "app.router.project_level."
                    "create_project_level_service",
                    new=AsyncMock(side_effect=error_type),
                ):
                    with TestClient(app) as client:
                        response = client.post(
                            "/flame/admin/api/project-level/create",
                            json={"name": "铂金", "reward": 400},
                        )

                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json(), {"detail": detail})

    # 验证创建等级时也执行统一赛季配置窗口保护并返回明确冲突。
    def test_create_route_maps_configuration_window_conflict(self) -> None:
        with patch(
            "app.router.project_level.create_project_level_service",
            new=AsyncMock(
                side_effect=ActiveSeasonConfigurationWindowClosedError
            ),
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/flame/admin/api/project-level/create",
                    json={"name": "铂金", "reward": 400},
                )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {"detail": "当前激活赛季的配置修改窗口已关闭"},
        )

    # 验证空白或超长名称及越界积分在进入创建服务前被请求模型拒绝。
    def test_create_route_validates_request_fields(self) -> None:
        invalid_payloads = (
            {"name": "   ", "reward": 100},
            {"name": "级" * 33, "reward": 100},
            {"name": "铂金", "reward": -1},
            {"name": "铂金", "reward": 4_294_967_296},
            {"name": "铂金"},
        )
        service_mock = AsyncMock()

        with patch(
            "app.router.project_level.create_project_level_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                for payload in invalid_payloads:
                    with self.subTest(payload=payload):
                        response = client.post(
                            "/flame/admin/api/project-level/create",
                            json=payload,
                        )
                        self.assertEqual(response.status_code, 422)

        service_mock.assert_not_awaited()

    # 验证创建接口同样受管理员认证保护，未登录请求不会进入写服务。
    def test_create_route_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.project_level.create_project_level_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/flame/admin/api/project-level/create",
                    json={"name": "铂金", "reward": 400},
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()

    # 验证合法等级主键与积分值通过 PATCH 接口返回更新后的完整等级信息。
    def test_update_reward_route_returns_project_level(self) -> None:
        service_mock = AsyncMock(
            return_value=ProjectLevelInformation(2, "白银", 260)
        )

        with patch(
            "app.router.project_level.update_project_level_reward_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.patch(
                    "/flame/admin/api/project-level/2/reward",
                    json={"reward": 260},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"id": 2, "name": "白银", "reward": 260},
        )
        service_mock.assert_awaited_once_with(self.session, 2, 260)

    # 验证积分修改把等级不存在及两类赛季窗口冲突映射为稳定错误协议。
    def test_update_reward_route_maps_business_errors(self) -> None:
        cases = (
            (ProjectLevelNotFoundError, 404, "挑战等级不存在"),
            (
                ActiveSeasonConfigurationWindowClosedError,
                409,
                "当前激活赛季的配置修改窗口已关闭",
            ),
            (
                MultipleActiveSeasonsForConfigurationError,
                409,
                "存在多个激活赛季，无法判断配置修改窗口",
            ),
        )

        for error_type, expected_status, detail in cases:
            with self.subTest(error_type=error_type):
                with patch(
                    "app.router.project_level."
                    "update_project_level_reward_service",
                    new=AsyncMock(side_effect=error_type),
                ):
                    with TestClient(app) as client:
                        response = client.patch(
                            "/flame/admin/api/project-level/2/reward",
                            json={"reward": 260},
                        )

                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.json(), {"detail": detail})

    # 验证非法等级主键、越界积分和缺失字段在进入写服务前被拒绝。
    def test_update_reward_route_validates_request(self) -> None:
        requests = (
            ("/flame/admin/api/project-level/0/reward", {"reward": 260}),
            ("/flame/admin/api/project-level/2/reward", {"reward": -1}),
            (
                "/flame/admin/api/project-level/2/reward",
                {"reward": 4_294_967_296},
            ),
            ("/flame/admin/api/project-level/2/reward", {}),
        )
        service_mock = AsyncMock()

        with patch(
            "app.router.project_level.update_project_level_reward_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                for path, payload in requests:
                    with self.subTest(path=path, payload=payload):
                        response = client.patch(path, json=payload)
                        self.assertEqual(response.status_code, 422)

        service_mock.assert_not_awaited()

    # 验证积分修改接口继承管理员认证，未登录请求不会进入更新服务。
    def test_update_reward_route_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.project_level.update_project_level_reward_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.patch(
                    "/flame/admin/api/project-level/2/reward",
                    json={"reward": 260},
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()
