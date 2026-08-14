"""定义管理员认证接口的请求与响应结构。"""

from typing import Literal

from pydantic import BaseModel, SecretStr


class AdminLoginRequest(BaseModel):
    admin_key: SecretStr


class AdminLoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class AdminSessionResponse(BaseModel):
    authenticated: Literal[True] = True
