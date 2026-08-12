# 积分变动记录表：point_record

## 表作用

`point_record` 表用于记录用户全局积分的每一次变动。

当前平台中，用户积分是跨赛季累积的，不再只归属于某一个赛季。  
无论是赛季结束后统一发放的奖励积分，还是用户在积分商城兑换商品消耗的积分，都会记录在本表中。

该表同时覆盖以下场景：

- 赛季奖励发放
- 商品兑换扣减
- 后台人工补发积分
- 后台人工扣减积分

---

## 字段说明

| 字段名        | 类型             | 是否必填 |            默认值 | 说明                                         |
| ------------- | ---------------- | -------: | ----------------: | -------------------------------------------- |
| id            | BIGINT UNSIGNED  |       是 |              自增 | 积分变动记录主键 ID                          |
| user_id       | VARCHAR(64)      |       是 |                无 | 用户 ID，关联 `user.id`                      |
| product_id    | BIGINT UNSIGNED  |       否 |              NULL | 商品 ID，关联 `product.id`，仅商品兑换时有值 |
| change_type   | VARCHAR(32)      |       是 |                无 | 积分变动类型                                 |
| change_points | INT              |       是 |                无 | 本次积分变动值，正数表示增加，负数表示扣减   |
| points_after  | INT UNSIGNED     |       是 |                无 | 本次变动后的用户积分余额                     |
| description   | VARCHAR(255)     |       否 |              NULL | 积分变动描述                                 |
| status        | TINYINT UNSIGNED |       是 |                 1 | 状态：`1` 有效，`0` 作废                     |
| created_at    | DATETIME         |       是 | CURRENT_TIMESTAMP | 积分变动时间                                 |

---

## 字段设计说明

### id

积分变动记录的唯一标识。

使用 `BIGINT UNSIGNED AUTO_INCREMENT` 作为主键。

---

### user_id

用户 ID。

该字段关联 `user.id`，表示这条积分变动属于哪个用户。

当前积分为用户全局积分，跨赛季累积，因此本表直接关联用户，不再关联 `season_user`。

---

### product_id

商品 ID。

该字段关联 `product.id`，允许为空。

当积分变动类型为商品兑换时，该字段记录兑换的商品：
```text
change_type = exchange
product_id = 对应商品 ID
```

当积分变动类型为赛季奖励或后台人工调整时，该字段为空：
```text
change_type = season_reward
product_id = NULL
```

```text
change_type = manual_adjust
product_id = NULL
```
---

### change_type

积分变动类型。

建议取值：
```text
season_reward
exchange
manual_adjust
```
含义如下：
```text
season_reward = 赛季奖励发放
exchange      = 商品兑换扣减
manual_adjust = 后台人工调整
```
后续如果需要扩展其他积分来源，可以继续增加类型。

---

### change_points

本次积分变动值。

该字段使用有符号整数。

规则：
```text
正数 = 增加积分
负数 = 扣减积分
```
示例：
```text
+60  2026年7月赛季黄金挑战达标奖励
-30  兑换商品：跳绳
+10  后台补发积分
-5   后台扣减异常积分
```
---

### points_after

本次积分变动后的用户积分余额。

示例：
```text
变动前积分：100
本次兑换商品：-30
变动后积分：70
```
则记录为：
```text
change_points = -30
points_after = 70
```
该字段用于：

- 前端展示积分余额
- 展示兑换后剩余积分
- 后台排查积分争议
- 避免每次查询都聚合全部积分记录

---

### description

积分变动描述。

用于记录本次积分变动的业务说明。

示例：
```text
2026年7月赛季黄金挑战达标奖励
兑换商品：运动水杯
后台补发积分
后台扣减异常积分
```
该字段允许为空，但建议业务写入时尽量填写。

---

### status

记录状态。

取值说明：
```text
1 = 有效
0 = 作废
```
保留该字段的原因是：积分记录属于业务流水，不建议物理删除。

如果后台需要撤销某条积分变动记录，可以将其状态改为作废。  
计算用户积分余额时，只应统计有效记录。

---

### created_at

积分变动时间。

用于展示积分记录、兑换记录和后台审计。

---

## MySQL 建表语句

索引设计原则：

- `point_record` 是积分流水表，后续写入会比较频繁，索引需要克制。
- 保留 `(user_id, created_at)` 复合索引，用于查询某个用户的积分流水并按时间展示。
- 保留 `product_id` 索引，用于后续按商品统计兑换记录或排查兑换流水。
- 暂不单独为 `change_type`、`status`、`created_at` 建索引。`change_type` 和 `status` 基数较低，单列索引收益有限；单独 `created_at` 只有在全局时间范围查询频繁时才需要。

```sql
CREATE TABLE point_record (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '积分变动记录ID',
  user_id VARCHAR(64) NOT NULL COMMENT '用户ID',
  product_id BIGINT UNSIGNED DEFAULT NULL COMMENT '商品ID，仅商品兑换时有值',
  change_type VARCHAR(32) NOT NULL COMMENT '积分变动类型：season_reward赛季奖励，exchange商品兑换，manual_adjust后台调整',
  change_points INT NOT NULL COMMENT '积分变动值，正数增加，负数扣减',
  points_after INT UNSIGNED NOT NULL COMMENT '变动后的积分余额',
  description VARCHAR(255) DEFAULT NULL COMMENT '积分变动描述',
  status TINYINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '状态：1有效，0作废',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '积分变动时间',
  PRIMARY KEY (id),
  KEY idx_point_record_user_created_at (user_id, created_at),
  KEY idx_point_record_product_id (product_id),
  CONSTRAINT fk_point_record_user
    FOREIGN KEY (user_id) REFERENCES `user`(id),
  CONSTRAINT fk_point_record_product
    FOREIGN KEY (product_id) REFERENCES product(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='积分变动记录表';
```
