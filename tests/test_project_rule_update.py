import unittest
from types import TracebackType
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.repositories.projects import (
    ProjectRuleConfiguration,
    lock_project_rule_configuration,
    update_project_rule_configuration as update_project_rule_configuration_repository,
)
from app.router.dependencies import require_admin_token
from app.services.configuration_guard import (
    ActiveSeasonConfigurationWindowClosedError,
    MultipleActiveSeasonsForConfigurationError,
)
from app.services.project_levels import (
    ProjectRuleConfigurationNotFoundError,
    ProjectRuleConfigurationPatch,
    ProjectRuleMetricLabelMismatchError,
    ProjectRuleMetricValueUpdate,
    ProjectRuleStoredContentInvalidError,
    apply_project_rule_metric_value_updates,
    update_project_rule_configuration as update_project_rule_configuration_service,
)


CURRENT_RULE = ProjectRuleConfiguration(
    project_id=2,
    level_id=3,
    sub_desc="提升有氧容量",
    rule_content=[
        {"label": "累计距离", "value": "30km", "unit": "km"},
        {"label": "配速要求", "value": "≤8'00''"},
    ],
    rule_note="跑步或快走均可累计",
)


class FakeMappingsResult:
    # 保存规则仓储测试预设行，以支持唯一记录查询调用链。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    # 返回自身以模拟 SQLAlchemy 的映射结果对象。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回首条规则行，空集合表示联合标识没有对应规则。
    def first(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None


class FakeRepositorySession:
    # 按顺序返回仓储结果并记录 SQL 与参数，避免连接开发数据库。
    def __init__(self, result_rows: list[list[dict[str, object]]]) -> None:
        self.results = [FakeMappingsResult(rows) for rows in result_rows]
        self.statements: list[object] = []
        self.params: list[dict[str, object] | None] = []

    # 模拟规则锁定与更新的连续异步数据库调用。
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
    # 记录规则修改服务是否进入、退出事务以及异常回滚类型。
    def __init__(self) -> None:
        self.entered = False
        self.exited = False
        self.exception_type: type[BaseException] | None = None

    # 标记项目规则修改事务已经开始。
    async def __aenter__(self) -> "FakeTransactionContext":
        self.entered = True
        return self

    # 记录事务退出状态并让业务异常继续向路由传播。
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
    # 为规则修改服务提供唯一且可观察的事务上下文。
    def __init__(self) -> None:
        self.transaction = FakeTransactionContext()

    # 返回当前规则修改用例应使用的写事务。
    def begin(self) -> FakeTransactionContext:
        return self.transaction


class ProjectRuleUpdateRepositoryTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证仓储使用联合标识和行锁读取完整规则配置并解码 JSON。
    async def test_repository_locks_rule_configuration(self) -> None:
        session = FakeRepositorySession(
            [
                [
                    {
                        "project_id": 2,
                        "level_id": 3,
                        "sub_desc": "提升有氧容量",
                        "rule_content": (
                            '[{"label":"累计距离","value":"30km"}]'
                        ),
                        "rule_note": "跑步或快走均可累计",
                    }
                ]
            ]
        )

        result = await lock_project_rule_configuration(  # type: ignore[arg-type]
            session,
            2,
            3,
        )

        self.assertEqual(
            result,
            ProjectRuleConfiguration(
                project_id=2,
                level_id=3,
                sub_desc="提升有氧容量",
                rule_content=[{"label": "累计距离", "value": "30km"}],
                rule_note="跑步或快走均可累计",
            ),
        )
        sql = str(session.statements[0])
        self.assertIn("project_rule.project_id = :project_id", sql)
        self.assertIn("project_rule.level_id = :level_id", sql)
        self.assertIn("FOR UPDATE", sql)
        self.assertEqual(
            session.params[0],
            {"project_id": 2, "level_id": 3},
        )

    # 验证规则不存在时锁定查询返回空值且不伪造配置。
    async def test_repository_reports_missing_rule(self) -> None:
        session = FakeRepositorySession([[]])

        result = await lock_project_rule_configuration(  # type: ignore[arg-type]
            session,
            2,
            99,
        )

        self.assertIsNone(result)
        self.assertEqual(len(session.statements), 1)

    # 验证仓储参数化覆盖三个可配置字段，并以紧凑 JSON 保存完整指标数组。
    async def test_repository_updates_complete_rule_configuration(
        self,
    ) -> None:
        session = FakeRepositorySession([])
        rule_content = [{"label": "累计距离", "value": "50km"}]

        result = await update_project_rule_configuration_repository(  # type: ignore[arg-type]
            session,
            2,
            3,
            None,
            rule_content,
            "按自然日累计",
        )

        self.assertEqual(
            result,
            ProjectRuleConfiguration(
                project_id=2,
                level_id=3,
                sub_desc=None,
                rule_content=rule_content,
                rule_note="按自然日累计",
            ),
        )
        sql = str(session.statements[0])
        self.assertIn("UPDATE project_rule", sql)
        self.assertIn("sub_desc = :sub_desc", sql)
        self.assertIn("rule_content = :rule_content", sql)
        self.assertIn("rule_note = :rule_note", sql)
        self.assertEqual(
            session.params[0],
            {
                "project_id": 2,
                "level_id": 3,
                "sub_desc": None,
                "rule_content": (
                    '[{"label":"累计距离","value":"50km"}]'
                ),
                "rule_note": "按自然日累计",
            },
        )


class ProjectRuleUpdateServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证指标更新仅替换目标 value，并保留标签、顺序及扩展字段。
    def test_metric_value_update_preserves_labels_and_structure(self) -> None:
        updated = apply_project_rule_metric_value_updates(
            CURRENT_RULE.rule_content,
            (
                ProjectRuleMetricValueUpdate("累计距离", "50km"),
                ProjectRuleMetricValueUpdate("配速要求", None),
            ),
        )

        self.assertEqual(
            updated,
            [
                {"label": "累计距离", "value": "50km", "unit": "km"},
                {"label": "配速要求", "value": None},
            ],
        )
        self.assertEqual(
            CURRENT_RULE.rule_content[0]["value"],  # type: ignore[index]
            "30km",
        )

    # 验证未知标签和重复标签不能借由值更新改变既有指标集合。
    def test_metric_value_update_rejects_mismatched_labels(self) -> None:
        cases = (
            (ProjectRuleMetricValueUpdate("不存在的指标", "1"),),
            (
                ProjectRuleMetricValueUpdate("累计距离", "50km"),
                ProjectRuleMetricValueUpdate("累计距离", "60km"),
            ),
        )

        for updates in cases:
            with self.subTest(updates=updates):
                with self.assertRaises(ProjectRuleMetricLabelMismatchError):
                    apply_project_rule_metric_value_updates(
                        CURRENT_RULE.rule_content,
                        updates,
                    )

    # 验证既有规则不是标准标签数组时拒绝写入，避免破坏历史配置。
    def test_metric_value_update_rejects_invalid_stored_content(self) -> None:
        invalid_contents = (
            {"label": "累计距离", "value": "30km"},
            ["累计距离"],
            [{"value": "30km"}],
            [
                {"label": "累计距离", "value": "30km"},
                {"label": "累计距离", "value": "40km"},
            ],
        )

        for rule_content in invalid_contents:
            with self.subTest(rule_content=rule_content):
                with self.assertRaises(ProjectRuleStoredContentInvalidError):
                    apply_project_rule_metric_value_updates(
                        rule_content,  # type: ignore[arg-type]
                        (ProjectRuleMetricValueUpdate("累计距离", "50km"),),
                    )

    # 验证服务先校验配置窗口，再锁定规则并原子保存局部修改后的完整配置。
    async def test_service_updates_rule_in_configuration_window(self) -> None:
        session = FakeServiceSession()
        guard_mock = AsyncMock()
        lock_mock = AsyncMock(return_value=CURRENT_RULE)
        repository_mock = AsyncMock(
            return_value=ProjectRuleConfiguration(
                project_id=2,
                level_id=3,
                sub_desc=None,
                rule_content=[
                    {"label": "累计距离", "value": "50km", "unit": "km"},
                    {"label": "配速要求", "value": "≤8'00''"},
                ],
                rule_note="跑步或快走均可累计",
            )
        )
        configuration_patch = ProjectRuleConfigurationPatch(
            metric_values=(
                ProjectRuleMetricValueUpdate("累计距离", "50km"),
            ),
            update_sub_desc=True,
            sub_desc=None,
            update_rule_note=False,
            rule_note=None,
        )

        with (
            patch(
                "app.services.project_levels."
                "ensure_active_season_configuration_editable",
                new=guard_mock,
            ),
            patch(
                "app.services.project_levels.lock_project_rule_configuration",
                new=lock_mock,
            ),
            patch(
                "app.services.project_levels."
                "update_project_rule_configuration_repository",
                new=repository_mock,
            ),
        ):
            result = await update_project_rule_configuration_service(  # type: ignore[arg-type]
                session,
                2,
                3,
                configuration_patch,
                edit_window_hours=48,
            )

        self.assertEqual(result, repository_mock.return_value)
        guard_mock.assert_awaited_once_with(session, 48)
        lock_mock.assert_awaited_once_with(session, 2, 3)
        repository_mock.assert_awaited_once_with(
            session,
            2,
            3,
            None,
            [
                {"label": "累计距离", "value": "50km", "unit": "km"},
                {"label": "配速要求", "value": "≤8'00''"},
            ],
            "跑步或快走均可累计",
        )
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)

    # 验证规则不存在时服务抛出稳定异常且不会执行更新语句。
    async def test_service_reports_missing_rule(self) -> None:
        session = FakeServiceSession()
        repository_mock = AsyncMock()

        with (
            patch(
                "app.services.project_levels."
                "ensure_active_season_configuration_editable",
                new=AsyncMock(),
            ),
            patch(
                "app.services.project_levels.lock_project_rule_configuration",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.services.project_levels."
                "update_project_rule_configuration_repository",
                new=repository_mock,
            ),
        ):
            with self.assertRaises(ProjectRuleConfigurationNotFoundError):
                await update_project_rule_configuration_service(  # type: ignore[arg-type]
                    session,
                    2,
                    99,
                    ProjectRuleConfigurationPatch((), True, "描述", False, None),
                )

        repository_mock.assert_not_awaited()
        self.assertIs(
            session.transaction.exception_type,
            ProjectRuleConfigurationNotFoundError,
        )

    # 验证配置窗口关闭时服务在读取或锁定规则前终止写用例。
    async def test_service_rejects_update_outside_configuration_window(
        self,
    ) -> None:
        session = FakeServiceSession()
        lock_mock = AsyncMock()
        repository_mock = AsyncMock()

        with (
            patch(
                "app.services.project_levels."
                "ensure_active_season_configuration_editable",
                new=AsyncMock(
                    side_effect=ActiveSeasonConfigurationWindowClosedError
                ),
            ),
            patch(
                "app.services.project_levels.lock_project_rule_configuration",
                new=lock_mock,
            ),
            patch(
                "app.services.project_levels."
                "update_project_rule_configuration_repository",
                new=repository_mock,
            ),
        ):
            with self.assertRaises(
                ActiveSeasonConfigurationWindowClosedError
            ):
                await update_project_rule_configuration_service(  # type: ignore[arg-type]
                    session,
                    2,
                    3,
                    ProjectRuleConfigurationPatch(
                        (),
                        True,
                        "描述",
                        False,
                        None,
                    ),
                    edit_window_hours=24,
                )

        lock_mock.assert_not_awaited()
        repository_mock.assert_not_awaited()
        self.assertIs(
            session.transaction.exception_type,
            ActiveSeasonConfigurationWindowClosedError,
        )


