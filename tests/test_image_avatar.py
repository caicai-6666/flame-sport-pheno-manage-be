import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from tests.support import configure_test_environment

configure_test_environment()

from app.main import app
from app.router.dependencies import get_client_backend, require_admin_token


class FakeClientBackend:
    # 保存预设的上游响应或异常，并记录管理端发起的固定头像请求。
    def __init__(
        self,
        response: httpx.Response | None = None,
        error: httpx.RequestError | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    # 模拟客户端后端请求，保持真实适配器对非成功状态调用 raise_for_status 的行为。
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
            raise AssertionError("测试未配置客户端后端响应")
        self.response.raise_for_status()
        return self.response


# 创建带有请求上下文的上游响应，使 HTTPX 能准确生成状态异常。
def build_upstream_response(
    status_code: int,
    *,
    content: bytes = b"",
    headers: dict[str, str] | None = None,
    json: dict[str, object] | None = None,
    upstream_path: str = "avator",
) -> httpx.Response:
    request = httpx.Request(
        "GET",
        f"http://backend:8000/flame/api/admin/{upstream_path}",
    )
    response_body = (
        {"json": json}
        if json is not None
        else {"content": content}
    )
    return httpx.Response(
        status_code,
        headers=headers,
        request=request,
        **response_body,
    )


class AvatarRouteTestCase(unittest.TestCase):
    # 为头像路由测试绕过已单独验证的管理员认证。
    def setUp(self) -> None:
        app.dependency_overrides[require_admin_token] = lambda: None

    # 清理测试依赖覆盖，防止上游替身影响其他接口。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 将当前测试的客户端后端替身注入应用，避免访问真实服务。
    def override_client_backend(self, client_backend: FakeClientBackend) -> None:
        app.dependency_overrides[get_client_backend] = lambda: client_backend

    # 验证头像使用全局图片缓存时效，并且请求只发往固定 avator 路径。
    def test_avatar_proxies_image_with_private_cache(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                200,
                content=b"jpeg-content",
                headers={"Content-Type": "image/jpeg"},
            )
        )
        self.override_client_backend(client_backend)

        with patch("app.router.image.settings.image_cache_seconds", 3600):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/image/avator",
                    params={"avatar_url": "/xxx.jpg"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"jpeg-content")
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertEqual(
            response.headers["cache-control"],
            "private, max-age=3600",
        )
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(
            client_backend.calls,
            [
                (
                    "GET",
                    "/avator",
                    {
                        "params": {"avatar_url": "/xxx.jpg"},
                        "headers": {"Accept": "image/*"},
                    },
                )
            ],
        )

    # 验证空白头像地址由管理端直接拒绝，不向客户端后端发起请求。
    def test_avatar_rejects_blank_address(self) -> None:
        client_backend = FakeClientBackend()
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/avator",
                params={"avatar_url": "   "},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "头像地址不能为空"})
        self.assertEqual(client_backend.calls, [])


    # 验证显式传入空字符串时返回业务 400，与完全缺少参数的 422 相区分。
    def test_avatar_rejects_empty_address(self) -> None:
        client_backend = FakeClientBackend()
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/avator",
                params={"avatar_url": ""},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "头像地址不能为空"})
        self.assertEqual(client_backend.calls, [])

    # 验证缺少头像地址时使用 FastAPI 标准参数校验响应。
    def test_avatar_requires_address(self) -> None:
        client_backend = FakeClientBackend()
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get("/flame/admin/api/image/avator")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(client_backend.calls, [])

    # 验证客户端后端的路径非法错误能够以相同业务语义返回管理前端。
    def test_avatar_preserves_invalid_path_error(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                400,
                json={"detail": "头像路径非法"},
            )
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/avator",
                params={"avatar_url": "../secret.jpg"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "头像路径非法"})

    # 验证头像不存在时保留客户端接口的 404 与公开提示。
    def test_avatar_preserves_not_found_error(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                404,
                json={"detail": "头像文件不存在"},
            )
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/avator",
                params={"avatar_url": "/missing.jpg"},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "头像文件不存在"})

    # 验证客户端后端异常状态不会把内部响应正文直接暴露给管理前端。
    def test_avatar_hides_unexpected_upstream_error(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                500,
                json={"detail": "internal path and stack"},
            )
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/avator",
                params={"avatar_url": "/xxx.jpg"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json(),
            {"detail": "客户端后端头像服务响应异常"},
        )
        self.assertNotIn("internal path", response.text)

    # 验证网络连接失败被转换为稳定的 502，而不是泄露 HTTPX 异常细节。
    def test_avatar_maps_network_error_to_bad_gateway(self) -> None:
        request = httpx.Request("GET", "http://backend:8000")
        client_backend = FakeClientBackend(
            error=httpx.ConnectError("connection failed", request=request)
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/avator",
                params={"avatar_url": "/xxx.jpg"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json(),
            {"detail": "客户端后端头像服务不可用"},
        )
        self.assertNotIn("connection failed", response.text)

    # 验证客户端后端即使返回 200，非图片内容也不能被代理到管理前端。
    def test_avatar_rejects_non_image_content(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                200,
                content=b'{"code": 200}',
                headers={"Content-Type": "application/json"},
            )
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/avator",
                params={"avatar_url": "/xxx.jpg"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json(),
            {"detail": "客户端后端返回了无效的头像内容"},
        )

    # 验证 SVG 等可能包含活动内容的图片格式不会经安全中转直接返回浏览器。
    def test_avatar_rejects_active_image_content(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                200,
                content=b"<svg></svg>",
                headers={"Content-Type": "image/svg+xml"},
            )
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/avator",
                params={"avatar_url": "/xxx.svg"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json(),
            {"detail": "客户端后端返回了无效的头像内容"},
        )

    # 验证头像中转接口继承统一管理员认证，未登录请求不会访问客户端后端。
    def test_avatar_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        client_backend = FakeClientBackend()
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/avator",
                params={"avatar_url": "/xxx.jpg"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        self.assertEqual(client_backend.calls, [])


class ProjectIconSuccessRouteTestCase(unittest.TestCase):
    # 为项目图标路由测试绕过已单独验证的管理员认证。
    def setUp(self) -> None:
        app.dependency_overrides[require_admin_token] = lambda: None

    # 清理测试依赖覆盖，避免项目图标替身影响其他接口。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 注入当前测试的客户端后端替身，保证测试不访问真实图片服务。
    def override_client_backend(self, client_backend: FakeClientBackend) -> None:
        app.dependency_overrides[get_client_backend] = lambda: client_backend

    # 验证项目图标固定转发至 project_icon，并使用全局图片缓存时效。
    def test_project_icon_proxies_image_with_private_cache(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                200,
                content=b"png-content",
                headers={"Content-Type": "image/png"},
                upstream_path="project_icon",
            )
        )
        self.override_client_backend(client_backend)

        with patch("app.router.image.settings.image_cache_seconds", 7200):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/image/project_icon",
                    params={"icon_url": "/project_icon/xxx.png"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"png-content")
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(
            response.headers["cache-control"],
            "private, max-age=7200",
        )
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(
            client_backend.calls,
            [
                (
                    "GET",
                    "/project_icon",
                    {
                        "params": {"icon_url": "/project_icon/xxx.png"},
                        "headers": {"Accept": "image/*"},
                    },
                )
            ],
        )

    # 验证空白项目图标地址在管理端被拒绝，不访问客户端后端。
    def test_project_icon_rejects_blank_address(self) -> None:
        client_backend = FakeClientBackend()
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/project_icon",
                params={"icon_url": "   "},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "项目图标路径不能为空"})
        self.assertEqual(client_backend.calls, [])


