# 排行榜快照表：leaderboard_snapshot

## 表作用
`leaderboard_snapshot` 表用于存储当前赛季的排行榜快照数据。
当前平台中，排行榜按照用户在当前赛季的打卡次数进行排名。  
排行榜数据由定时任务每天固定时间统计并写入本表，前端排行榜页面直接读取本表，避免每次访问时实时统计凭证记录。

当前设计中，本表只保存最新排行榜快照，不保留每日历史快照。  
定时任务每次执行时会覆盖更新当前赛季的排行榜数据。

排行榜计算时间不再落库。后端进程内维护最近一次排行榜计算时间，用于接口返回或运行状态展示。
如果服务重启，该时间会重新初始化，并在下一次排行榜计算任务完成后更新。

---

## 字段说明

| 字段名         | 类型            | 是否必填 |            默认值 | 说明                                   |
| -------------- | --------------- | -------: | ----------------: | -------------------------------------- |
| id             | BIGINT UNSIGNED |       是 |              自增 | 排行榜快照记录主键 ID                  |
| season_user_id | BIGINT UNSIGNED |       是 |                无 | 赛季用户记录 ID，关联 `season_user.id` |
| checkin_count  | INT UNSIGNED    |       是 |                 0 | 当前赛季累计打卡次数                   |

---

## 字段设计说明

### id

排行榜快照记录的唯一标识。

使用 `BIGINT UNSIGNED AUTO_INCREMENT` 作为主键。

---

### season_user_id

赛季用户记录 ID。

该字段关联 `season_user.id`。
`season_user` 已经唯一表示：

```text
某个用户 + 某个赛季
```

因此本表不再重复存储 `season_id` 和 `user_id`，避免数据冗余。

通过 `season_user_id` 可以间接获取：

```text
season_id
user_id
用户名称
用户头像
用户部门
```

---

### checkin_count

当前赛季累计打卡次数。

该字段是排行榜排序的核心指标。

当前计算口径为：

```text
当前 season_user_id 下，status = 1 且已初审通过的 proof_record 数量
```

统计的审核状态为 `preliminary_approved`、`approved` 和 `rejected`。其中后两者表示赛后终审结果；它们都曾初审通过，因此不回溯改变赛季内排行榜。`pending` 和 `preliminary_rejected` 不计入。

---

## 排名与计算时间

### 排名

本表不保存 `rank_no`。

接口只返回快照数据，不在后端计算排名。前端可以基于 `checkin_count` 自行决定展示排序并计算排名。

### 计算时间

本表不保存 `calculated_at`。

后端进程内维护最近一次排行榜计算时间，例如：

```text
LeaderboardRuntime.calculated_at
```

前端如果需要展示“排行榜更新时间”，应由排行榜接口从进程内运行时状态返回。

---

## MySQL 建表语句

```sql
CREATE TABLE leaderboard_snapshot (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '排行榜快照记录ID',
  season_user_id BIGINT UNSIGNED NOT NULL COMMENT '赛季用户记录ID',
  checkin_count INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '当前赛季累计打卡次数',
  PRIMARY KEY (id),
  UNIQUE KEY uk_leaderboard_snapshot_season_user (season_user_id),
  KEY idx_leaderboard_snapshot_checkin_count (checkin_count),
  CONSTRAINT fk_leaderboard_snapshot_season_user
    FOREIGN KEY (season_user_id) REFERENCES season_user(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='排行榜快照表';
```

---

## 旧表结构调整 SQL

如果数据库中已经存在旧版字段，可以执行：

```sql
ALTER TABLE leaderboard_snapshot
  DROP INDEX idx_leaderboard_snapshot_rank_no,
  DROP INDEX idx_leaderboard_snapshot_calculated_at,
  DROP COLUMN rank_no,
  DROP COLUMN calculated_at;
```
