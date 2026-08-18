# 图片安全中转 API

> **文档目的**
>
> 本文档定义管理端后端从客户端后端读取头像、项目图标、商品图片、运动凭证图片与活动海报，以及替换唯一活动海报的接口契约、缓存规则和信任边界。

## 1. 功能目标

图片保存在客户端后端。管理端后端提供受管理员认证保护的安全中转，管理前端无需直接访问局域网地址。

| 图片类型 | 管理端子路径 | 客户端后端子路径 | 定位参数 |
| --- | --- | --- | --- |
| 用户头像 | `/image/avator` | `/avator` | `avatar_url` |
| 项目图标 | `/image/project_icon` | `/project_icon` | `icon_url` |
| 商品图片 | `/image/product` | `/product` | `image_url` |
| 运动凭证 | `/image/proof_record/{proof_record_id}` | `/proof_record/{proof_record_id}` | 正整数路径参数 |
| 活动海报读取与替换 | `/image/poster` | `/poster` | 无定位参数，上传字段固定为 `image` |

前端参数不能控制上游主机、端口或接口路径，因此该能力不是通用 HTTP 代理。`avator` 与 `project_icon` 沿用双方既有协议名称，不修正拼写以免破坏调用方。商品图片接口只转发已有 `image_url`，不查询 `product` 表。

---

## 2. `GET /image/avator` 获取用户头像

### 2.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/image/avator` |
| Nginx 开发路径 | `/dev/flame/admin/api/image/avator` |
| 客户端后端路径 | `/flame/api/admin/avator` |
| 认证要求 | 有效的管理员 Bearer Token |

请求示例：

```http
GET /dev/flame/admin/api/image/avator?avatar_url=%2Fxxx.jpg
Authorization: Bearer <admin-token>
```

### 2.2 请求参数

| 参数 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `avatar_url` | `string` | 是 | 最长 255 个字符，不能仅包含空白 | 管理数据中的头像相对地址 |

### 2.3 成功响应

状态码：`200 OK`。响应直接返回图片二进制，不使用 JSON 包装。

```http
Content-Type: image/jpeg
Cache-Control: private, max-age=86400
X-Content-Type-Options: nosniff
```

允许的媒体类型为 `image/jpeg`、`image/png`、`image/webp` 和 `image/gif`。

### 2.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| 管理员 Token 无效 | `303 See Other` | 重定向至登录接口 |
| 参数缺失或超过 255 个字符 | `422 Unprocessable Entity` | FastAPI 参数校验错误 |
| 地址为空或只有空白 | `400 Bad Request` | `头像地址不能为空` |
| 上游判定路径非法 | `400 Bad Request` | 上游安全提示，异常格式时为 `头像路径非法` |
| 头像文件不存在 | `404 Not Found` | 上游安全提示，异常格式时为 `头像文件不存在` |
| 上游网络失败 | `502 Bad Gateway` | `客户端后端头像服务不可用` |
| 上游返回未约定状态 | `502 Bad Gateway` | `客户端后端头像服务响应异常` |
| 上游返回非允许图片内容 | `502 Bad Gateway` | `客户端后端返回了无效的头像内容` |

---

## 3. `GET /image/project_icon` 获取项目图标

### 3.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/image/project_icon` |
| Nginx 开发路径 | `/dev/flame/admin/api/image/project_icon` |
| 客户端后端路径 | `/flame/api/admin/project_icon` |
| 认证要求 | 有效的管理员 Bearer Token |

请求示例：

```http
GET /dev/flame/admin/api/image/project_icon?icon_url=%2Fxxx.png
Authorization: Bearer <admin-token>
```

### 3.2 请求参数

| 参数 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `icon_url` | `string` | 是 | 最长 255 个字符，不能仅包含空白 | 项目图标相对地址 |

`icon_url` 支持 `/xxx.png` 和历史格式 `/project_icon/xxx.png`。路径清理、历史前缀兼容和目录逃逸校验由客户端后端执行。

