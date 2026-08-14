# 图片安全中转 API

> **文档目的**
>
> 本文档定义管理端后端从客户端后端读取头像、项目图标、商品图片与运动凭证图片，并安全返回管理前端的接口契约、缓存规则和信任边界。

## 1. 功能目标

图片保存在客户端后端。管理端后端提供受管理员认证保护的安全中转，管理前端无需直接访问局域网地址。

| 图片类型 | 管理端子路径 | 客户端后端子路径 | 定位参数 |
| --- | --- | --- | --- |
| 用户头像 | `/image/avator` | `/avator` | `avatar_url` |
| 项目图标 | `/image/project_icon` | `/project_icon` | `icon_url` |
| 商品图片 | `/image/product` | `/product` | `image_url` |
| 运动凭证 | `/image/proof_record/{proof_record_id}` | `/proof_record/{proof_record_id}` | 正整数路径参数 |

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

## 6. 公共中转、缓存与安全规则

四个接口共用以下流程：

1. 验证管理员 Token 和接口参数。
2. 选择代码中预定义的上游路径与参数名。
3. 使用生命周期内共享的客户端发起 `GET` 请求。
4. 校验上游状态和图片媒体类型。
5. 返回图片字节，不在管理端服务器落盘。

所有图片使用环境变量 `IMAGE_CACHE_SECONDS` 控制浏览器私有缓存，允许范围为 `0～31536000` 秒。响应使用 `private` 防止共享代理缓存，不使用 `immutable`。管理端不会透传上游 Cookie、内部信息或缓存策略，也拒绝 SVG 等可能携带活动内容的格式。

上游地址只读取 `CLIENT_BACKEND_BASE_URL`，内部客户端设置 `trust_env=False`，不会受宿主机代理环境变量影响。未知上游响应正文、网络地址和异常信息不会暴露给管理前端。

---

## 7. 数据与依赖

接口不查询管理端数据库，也不修改业务数据：

- 头像字段参考 [用户表说明](../db/user.md)；
- 项目图标字段参考 [项目表说明](../db/project.md)；
- 商品图片字段参考 [商品表说明](../db/product.md)；
- 凭证图片字段参考 [凭证记录表说明](../db/proof-record.md)。

代码入口包括 `app/router/image.py`、`app/services/images.py` 和 `app/clients/client_backend.py`。

---

## 8. 验证方式

```bash
python -m unittest tests.test_image_avatar -v
```

测试覆盖固定上游路径、中文商品图片地址、参数、图片内容、媒体类型、缓存、安全响应头、上游业务错误、网络异常、非法内容与管理员认证。

---

## 9. 已知限制

- 管理端进程不缓存图片，浏览器缓存失效后仍会访问客户端后端。
- 尚无管理端到客户端后端的独立服务凭证，当前依赖局域网隔离。
- 尚未设置单张图片的独立响应体大小上限。
