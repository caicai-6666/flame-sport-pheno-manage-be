# 查询智能体 API

> **文档目的**
>
> 本文档定义管理端创建自然语言查询、订阅进度、处理用户交互、读取历史轨迹与表格结果，以及取消任务的 HTTP 契约。

## 1. 通用规则

FastAPI 路由前缀为 `/flame/admin/api/agent/queries`，开发环境 Nginx 路径前缀为 `/dev/flame/admin/api/agent/queries`。全部接口要求有效管理员 Bearer Token。

查询异步执行。创建成功只表示任务已进入当前进程，不能把 `202 Accepted` 理解为结果已经生成。当前支持业务域 `sports` 和 `rewards`。

| `domain_key` | 业务范围 | 允许查询的主要内容 |
| --- | --- | --- |
| `sports` | 运动数据 | 赛季、报名、运动项目、项目进度、运动凭证和排行榜 |
| `rewards` | 积分与奖品数据 | 用户、部门、赛季参与、赛季结算积分、积分流水、商品和奖品履约 |

业务域决定表白名单、业务词汇、核心规则、实体解析配置和查询计划校验规则。未知业务域返回 `422`；请求未提供 `domain_key` 时默认使用 `sports`。

---

## 2. 列出缓存查询标识

```http
GET /dev/flame/admin/api/agent/queries/cached-record-ids?limit=100
Authorization: Bearer <admin-token>
```

状态码：`200 OK`。接口只返回当前进程内、尚未超过 `AGENT_QUERY_SESSION_TTL_SECONDS` 保留期的查询标识，按创建时间倒序排列；运行中、等待交互和终态查询都会包含在内。

```json
{
  "query_ids": [
    "b5316a1a8e504dd1bb7a9dc5e4df74f0",
    "75f57282d3714a9e8bea8b5e49cdb6e3"
  ]
}
```

`limit` 可选，默认 `100`，范围为 `1～200`。前端必须使用返回的 `query_id` 分别调用状态、轨迹或结果接口；本接口不返回问题、轨迹或表格，避免一次传输大量缓存内容。服务重启、会话过期或多进程部署后，列表不保证保留旧记录。

---

## 3. 创建查询

### 3.1 请求

```http
POST /dev/flame/admin/api/agent/queries
Authorization: Bearer <admin-token>
Content-Type: application/json
```

```json
{
  "question": "查询 Amy 当前的积分余额",
  "domain_key": "rewards"
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `question` | `string` | 是 | `1～2000` 个字符 | 操作员自然语言问题 |
| `domain_key` | `string` | 否 | `1～64` 个字符，默认 `sports`；可选 `sports`、`rewards` | 显式注册的业务域 |

### 3.2 响应

状态码：`202 Accepted`。

```json
{
  "query_id": "b5316a1a8e504dd1bb7a9dc5e4df74f0",
  "domain_key": "rewards",
  "question": "查询 Amy 当前的积分余额",
  "status": "running",
  "latest_sequence": 1,
  "pending_interaction": null,
  "result_available": false,
  "user_message": null,
  "created_at": "2026-08-20T10:00:00+08:00",
  "updated_at": "2026-08-20T10:00:00+08:00"
}
```

活动会话达到配置上限时返回 `429`；模型密钥缺失时返回 `503`；未知业务域返回 `422`。

---

## 4. 查询状态

```http
GET /dev/flame/admin/api/agent/queries/<query_id>
Authorization: Bearer <admin-token>
```

状态码：`200 OK`。响应与创建接口相同。等待交互时，`pending_interaction` 示例为：

```json
{
  "interaction_id": "9c3d5314a9554ed0ae63b15ad8ecbf06",
  "interaction_type": "confirmation",
  "question": "我将按以下需求继续查询：查询当前进行中的赛季名称和起止日期",
  "options": ["确认并继续", "取消查询"],
  "allow_free_text": false
}
```

`interaction_type` 为 `clarification` 时，前端应允许填写自由文本。会话不存在或终态保留时间已过时返回 `404`。

---

## 5. 订阅 SSE 进度

```http
GET /dev/flame/admin/api/agent/queries/<query_id>/events
Authorization: Bearer <admin-token>
Accept: text/event-stream
Last-Event-ID: 3
```

`Last-Event-ID` 可省略；提供时必须是非负整数，非法值返回 `400`。服务器先补发仍保留且序号更大的历史事件，再推送实时事件；任务进入终态并发送完剩余事件后关闭连接。

事件帧示例：

```text
id: 4
event: interaction_required
data: {"stage":"confirmation","event_type":"interaction_required","status":"waiting","title":"请确认查询需求","message":"我将按以下需求继续查询：...","payload":{"interaction_id":"...","interaction_type":"confirmation","options":["确认并继续","取消查询"],"allow_free_text":false},"query_id":"...","sequence":4,"occurred_at":"2026-08-20T10:00:02+08:00"}

