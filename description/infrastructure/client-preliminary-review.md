# 客户端初审集成

> **文档目的**
>
> 本文档说明管理端结算任务调用客户端后端立即初审和补交专用初审接口的固定协议、网络边界和失败语义。

## 1. 集成目标

赛季进入结算中后，管理端使用通用立即初审接口处理赛季截止前遗留的 `pending` 凭证，并使用补交专用接口处理资格状态为 `2` 的新补交记录。管理端不读取图片、不调用 DeepSeek，也不自行决定初审结论。

---

## 2. 请求协议

两类固定请求为：

```http
POST /flame/api/admin/proof_record/{proof_record_id}/preliminary-review
POST /flame/api/admin/supplement/{proof_record_id}/preliminary-review
```

第一条处理非未开始赛季的非补交待审记录，并读取当前启用规则；结算任务实际只提交当前结算赛季截止前遗留记录。第二条只处理结算中且资格状态为 `2` 的记录，并强制读取资格表固化的初审上下文。管理端通过 `CLIENT_BACKEND_BASE_URL` 配置管理接口基础地址，并使用正整数数据库凭证 ID 拼接相对路径。请求不接收用户提供的任意 URL，也不传递审核结论。

成功响应必须包含与请求一致的 `proof_record_id`、合法的 `review_status`、字符串 `review_comment`、`progress_delta` 和 `increase`。合法状态仅包括：

```text
preliminary_approved
preliminary_rejected
```

该内部接口响应中的 `review_comment` 表示本次初审意见。客户端后端应将其写入 `proof_record.preliminary_review_comment`，不得写入或覆盖管理员终审使用的 `proof_record.review_comment`；管理端这里只校验调用结果，不重复写库。

---

## 3. 调用边界

- 客户端后端负责验证凭证仍有效且为 `pending`。
- 通用立即入口允许处理进行中、结算中或已结束赛季，不应用普通定时初审等待时间。
- 补交入口校验资格状态和快照，并把审核结果与资格状态原子提交：初审通过执行 `2 → 3`，初审失败执行 `2 → 1`；不得降级读取当前项目规则。
- 客户端后端写回时校验 `created_at`、`note` 和审核状态，避免覆盖重传版本。
- 管理端在数据库事务外调用接口，并在调用后重新查询数据库状态。
- 请求复用 FastAPI 生命周期创建的 `httpx.AsyncClient` 连接池。

### 3.1 月初与月末的依赖顺序

结算任务按 `project_upload_config.record_type` 中的 `月初记录`、`月末记录` 识别阶段型凭证，以 `season_user_id + project_id` 确定同赛季、同用户、同项目的依赖范围。

- 普通凭证和月初记录保持分批并发处理。
- 同组存在有效且仍为 `pending` 的月初记录时，月末不进入可执行批次。过滤在分页限制之前执行，因此月末主键更小、月初位于后续批次时也不会提前审核。
- 月初审核通过并写库后，后续批次才提交月末请求；已经通过初审或终审的月初不需要重复审核。
- 月初被拒绝、没有有效通过的月初基线时，客户端后端直接把月末判为 `preliminary_rejected`，意见为“缺少有效月初记录，无法审核月末结果。”，原始及实际进度贡献为零。**该分支在读取图片和调用大模型之前返回，不产生月末模型请求。**
- 管理端仍调用月末的内部 HTTP 接口，以复用客户端的规则拒绝、版本校验和事务写回；不在管理端重复写入初审结果。补交月末被规则拒绝时，客户端同时将资格从 `2` 恢复为 `1`。
- 月初网络失败且仍为 `pending` 时，月末继续等待，不能把调用失败当作月初拒绝。仅有月末、月初已作废或基线意见缺失时，也由客户端校验并直接拒绝。

两类队列都会检查同组月初依赖。遗留月末等待补交月初时，本轮仍允许处理补交队列，月末在下一轮重试；待审总数仍包含被依赖阻塞的记录，不能因此提前定分。

