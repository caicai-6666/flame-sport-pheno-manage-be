# 用户建议表（user_suggestion）

## 表介绍

`user_suggestion` 用于保存用户提交的建议或反馈。每条建议必须归属于一个已存在的用户；后台可通过可见标记控制其是否展示。用户建议不配置数据库级联删除，用户删除时的保留、匿名化或清理策略应由业务层明确处理。

## 字段介绍

| 字段名 | 类型 | 约束/默认值 | 中文说明 |
| --- | --- | --- | --- |
| `id` | `BIGINT UNSIGNED` | 主键、非空、自增 | 用户建议唯一标识。 |
| `user_id` | `VARCHAR(64)` | 非空，使用 `utf8mb4_0900_ai_ci`，外键关联 `user.id` | 提交该建议的用户 ID。 |
| `content` | `TEXT` | 非空 | 用户填写的建议内容。 |
| `is_visible` | `TINYINT UNSIGNED` | 非空，默认 `1` | 是否可见；`1` 表示可见，`0` 表示隐藏。 |
| `created_at` | `DATETIME` | 非空，默认当前时间 | 用户提交建议的时间。 |

## 建表语句

```sql
CREATE TABLE `user_suggestion` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '用户建议唯一标识',
    `user_id` VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL COMMENT '提交建议的用户ID，关联user表主键',
    `content` TEXT NOT NULL COMMENT '用户填写的建议内容',
    `is_visible` TINYINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '是否可见：1可见，0隐藏',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '用户提交建议的时间',

    PRIMARY KEY (`id`),
    CONSTRAINT `fk_user_suggestion_user`
        FOREIGN KEY (`user_id`) REFERENCES `user` (`id`),
    INDEX `idx_user_suggestion_user_created` (`user_id`, `created_at`),
    INDEX `idx_user_suggestion_visible_created` (`is_visible`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户建议表';
```

> `user_id` 必须与 `user.id` 保持完全一致。本项目已部署的 `user.id` 为 `VARCHAR(64)`、`utf8mb4_0900_ai_ci`，因此本表显式使用相同字符集和排序规则以保证外键可以创建。