```

事件字段：

| 字段 | 说明 |
| --- | --- |
| `query_id` | 查询任务标识 |
| `sequence` | 查询内单调递增事件序号 |
| `stage` | 当前阶段，例如 `alignment`、`planning`、`execution`、`translation` 或 `result` |
| `event_type` | 阶段开始、进度、交互、阶段完成或查询终态事件 |
| `status` | `running`、`waiting`、`success`、`abandoned`、`cancelled` 或 `failure` |
| `title`、`message` | 面向操作员的友好进度说明 |
| `payload` | 交互标识、选项等受控附加数据 |
| `occurred_at` | `Asia/Shanghai` 时区时间 |

服务器还会发送 `: heartbeat` 注释维持连接。响应禁止缓存并设置 `X-Accel-Buffering: no`。

最终结果审计成功时，`stage = result` 的 `stage_completed` 与最终 `query_completed` 事件的 `message` 都是受约束的 `result_summary`，用于直接向操作员说明完整结果的行数、类别分布或数值极值。审计不可用时，事件改为说明结果表已生成但摘要暂不可用。

> **前端接入**
>
> 原生 `EventSource` 不能设置现有 Bearer Header。管理前端应使用 `fetch` 流式读取 SSE，不要把 Bearer Token 放入 URL。

---

## 6. 提交交互回答

```http
POST /dev/flame/admin/api/agent/queries/<query_id>/interactions/<interaction_id>/answer
Authorization: Bearer <admin-token>
Content-Type: application/json
```

```json
{
  "answer": "确认并继续"
}
```

`answer` 长度为 `1～1000` 个字符。成功返回 `200` 和最新会话状态。每次交互从请求发出起最多等待 `5` 分钟；超时后查询转为 `failed`，SSE 发布 `query_failed`，轨迹保留交互问题和失败终态。错误 `interaction_id`、已结束交互、纯空白答案或重复回答返回 `409`；会话不存在返回 `404`；缺少字段或长度不合法等请求 Schema 错误返回 `422`。

---

## 7. 读取历史轨迹

```http
GET /dev/flame/admin/api/agent/queries/<query_id>/trace
Authorization: Bearer <admin-token>
```

状态码：`200 OK`。接口返回当前内存保留期内的原始问题、已对齐问题、用户交互问答和面向操作员的关键阶段时间线；不会返回表格、SQL、模型原始响应、隐藏推理或工具参数。业务对齐尚未完成时，`aligned_question` 为 `null`，但对应阶段事件仍会出现在 `entries` 中。成功查询的最终 `result` 阶段记录包含与 SSE 一致的 `result_summary`；表格与完整审计字段仍由 `result` 接口单独返回。

```json
{
  "query_id": "b5316a1a8e504dd1bb7a9dc5e4df74f0",
  "domain_key": "sports",
  "question": "查询当前进行中的赛季名称和起止日期",
  "aligned_question": "查询当前进行中的赛季名称、开始日期和结束日期",
  "status": "completed",
  "user_message": "结果共 1 行。",
  "created_at": "2026-08-20T10:00:00+08:00",
  "updated_at": "2026-08-20T10:00:08+08:00",
  "entries": [
    {
      "sequence": 1,
      "entry_type": "question_submitted",
      "stage": "accepted",
      "status": "running",
      "title": "已提交查询问题",
      "message": "查询当前进行中的赛季名称和起止日期",
      "options": [],
      "occurred_at": "2026-08-20T10:00:00+08:00"
    },
    {
      "sequence": 4,
      "entry_type": "interaction_answered",
      "stage": "confirmation",
      "status": "success",
      "title": "已提交查询确认",
      "message": "操作员选择：确认并继续",
      "options": [],
      "occurred_at": "2026-08-20T10:00:03+08:00"
    }
  ]
}
```

`entry_type` 包括 `question_submitted`、`progress`、`interaction_requested` 和 `interaction_answered`。`interaction_requested` 会在 `options` 中返回当时展示给操作员的选项；用户答案作为 `interaction_answered` 单独记录，不通过 SSE 再次推送。

轨迹与 SSE 补发队列是独立的内存集合，均受 `AGENT_QUERY_EVENT_HISTORY_SIZE` 限制，并跟随 `AGENT_QUERY_SESSION_TTL_SECONDS` 清理。会话不存在、服务重启或保留期已过时返回 `404`。

---

## 8. 读取表格结果

```http
GET /dev/flame/admin/api/agent/queries/<query_id>/result
Authorization: Bearer <admin-token>
```

任务仍运行或等待交互时返回 `202`，表格为空。终态返回 `200`，成功示例为：

```json
{
  "query_id": "b5316a1a8e504dd1bb7a9dc5e4df74f0",
  "status": "completed",
  "user_message": "结果共 1 行。",
  "matches_user_request": true,
  "relevance_explanation": "每行代表一个符合条件的赛季，并返回名称与日期。",
  "table_description": "每行是一条赛季记录。",
  "result_summary": "结果共 1 行。",
  "issues": [],
  "headers": [
    {"key": "season_name", "label": "赛季名称"},
    {"key": "start_date", "label": "开始日期"},
    {"key": "end_date", "label": "结束日期"}
  ],
  "rows": [
    {
      "season_name": "2026年8月赛季",
      "start_date": "2026-08-01",
      "end_date": "2026-08-31"
    }
  ],
  "statistics": {
    "row_count": 1,
    "planned_limit": null,
    "limit_reached": false,
    "category_fields": [],
    "numeric_extremes": []
  }
}
```

`rows` 是程序依据数据库字段注释映射后的完整 SQL 结果，不是模型重写的样本。只有可追溯到直接原始列的状态、类型或布尔编码会被转换；注释未定义的值以及翻译层失败时的全部值保持数据库原样。审计失败时仍可返回表头、结果行和统计，但相关性说明字段可能为空。

---

## 9. 取消查询

```http
DELETE /dev/flame/admin/api/agent/queries/<query_id>
Authorization: Bearer <admin-token>
```

活动查询转为 `cancelled` 并返回 `200`；终态查询重复取消保持原状态，按幂等成功返回。会话不存在返回 `404`。

---

## 10. 通用异常与安全

| 场景 | 状态码 | 说明 |
| --- | --- | --- |
| 管理员 Token 缺失、无效或过期 | `303` | 沿用管理端统一认证重定向 |
| 请求字段不合法 | `422` | FastAPI 或 Pydantic 校验错误 |
| 查询会话不存在或已过期 | `404` | 前端应提示重新创建查询 |
| 活动会话达到上限 | `429` | 不创建后台任务 |
| 查询功能未配置模型服务 | `503` | 不泄漏密钥和内部地址 |
| `Last-Event-ID` 非法 | `400` | SSE 连接不会建立 |

接口不会返回模型原始响应、隐藏推理、工具参数、表结构、SQL 或数据库原始异常。完整运行边界见 [查询智能体运行时与业务域扩展](../infrastructure/query-agent-runtime.md)。

---

## 11. 验证方式与已知限制

自动化接口测试覆盖认证、创建、等待确认、提交回答、历史轨迹、完成、SSE 历史重放和表格结果读取。当前会话仅存于单进程内存，服务重启后旧 `query_id` 不再可用；当前部署不得启用多 Worker。
