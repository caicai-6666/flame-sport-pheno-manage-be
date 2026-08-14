# 赛季用户项目表：season_user_project

## 表作用

`season_user_project` 表用于记录用户在某个赛季中选择并锁定的运动项目。

每条有效锁定记录还保存该项目在本赛季的完成进度。进度由后续的定时初审任务依据用户填写的运动指标累积，取值始终在 `0` 到 `1` 之间。

当前平台中，用户在每个赛季需要选择指定数量的运动项目，并在达到当前赛季要求后选择统一的挑战等级。  
该表对应项目首页中的“选择项目 / 锁定项目”逻辑。

本表通过 `season_user_id` 关联 `season_user.id`。  
当用户开始锁定运动项目时，后端需要先创建或获取该用户在当前赛季的 `season_user` 记录，然后再写入本表。

需要注意：`season_user` 记录存在，不代表该用户已经正式完成赛季参与确认。  
只有当 `season_user.status` 达到当前赛季 `season.required_project_count` 要求的项目数量时，后续赛季结算才会将该用户纳入参与范围。  
当前原型要求锁定 3 个项目，即 `season.required_project_count = 3`；如果后续赛季强制选择 4 个项目，则可配置 `season.required_project_count = 4`。

---

## 字段说明

| 字段名         | 类型             | 是否必填 | 默认值 | 说明                                   |
| -------------- | ---------------- | -------: | -----: | -------------------------------------- |
| id             | BIGINT UNSIGNED  |       是 |   自增 | 赛季用户项目记录主键 ID                |
| season_user_id | BIGINT UNSIGNED  |       是 |     无 | 赛季用户记录 ID，关联 `season_user.id` |
| project_id     | BIGINT UNSIGNED  |       是 |     无 | 项目 ID，关联 `project.id`             |
| completion_progress | DECIMAL(5,4) | 是 | 0.0000 | 本赛季该项目完成进度，范围 `0`～`1` |
| status         | TINYINT UNSIGNED |       是 |      1 | 状态：`1` 已锁定，`0` 无效/取消        |

---

## 字段设计说明

### id

赛季用户项目记录的唯一标识。

使用 `BIGINT UNSIGNED AUTO_INCREMENT` 作为主键。

---

### season_user_id

赛季用户记录 ID。

该字段关联 `season_user.id`，用于标识“哪个用户在当前赛季中的项目选择”。

`season_user` 已经唯一表示：

```text
某个用户 + 某个赛季
```

因此本表不再重复存储 `season_id` 和 `user_id`，避免数据冗余。

当用户锁定项目时，如果当前赛季下还没有对应的 `season_user` 记录，后端应先创建该记录，再写入本表。

---

### project_id

项目 ID。

该字段关联 `project.id`，表示用户在当前赛季中选择并锁定的运动项目。

示例：

```text
日常步数
跑步/快走
健身打卡
公司运动
户外登山
减重挑战
```

---

### status

项目选择状态。

取值说明：

```text
1 = 已锁定
0 = 无效/取消
```

当前用户侧规则是：项目一旦锁定，本赛季不可更改。

但保留 `status` 字段是为了支持后台在特殊情况下进行纠错或作废，例如：

- 用户误选项目
- 后台人工处理异常数据
- 用户赛季参与资格被作废
- 需要保留历史记录但不再视为有效选择

---

### completion_progress

本赛季该锁定项目的完成进度，范围为：

```text
0.0000 = 尚未完成任何有效进度
1.0000 = 已完成该项目本赛季目标
```

字段使用 `DECIMAL(5,4)` 而不是浮点类型，避免多次累计时产生二进制浮点精度误差。数据库通过 `CHECK` 约束保证值不会小于 `0` 或大于 `1`。

用户锁定项目时默认写入 `0.0000`。定时初审任务仅处理已超过最小等待时间仍为待审的凭证；初审通过后，在同一事务中更新凭证审核状态和本字段。管理员终审拒绝凭证时，会撤销该凭证的实际贡献，并使用同项目其他尚未完全分配进度的有效凭证回补，随后在同一事务中更新本字段。

---

## MySQL 建表语句

```sql
CREATE TABLE season_user_project (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '赛季用户项目记录ID',
  season_user_id BIGINT UNSIGNED NOT NULL COMMENT '赛季用户记录ID',
  project_id BIGINT UNSIGNED NOT NULL COMMENT '项目ID',
  completion_progress DECIMAL(5,4) NOT NULL DEFAULT 0.0000 COMMENT '本赛季项目完成进度：0未完成，1已完成',
  status TINYINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '状态：1已锁定，0无效/取消',
  PRIMARY KEY (id),
  UNIQUE KEY uk_season_user_project (season_user_id, project_id),
  KEY idx_season_user_project_season_user_id (season_user_id),
  KEY idx_season_user_project_project_id (project_id),
  KEY idx_season_user_project_status (status),
  CONSTRAINT chk_season_user_project_completion_progress
    CHECK (completion_progress >= 0 AND completion_progress <= 1),
  CONSTRAINT fk_season_user_project_season_user
    FOREIGN KEY (season_user_id) REFERENCES season_user(id),
  CONSTRAINT fk_season_user_project_project
    FOREIGN KEY (project_id) REFERENCES project(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='赛季用户项目表';
```

## 现有数据库迁移 SQL

```sql
ALTER TABLE season_user_project
  ADD COLUMN completion_progress DECIMAL(5,4) NOT NULL DEFAULT 0.0000
    COMMENT '本赛季项目完成进度：0未完成，1已完成' AFTER project_id,
  ADD CONSTRAINT chk_season_user_project_completion_progress
    CHECK (completion_progress >= 0 AND completion_progress <= 1);
```
