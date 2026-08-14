"""定义用户基础信息查询接口的数据结构。"""

from typing import Annotated

from pydantic import BaseModel, StringConstraints

UserId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]


class UserBasicInformationResponse(BaseModel):
    user_id: str
    name: str
    department_name: str
    avatar_url: str | None