class ProofRecordImageRouteTestCase(unittest.TestCase):
    # 为凭证图片路由测试绕过已单独验证的管理员认证。
    def setUp(self) -> None:
        app.dependency_overrides[require_admin_token] = lambda: None

    # 清理测试依赖覆盖，防止凭证图片替身影响其他接口。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 注入当前测试专用客户端后端，禁止测试访问真实凭证文件。
    def override_client_backend(self, client_backend: FakeClientBackend) -> None:
        app.dependency_overrides[get_client_backend] = lambda: client_backend

    # 验证凭证主键只组成固定上游路径，并统一应用图片缓存与安全响应头。
    def test_proof_record_proxies_image_with_private_cache(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                200,
                content=b"proof-jpeg-content",
                headers={"Content-Type": "image/jpeg"},
                upstream_path="proof_record/115",
            )
        )
        self.override_client_backend(client_backend)

        with patch("app.router.image.settings.image_cache_seconds", 1800):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/image/proof_record/115"
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"proof-jpeg-content")
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertEqual(
            response.headers["cache-control"],
            "private, max-age=1800",
        )
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(
            client_backend.calls,
            [
                (
                    "GET",
                    "/proof_record/115",
                    {
                        "params": None,
                        "headers": {"Accept": "image/*"},
                    },
                )
            ],
        )

    # 验证无效或已失效凭证使用客户端后端约定的统一 404 提示。
    def test_proof_record_preserves_missing_proof_error(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                404,
                json={"detail": "凭证不存在"},
                upstream_path="proof_record/115",
            )
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get("/flame/admin/api/image/proof_record/115")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "凭证不存在"})

    # 验证凭证缺少有效赛季关联时保留对应的安全 404 业务提示。
    def test_proof_record_preserves_missing_season_error(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                404,
                json={"detail": "凭证所属赛季不存在"},
                upstream_path="proof_record/115",
            )
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get("/flame/admin/api/image/proof_record/115")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "凭证所属赛季不存在"})

    # 验证凭证图片文件不存在时保留客户端后端的具体 404 提示。
    def test_proof_record_preserves_missing_image_error(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                404,
                json={"detail": "凭证图片文件不存在"},
                upstream_path="proof_record/115",
            )
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get("/flame/admin/api/image/proof_record/115")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "凭证图片文件不存在"})

    # 验证客户端判定文件路径逃逸时映射为受控的 400 响应。
    def test_proof_record_preserves_invalid_path_error(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                400,
                json={"detail": "凭证图片路径非法"},
                upstream_path="proof_record/115",
            )
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get("/flame/admin/api/image/proof_record/115")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "凭证图片路径非法"})

    # 验证非图片内容不能借凭证图片接口转发给管理前端。
    def test_proof_record_rejects_non_image_content(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                200,
                content=b'{}',
                headers={"Content-Type": "application/json"},
                upstream_path="proof_record/115",
            )
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get("/flame/admin/api/image/proof_record/115")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json(),
            {"detail": "客户端后端返回了无效的凭证图片内容"},
        )

    # 验证凭证图片上游网络失败时隐藏连接细节并返回稳定 502。
    def test_proof_record_maps_network_error_to_bad_gateway(self) -> None:
        request = httpx.Request("GET", "http://backend:8000")
        client_backend = FakeClientBackend(
            error=httpx.ConnectError("connection failed", request=request)
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get("/flame/admin/api/image/proof_record/115")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json(),
            {"detail": "客户端后端凭证图片服务不可用"},
        )
        self.assertNotIn("connection failed", response.text)

    # 验证未约定的上游状态被隔离为稳定 502，不透传内部错误正文。
    def test_proof_record_hides_unexpected_upstream_error(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                500,
                json={"detail": "internal database and file path"},
                upstream_path="proof_record/115",
            )
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get("/flame/admin/api/image/proof_record/115")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json(),
            {"detail": "客户端后端凭证图片服务响应异常"},
        )
        self.assertNotIn("internal database", response.text)

    # 验证凭证主键不是正整数时在请求边界拒绝，且不访问客户端后端。
    def test_proof_record_validates_identifier(self) -> None:
        client_backend = FakeClientBackend()
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            zero_response = client.get(
                "/flame/admin/api/image/proof_record/0"
            )
            invalid_response = client.get(
                "/flame/admin/api/image/proof_record/not-an-integer"
            )

        self.assertEqual(zero_response.status_code, 422)
        self.assertEqual(invalid_response.status_code, 422)
        self.assertEqual(client_backend.calls, [])

    # 验证凭证图片接口继承管理员认证，未登录时不访问客户端后端。
    def test_proof_record_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        client_backend = FakeClientBackend()
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/proof_record/115",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        self.assertEqual(client_backend.calls, [])