### 3.3 成功响应

状态码：`200 OK`。响应直接返回图片二进制，响应头和允许媒体类型与头像接口一致，并使用 `IMAGE_CACHE_SECONDS` 作为 `max-age`。

### 3.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| 管理员 Token 无效 | `303 See Other` | 重定向至登录接口 |
| 参数缺失或超过 255 个字符 | `422 Unprocessable Entity` | FastAPI 参数校验错误 |
| 地址为空或只有空白 | `400 Bad Request` | `项目图标路径不能为空` |
| 上游判定路径非法 | `400 Bad Request` | 上游安全提示，异常格式时为 `项目图标路径非法` |
| 项目图标不存在 | `404 Not Found` | 上游安全提示，异常格式时为 `项目图标文件不存在` |
| 上游网络失败 | `502 Bad Gateway` | `客户端后端项目图标服务不可用` |
| 上游返回未约定状态 | `502 Bad Gateway` | `客户端后端项目图标服务响应异常` |
| 上游返回非允许图片内容 | `502 Bad Gateway` | `客户端后端返回了无效的项目图标内容` |

---

## 4. `GET /image/proof_record/{proof_record_id}` 获取凭证图片

### 4.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/image/proof_record/{proof_record_id}` |
| Nginx 开发路径 | `/dev/flame/admin/api/image/proof_record/{proof_record_id}` |
| 客户端后端路径 | `/flame/api/admin/proof_record/{proof_record_id}` |
| 认证要求 | 有效的管理员 Bearer Token |

请求示例：

```http
GET /dev/flame/admin/api/image/proof_record/115
Authorization: Bearer <admin-token>
```

### 4.2 路径参数

| 参数 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `proof_record_id` | `integer` | 是 | 大于 `0` | 待查看的有效凭证记录 ID |

前端不提供用户 ID、赛季 ID 或文件路径。客户端后端根据凭证关联赛季，并且只读取 `proof_record.status = 1` 的有效记录。

### 4.3 成功响应

状态码：`200 OK`。响应直接返回图片二进制，响应头和允许媒体类型与头像接口一致，并使用 `IMAGE_CACHE_SECONDS` 作为 `max-age`。

### 4.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| 管理员 Token 无效 | `303 See Other` | 重定向至登录接口 |
| ID 不是正整数 | `422 Unprocessable Entity` | FastAPI 路径参数校验错误 |
| 图片路径逃逸 | `400 Bad Request` | `凭证图片路径非法` |
| 凭证不存在或已失效 | `404 Not Found` | `凭证不存在` |
| 凭证缺少有效赛季主键 | `404 Not Found` | `凭证所属赛季不存在` |
| 最终图片文件不存在 | `404 Not Found` | `凭证图片文件不存在` |
| 上游网络失败 | `502 Bad Gateway` | `客户端后端凭证图片服务不可用` |
| 上游返回未约定状态 | `502 Bad Gateway` | `客户端后端凭证图片服务响应异常` |
| 上游返回非允许图片内容 | `502 Bad Gateway` | `客户端后端返回了无效的凭证图片内容` |

---

## 5. `GET /image/product` 获取商品图片

### 5.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/image/product` |
| Nginx 开发路径 | `/dev/flame/admin/api/image/product` |
| 客户端后端路径 | `/flame/api/admin/product` |
| 认证要求 | 有效的管理员 Bearer Token |

请求示例：

```http
GET /dev/flame/admin/api/image/product?image_url=%2FKeep%20%E5%BC%B9%E5%8A%9B%E5%B8%A6.jpg
Authorization: Bearer <admin-token>
```

### 5.2 请求参数

| 参数 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `image_url` | `string` | 是 | 最长 255 个字符，不能仅包含空白 | 管理数据中的商品图片相对地址，例如 `/Keep 弹力带.jpg` |

