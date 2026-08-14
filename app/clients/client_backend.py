from typing import Any

import httpx


class ClientBackendClient:
    # 创建可在应用生命周期内复用连接池的客户端后端 HTTP 客户端。
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
            headers={
                "Accept": "application/json",
                "User-Agent": "flame-sport-pheno-manage-be",
            },
        )

    # 统一发送客户端后端请求，并把非成功状态转换为明确的 HTTP 异常。
    async def request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        response = await self._client.request(method, path, **kwargs)
        response.raise_for_status()
        return response

    # 关闭连接池，避免应用重载或退出时遗留未释放的网络连接。
    async def aclose(self) -> None:
        await self._client.aclose()
