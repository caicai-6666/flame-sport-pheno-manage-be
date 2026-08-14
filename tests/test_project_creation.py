import json
import unittest
from io import BytesIO
from types import TracebackType
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.exc import IntegrityError

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.repositories.projects import (
    ProjectInformation,
    ProjectRuleCreation,
    ProjectUploadConfigurationCreation,
    insert_project,
    insert_project_rules,
    insert_project_upload_configurations,
    lock_all_project_level_ids,
)
from app.router.dependencies import get_client_backend, require_admin_token
from app.services.configuration_guard import (
    ActiveSeasonConfigurationWindowClosedError,
    MultipleActiveSeasonsForConfigurationError,
)
from app.services.images import (
    InvalidProjectIconUploadError,
    ProjectIconUploadBackendResponseError,
    ProjectIconUploadBackendUnavailableError,
    ProjectIconUploadTooLargeError,
    UploadedProjectIcon,
    upload_project_icon,
)
from app.services.projects import (
    InvalidProjectIconContentError,
    InvalidProjectIconMediaTypeError,
    ProjectCreation,
    ProjectIconDimensionsExceededError,
    ProjectIconSizeExceededError,
    ProjectNameConflictError,
    ProjectRuleLevelCoverageError,
    ProjectRuleMetricLabelsInconsistentError,
    create_project,
    generate_project_icon_url,
    validate_project_icon,
    validate_project_rule_matrix,
)


PROJECT_RULES = (
    ProjectRuleCreation(
        level_id=1,
        sub_desc="建立稳定骑行习惯",
        rule_content=[{"label": "累计距离", "value": "100km"}],
        rule_note=None,
        status=1,
    ),
    ProjectRuleCreation(
        level_id=2,
        sub_desc="提升骑行耐力",
        rule_content=[{"label": "累计距离", "value": "200km"}],
        rule_note="按赛季累计",
        status=1,
    ),
)

UPLOAD_CONFIGURATIONS = (
    ProjectUploadConfigurationCreation(
        record_type="普通凭证",
        upload_hint="上传骑行轨迹截图",
        note_example=None,
        sort_order=0,
        status=1,
    ),
)


# 生成尺寸可控的真实 WebP 字节，供上传边界和 multipart 测试复用。
def build_webp(width: int = 16, height: int = 16) -> bytes:
    output = BytesIO()
    Image.new("RGBA", (width, height), (255, 0, 0, 128)).save(
        output,
        format="WEBP",
    )
    return output.getvalue()


class FakeMappingsResult:
    # 保存预设映射行和可选自增主键，模拟 SQLAlchemy 执行结果。
    def __init__(
        self,
        rows: list[dict[str, object]] | None = None,
        lastrowid: int = 0,
    ) -> None:
        self.rows = rows or []
        self.lastrowid = lastrowid

    # 返回自身以支持 mappings() 查询调用链。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回全部预设数据库映射行并保持顺序。
    def all(self) -> list[dict[str, object]]:
        return self.rows