管理端不查询 `product` 表，只把清除首尾空白后的 `image_url` 传给固定的客户端后端接口。客户端后端负责去除开头的 `/` 或 `\`、拼接商品图片目录并校验路径没有逃逸。

### 5.3 成功响应

状态码：`200 OK`。响应直接返回图片二进制，并根据上游识别结果返回允许的图片媒体类型。缓存响应头使用 `IMAGE_CACHE_SECONDS`：

```http
Content-Type: image/jpeg
Cache-Control: private, max-age=86400
X-Content-Type-Options: nosniff
```

### 5.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| 管理员 Token 无效 | `303 See Other` | 重定向至登录接口 |
| 参数缺失或超过 255 个字符 | `422 Unprocessable Entity` | FastAPI 参数校验错误 |
| 地址为空或只有空白 | `400 Bad Request` | `商品图片路径不能为空` |
| 上游判定路径非法 | `400 Bad Request` | 上游安全提示，异常格式时为 `商品图片路径非法` |
| 商品图片不存在 | `404 Not Found` | 上游安全提示，异常格式时为 `商品图片文件不存在` |
| 上游网络失败 | `502 Bad Gateway` | `客户端后端商品图片服务不可用` |
| 上游返回未约定状态 | `502 Bad Gateway` | `客户端后端商品图片服务响应异常` |
| 上游返回非允许图片内容 | `502 Bad Gateway` | `客户端后端返回了无效的商品图片内容` |

---

## 6. `GET /image/poster` 获取活动海报

### 6.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/image/poster` |
| Nginx 开发路径 | `/dev/flame/admin/api/image/poster` |
| 客户端后端路径 | `/flame/api/admin/poster` |
| 认证要求 | 有效的管理员 Bearer Token |

请求不接受文件名、路径或查询参数。管理端始终读取客户后端的唯一活动海报。

### 6.2 成功响应

状态码：`200 OK`。响应直接返回 WebP 二进制：

```http
Content-Type: image/webp
Cache-Control: private, no-store
X-Content-Type-Options: nosniff
```

海报可以被覆盖，因此本接口不使用 `IMAGE_CACHE_SECONDS`，避免管理前端继续展示旧图。客户后端返回空内容或非 WebP 媒体类型时，管理端将其视为异常响应。

### 6.3 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| 管理员 Token 无效 | `303 See Other` | 重定向至登录接口 |
| 固定海报不存在 | `404 Not Found` | 上游安全提示，异常格式时为 `活动海报文件不存在` |
| 上游网络失败 | `502 Bad Gateway` | `客户端后端活动海报服务不可用` |
| 上游返回未约定状态 | `502 Bad Gateway` | `客户端后端活动海报服务响应异常` |
| 上游返回空内容或非 WebP | `502 Bad Gateway` | `客户端后端返回了无效的活动海报内容` |

---

## 7. `POST /image/poster` 变更活动海报

### 7.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/image/poster` |
| Nginx 开发路径 | `/dev/flame/admin/api/image/poster` |
| 客户端后端路径 | `/flame/api/admin/poster` |
| 请求类型 | `multipart/form-data` |
| 认证要求 | 有效的管理员 Bearer Token |

表单字段：

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `image` | `File` | 是 | JPEG、PNG 或 WebP，最大 10 MiB | 待重编码并覆盖的活动海报 |

请求示例：

```bash
curl -X POST 'https://example.com/dev/flame/admin/api/image/poster' \
  -H 'Authorization: Bearer <admin-token>' \
  -F 'image=@./活动规则.png;type=image/png'
```

调用方不能提交目标文件名或路径。管理端也不会向上游转发原始文件名，而是按声明媒体类型使用内部固定文件名，并始终请求固定 `/poster` 路径。

### 7.2 处理流程

1. FastAPI 校验 `image` 字段存在。
2. 管理端限量读取 10 MiB 加一字节，拒绝空文件、超限文件和不受支持的声明媒体类型。
3. 管理端把原始图片作为 multipart 文件中转到客户后端，不在本地落盘或重新编码。
4. 客户后端校验实际图片内容、修正 EXIF 方向、以质量 90 重编码为 WebP，并原子覆盖固定资源。
5. 管理端验证上游响应中的固定 `image_url` 和正整数 `size_bytes` 后返回管理前端。

