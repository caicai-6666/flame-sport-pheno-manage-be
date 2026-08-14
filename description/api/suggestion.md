# 用户意见 API

> **文档目的**
>
> 本文档定义管理端拉取可见用户意见及记录处理结论的接口契约、业务规则、成功响应和异常处理。

## 1. 功能目标

`suggestion` 路由集中承载用户意见的管理端查询与处理能力。管理端列表只读取**可见且待处理**的意见，并可以将其标记为**拒绝**或**已解决**。

> **状态口径**
>
> API 使用更贴近管理端操作的 `resolved` 表示“已解决”；现有数据库使用 `optimized` 保存同一业务结果。该映射只由应用服务维护，不要求前端了解数据库枚举。

---

## 2. `GET /suggestion/list` 拉取待处理用户意见

### 2.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/suggestion/list` |
| Nginx 开发路径 | `/dev/flame/admin/api/suggestion/list` |
| 请求参数 | 无 |
| 认证要求 | 有效的管理员 Bearer Token |

调用示例：

```http
GET /dev/flame/admin/api/suggestion/list
Authorization: Bearer <admin-token>
```

### 2.2 查询口径

接口只返回满足以下条件的记录：

```text
user_suggestion.status = 1
user_suggestion.processing_stage = 'pending'
user_suggestion.user_id = user.id
```

已经进入 `rejected` 或 `optimized` 阶段的意见不会返回。查询通过 `user_id` 联动用户名称和头像地址，不根据 `user.status` 过滤，保证已停用用户提交的待处理意见仍能在管理端展示。

结果按照以下顺序倒序排列：

1. `user_suggestion.created_at`；
2. `user_suggestion.id`。

### 2.3 成功响应

状态码：`200 OK`。

```json
[
  {
    "id": 12,
    "user_name": "张三",
    "avatar_url": "/zhang-san.jpg",
    "content": "希望增加更多户外运动项目",
    "created_at": "2026-08-12T09:30:00"
  }
]
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `integer` | 用户意见主键 `user_suggestion.id` |
| `user_name` | `string` | 提交用户名称 `user.name` |
| `avatar_url` | `string \| null` | 用户头像地址；未配置时为 `null` |
| `content` | `string` | 用户提交的意见正文 `user_suggestion.content` |
| `created_at` | `datetime` | 意见创建时间，使用 ISO 8601 日期时间格式 |

没有可见且待处理的意见时返回空数组 `[]`，状态码仍为 `200 OK`。

### 2.4 异常处理

| 场景 | 状态码 | 处理方式 |
| --- | --- | --- |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库查询失败 | `500 Internal Server Error` | 不转换成虚假空结果，也不暴露 SQL 和连接信息 |

---

## 3. `POST /suggestion/process` 处理用户意见

### 3.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/suggestion/process` |
| Nginx 开发路径 | `/dev/flame/admin/api/suggestion/process` |
| 请求体 | JSON，包含意见 ID 和处理动作 |
| 认证要求 | 有效的管理员 Bearer Token |

调用示例：

```http
POST /dev/flame/admin/api/suggestion/process
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "suggestion_id": 12,
  "action": "resolved"
}
```

请求字段：

| 字段 | 类型 | 是否必填 | 规则与说明 |
| --- | --- | --- | --- |
| `suggestion_id` | `integer` | 是 | 用户意见主键，必须大于 `0` |
| `action` | `string` | 是 | 只允许 `rejected` 或 `resolved` |

动作与持久化阶段映射：

| API 动作 | 管理端含义 | `user_suggestion.processing_stage` |
| --- | --- | --- |
| `rejected` | 拒绝该意见 | `rejected` |
| `resolved` | 意见已解决 | `optimized` |

### 3.2 处理规则

接口只处理 `user_suggestion.status = 1` 的可见意见，并遵循以下状态规则：

1. `pending` 可以转换为请求指定的最终处理阶段。
2. 重复提交与当前结论相同的动作时，按幂等成功处理，不重复写入。
3. 意见已进入另一个最终阶段时，拒绝覆盖并返回 `409 Conflict`。
4. 意见不存在或已经隐藏时，统一按不可处理返回 `404 Not Found`。

服务在单一数据库事务中先使用行锁读取意见，再校验和更新处理阶段，避免两个并发请求互相覆盖处理结论。

### 3.3 成功响应

状态码：`200 OK`。

```json
{
  "suggestion_id": 12,
  "processing_stage": "resolved"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `suggestion_id` | `integer` | 已处理的用户意见主键 |
| `processing_stage` | `string` | 管理端处理结果，值为 `rejected` 或 `resolved` |

重复提交同一处理动作时，响应格式和状态码保持不变。

### 3.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| `suggestion_id` 不大于 `0`、缺少字段或动作不在允许范围 | `422 Unprocessable Entity` | FastAPI 参数校验错误 |
| 意见不存在或 `status != 1` | `404 Not Found` | `意见不存在或已隐藏` |
| 意见已有不同的最终处理结论 | `409 Conflict` | `意见已有不同处理结论，不能重复处理` |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库处理失败 | `500 Internal Server Error` | 事务自动回滚，不暴露 SQL 和连接信息 |

---

## 4. 数据、事务与安全边界

数据事实来源：

- [用户意见表说明](../db/user-suggestion.md)
- [用户表说明](../db/user.md)

代码入口：

- `app/router/suggestion.py`
- `app/services/suggestions.py`
- `app/repositories/suggestions.py`
- `app/router/__init__.py`

列表用例在显式只读事务中执行一次联表查询，并同时筛选可见状态与待处理阶段。处理用例在单一写事务中锁定目标意见并更新 `processing_stage`，仓储不自行提交或关闭会话。

处理接口不会修改意见可见状态 `status` 或用户数据。列表接口仍不返回处理阶段；处理接口只返回本次动作对应的管理端阶段。

---

## 5. 验证方式

```bash
python -m unittest tests.test_suggestion -v
python -m unittest tests.test_suggestion_processing -v
```

测试覆盖意见正文、可见状态和待处理阶段筛选、用户联表、排序、管理员认证，以及处理动作校验、阶段映射、事务、行锁、幂等和冲突响应。

---

## 6. 已知限制

- 当前按需求拉取全部可见且待处理的意见，尚未分页；数据量增长后应增加分页参数。
- 列表接口当前不返回 `processing_stage`；需要在列表中展示阶段时应扩展其响应契约。
- 当前不提供隐藏、恢复、删除或重新打开意见的写接口。