class FakeRepositorySession:
    # 按顺序返回仓储结果并捕获每条 SQL 与绑定参数。
    def __init__(self, results: list[FakeMappingsResult]) -> None:
        self.results = list(results)
        self.statements: list[object] = []
        self.params: list[dict[str, object] | None] = []

    # 模拟项目创建相关的连续异步数据库操作。
    async def exec(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> FakeMappingsResult:
        self.statements.append(statement)
        self.params.append(params)
        if self.results:
            return self.results.pop(0)
        return FakeMappingsResult()


class FakeTransactionContext:
    # 记录项目创建事务的进入、退出与回滚异常类型。
    def __init__(self) -> None:
        self.entered = False
        self.exited = False
        self.exception_type: type[BaseException] | None = None

    # 标记项目创建写事务已经开始。
    async def __aenter__(self) -> "FakeTransactionContext":
        self.entered = True
        return self

    # 记录事务退出状态，并保留异常传播以模拟真实回滚。
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
    # 为项目创建服务提供唯一且可观察的事务上下文。
    def __init__(self) -> None:
        self.transaction = FakeTransactionContext()

    # 返回当前项目创建用例应使用的写事务。
    def begin(self) -> FakeTransactionContext:
        return self.transaction


class FakeClientBackend:
    # 保存预设上游响应或网络异常，并记录固定项目图标上传请求。
    def __init__(
        self,
        response: httpx.Response | None = None,
        error: httpx.RequestError | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    # 模拟客户端后端通用请求方法，并为异常状态构造 HTTPStatusError。
    async def request(
        self,
        method: str,
        path: str,
        **kwargs: object,
    ) -> httpx.Response:
        self.calls.append((method, path, kwargs))
        if self.error is not None:
            raise self.error
        if self.response is None:
            raise AssertionError("未配置客户端后端响应")
        if self.response.status_code >= 400:
            raise httpx.HTTPStatusError(
                "upstream error",
                request=self.response.request,
                response=self.response,
            )
        return self.response


# 构造具备请求上下文的客户端后端响应，支持 HTTPX 状态异常测试。
def build_upstream_response(
    status_code: int,
    payload: object,
) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request(
            "POST",
            "http://backend:8000/flame/api/admin/project_icon",
        ),
    )


class ProjectCreationRepositoryTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证等级快照查询锁定全部等级且不按启停状态过滤。
    async def test_repository_locks_all_project_levels(self) -> None:
        session = FakeRepositorySession(
            [FakeMappingsResult([{"id": 1}, {"id": 2}])]
        )

        level_ids = await lock_all_project_level_ids(  # type: ignore[arg-type]
            session
        )

        self.assertEqual(level_ids, (1, 2))
        sql = str(session.statements[0])
        self.assertIn("FROM project_level", sql)
        self.assertNotIn("WHERE", sql)
        self.assertIn("FOR SHARE", sql)

    # 验证项目主记录写入名称、图标、状态并返回自增主键。
    async def test_repository_inserts_project(self) -> None:
        session = FakeRepositorySession(
            [FakeMappingsResult(lastrowid=8)]
        )

        project = await insert_project(  # type: ignore[arg-type]
            session,
            "骑行",
            "通过骑行提升心肺耐力",
            "/project-unique.webp",
            0,
        )

        self.assertEqual(
            project,
            ProjectInformation(
                8,
                "骑行",
                "通过骑行提升心肺耐力",
                "/project-unique.webp",
                0,
            ),
        )
        self.assertIn("INSERT INTO project", str(session.statements[0]))
        self.assertEqual(
            session.params[0],
            {
                "name": "骑行",
                "description": "通过骑行提升心肺耐力",
                "icon_url": "/project-unique.webp",
                "project_status": 0,
            },
        )

    # 验证规则与上传配置分别批量写入并使用紧凑 JSON 和绑定参数。
    async def test_repository_bulk_inserts_project_children(self) -> None:
        session = FakeRepositorySession([])

        await insert_project_rules(  # type: ignore[arg-type]
            session,
            8,
            PROJECT_RULES,
        )
        await insert_project_upload_configurations(  # type: ignore[arg-type]
            session,
            8,
            UPLOAD_CONFIGURATIONS,
        )

        self.assertIn("INSERT INTO project_rule", str(session.statements[0]))
        self.assertEqual(session.params[0]["project_id"], 8)  # type: ignore[index]
        self.assertEqual(session.params[0]["level_id_1"], 2)  # type: ignore[index]
        self.assertEqual(
            session.params[0]["rule_content_0"],  # type: ignore[index]
            '[{"label":"累计距离","value":"100km"}]',
        )
        self.assertIn(
            "INSERT INTO project_upload_config",
            str(session.statements[1]),
        )
        self.assertEqual(
            session.params[1]["record_type_0"],  # type: ignore[index]
            "普通凭证",
        )


class ProjectIconUploadServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证图标上传使用固定上游路径、最终地址和 WebP multipart 文件字段。
    async def test_upload_project_icon_uses_fixed_contract(self) -> None:
        icon_content = build_webp()
        client_backend = FakeClientBackend(
            build_upstream_response(
                201,
                {
                    "icon_url": "/project-unique.webp",
                    "size_bytes": len(icon_content),
                },
            )
        )

        uploaded = await upload_project_icon(  # type: ignore[arg-type]
            client_backend,
            "/project-unique.webp",
            icon_content,
        )

        self.assertEqual(
            uploaded,
            UploadedProjectIcon(
                "/project-unique.webp",
                len(icon_content),
            ),
        )
        method, path, kwargs = client_backend.calls[0]
        self.assertEqual((method, path), ("POST", "/project_icon"))
        self.assertEqual(kwargs["data"], {"icon_url": "/project-unique.webp"})
        self.assertEqual(
            kwargs["files"],
            {
                "image": (
                    "project-unique.webp",
                    icon_content,
                    "image/webp",
                )
            },
        )

    # 验证上游业务错误、超限、网络失败与异常成功响应映射为稳定异常。
    async def test_upload_project_icon_maps_upstream_errors(self) -> None:
        request = httpx.Request("POST", "http://backend:8000")
        cases = (
            (
                FakeClientBackend(
                    build_upstream_response(
                        400,
                        {"detail": "上传内容不是有效的 WebP 图片"},
                    )
                ),
                InvalidProjectIconUploadError,
            ),
            (
                FakeClientBackend(
                    build_upstream_response(
                        413,
                        {"detail": "项目图标不能超过 5 MiB"},
                    )
                ),
                ProjectIconUploadTooLargeError,
            ),
            (
                FakeClientBackend(
                    error=httpx.ConnectError(
                        "connection failed",
                        request=request,
                    )
                ),
                ProjectIconUploadBackendUnavailableError,
            ),
            (
                FakeClientBackend(
                    build_upstream_response(
                        200,
                        {
                            "icon_url": "/project-unique.webp",
                            "size_bytes": 1,
                        },
                    )
                ),
                ProjectIconUploadBackendResponseError,
            ),
        )

        for client_backend, error_type in cases:
            with self.subTest(error_type=error_type):
                with self.assertRaises(error_type):
                    await upload_project_icon(  # type: ignore[arg-type]
                        client_backend,
                        "/project-unique.webp",
                        build_webp(),
                    )


class ProjectCreationServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证新项目图标始终生成唯一的 WebP 地址，避免复用历史缓存键。
    def test_project_icon_url_uses_unique_webp_path(self) -> None:
        first_url = generate_project_icon_url()
        second_url = generate_project_icon_url()

        self.assertRegex(first_url, r"^/project-[0-9a-f]{32}\.webp$")
        self.assertNotEqual(first_url, second_url)

    # 验证项目图标本地校验接受真实 WebP，并拒绝类型、内容、尺寸和大小异常。
    def test_project_icon_validation(self) -> None:
        validate_project_icon(build_webp(), "image/webp")
        png_output = BytesIO()
        Image.new("RGB", (16, 16), (255, 0, 0)).save(
            png_output,
            format="PNG",
        )

        cases = (
            (build_webp(), "image/png", InvalidProjectIconMediaTypeError),
            (
                png_output.getvalue(),
                "image/webp",
                InvalidProjectIconContentError,
            ),
            (b"not-webp", "image/webp", InvalidProjectIconContentError),
            (
                build_webp(width=1601, height=1),
                "image/webp",
                ProjectIconDimensionsExceededError,
            ),
            (
                b"x" * (5 * 1024 * 1024 + 1),
                "image/webp",
                ProjectIconSizeExceededError,
            ),
        )
        for content, media_type, error_type in cases:
            with self.subTest(error_type=error_type):
                with self.assertRaises(error_type):
                    validate_project_icon(content, media_type)

    # 验证规则必须覆盖全部等级并在各等级保持同一有序标签列表。
    def test_project_rule_matrix_validation(self) -> None:
        validate_project_rule_matrix((1, 2), PROJECT_RULES)

        with self.assertRaises(ProjectRuleLevelCoverageError):
            validate_project_rule_matrix((1, 2, 3), PROJECT_RULES)
        with self.assertRaises(ProjectRuleMetricLabelsInconsistentError):
            validate_project_rule_matrix(
                (1, 2),
                (
                    PROJECT_RULES[0],
                    ProjectRuleCreation(
                        level_id=2,
                        sub_desc=None,
                        rule_content=[
                            {"label": "累计时长", "value": "10小时"}
                        ],
                        rule_note=None,
                        status=1,
                    ),
                ),
            )

    # 验证服务在窗口与等级快照保护下写入三类数据，再上传唯一图标。
    async def test_service_creates_project_and_uploads_icon(self) -> None:
        session = FakeServiceSession()
        client_backend = object()
        creation = ProjectCreation(
            name="骑行",
            description="通过骑行提升心肺耐力",
            status=0,
            rules=PROJECT_RULES,
            upload_configurations=UPLOAD_CONFIGURATIONS,
            icon_content=build_webp(),
            icon_media_type="image/webp",
        )
        expected_project = ProjectInformation(
            8,
            "骑行",
            "通过骑行提升心肺耐力",
            "/project-unique.webp",
            0,
        )
        guard_mock = AsyncMock()
        insert_project_mock = AsyncMock(return_value=expected_project)
        rules_mock = AsyncMock()
        configurations_mock = AsyncMock()
        upload_mock = AsyncMock()

        with (
            patch(
                "app.services.projects.generate_project_icon_url",
                return_value="/project-unique.webp",
            ),
            patch(
                "app.services.projects."
                "ensure_active_season_configuration_editable",
                new=guard_mock,
            ),
            patch(
                "app.services.projects.lock_all_project_level_ids",
                new=AsyncMock(return_value=(1, 2)),
            ),
            patch(
                "app.services.projects.insert_project",
                new=insert_project_mock,
            ),
            patch(
                "app.services.projects.insert_project_rules",
                new=rules_mock,
            ),
            patch(
                "app.services.projects.insert_project_upload_configurations",
                new=configurations_mock,
            ),
            patch(
                "app.services.projects.upload_project_icon",
                new=upload_mock,
            ),
        ):
            project = await create_project(  # type: ignore[arg-type]
                session,
                client_backend,
                creation,
                edit_window_hours=48,
            )

        self.assertEqual(project, expected_project)
        guard_mock.assert_awaited_once_with(session, 48)
        insert_project_mock.assert_awaited_once_with(
            session,
            "骑行",
            "通过骑行提升心肺耐力",
            "/project-unique.webp",
            0,
        )
        rules_mock.assert_awaited_once_with(session, 8, PROJECT_RULES)
        configurations_mock.assert_awaited_once_with(
            session,
            8,
            UPLOAD_CONFIGURATIONS,
        )
        upload_mock.assert_awaited_once_with(
            client_backend,
            "/project-unique.webp",
            creation.icon_content,
        )
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)

    # 验证图标上传失败时异常退出事务，使项目、规则和上传配置一并回滚。
    async def test_service_rolls_back_database_when_icon_upload_fails(
        self,
    ) -> None:
        session = FakeServiceSession()
        creation = ProjectCreation(
            "骑行",
            None,
            0,
            PROJECT_RULES,
            UPLOAD_CONFIGURATIONS,
            build_webp(),
            "image/webp",
        )
        expected_project = ProjectInformation(
            8,
            "骑行",
            None,
            "/project-unique.webp",
            0,
        )

        with (
            patch(
                "app.services.projects.generate_project_icon_url",
                return_value="/project-unique.webp",
            ),
            patch(
                "app.services.projects."
                "ensure_active_season_configuration_editable",
                new=AsyncMock(),
            ),
            patch(
                "app.services.projects.lock_all_project_level_ids",
                new=AsyncMock(return_value=(1, 2)),
            ),
            patch(
                "app.services.projects.insert_project",
                new=AsyncMock(return_value=expected_project),
            ),
            patch(
                "app.services.projects.insert_project_rules",
                new=AsyncMock(),
            ),
            patch(
                "app.services.projects.insert_project_upload_configurations",
                new=AsyncMock(),
            ),
            patch(
                "app.services.projects.upload_project_icon",
                new=AsyncMock(
                    side_effect=ProjectIconUploadBackendUnavailableError(
                        "客户端后端项目图标上传服务不可用"
                    )
                ),
            ),
        ):
            with self.assertRaises(
                ProjectIconUploadBackendUnavailableError
            ):
                await create_project(  # type: ignore[arg-type]
                    session,
                    object(),
                    creation,
                )

        self.assertIs(
            session.transaction.exception_type,
            ProjectIconUploadBackendUnavailableError,
        )

    # 验证项目名称重复键在事务中转换为稳定业务冲突。
    async def test_service_maps_duplicate_project_name(self) -> None:
        session = FakeServiceSession()
        duplicate_error = IntegrityError(
            "INSERT",
            {"name": "骑行"},
            Exception(1062, "Duplicate entry"),
        )
        creation = ProjectCreation(
            "骑行",
            None,
            0,
            PROJECT_RULES,
            UPLOAD_CONFIGURATIONS,
            build_webp(),
            "image/webp",
        )

        with (
            patch(
                "app.services.projects."
                "ensure_active_season_configuration_editable",
                new=AsyncMock(),
            ),
            patch(
                "app.services.projects.lock_all_project_level_ids",
                new=AsyncMock(return_value=(1, 2)),
            ),
            patch(
                "app.services.projects.insert_project",
                new=AsyncMock(side_effect=duplicate_error),
            ),
        ):
            with self.assertRaises(ProjectNameConflictError):
                await create_project(  # type: ignore[arg-type]
                    session,
                    object(),
                    creation,
                )

        self.assertIs(
            session.transaction.exception_type,
            ProjectNameConflictError,
        )


