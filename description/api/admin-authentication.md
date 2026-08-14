# 管理端密钥认证 API

> **文档目的**
>
> 本文档描述管理员共享密钥换取短期访问令牌、前端校验令牌，以及服务端进程内缓存的安全边界。

## 1. 功能目标

管理前端首次进入时提交管理员密钥，后端校验成功后签发短期 Bearer Token。前端后续只携带 Token，**不得持久缓存或反复发送管理员密钥**。

接口认证范围：

| 接口 | 是否需要 Bearer Token |
| --- | --- |
| `GET /health` | 否 |
| `POST /auth/login` | 否，使用管理员密钥登录 |
| `GET /auth/session` | 是 |
| 其他管理接口 | 是 |

FastAPI 基础路径为 `/flame/admin/api`；经开发环境 Nginx 访问时使用 `/dev/flame/admin/api`。

---

## 2. `POST /auth/login` 换取访问令牌

### 2.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/auth/login` |
| Nginx 开发路径 | `/dev/flame/admin/api/auth/login` |
| 认证要求 | 请求体提供管理员密钥 |

### 2.2 请求体

```json
{
  "admin_key": "<administrator-key>"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `admin_key` | `string` | 是 | 环境变量 `ADMIN_KEY` 对应的管理员共享密码 |

### 2.3 成功响应

状态码：`200 OK`。

```json
{
  "access_token": "<opaque-access-token>",
  "token_type": "bearer",
  "expires_in": 28800
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `access_token` | `string` | 高熵随机令牌，只在签发响应中返回一次 |
| `token_type` | `string` | 固定为 `bearer` |
| `expires_in` | `integer` | 令牌从签发起的有效秒数 |

### 2.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| 缺少请求体或 `admin_key` | `422 Unprocessable Entity` | FastAPI 参数校验错误 |
| 管理员密钥错误 | `401 Unauthorized` | `管理员密钥无效`，且不回显提交值 |

---

## 3. `GET /auth/session` 验证访问令牌

### 3.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/auth/session` |
| Nginx 开发路径 | `/dev/flame/admin/api/auth/session` |
| 请求参数 | 无 |
| 认证要求 | `Authorization: Bearer <access-token>` |

### 3.2 成功响应

状态码：`200 OK`。

```json
{
  "authenticated": true
}
```

### 3.3 异常处理

| 场景 | 状态码 | 处理方式 |
| --- | --- | --- |
| Token 缺失、伪造、过期或因服务重启失效 | `303 See Other` | `Location` 指向 `/dev/flame/admin/api/auth/login` |

采用 `303` 可以避免浏览器把原接口的方法和请求体重放到登录接口。`/auth/login` 是 API 而非 HTML 页面，前端应拦截重定向、删除失效 Token 并展示自己的登录视图。

---

## 4. 服务端缓存机制

当前缓存关系为：

```text
SHA-256(token) -> SHA-256(ADMIN_KEY) + expires_at
```

安全与资源约束：

- 不保存 Token 明文和管理员密钥明文；
- 使用安全随机源签发令牌，并使用恒定时间比较摘要；
- 默认有效期为 `28800` 秒，即 8 小时；
- 签发和验证时清理过期记录；
- 达到容量上限时淘汰最早到期的 Token；
- 服务重启或更换 `ADMIN_KEY` 后，旧 Token 失效。

> **警告**
>
> 当前缓存仅适用于单工作进程。多 worker 或多实例部署必须改用 Redis 等共享缓存。

---

## 5. 环境配置与安全

| 配置项 | 约束 | 说明 |
| --- | --- | --- |
| `PUBLIC_API_PREFIX` | 必填路径 | 生成包含 `/dev` 的登录重定向地址 |
| `ADMIN_KEY` | 必填非空字符串 | 管理员共享密码，不得提交版本库 |
| `ADMIN_TOKEN_TTL_SECONDS` | `60～604800` | Token 有效期，默认 `28800` 秒 |
| `ADMIN_TOKEN_CACHE_MAX_SIZE` | `1～10000` | 单进程最大缓存数量，默认 `1000` |

禁止把管理员密钥、完整 Token 或认证请求体写入日志、错误响应和埋点。生产链路必须使用 HTTPS，并应在 Nginx 对登录接口配置独立频率限制。

---

## 6. 验证方式

```bash
python -m unittest tests.test_admin_auth -v
```

测试覆盖正确与错误密钥、Token 会话校验、缺失或伪造 Token 重定向、缓存过期和容量淘汰。

---

## 7. 已知限制

- 尚未提供主动退出或撤销单个 Token 的接口。
- 缓存不跨进程、不持久化。
- 尚未实现管理员个人账号、角色权限、操作审计和应用层登录限流。
