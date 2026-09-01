"""提供上传文件的传输层安全读取能力。"""

from fastapi import UploadFile


# 只读取校验上限外的一个额外字节并关闭文件，供服务层准确判断内容是否超限。
async def read_limited_upload(
    upload: UploadFile,
    maximum_size_bytes: int,
) -> bytes:
    try:
        return await upload.read(maximum_size_bytes + 1)
    finally:
        await upload.close()

