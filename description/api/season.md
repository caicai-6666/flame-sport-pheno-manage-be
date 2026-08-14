# 赛季管理 API 路由

> **文档目的**
>
> 本文档定义管理端赛季路由的认证边界、全部赛季列表接口和后续管理接口归属，避免基础查询、配置与聚合统计能力混杂。

## 1. 路由定义

赛季管理路由使用以下前缀：

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径前缀 | `/flame/admin/api/season` |
| Nginx 开发路径前缀 | `/dev/flame/admin/api/season` |
| OpenAPI 标签 | `season` |
| 认证要求 | 有效的管理员 Bearer Token |

该路由已经注册到主应用的统一受保护路由下。管理员 Token 缺失、无效或过期时，请求会由公共依赖重定向至管理端登录接口。

`/season` 根路径本身不提供占位响应，具体能力通过明确的子路径暴露。

---

## 2. `GET /season/list` 获取全部赛季列表

### 2.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/season/list` |
| Nginx 开发路径 | `/dev/flame/admin/api/season/list` |
| 请求参数 | 无 |
| 认证要求 | 有效的管理员 Bearer Token |

### 2.2 查询口径

接口读取 `season` 表中的全部记录，不按赛季状态过滤。响应顺序为：

```text
start_date DESC, end_date DESC, id DESC
```

因此最近开始的赛季优先展示；日期相同时使用结束日期和主键保证稳定顺序。接口返回状态原值及对应中文含义，不返回要求项目数量或参赛统计。

### 2.3 成功响应

状态码：`200 OK`。

```json
[
  {
    "id": 2,
    "name": "2026年9月赛季",
    "start_date": "2026-09-01",
    "end_date": "2026-09-30",
    "status": 2,
    "status_name": "结算中"
  }
]
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `integer` | 赛季主键 ID |
| `name` | `string` | 赛季名称 |
| `start_date` | `date` | 赛季开始日期，格式为 `YYYY-MM-DD` |
| `end_date` | `date` | 赛季结束日期，格式为 `YYYY-MM-DD` |
| `status` | `integer` | 赛季状态值：`0` 未开始、`1` 进行中、`2` 结算中、`3` 已结束 |
| `status_name` | `string` | `status` 对应的中文含义 |

数据库没有赛季时返回空数组 `[]` 和 `200 OK`。

### 2.4 异常处理

| 场景 | 状态码 | 处理方式 |
| --- | --- | --- |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库存在 `0～3` 之外的赛季状态 | `500 Internal Server Error` | `赛季状态数据异常` |
| 数据库查询失败 | `500 Internal Server Error` | 不返回虚假空列表，也不暴露 SQL 或连接信息 |

---

## 3. `POST /season/create` 新增赛季

### 3.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/season/create` |
| Nginx 开发路径 | `/dev/flame/admin/api/season/create` |
| Content-Type | `application/json` |
| 认证要求 | 有效的管理员 Bearer Token |

请求体示例：

