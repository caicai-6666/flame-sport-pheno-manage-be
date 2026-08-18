# 用户通知表：notification

## 表作用

`notification` 表用于记录需要发送给用户的 Markdown 工作通知及其钉钉投递状态。

管理端后端创建通知记录，客户端后端读取待发送记录、调用钉钉工作通知接口并更新投递状态。

---

## 字段说明

| 字段名 | 类型 | 是否必填 | 默认值 | 说明 |
| --- | --- | ---: | ---: | --- |
| `id` | `BIGINT UNSIGNED` | 是 | 自增 | 通知记录主键 ID |
| `task_id` | `BIGINT UNSIGNED` | 否 | `NULL` | 钉钉异步工作通知任务 ID |
| `user_id` | `VARCHAR(64)` | 是 | 无 | 接收用户 ID，关联 `user.id` |
| `message_title` | `VARCHAR(100)` | 是 | 无 | Markdown 工作通知标题 |
| `message_fields` | `JSON` | 是 | 无 | 按展示顺序保存的消息键值列表 |
| `notification_status` | `VARCHAR(32)` | 是 | `pending` | 通知状态，覆盖待发送至失败的完整投递阶段 |
| `notification_updated_at` | `DATETIME(6)` | 是 | `CURRENT_TIMESTAMP(6)` | 通知状态最后更新时间 |
| `created_at` | `DATETIME(6)` | 是 | `CURRENT_TIMESTAMP(6)` | 通知创建时间 |

---

## 字段设计说明

### task_id

客户端后端成功提交钉钉工作通知后写入的任务 ID，通知尚未提交时为空。

### user_id

接收通知的用户 ID，关联 `user.id`。该值用于指定钉钉工作通知的接收人。

### message_title

Markdown 工作通知的标题，对应钉钉消息中的 `markdown.title`。

### message_fields

消息正文的有序键值列表，客户端后端将其转换为 `markdown.text`。格式如下：

```json
[
  {
    "key": "审核结果",
    "value": "未通过"
  },
  {
    "key": "审核意见",
    "value": "凭证信息不清晰"
  }
]
```

### notification_status

通知投递状态允许以下取值：

| 字段值 | 说明 |
| --- | --- |
| `pending` | 待发送 |
| `processing` | 发送中 |
| `accepted` | 钉钉已受理 |
| `delivered` | 已送达但未读 |
| `read` | 已读 |
| `failed` | 发送失败 |

### notification_updated_at

每次修改 `notification_status` 时同步更新该字段。

---

## MySQL 建表语句

保留以下索引：

- `(notification_status, notification_updated_at, id)` 用于客户端后端读取通知任务。
- `user_id` 索引用于用户关联查询和外键检查。

```sql
CREATE TABLE notification (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '通知记录ID',
  task_id BIGINT UNSIGNED DEFAULT NULL COMMENT '钉钉异步工作通知任务ID',
  user_id VARCHAR(64)
    CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL
    COMMENT '接收通知的用户ID',
  message_title VARCHAR(100) NOT NULL COMMENT 'Markdown工作通知标题',
  message_fields JSON NOT NULL COMMENT '按展示顺序保存的消息键值列表',
  notification_status VARCHAR(32) NOT NULL DEFAULT 'pending' COMMENT '通知状态：pending待发送，processing发送中，accepted已受理，delivered已送达，read已读，failed失败',
  notification_updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '通知状态最后更新时间',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '通知创建时间',
  PRIMARY KEY (id),
  KEY idx_notification_consume
    (notification_status, notification_updated_at, id),
  KEY idx_notification_user_id (user_id),
  CONSTRAINT chk_notification_delivery_status
    CHECK (notification_status IN (
      'pending',
      'processing',
      'accepted',
      'delivered',
      'read',
      'failed'
    )),
  CONSTRAINT chk_notification_message_fields
    CHECK (JSON_TYPE(message_fields) = 'ARRAY'),
  CONSTRAINT fk_notification_user
    FOREIGN KEY (user_id) REFERENCES `user`(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户通知表';
```

---

## 表关系

```text
user 1 : N notification
```
