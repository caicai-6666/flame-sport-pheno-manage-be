# 赛季表：season

## 表作用

`season` 表用于存储企业运动平台中的赛季信息。

当前平台中，一个赛季对应一个自然月或一个月度活动周期。  
用户的项目选择、挑战等级、凭证上传、积分累计、排行榜统计和商品兑换，都应归属于某个具体赛季。

当前设计中，同一时间只允许存在一个进行中的赛季。

---

## 字段说明

| 字段名     | 类型             | 是否必填 | 默认值 | 说明                                               |
| ---------- | ---------------- | -------: | -----: | -------------------------------------------------- |
| id         | BIGINT UNSIGNED  |       是 |   自增 | 赛季主键 ID                                        |
| name       | VARCHAR(64)      |       是 |     无 | 赛季名称，例如“2026年7月赛季”                      |
| start_date | DATE             |       是 |     无 | 赛季开始日期                                       |
| end_date   | DATE             |       是 |     无 | 赛季结束日期                                       |
| required_project_count | TINYINT UNSIGNED |       是 |      3 | 当前赛季要求用户固定选择的项目数量                 |
| status     | TINYINT UNSIGNED |       是 |      0 | 赛季状态：`0` 未开始，`1` 进行中，`2` 已结束       |

---

## 字段设计说明

### id

赛季的唯一标识。

使用 `BIGINT UNSIGNED AUTO_INCREMENT` 作为主键。  
平台内其他业务表会通过 `season_id` 关联到该字段。

例如：
```text
season_user.season_id
user_season_project.season_id
proof_record.season_id
point_ledger.season_id
exchange_order.season_id
```

其中 `leaderboard_snapshot` 通过 `season_user_id` 间接关联赛季，不直接保存 `season_id`。

---

### name

赛季名称。

用于前端展示和后台管理识别。

示例：
```text
2026年7月赛季
2026年8月赛季
2026年9月赛季
```
当前业务倾向于一个赛季对应一个月，因此赛季名称建议包含年份和月份，避免跨年后产生歧义。

---

### start_date

赛季开始日期。

用于判断当前日期是否处于赛季周期内，也用于限制用户参与、上传凭证和统计数据。

示例：
```text
2026-07-01
```
---

### end_date

赛季结束日期。

用于判断赛季结束时间，也用于停止普通用户继续上传凭证、冻结排行榜或进入后续结算流程。

示例：
```text
2026-07-31
```
---

### required_project_count

当前赛季要求用户固定选择的项目数量。

例如：

```text
required_project_count = 3 表示该赛季强制选择 3 个运动项目
required_project_count = 4 表示该赛季强制选择 4 个运动项目
```

该字段用于控制用户赛季参与流程：

- 用户锁定项目数量达到 `required_project_count` 后，才满足当前赛季参与条件
- 后续赛季结算时，只统计 `season_user.status >= season.required_project_count` 的用户
- 前端项目首页可以根据该字段展示“已锁定 N / required_project_count”

当前原型中，赛季项目固定数为：

```text
required_project_count = 3
```

---

### status

赛季状态。

取值说明：
```text
0 = 未开始
1 = 进行中
2 = 已结束
```
保留该字段的原因是：赛季状态不一定完全等同于日期判断。

例如：

- 到了开始日期，但管理员暂未开启赛季
- 赛季已到结束日期，但仍允许后台补审核
- 特殊情况下需要提前结束赛季
- 需要明确标记当前正在进行的赛季

当前业务规则要求同一时间只允许存在一个进行中的赛季，即同一时间只能有一条记录的 `status = 1`。

---

## MySQL 建表语句

```sql
CREATE TABLE season (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '赛季ID',
  name VARCHAR(64) NOT NULL COMMENT '赛季名称',
  start_date DATE NOT NULL COMMENT '赛季开始日期',
  end_date DATE NOT NULL COMMENT '赛季结束日期',
  required_project_count TINYINT UNSIGNED NOT NULL DEFAULT 3 COMMENT '当前赛季要求选择的项目数量',
  status TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '状态：0未开始，1进行中，2已结束',
  PRIMARY KEY (id),
  KEY idx_season_status (status),
  KEY idx_season_date (start_date, end_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='赛季表';
```