```json
{
  "name": "2026年9月赛季",
  "start_date": "2026-09-01",
  "end_date": "2026-09-30",
  "required_project_count": 3
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `name` | `string` | 是 | 去除首尾空白后长度为 `1～64` | 赛季名称 |
| `start_date` | `date` | 是 | `YYYY-MM-DD` | 赛季开始日期 |
| `end_date` | `date` | 是 | `YYYY-MM-DD` | 赛季结束日期 |
| `required_project_count` | `integer` | 是 | `1～255`，且不得超过当前可见项目数 | 用户正式参赛必须选择的项目数量 |

### 3.2 创建规则

新增赛季必须同时满足：

1. `start_date` 严格晚于所有已有赛季的最大 `end_date`。
2. 从 `start_date` 到 `end_date` 的闭区间不少于一个完整日历月。
3. 日期必须是合法公历日期；格式或日期不存在时由请求模型拒绝。
4. `required_project_count` 必须是 `1～255` 的整数。
5. `required_project_count` 不得超过创建时 `project.status = 1` 的可见项目数量。

“一个完整日历月”按包含首尾日期计算：

| 开始日期 | 允许的最早结束日期 |
| --- | --- |
| `2026-08-01` | `2026-08-31` |
| `2026-08-15` | `2026-09-14` |
| `2026-12-15` | `2027-01-14` |

创建后的默认字段为：

```text
status = 0
status_name = 未开始
```

`required_project_count` 使用请求值写入，不再采用固定项目数量。接口只按创建时的可见项目集合校验容量；赛季创建后项目可见性如何变更，仍应由后续项目管理能力另行约束。

### 3.3 成功响应

状态码：`201 Created`。

```json
{
  "id": 8,
  "name": "2026年9月赛季",
  "start_date": "2026-09-01",
  "end_date": "2026-09-30",
  "required_project_count": 3,
  "status": 0,
  "status_name": "未开始"
}
```

`id` 为数据库生成的赛季主键；`required_project_count` 是本次实际写入的新赛季要求项目数量。列表接口保持现有轻量结构，不额外返回该字段。

### 3.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| 名称为空、超过 `64` 字符、日期格式非法或缺少必填字段 | `422 Unprocessable Content` | FastAPI 请求校验错误 |
| `required_project_count` 小于 `1` 或大于 `255` | `422 Unprocessable Content` | FastAPI 请求校验错误 |
| 赛季周期不足一个完整日历月 | `422 Unprocessable Content` | `赛季周期不能少于一个完整日历月` |
| 开始日期未晚于已有赛季最晚结束日期 | `409 Conflict` | `赛季开始日期必须晚于已有赛季的最晚结束日期` |
| 要求项目个数超过当前可见项目个数 | `409 Conflict` | `要求项目个数不能超过当前可见项目个数` |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库写入失败 | `500 Internal Server Error` | 回滚事务，不暴露 SQL 或连接信息 |

---

## 4. 职责边界

`season` 路由用于承载会改变赛季业务状态或配置的管理操作，例如后续确认的赛季创建、编辑、状态流转与结算触发接口。

以下能力不放入该路由：

- 当前赛季与项目参赛进度等只读聚合查询，继续归属 [`season-statistics`](season-statistics.md)；
- 赛季生命周期规则，统一以[赛季生命周期](../domain/season-lifecycle.md)为准；
- 复杂事务与数据访问逻辑，应分别下沉到 `app/services/` 和 `app/repositories/`，不能写在路由层。

---

## 5. 安全与事务要求

列表接口由服务层管理只读事务。创建接口先完成自身日期范围校验，再在单一写事务中锁定结束日期最晚的赛季，并共享锁定当前可见项目集合；只有历史边界和项目容量均通过校验后才插入新记录。任一业务冲突或数据库异常都会使事务整体回滚。

后续在该命名空间增加其他写接口时必须满足：

- 复用统一管理员认证依赖，不允许匿名访问；
- 输入在 HTTP 边界完成类型与范围校验；
- 状态流转遵循 `0 未开始 → 1 进行中 → 2 结算中 → 3 已结束` 的已确认语义；
- 涉及结算、积分和多表状态变化时，由服务层管理单一明确事务；
- 重复请求和并发状态变更必须具备幂等或冲突检测，不能静默覆盖。

---

## 6. 数据与实现位置

- [赛季表说明](../db/season.md)
- [项目表说明](../db/project.md)
- [赛季生命周期](../domain/season-lifecycle.md)
- [赛季统计 API](season-statistics.md)
- `app/router/season.py`
- `app/services/seasons.py`
- `app/repositories/seasons.py`
- `app/router/__init__.py`

---

## 7. 验证方式

```bash
python -m unittest tests.test_season_router tests.test_season_list tests.test_season_creation -v
```

测试覆盖路由命名空间、字段序列化、四态中文映射、全部状态查询、要求项目数量、完整日历月边界、历史结束边界、可见项目容量、事务锁、错误响应和统一管理员认证。

---

## 8. 已知限制

- 当前列表尚无状态筛选或分页。
- 当前尚无赛季编辑和状态流转接口，其请求字段、异常协议与幂等规则仍需结合后续前端需求定义。
