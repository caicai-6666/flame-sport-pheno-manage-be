# 用户基础信息 API

> **文档目的**
>
> 本文档定义管理端按用户 ID 批量获取名称、部门名称和头像地址的接口契约、查询边界与异常语义。

## 1. 功能目标

`user-info` 接口供赛季统计、排行榜等管理页面根据已有用户 ID 补充基础展示信息，不承担用户新增、修改或状态管理。

---

## 2. `GET /user/user-info` 批量获取用户信息

### 2.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/user/user-info` |
| Nginx 开发路径 | `/dev/flame/admin/api/user/user-info` |
| 认证要求 | 有效的管理员 Bearer Token |

该接口是无副作用的小批量查询，使用 `GET` 符合 HTTP 语义。调用示例：

```http
GET /dev/flame/admin/api/user/user-info?user_ids=user-1&user_ids=user-2
Authorization: Bearer <admin-token>
```

### 2.2 请求参数与查询口径

| 参数 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `user_ids` | `string[]` | 是 | `1～50` 项；每项去除首尾空白后长度为 `1～64` | 待查询的用户 ID，通过重复查询参数传递 |

查询规则：

- 重复 ID 只查询并返回一次，响应位置以首次出现的位置为准；
- 使用一次参数化批量 SQL 联动 `department` 表，避免 N+1 查询；
- 不存在的用户 ID 直接省略，不生成占位记录；
- 不根据用户或部门启停状态过滤，以支持历史业务数据展示。

### 2.3 成功响应

状态码：`200 OK`。

```json
[
  {
    "user_id": "user-1",
    "name": "张三",
    "department_name": "研发部",
    "avatar_url": "/avatar/user-1.jpg"
  },
  {
    "user_id": "user-2",
    "name": "李四",
    "department_name": "产品部",
    "avatar_url": null
  }
]
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `user_id` | `string` | 用户唯一标识 `user.id` |
| `name` | `string` | 用户展示名称 |
| `department_name` | `string` | 用户所属部门名称 |
| `avatar_url` | `string \| null` | 用户头像地址；未配置头像时为 `null` |

若所有 ID 都不存在，返回空数组 `[]` 和 `200 OK`。

### 2.4 异常处理

| 场景 | 状态码 | 处理方式 |
| --- | --- | --- |
| 缺少 `user_ids` | `422 Unprocessable Entity` | FastAPI 参数校验错误 |
| ID 为空或超过 64 个字符 | `422 Unprocessable Entity` | FastAPI 参数校验错误 |
| 一次传入超过 50 个 ID | `422 Unprocessable Entity` | 拒绝无界批量查询 |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库查询失败 | `500 Internal Server Error` | 不暴露 SQL 和连接信息 |

---

## 3. 数据、事务与依赖

数据事实来源：

- [用户表说明](../db/user.md)
- [部门表说明](../db/department.md)

代码入口：

- `app/router/user.py`
- `app/services/users.py`
- `app/repositories/users.py`

接口只读。应用服务先执行用户 ID 保序去重，再在显式事务中调用仓储完成读取。

---

## 4. 验证方式

```bash
python -m unittest tests.test_user_info -v
```

测试覆盖批量查询、部门映射、顺序保持、ID 去重、不存在 ID 省略、参数边界、认证和服务事务。

---

## 5. 已知限制

- 当前最多接收 50 个用户 ID；大量查询应新增接受 JSON 请求体的 `POST` 接口。
- 成功响应不额外返回“未找到的 ID”集合，调用方可自行比较请求与返回结果。