class ProjectIconErrorRouteTestCase(unittest.TestCase):
    # 为项目图标异常路由测试绕过已单独验证的管理员认证。
    def setUp(self) -> None:
        app.dependency_overrides[require_admin_token] = lambda: None

    # 清理项目图标异常测试依赖，避免替身影响其他接口。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 注入项目图标异常测试专用客户端，禁止访问真实图片服务。
    def override_client_backend(self, client_backend: FakeClientBackend) -> None:
        app.dependency_overrides[get_client_backend] = lambda: client_backend

    # 验证缺少 icon_url 时使用 FastAPI 标准参数校验响应。
    def test_project_icon_requires_address(self) -> None:
        client_backend = FakeClientBackend()
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get("/flame/admin/api/image/project_icon")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(client_backend.calls, [])


class ProductImageRouteTestCase(unittest.TestCase):
    # 为商品图片路由测试绕过已单独验证的管理员认证。
    def setUp(self) -> None:
        app.dependency_overrides[require_admin_token] = lambda: None

    # 清理商品图片测试依赖覆盖，防止上游替身影响其他接口。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 注入商品图片测试专用客户端，保证测试不会访问真实客户端后端。
    def override_client_backend(self, client_backend: FakeClientBackend) -> None:
        app.dependency_overrides[get_client_backend] = lambda: client_backend

    # 验证中文商品图片地址只传给固定 product 路径，并应用全局图片缓存时效。
    def test_product_proxies_image_with_private_cache(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                200,
                content=b"product-jpeg-content",
                headers={"Content-Type": "image/jpeg"},
                upstream_path="product",
            )
        )
        self.override_client_backend(client_backend)

        with patch("app.router.image.settings.image_cache_seconds", 7200):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/image/product",
                    params={"image_url": "/Keep 弹力带.jpg"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"product-jpeg-content")
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertEqual(
            response.headers["cache-control"],
            "private, max-age=7200",
        )
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(
            client_backend.calls,
            [
                (
                    "GET",
                    "/product",
                    {
                        "params": {"image_url": "/Keep 弹力带.jpg"},
                        "headers": {"Accept": "image/*"},
                    },
                )
            ],
        )

    # 验证空白商品图片地址由管理端直接拒绝，不访问客户端后端。
    def test_product_rejects_blank_address(self) -> None:
        client_backend = FakeClientBackend()
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/product",
                params={"image_url": "   "},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "商品图片路径不能为空"})
        self.assertEqual(client_backend.calls, [])

    # 验证缺少 image_url 时使用 FastAPI 标准参数校验响应。
    def test_product_requires_address(self) -> None:
        client_backend = FakeClientBackend()
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get("/flame/admin/api/image/product")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(client_backend.calls, [])

    # 验证客户端后端的商品图片非法路径提示保持原业务语义。
    def test_product_preserves_invalid_path_error(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                400,
                json={"detail": "商品图片路径非法"},
                upstream_path="product",
            )
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/product",
                params={"image_url": "../secret.jpg"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "商品图片路径非法"})

    # 验证商品图片不存在时保留客户端后端的 404 业务提示。
    def test_product_preserves_not_found_error(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                404,
                json={"detail": "商品图片文件不存在"},
                upstream_path="product",
            )
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/product",
                params={"image_url": "/missing.jpg"},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "商品图片文件不存在"})

    # 验证商品图片上游网络失败被转换为不泄露连接信息的稳定 502。
    def test_product_maps_network_error_to_bad_gateway(self) -> None:
        request = httpx.Request("GET", "http://backend:8000")
        client_backend = FakeClientBackend(
            error=httpx.ConnectError("connection failed", request=request)
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/product",
                params={"image_url": "/Keep 弹力带.jpg"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json(),
            {"detail": "客户端后端商品图片服务不可用"},
        )
        self.assertNotIn("connection failed", response.text)

    # 验证未知上游状态被隔离为稳定错误，不透传客户端内部正文。
    def test_product_hides_unexpected_upstream_error(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                500,
                json={"detail": "internal product image path"},
                upstream_path="product",
            )
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/product",
                params={"image_url": "/Keep 弹力带.jpg"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json(),
            {"detail": "客户端后端商品图片服务响应异常"},
        )
        self.assertNotIn("internal product image path", response.text)

    # 验证非图片响应无法经商品图片接口中转到管理前端。
    def test_product_rejects_non_image_content(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                200,
                content=b'{}',
                headers={"Content-Type": "application/json"},
                upstream_path="product",
            )
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/product",
                params={"image_url": "/Keep 弹力带.jpg"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json(),
            {"detail": "客户端后端返回了无效的商品图片内容"},
        )

    # 验证商品图片接口继承统一认证，未登录时不访问客户端后端。
    def test_product_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        client_backend = FakeClientBackend()
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/product",
                params={"image_url": "/Keep 弹力带.jpg"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        self.assertEqual(client_backend.calls, [])


class ProjectIconUpstreamErrorRouteTestCase(unittest.TestCase):
    # 为项目图标上游异常测试绕过已单独验证的管理员认证。
    def setUp(self) -> None:
        app.dependency_overrides[require_admin_token] = lambda: None

    # 清理项目图标上游异常测试依赖，避免替身影响其他接口。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 注入项目图标上游异常测试专用客户端，禁止访问真实图片服务。
    def override_client_backend(self, client_backend: FakeClientBackend) -> None:
        app.dependency_overrides[get_client_backend] = lambda: client_backend

    # 验证客户端后端的项目图标非法路径提示保持原业务语义。
    def test_project_icon_preserves_invalid_path_error(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                400,
                json={"detail": "项目图标路径非法"},
                upstream_path="project_icon",
            )
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/project_icon",
                params={"icon_url": "../secret.png"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "项目图标路径非法"})

    # 验证项目图标不存在时保留客户端后端的 404 业务提示。
    def test_project_icon_preserves_not_found_error(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                404,
                json={"detail": "项目图标文件不存在"},
                upstream_path="project_icon",
            )
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/project_icon",
                params={"icon_url": "/missing.png"},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "项目图标文件不存在"})

    # 验证未知上游状态被隔离为项目图标服务异常，不泄露内部正文。
    def test_project_icon_hides_unexpected_upstream_error(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                500,
                json={"detail": "internal path and stack"},
                upstream_path="project_icon",
            )
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/project_icon",
                params={"icon_url": "/xxx.png"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json(),
            {"detail": "客户端后端项目图标服务响应异常"},
        )
        self.assertNotIn("internal path", response.text)

    # 验证项目图标上游网络失败被转换为不泄露连接信息的稳定 502。
    def test_project_icon_maps_network_error_to_bad_gateway(self) -> None:
        request = httpx.Request("GET", "http://backend:8000")
        client_backend = FakeClientBackend(
            error=httpx.ConnectError("connection failed", request=request)
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/project_icon",
                params={"icon_url": "/xxx.png"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json(),
            {"detail": "客户端后端项目图标服务不可用"},
        )
        self.assertNotIn("connection failed", response.text)

    # 验证非图片响应无法经项目图标接口中转到管理前端。
    def test_project_icon_rejects_non_image_content(self) -> None:
        client_backend = FakeClientBackend(
            response=build_upstream_response(
                200,
                content=b'{}',
                headers={"Content-Type": "application/json"},
                upstream_path="project_icon",
            )
        )
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/project_icon",
                params={"icon_url": "/xxx.png"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json(),
            {"detail": "客户端后端返回了无效的项目图标内容"},
        )

    # 验证项目图标接口继承统一认证，未登录时不访问客户端后端。
    def test_project_icon_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        client_backend = FakeClientBackend()
        self.override_client_backend(client_backend)

        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/image/project_icon",
                params={"icon_url": "/xxx.png"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        self.assertEqual(client_backend.calls, [])
