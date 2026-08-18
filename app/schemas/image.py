"""定义管理端图片读取与替换接口的数据结构。"""

from pydantic import BaseModel


class PosterReplacementResponse(BaseModel):
    image_url: str
    size_bytes: int
