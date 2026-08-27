# 客户端立即初审集成

> **文档目的**
>
> 本文档说明管理端结算任务调用客户端后端立即初审接口的固定协议、网络边界和失败语义。

## 1. 集成目标

赛季进入结算中后，管理端使用客户端后端已有接口处理赛季截止前遗留的 `pending` 凭证。管理端不读取图片、不调用 DeepSeek，也不自行决定初审结论。

---

## 2. 请求协议

固定请求为：

```http
POST /flame/api/admin/proof_record/{proof_record_id}/preliminary-review
```

管理端通过 `CLIENT_BACKEND_BASE_URL` 配置管理接口基础地址，并使用正整数数据库凭证 ID 拼接相对路径。请求不接收用户提供的任意 URL，也不传递审核结论。

成功响应必须包含与请求一致的 `proof_record_id`、合法的 `review_status`、字符串 `review_comment`、`progress_delta` 和 `increase`。合法状态仅包括：

```text
preliminary_approved
preliminary_rejected
```

该内部接口响应中的 `review_comment` 表示本次初审意见。客户端后端应将其写入 `proof_record.preliminary_review_comment`，不得写入或覆盖管理员终审使用的 `proof_record.review_comment`；管理端这里只校验调用结果，不重复写库。

---

## 3. 调用边界

- 客户端后端负责验证凭证仍有效且为 `pending`。
- 客户端后端允许处理进行中或结算中赛季，不应用普通定时初审等待时间。
- 客户端后端写回时校验 `created_at`、`note` 和审核状态，避免覆盖重传版本。
- 管理端在数据库事务外调用接口，并在调用后重新查询数据库状态。
- 请求复用 FastAPI 生命周期创建的 `httpx.AsyncClient` 连接池。

---

## 4. 错误处理

| 错误 | 管理端行为 |
| --- | --- |
| `404` | 记录凭证 ID 和状态码，下一轮重新查询数据库 |
| `409` | 视为状态竞争或规则不完整，不覆盖数据库结果 |
| `502` | 保留 `pending`，等待后续轮询重试 |
| 连接或超时错误 | 保留 `pending`，等待后续轮询重试 |
| 响应契约错误 | 不进入用户定分，记录不含响应正文的安全日志 |

任何调用错误都不能被解释为 `preliminary_rejected`。只要数据库仍存在符合截止条件的 `pending`，整个赛季用户定分阶段保持阻塞。

---

## 5. 配置

| 配置 | 默认值 | 说明 |
| --- | ---: | --- |
| `CLIENT_BACKEND_TIMEOUT_SECONDS` | `10` | 单次内部 HTTP 超时秒数 |
| `SEASON_SETTLEMENT_REVIEW_BATCH_SIZE` | `100` | 每批查询的待初审凭证数 |
| `SEASON_SETTLEMENT_REVIEW_CONCURRENCY` | `5` | 单进程最大并发初审请求数 |

配置在应用启动时校验为正数，并设置上限，避免错误配置造成无界并发。

---

## 6. 安全限制

该接口当前依赖 Docker 内网边界。部署时不得把客户端后端管理接口直接开放到不可信网络；开放前需要增加真实的服务间认证。

日志只记录 `proof_record_id` 和 HTTP 状态码，不记录未知响应正文、凭证备注、图片内容或认证信息。

---

## 7. 实现与验证

- `app/clients/client_backend.py`
- `app/services/season_settlements.py`
- `tests/test_client_backend_config.py`
- `tests/test_season_settlement.py`

验证覆盖固定路径、响应 ID 与状态校验、外部失败不伪造结果，以及遗留待初审对定分的阻塞。
