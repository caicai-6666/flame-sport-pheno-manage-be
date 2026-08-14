# 赛季用户表：season_user

## 表作用

`season_user` 表用于记录用户在某个赛季中的项目选择进度、挑战等级选择结果，以及赛季结束后的最终积分结算结果。

当前平台中，用户在每个赛季中需要完成以下流程：
```text
进入赛季
选择当前赛季要求数量的运动项目
选择一个挑战等级
赛季期间上传运动凭证
月末由审核人员统一审核
根据最终完成情况发放赛季积分
```
当用户开始锁定运动项目时，后端即可创建该用户在当前赛季的 `season_user` 记录。  
该表中的 `status` 字段用于记录用户当前赛季已锁定的项目数量。

该表不记录每条凭证的积分变化，也不做实时积分账户。  
本平台当前积分发放逻辑是：赛季结束后统一审核、统一结算。

---

## 字段说明

| 字段名       | 类型             | 是否必填 | 默认值 | 说明                                              |
| ------------ | ---------------- | -------: | -----: | ------------------------------------------------- |
| id           | BIGINT UNSIGNED  |       是 |   自增 | 赛季用户记录主键 ID                               |
| season_id    | BIGINT UNSIGNED  |       是 |     无 | 赛季 ID，关联 `season.id`                         |
| user_id      | VARCHAR(64)      |       是 |     无 | 用户 ID，关联 `user.id`                           |
| level_id     | BIGINT UNSIGNED  |       否 |   NULL | 用户本赛季选择的项目等级，关联 `project_level.id` |
| participated_at | DATETIME      |       否 |   NULL | 首次成功锁定挑战等级的正式报名时间 |
| final_points | INT UNSIGNED     |       否 |   NULL | 用户本赛季最终获得的积分                          |
| status       | TINYINT UNSIGNED |       是 |      0 | 已锁定项目数量，用于判断用户是否满足当前赛季参与要求 |

---

## 字段设计说明

### id

赛季用户记录的唯一标识。

使用 `BIGINT UNSIGNED AUTO_INCREMENT` 作为主键。

---

### season_id

赛季 ID。

用于标识该用户参与的是哪个赛季。  
该字段关联 `season.id`。

例如：
```text
2026年7月赛季
2026年8月赛季
```
一个赛季可以有多个用户参与。

---

### user_id

用户 ID。

用于标识参与赛季的具体用户。  
该字段关联 `user.id`。

一个用户可以参与多个赛季。

---

### level_id

用户本赛季选择的项目等级。

该字段关联 `project_level.id`。

当前原型流程中，用户先选择当前赛季要求数量的运动项目，然后选择一个挑战等级。  
该挑战等级作用于用户本赛季选择的全部项目。

该字段允许为空。

原因是用户可能已经进入赛季，但尚未完成：
```text
选择当前赛季要求数量的项目
选择挑战等级
```
当用户完成挑战等级选择后，再写入该字段。

---

### participated_at

用户首次成功锁定挑战等级时写入的正式报名时间。该字段与 `level_id` 在同一事务中更新，因此：

```text
level_id IS NULL        -> participated_at 必须为 NULL
level_id IS NOT NULL    -> 新产生的记录 participated_at 必须有值
```

历史数据迁移后无法准确还原原始报名时间时保留 `NULL`，不使用迁移执行时间伪造数据。

---

### final_points

用户本赛季最终获得的积分。

该字段用于记录月末审核和结算后的最终积分结果。

当前平台不是按照“每上传一条凭证就发放一点积分”的方式运行。管理员在赛季期间持续终审用户凭证，赛季结束后再根据终审结果和用户是否达成目标统一发放积分。

该字段允许为空。

语义如下：
```text
NULL = 尚未结算
0    = 已结算，但未获得积分
> 0  = 已结算，并获得对应积分
```
这样可以明确区分“还没有结算”和“已经结算但没有得分”。

---

### status

用户当前赛季已锁定项目数量。

取值说明：

```text
0 = 未锁定项目
1 = 已锁定 1 个项目
2 = 已锁定 2 个项目
3 = 已锁定 3 个项目
4 = 已锁定 4 个项目
...
```

当用户每锁定一个运动项目时，后端应将该字段加 1。  
当该字段达到当前赛季 `season.required_project_count` 要求的项目数量时，后续赛季结算才会将该用户视为有效赛季参与用户。

需要注意：该字段不固定限制为 `3`。  
当前原型要求选择 3 个项目，即 `season.required_project_count = 3`。  
如果后续赛季强制选择 4 个项目，则可配置 `season.required_project_count = 4`。
如果后台作废某个已锁定项目，应同步调整该字段，保证它和 `season_user_project` 中有效项目数量一致。

---

## MySQL 建表语句
```sql
CREATE TABLE season_user (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '赛季用户记录ID',
  season_id BIGINT UNSIGNED NOT NULL COMMENT '赛季ID',
  user_id VARCHAR(64) NOT NULL COMMENT '用户ID',
  level_id BIGINT UNSIGNED DEFAULT NULL COMMENT '项目等级ID',
  participated_at DATETIME DEFAULT NULL COMMENT '正式报名时间（首次锁定挑战等级时写入）',
  final_points INT UNSIGNED DEFAULT NULL COMMENT '赛季最终获得积分，NULL表示尚未结算',
  status TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '已锁定项目数量',
  PRIMARY KEY (id),
  UNIQUE KEY uk_season_user (season_id, user_id),
  KEY idx_season_user_season_id (season_id),
  KEY idx_season_user_user_id (user_id),
  KEY idx_season_user_level_id (level_id),
  KEY idx_season_user_participated_at (participated_at),
  KEY idx_season_user_status (status),
  CONSTRAINT fk_season_user_season
    FOREIGN KEY (season_id) REFERENCES season(id),
  CONSTRAINT fk_season_user_user
    FOREIGN KEY (user_id) REFERENCES `user`(id),
  CONSTRAINT fk_season_user_level
    FOREIGN KEY (level_id) REFERENCES project_level(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='赛季用户表';
```
