from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx


@dataclass(frozen=True, slots=True)
class ImmediatePreliminaryReview:
    proof_record_id: int
    review_status: str
    review_comment: str
    progress_delta: Decimal
    increase: Decimal


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

    # 调用客户端后端立即初审并校验关键响应字段，避免把不完整响应误认为已处理。
    async def review_proof_immediately(
        self,
        proof_record_id: int,
    ) -> ImmediatePreliminaryReview:
        return await self._review_proof_by_path(
            proof_record_id,
            f"/proof_record/{proof_record_id}/preliminary-review",
        )

    # 补交凭证必须使用资格表中的固化上下文，禁止退回通用立即初审入口。
    async def review_supplement_immediately(
        self,
        proof_record_id: int,
    ) -> ImmediatePreliminaryReview:
        return await self._review_proof_by_path(
            proof_record_id,
            f"/supplement/{proof_record_id}/preliminary-review",
        )

    async def _review_proof_by_path(
        self,
        proof_record_id: int,
        path: str,
    ) -> ImmediatePreliminaryReview:
        """统一校验客户后端两类单条初审响应契约。"""
        response = await self.request("POST", path)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("客户端后端立即初审响应不是对象")
        response_proof_record_id = payload.get("proof_record_id")
        review_status = payload.get("review_status")
        review_comment = payload.get("review_comment")
        if response_proof_record_id != proof_record_id:
            raise ValueError("客户端后端立即初审响应凭证 ID 不一致")
        if review_status not in {
            "preliminary_approved",
            "preliminary_rejected",
        }:
            raise ValueError("客户端后端立即初审响应状态无效")
        if not isinstance(review_comment, str):
            raise ValueError("客户端后端立即初审响应缺少审核意见")
        try:
            progress_delta = Decimal(str(payload["progress_delta"]))
            increase = Decimal(str(payload["increase"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("客户端后端立即初审响应进度无效") from error
        return ImmediatePreliminaryReview(
            proof_record_id=proof_record_id,
            review_status=review_status,
            review_comment=review_comment,
            progress_delta=progress_delta,
            increase=increase,
        )

    # 关闭连接池，避免应用重载或退出时遗留未释放的网络连接。
    async def aclose(self) -> None:
        await self._client.aclose()
