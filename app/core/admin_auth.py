"""提供基于管理员共享密钥的短期令牌签发与进程内缓存。"""

import asyncio
import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

from pydantic import SecretStr


@dataclass(frozen=True, slots=True)
class AdminTokenRecord:
    admin_key_digest: bytes
    expires_at: float


class AdminTokenCache:
    # 保存管理员密钥摘要并限制令牌生命周期与数量，避免内存缓存无界增长。
    def __init__(
        self,
        admin_key: SecretStr | str,
        ttl_seconds: int,
        max_size: int,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        raw_admin_key = (
            admin_key.get_secret_value()
            if isinstance(admin_key, SecretStr)
            else admin_key
        )
        self._admin_key_digest = self._digest(raw_admin_key)
        self._ttl_seconds = ttl_seconds
        self._max_size = max_size
        self._clock = clock
        self._records: dict[bytes, AdminTokenRecord] = {}
        self._lock = asyncio.Lock()

    # 返回令牌固定有效期，供认证接口构造不泄露内部状态的响应。
    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    # 校验明文管理员密钥，并在成功后签发无法预测的短期令牌。
    async def issue(self, submitted_admin_key: str) -> str | None:
        submitted_digest = self._digest(submitted_admin_key)
        if not hmac.compare_digest(submitted_digest, self._admin_key_digest):
            return None

        token = secrets.token_urlsafe(32)
        token_digest = self._digest(token)
        now = self._clock()

        async with self._lock:
            self._purge_expired(now)
            if len(self._records) >= self._max_size:
                oldest_token_digest = min(
                    self._records,
                    key=lambda digest: self._records[digest].expires_at,
                )
                self._records.pop(oldest_token_digest)
            self._records[token_digest] = AdminTokenRecord(
                admin_key_digest=self._admin_key_digest,
                expires_at=now + self._ttl_seconds,
            )

        return token

    # 同时校验令牌存在性、有效期和管理员密钥摘要，拒绝过期或失配令牌。
    async def validate(self, token: str) -> bool:
        token_digest = self._digest(token)
        now = self._clock()

        async with self._lock:
            self._purge_expired(now)
            record = self._records.get(token_digest)
            if record is None:
                return False
            return hmac.compare_digest(
                record.admin_key_digest,
                self._admin_key_digest,
            )

    # 删除已经超过有效期的缓存记录，保持长期运行时的内存占用有界。
    def _purge_expired(self, now: float) -> None:
        expired_digests = [
            token_digest
            for token_digest, record in self._records.items()
            if record.expires_at <= now
        ]
        for token_digest in expired_digests:
            self._records.pop(token_digest)

    # 对密钥和令牌做单向摘要，避免在服务端缓存中保存可直接使用的明文凭证。
    @staticmethod
    def _digest(value: str) -> bytes:
        return hashlib.sha256(value.encode("utf-8")).digest()
