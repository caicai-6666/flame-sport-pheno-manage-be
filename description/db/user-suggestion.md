# 用户建议表（user_suggestion）

## 表介绍

`user_suggestion` 用于保存用户提交的建议或反馈。每条建议必须归属于一个已存在的用户；后台可以控制记录是否可见，并记录意见当前所处的处理阶段。用户建议不配置数据库级联删除，用户删除时的保留、匿名化或清理策略应由业务层明确处理。

## 字段介绍

| 字段名 | 类型 | 约束/默认值 | 中文说明 |
| --- | --- | --- | --- |
| `id` | `BIGINT UNSIGNED` | 主键、非空、自增 | 用户建议唯一标识。 |
| `user_id` | `VARCHAR(64)` | 非空，使用 `utf8mb4_0900_ai_ci`，外键关联 `user.id` | 提交该建议的用户 ID。 |
| `content` | `TEXT` | 非空 | 用户填写的建议内容。 |
| `status` | `TINYINT UNSIGNED` | 非空，默认 `1` | 记录状态；`1` 表示可见，`0` 表示隐藏。 |
| `processing_stage` | `VARCHAR(32)` | 非空，默认 `pending` | 处理阶段：`pending` 待处理、`rejected` 拒绝、`optimized` 已优化。 |
| `created_at` | `DATETIME` | 非空，默认当前时间 | 用户提交建议的时间。 |

### 处理阶段

`processing_stage` 只表达管理端对意见的处理结果，不替代 `status` 的可见性语义：

| 字段值 | 中文阶段 | 说明 |
| --- | --- | --- |
| `pending` | 待处理 | 意见尚未形成最终处理结论，也是新增记录的默认阶段。 |
| `rejected` | 拒绝 | 意见已经评估，但决定不采纳。 |
| `optimized` | 已优化 | 意见已经采纳并完成相应优化。 |

处理阶段必须是以上三个值之一。隐藏记录仍然保留原处理阶段，重新设为可见时无需重置处理进度。

## 建表语句

```sql
CREATE TABLE `user_suggestion` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '用户建议唯一标识',
    `user_id` VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL COMMENT '提交建议的用户ID，关联user表主键',
    `content` TEXT NOT NULL COMMENT '用户填写的建议内容',
    `status` TINYINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '记录状态：1可见，0隐藏',
    `processing_stage` VARCHAR(32) NOT NULL DEFAULT 'pending' COMMENT '处理阶段：pending待处理，rejected拒绝，optimized已优化',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '用户提交建议的时间',

    PRIMARY KEY (`id`),
    CONSTRAINT `fk_user_suggestion_user`
        FOREIGN KEY (`user_id`) REFERENCES `user` (`id`),
    INDEX `idx_user_suggestion_user_created` (`user_id`, `created_at`),
    INDEX `idx_user_suggestion_status_created` (`status`, `created_at`),
    INDEX `idx_user_suggestion_processing_stage_created` (`processing_stage`, `created_at`),
    CONSTRAINT `chk_user_suggestion_processing_stage`
        CHECK (`processing_stage` IN ('pending', 'rejected', 'optimized'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户建议表';
```

> `user_id` 必须与 `user.id` 保持完全一致。本项目已部署的 `user.id` 为 `VARCHAR(64)`、`utf8mb4_0900_ai_ci`，因此本表显式使用相同字符集和排序规则以保证外键可以创建。

---

## 现有数据库迁移 SQL

可直接应用于仍使用旧版 `is_visible` 字段的数据库迁移脚本位于：

```text
script/migrate-user-suggestion-status-and-processing-stage.sql
```

脚本包含执行前检查、正式结构迁移和执行后验证。生产执行前必须先选定目标数据库并完成备份；该脚本不可对已经迁移的表重复执行。

```sql
ALTER TABLE `user_suggestion`
    CHANGE COLUMN `is_visible` `status` TINYINT UNSIGNED NOT NULL DEFAULT 1
        COMMENT '记录状态：1可见，0隐藏',
    ADD COLUMN `processing_stage` VARCHAR(32) NOT NULL DEFAULT 'pending'
        COMMENT '处理阶段：pending待处理，rejected拒绝，optimized已优化'
        AFTER `status`,
    DROP INDEX `idx_user_suggestion_visible_created`,
    ADD INDEX `idx_user_suggestion_status_created` (`status`, `created_at`),
    ADD INDEX `idx_user_suggestion_processing_stage_created`
        (`processing_stage`, `created_at`),
    ADD CONSTRAINT `chk_user_suggestion_processing_stage`
        CHECK (`processing_stage` IN ('pending', 'rejected', 'optimized'));
```

迁移时，原 `is_visible` 数值原样保留到 `status`；现有记录通过非空默认值统一初始化为 `pending`，不会删除意见或改变其可见性。