依赖查询只保证每次取批次时观察到的先后顺序，不会跨 HTTP 调用长期锁定月初记录。查询后发生并发重传时，客户端仍须复核基线和凭证版本；管理端不引入跨服务分布式锁。

---

## 4. 错误处理

| 错误 | 管理端行为 |
| --- | --- |
| `404` | 记录凭证 ID 和状态码，下一轮重新查询数据库 |
| `409` | 视为状态竞争或规则不完整，不覆盖数据库结果 |
| `502` | 保留 `pending`，等待后续轮询重试 |
| 连接或超时错误 | 保留 `pending`，等待后续轮询重试 |
| 响应契约错误 | 不进入用户定分，记录不含响应正文的安全日志 |

任何调用错误都不能被解释为 `preliminary_rejected`。只要数据库仍存在符合截止条件的遗留 `pending`，或存在资格状态为 `2` 的补交待审记录，整个赛季用户定分阶段保持阻塞。

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

验证覆盖两类固定路径、响应 ID 与状态校验、外部失败不伪造结果，以及遗留待初审和补交待初审对定分的阻塞。

月初依赖回归测试位于本地 `tests/test_monthly_review_order.py`，使用内存 SQLite 执行实际筛选 SQL，并使用客户端替身验证跨批次顺序、普通凭证并发、失败重试、拒绝分支、补交资格及跨队列等待：

```bash
python -m unittest discover -s tests -p 'test_monthly_review_order.py' -v
```

测试不连接真实 MySQL 或模型服务；客户端“不调用模型直接拒绝”的实现属于客户端后端契约，需要在客户端后端同步验证。`tests/` 按仓库约定被 Git 忽略。

### 7.1 双后端 HTTP 与 MySQL 联调

本地联调使用独立空白 MySQL 8.4 数据库、客户端真实管理路由及服务、管理端真实批处理与 HTTP 适配器。客户端执行实际图片读取、初审工作流、SDK 请求构造、响应校验和数据库事务，仅在模型 HTTP transport 边界返回确定性测试结果。

测试脚本位于管理端本地 `tests/`：

| 脚本 | 职责 |
| --- | --- |
| `monthly_review_client_server.py` | 加载相邻客户端仓库、初始化空白测试库及合成图片，启动仅绑定本机的测试服务；不启动业务调度和通知投递 |
| `run_monthly_review_integration.py` | 运行管理端初审批处理，经真实 HTTP 调用客户端，检查数据库写回及模型调用记录 |

准备仅绑定本机随机端口的临时 MySQL 容器，数据库名称固定为 `monthly_review_test`。必须使用新建空白实例；客户端测试脚本检测到已有表时会拒绝初始化。不得填入已有业务数据库地址。

在管理端仓库根目录，使用客户端 Python 环境启动测试服务；将占位符替换为本机测试资源：

```bash
<client-python> tests/monthly_review_client_server.py \
  --client-repo ../flame-sport-pheno-be \
  --database-url '<isolated-local-mysql-url>' \
  --assets '<temporary-image-directory>' \
  --port <local-test-port>
```

随后使用管理端 Python 环境运行：

```bash
python tests/run_monthly_review_integration.py \
  --database-url '<isolated-local-mysql-url>' \
  --client-url 'http://127.0.0.1:<local-test-port>'
```

固定九组样本覆盖普通凭证并发、跨批次月初先审、月初拒绝、月初接口失败后重试、补交通过与拒绝、已有终审通过基线、两种跨队列依赖以及缺少月初。断言包括：

- 月初成功响应先于月末请求；已完成的月初不会重复调用。
- 月初拒绝或缺少基线的月末没有模型请求，数据库写入 `preliminary_rejected` 和零进度。
- 初审意见与终审意见保持分离，项目进度与凭证贡献一致。
- 补交通过资格为 `3`、补交拒绝恢复为 `1`，拒绝通知数量正确。
- 注入的 `502` 保留待审状态，后续轮询可恢复；并发数不超过管理端传入的限制。

测试结束后停止专用客户端进程，删除本次创建的临时容器及合成图片。此联调不验证真实模型识别质量、部署网络或钉钉通知实际投递。
