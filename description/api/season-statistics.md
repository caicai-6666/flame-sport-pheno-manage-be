# 赛季统计 API

> **文档目的**
>
> 本文档定义管理端赛季统计接口的路由边界，以及当前赛季基础信息和指定用户项目进度接口的完整契约。

## 1. 功能目标与通用规则

赛季统计子路由集中承载按赛季查询数据库聚合结果的只读接口。FastAPI 前缀为 `/flame/admin/api/season-statistics`，Nginx 开发前缀为 `/dev/flame/admin/api/season-statistics`。

通用规则：

- 接口不修改数据库，也不触发隐式结算；
- 所有接口均要求有效的管理员 Bearer Token；
- “当前赛季”指 `season.status = 1`，不根据当前日期推导，`status = 2` 的结算中赛季不在查询范围内；
- 聚合查询必须明确过滤、去重、状态、时间和空数据口径。

---

## 2. `GET /season-statistics/current` 获取当前赛季

### 2.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/season-statistics/current` |
| Nginx 开发路径 | `/dev/flame/admin/api/season-statistics/current` |
| 请求参数 | 无 |
| 认证要求 | 有效的管理员 Bearer Token |

### 2.2 查询口径

接口查询 `season.status = 1` 的赛季。正式参赛人员必须同时满足：

```text
season_user.season_id = season.id
season_user.status >= season.required_project_count
season_user.level_id IS NOT NULL
```

`participated_at` 不作为排除条件。接口通过 `season_user.level_id` 关联等级名称，不按等级当前启停状态过滤；关联等级缺失时防御性排除该参赛记录。

### 2.3 成功响应

状态码：`200 OK`。

```json
{
  "id": 7,
  "name": "2026年8月赛季",
  "start_date": "2026-08-01",
  "end_date": "2026-08-31",
  "required_project_count": 3,
  "status": 1,
  "participants": [
    {
      "season_user_id": 101,
      "user_id": "<user-id>",
      "level_id": 2,
      "level_name": "白银"
    }
  ]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `integer` | 当前赛季 ID |
| `name` | `string` | 赛季名称 |
| `start_date` | `date` | 赛季开始日期 |
| `end_date` | `date` | 赛季结束日期 |
| `required_project_count` | `integer` | 正式参赛要求的项目数量 |
| `status` | `integer` | 固定为 `1` |
| `participants` | `array` | 正式参赛人员；无人时为空数组 |
| `participants[].season_user_id` | `integer` | 参赛记录主键 |
| `participants[].user_id` | `string` | 用户 ID |
| `participants[].level_id` | `integer` | 已锁定等级 ID |
| `participants[].level_name` | `string` | 已锁定等级名称 |

### 2.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| 当前没有激活赛季 | `404 Not Found` | `当前没有激活的赛季` |
| 同时存在多个激活赛季 | `409 Conflict` | `存在多个激活赛季，无法确定当前赛季` |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库查询失败 | `500 Internal Server Error` | 不暴露 SQL 和连接信息 |

---

## 3. `GET /season-statistics/project-participants` 查询项目参赛进度

### 3.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/season-statistics/project-participants` |
| Nginx 开发路径 | `/dev/flame/admin/api/season-statistics/project-participants` |
| 认证要求 | 有效的管理员 Bearer Token |

请求示例：

```http
GET /dev/flame/admin/api/season-statistics/project-participants?season_user_id=101&project_id=5
Authorization: Bearer <admin-token>
```

### 3.2 请求参数与查询口径

| 参数 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `season_user_id` | `integer` | 是 | 大于 `0` | 赛季用户记录 ID |
| `project_id` | `integer` | 是 | 大于 `0` | 运动项目 ID |

返回记录必须同时满足：

```text
season_user_project.season_user_id = 请求 season_user_id
season_user_project.project_id = 请求 project_id
season_user_project.status = 1
season.status = 1
season_user.status >= season.required_project_count
season_user.level_id IS NOT NULL
```

数据库以 `(season_user_id, project_id)` 唯一约束限制同一组合，因此当前响应最多包含一项。

### 3.3 成功响应

状态码：`200 OK`。

```json
[
  {
    "user_id": "<user-id>",
    "completion_progress": 0.75
  }
]
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `user_id` | `string` | 对应 `season_user.user_id` |
| `completion_progress` | `number` | 完成进度，范围为 `0～1` |

没有匹配记录时返回空数组 `[]` 和 `200 OK`，不区分历史赛季、报名未完成、项目已作废或记录不存在。

### 3.4 异常处理

| 场景 | 状态码 | 处理方式 |
| --- | --- | --- |
| 任一参数缺失、非整数或不大于 `0` | `422 Unprocessable Entity` | FastAPI 参数校验错误 |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库查询失败 | `500 Internal Server Error` | 不转换成虚假空结果，也不暴露内部信息 |

---

## 4. 数据、事务与安全边界

数据事实来源：

- [赛季表说明](../db/season.md)
- [赛季用户表说明](../db/season-user.md)
- [赛季用户项目表说明](../db/season-user-project.md)
- [挑战等级表说明](../db/project-level.md)

代码入口：

- `app/router/season_statistics.py`
- `app/services/season_statistics.py`
- `app/repositories/season_statistics.py`

两个用例均由应用服务管理只读事务，使用参数化 SQL，且不修改赛季、报名或项目进度。当前赛季查询使用一次联表查询避免 N+1。

---

## 5. 验证方式

```bash
python -m unittest tests.test_current_season_statistics tests.test_season_statistics_router -v
```

测试覆盖成功响应、正式参赛口径、等级名称、空人员、当前赛季不存在或冲突、项目进度、空结果、参数校验、认证和事务边界。

---

## 6. 已知限制

- 当前没有为统计查询增加缓存、物化结果或专用索引，后续优化应依据性能数据。
- `project-participants` 当前按唯一组合查询，最多返回一人；查询某项目全部人员应新增独立分页接口。
