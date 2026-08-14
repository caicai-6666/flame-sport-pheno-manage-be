# 服务健康检查 API

> **文档目的**
>
> 本文档描述管理端后端的基础存活检查接口。该接口用于确认 FastAPI 进程能够接收并响应请求，不承担 MySQL 或客户端后端服务的就绪检查。

## 1. `GET /health` 检查服务存活

### 1.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/health` |
| Nginx 开发路径 | `/dev/flame/admin/api/health` |
| 认证要求 | 无 |
| 请求参数 | 无 |

### 1.2 成功响应

状态码：`200 OK`。

```json
{
  "status": "ok",
  "service": "燃动现象管理端后端",
  "environment": "development"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | `string` | 固定为 `ok`，表示当前进程能够正常响应 |
| `service` | `string` | 配置项 `APP_NAME` 对应的服务名称 |
| `environment` | `string` | 配置项 `APP_ENV` 对应的运行环境 |

### 1.3 异常与边界

该接口没有业务异常响应，但存在以下边界：

- 不访问 MySQL，数据库不可用时仍可能返回 `200`；
- 不请求客户端后端，外部服务不可用时仍可能返回 `200`；
- 不返回数据库地址、密钥、内部异常或其他敏感配置；
- Nginx 会摘除 `/dev`，FastAPI 只注册不带该前缀的路径。

> **注意**
>
> 本接口是管理 API 中明确不要求 Bearer Token 的例外。后续如需表达依赖就绪状态，应新增独立接口，不能改变本接口的存活语义。

---

## 2. 验证方式

```bash
python -m unittest tests.test_health -v
```

测试验证接口返回 `200`、`status=ok` 和当前环境名称，并确认旧路径 `/api/v1/health` 不再可用。