实际图片格式、像素与解码安全由执行重编码的客户后端作最终校验。管理端的前置校验用于减少明显无效或超大请求占用内部网络和上游资源。

### 7.3 成功响应

状态码：`200 OK`。

```json
{
  "image_url": "/活动规则.webp",
  "size_bytes": 470258
}
```

`size_bytes` 是客户后端重编码后 WebP 文件的大小，不要求等于上传原图大小。

### 7.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| 管理员 Token 无效 | `303 See Other` | 重定向至登录接口 |
| 缺少 `image` | `422 Unprocessable Entity` | FastAPI 参数校验错误 |
| 文件为空 | `400 Bad Request` | `活动海报不能为空` |
| 声明媒体类型不受支持 | `400 Bad Request` | `活动海报仅支持 JPEG、PNG 或 WebP` |
| 实际图片内容无效 | `400 Bad Request` | 客户后端返回的安全业务提示 |
| 文件超过 10 MiB | `413 Content Too Large` | `活动海报不能超过 10 MiB` |
| 上游网络失败 | `502 Bad Gateway` | `客户端后端活动海报服务不可用` |
| 上游返回未知状态或畸形成功响应 | `502 Bad Gateway` | `客户端后端活动海报服务响应异常` |

覆盖是客户后端的一次原子文件操作，不涉及管理端数据库事务。请求失败时不能声称海报已经变更；对于结果未知的网络中断，管理员可以重新读取固定海报确认当前内容，并安全地重复上传目标图片。

---

## 8. 公共中转、缓存与安全规则

所有图片读取接口共用以下流程：

1. 验证管理员 Token 和接口参数。
2. 选择代码中预定义的上游路径与参数名。
3. 使用生命周期内共享的客户端发起 `GET` 请求。
4. 校验上游状态和图片媒体类型。
5. 返回图片字节，不在管理端服务器落盘。

头像、项目图标、商品图片和运动凭证图片使用环境变量 `IMAGE_CACHE_SECONDS` 控制浏览器私有缓存，允许范围为 `0～31536000` 秒。活动海报固定使用 `private, no-store`。管理端不会透传上游 Cookie、内部信息或缓存策略，也拒绝 SVG 等可能携带活动内容的格式。

上游地址只读取 `CLIENT_BACKEND_BASE_URL`，内部客户端设置 `trust_env=False`，不会受宿主机代理环境变量影响。未知上游响应正文、网络地址和异常信息不会暴露给管理前端。

---

## 9. 数据与依赖

接口不查询管理端数据库，也不修改业务数据：

- 头像字段参考 [用户表说明](../db/user.md)；
- 项目图标字段参考 [项目表说明](../db/project.md)；
- 商品图片字段参考 [商品表说明](../db/product.md)；
- 凭证图片字段参考 [凭证记录表说明](../db/proof-record.md)；
- 活动海报是客户后端文件系统中的固定资源，不对应数据库字段。

代码入口包括 `app/router/image.py`、`app/services/images.py` 和 `app/clients/client_backend.py`。

---

## 10. 验证方式

```bash
python -m unittest tests.test_image_avatar tests.test_poster -v
```

测试覆盖固定上游路径、中文商品图片地址、参数、图片内容、媒体类型、缓存、安全响应头、海报上传边界、固定海报地址、上游业务错误、网络异常、非法内容与管理员认证。

---

## 11. 已知限制

- 管理端进程不缓存图片，浏览器缓存失效后仍会访问客户端后端。
- 尚无管理端到客户端后端的独立服务凭证，当前依赖局域网隔离。
- 除活动海报上传有 10 MiB 限制外，其他图片读取接口尚未设置单张响应体大小上限。
