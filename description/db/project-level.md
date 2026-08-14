# 项目等级表：project_level

## 表作用

`project_level` 表用于存储企业运动平台支持的项目挑战等级。

当前平台中，项目等级表示用户参与赛季挑战时可选择的难度等级，例如：
```text
青铜
白银
黄金
```

该表只存储等级本身，不存储某个具体项目的挑战规则。  
具体某个赛季、某个项目、某个等级下的规则，应由后续规则表维护。

---

## 字段说明

| 字段名 | 类型             | 是否必填 | 默认值 | 说明                                 |
| ------ | ---------------- | -------: | -----: | ------------------------------------ |
| id     | BIGINT UNSIGNED  |       是 |   自增 | 项目等级主键 ID                      |
| name   | VARCHAR(32)      |       是 |     无 | 等级名称，例如“青铜”“白银”“黄金”     |
| reward | INT UNSIGNED     |       是 |     10 | 该挑战成功后对应的积分               |
| status | TINYINT UNSIGNED |       是 |      1 | 等级状态：`1` 表示启用，`0` 表示停用 |

---

## 字段设计说明

### id

项目等级的唯一标识。

使用 `BIGINT UNSIGNED AUTO_INCREMENT` 作为主键。  
平台内其他业务表会通过 `level_id` 关联到该字段。

例如：
```text
project_rule.level_id
season_user.level_id
```
---

### name

项目等级名称。

用于前端展示和后台配置识别。

示例：
```text
青铜
白银
黄金
```
当前设计中，等级名称应保持唯一。  
原因是项目等级属于平台基础配置，不应存在多个同名等级，否则会导致用户选择、规则配置和统计结果产生歧义。

---

### reward

挑战成功后的奖励积分，可用于指代挑战展示先后。

例如：
```text
青铜：100
白银：200
黄金：300
```
---

### status

项目等级状态。

取值说明：
```text
1 = 启用
0 = 停用
```
保留该字段的原因是：等级一旦被项目规则、用户赛季选择或历史数据引用，就不建议物理删除。

如果某个等级后续不再开放给用户选择，可以将其状态改为停用。

---

## MySQL 建表语句
```sql
CREATE TABLE project_level (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '项目等级ID',
  name VARCHAR(32) NOT NULL COMMENT '等级名称',
  reward INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '挑战成功后奖励积分',
  status TINYINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '状态：1启用，0停用',
  PRIMARY KEY (id),
  UNIQUE KEY uk_project_level_name (name),
  KEY idx_project_level_status (status),
  KEY idx_project_level_sort_order (reward)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='项目等级表';
```