class ProjectCreationRouteTestCase(unittest.TestCase):
    # 为项目创建路由绕过独立认证测试并注入数据库与客户端替身。
    def setUp(self) -> None:
        self.session = object()
        self.client_backend = object()

        # 返回项目创建路由测试专用会话，避免访问开发数据库。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[get_client_backend] = (
            lambda: self.client_backend
        )

    # 清理依赖覆盖，避免 multipart 创建测试污染其他路由。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 生成符合接口定义的 multipart 字段，允许按测试场景覆盖单项内容。
    def build_multipart_files(
        self,
        project: object | None = None,
        project_rules: object | None = None,
        upload_configurations: object | None = None,
        icon_content: bytes | None = None,
        icon_media_type: str = "image/webp",
    ) -> dict[str, tuple[object, ...]]:
        effective_project = project if project is not None else {
            "name": "骑行",
            "description": "通过骑行提升心肺耐力",
            "status": 0,
        }
        effective_rules = project_rules if project_rules is not None else [
            {
                "level_id": 1,
                "sub_desc": "建立稳定骑行习惯",
                "rule_content": [
                    {"label": "累计距离", "value": "100km"}
                ],
                "rule_note": None,
                "status": 1,
            }
        ]
        effective_configurations = (
            upload_configurations
            if upload_configurations is not None
            else [
                {
                    "record_type": "普通凭证",
                    "upload_hint": "上传骑行轨迹截图",
                    "note_example": None,
                    "sort_order": 0,
                    "status": 1,
                }
            ]
        )
        return {
            "project": (None, json.dumps(effective_project, ensure_ascii=False)),
            "project_rules": (
                None,
                json.dumps(effective_rules, ensure_ascii=False),
            ),
            "project_upload_configs": (
                None,
                json.dumps(effective_configurations, ensure_ascii=False),
            ),
            "icon_file": (
                "cycling.webp",
                icon_content if icon_content is not None else build_webp(),
                icon_media_type,
            ),
        }

    # 验证接口解析三段 JSON 和 WebP 文件，并返回创建后的完整项目。
    def test_route_creates_project_from_multipart(self) -> None:
        service_mock = AsyncMock(
            return_value=ProjectInformation(
                8,
                "骑行",
                "通过骑行提升心肺耐力",
                "/project-unique.webp",
                0,
            )
        )

        with patch(
            "app.router.project.create_project_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/flame/admin/api/project/create",
                    files=self.build_multipart_files(),
                )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.json(),
            {
                "project_id": 8,
                "project_name": "骑行",
                "description": "通过骑行提升心肺耐力",
                "icon_url": "/project-unique.webp",
                "status": 0,
            },
        )
        service_mock.assert_awaited_once()
        session, client_backend, creation = service_mock.await_args.args
        self.assertIs(session, self.session)
        self.assertIs(client_backend, self.client_backend)
        self.assertEqual(creation.name, "骑行")
        self.assertEqual(creation.rules[0].level_id, 1)
        self.assertEqual(
            creation.upload_configurations[0].record_type,
            "普通凭证",
        )
        self.assertEqual(creation.icon_media_type, "image/webp")
        self.assertEqual(creation.icon_content, build_webp())

    # 验证非法 JSON、重复凭证类型和严格状态约束在进入服务前被拒绝。
    def test_route_validates_multipart_json_fields(self) -> None:
        invalid_files = (
            {
                **self.build_multipart_files(),
                "project": (None, "not-json"),
            },
            self.build_multipart_files(
                project={"name": "骑行", "status": True}
            ),
            self.build_multipart_files(project_rules=[]),
            self.build_multipart_files(
                upload_configurations=[
                    {
                        "record_type": "普通凭证",
                        "upload_hint": "提示一",
                        "note_example": None,
                        "sort_order": 0,
                        "status": 1,
                    },
                    {
                        "record_type": "普通凭证",
                        "upload_hint": "提示二",
                        "note_example": None,
                        "sort_order": 1,
                        "status": 1,
                    },
                ]
            ),
        )
        service_mock = AsyncMock()

        with patch(
            "app.router.project.create_project_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                for files in invalid_files:
                    with self.subTest(files=files):
                        response = client.post(
                            "/flame/admin/api/project/create",
                            files=files,
                        )
                        self.assertEqual(response.status_code, 422)

        service_mock.assert_not_awaited()

    # 验证创建用例的业务、图片、窗口和上游异常均映射为约定状态码。
    def test_route_maps_project_creation_errors(self) -> None:
        cases = (
            (ProjectNameConflictError(), 409, "运动项目名称已存在"),
            (
                ProjectRuleLevelCoverageError(),
                409,
                "项目规则必须覆盖当前全部挑战等级",
            ),
            (
                ProjectRuleMetricLabelsInconsistentError(),
                409,
                "项目各等级的评估指标标签必须一致",
            ),
            (
                InvalidProjectIconMediaTypeError(),
                400,
                "仅支持上传 WebP 项目图标",
            ),
            (
                InvalidProjectIconContentError(),
                400,
                "上传内容不是有效的 WebP 图片",
            ),
            (
                ProjectIconDimensionsExceededError(),
                400,
                "项目图标最长边不能超过 1600 像素",
            ),
            (
                ProjectIconSizeExceededError(),
                413,
                "项目图标不能超过 5 MiB",
            ),
            (
                InvalidProjectIconUploadError("项目图标路径非法"),
                400,
                "项目图标路径非法",
            ),
            (
                ProjectIconUploadTooLargeError(
                    "项目图标不能超过 5 MiB"
                ),
                413,
                "项目图标不能超过 5 MiB",
            ),
            (
                ProjectIconUploadBackendUnavailableError(
                    "客户端后端项目图标上传服务不可用"
                ),
                502,
                "客户端后端项目图标上传服务不可用",
            ),
            (
                ProjectIconUploadBackendResponseError(
                    "客户端后端项目图标上传服务响应异常"
                ),
                502,
                "客户端后端项目图标上传服务响应异常",
            ),
            (
                ActiveSeasonConfigurationWindowClosedError(),
                409,
                "当前激活赛季的配置修改窗口已关闭",
            ),
            (
                MultipleActiveSeasonsForConfigurationError(),
                409,
                "存在多个激活赛季，无法判断配置修改窗口",
            ),
        )

        for error, expected_status, expected_detail in cases:
            with self.subTest(error=type(error)):
                with patch(
                    "app.router.project.create_project_service",
                    new=AsyncMock(side_effect=error),
                ):
                    with TestClient(app) as client:
                        response = client.post(
                            "/flame/admin/api/project/create",
                            files=self.build_multipart_files(),
                        )

                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(
                    response.json(),
                    {"detail": expected_detail},
                )

    # 验证项目创建继承统一管理员认证，未登录请求不解析业务或访问下游。
    def test_route_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.project.create_project_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/flame/admin/api/project/create",
                    files=self.build_multipart_files(),
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()