class ProjectRuleUpdateRouteTestCase(unittest.TestCase):
    # 为规则修改路由绕过独立认证测试并注入不访问数据库的会话。
    def setUp(self) -> None:
        self.session = object()

        # 返回规则修改路由测试专用会话，真实事务由服务替身隔离。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session

    # 清理依赖覆盖，避免规则修改测试影响其他接口。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证接口接收局部指标值和可清空文案，并返回完整最终配置。
    def test_route_updates_project_rule_configuration(self) -> None:
        service_mock = AsyncMock(
            return_value=ProjectRuleConfiguration(
                project_id=2,
                level_id=3,
                sub_desc=None,
                rule_content=[
                    {"label": "累计距离", "value": "50km"},
                    {"label": "配速要求", "value": "≤8'00''"},
                ],
                rule_note="按自然日累计",
            )
        )

        with patch(
            "app.router.project_level."
            "update_project_rule_configuration_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.patch(
                    "/flame/admin/api/project-level/3/project/2/rule",
                    json={
                        "rule_content": [
                            {"label": "累计距离", "value": "50km"}
                        ],
                        "sub_desc": None,
                        "rule_note": "按自然日累计",
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "project_id": 2,
                "level_id": 3,
                "sub_desc": None,
                "rule_content": [
                    {"label": "累计距离", "value": "50km"},
                    {"label": "配速要求", "value": "≤8'00''"},
                ],
                "rule_note": "按自然日累计",
            },
        )
        service_mock.assert_awaited_once()
        service_arguments = service_mock.await_args.args
        self.assertEqual(service_arguments[:3], (self.session, 2, 3))
        self.assertEqual(
            service_arguments[3],
            ProjectRuleConfigurationPatch(
                metric_values=(
                    ProjectRuleMetricValueUpdate("累计距离", "50km"),
                ),
                update_sub_desc=True,
                sub_desc=None,
                update_rule_note=True,
                rule_note="按自然日累计",
            ),
        )

    # 验证规则缺失、标签冲突、历史格式异常及窗口冲突映射为稳定响应。
    def test_route_maps_rule_update_errors(self) -> None:
        cases = (
            (
                ProjectRuleConfigurationNotFoundError,
                404,
                "未找到对应的项目规则",
            ),
            (
                ProjectRuleMetricLabelMismatchError,
                409,
                "规则指标标签与现有配置不一致",
            ),
            (
                ProjectRuleStoredContentInvalidError,
                409,
                "现有项目规则指标格式异常，无法修改",
            ),
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

        for error_type, expected_status, expected_detail in cases:
            with self.subTest(error_type=error_type):
                with patch(
                    "app.router.project_level."
                    "update_project_rule_configuration_service",
                    new=AsyncMock(side_effect=error_type),
                ):
                    with TestClient(app) as client:
                        response = client.patch(
                            "/flame/admin/api/project-level/3/project/2/rule",
                            json={"sub_desc": "提升有氧容量"},
                        )

                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(
                    response.json(),
                    {"detail": expected_detail},
                )

    # 验证空补丁、非法标签、越界文案和额外字段在进入服务前被拒绝。
    def test_route_validates_rule_update_request(self) -> None:
        invalid_payloads = (
            {},
            {"rule_content": None},
            {"rule_content": []},
            {
                "rule_content": [
                    {"label": "累计距离", "value": "50km"},
                    {"label": "累计距离", "value": "60km"},
                ]
            },
            {"rule_content": [{"label": "   ", "value": "50km"}]},
            {"sub_desc": "描" * 129},
            {"rule_note": "注" * 256},
            {"sub_desc": "描述", "label": "禁止修改"},
        )
        service_mock = AsyncMock()

        with patch(
            "app.router.project_level."
            "update_project_rule_configuration_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                for payload in invalid_payloads:
                    with self.subTest(payload=payload):
                        response = client.patch(
                            "/flame/admin/api/project-level/3/project/2/rule",
                            json=payload,
                        )
                        self.assertEqual(response.status_code, 422)

        service_mock.assert_not_awaited()

    # 验证项目和等级 ID 必须为正整数，非法路径不会进入业务服务。
    def test_route_validates_rule_update_identifiers(self) -> None:
        service_mock = AsyncMock()

        with patch(
            "app.router.project_level."
            "update_project_rule_configuration_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                for path in (
                    "/flame/admin/api/project-level/0/project/2/rule",
                    "/flame/admin/api/project-level/3/project/0/rule",
                    "/flame/admin/api/project-level/a/project/2/rule",
                ):
                    with self.subTest(path=path):
                        response = client.patch(
                            path,
                            json={"sub_desc": "描述"},
                        )
                        self.assertEqual(response.status_code, 422)

        service_mock.assert_not_awaited()

    # 验证规则修改继承统一认证，未登录请求重定向且不触发写服务。
    def test_route_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.project_level."
            "update_project_rule_configuration_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.patch(
                    "/flame/admin/api/project-level/3/project/2/rule",
                    json={"sub_desc": "描述"},
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()
