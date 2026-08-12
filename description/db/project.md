# 项目表：project

## 表作用

`project` 表用于存储企业运动平台支持的运动项目。

当前平台中，项目表示用户可以选择并参与的运动类型，例如：
```text
日常步数
跑步/快走
健身打卡
公司运动
户外登山
减重挑战
```

项目表属于基础主数据，不直接绑定某个赛季，也不直接存储挑战规则。  
具体某个赛季、某个项目、某个等级下的挑战规则，应由后续规则表维护。

---

## 字段说明

| 字段名      | 类型             | 是否必填 | 默认值 | 说明                                 |
| ----------- | ---------------- | -------: | -----: | ------------------------------------ |
| id          | BIGINT UNSIGNED  |       是 |   自增 | 项目主键 ID                          |
| name        | VARCHAR(64)      |       是 |     无 | 项目名称                             |
| description | VARCHAR(255)     |       否 |   NULL | 项目说明，用于首页项目卡片展示       |
| Icon_url    | VARCHAR(255)     |       否 |   NULL | 项目图标地址                         |
| status      | TINYINT UNSIGNED |       是 |      1 | 项目状态：`1` 表示启用，`0` 表示停用 |

---

## 字段设计说明

### id

项目的唯一标识。

使用 `BIGINT UNSIGNED AUTO_INCREMENT` 作为主键。  
平台内其他业务表会通过 `project_id` 关联到该字段。

例如：
```text
project_rule.project_id
user_season_project.project_id
proof_record.project_id
```
---

### name

项目名称。

用于前端展示和后台配置识别。

示例：
```text
日常步数
跑步/快走
健身打卡
公司运动
户外登山
减重挑战
```
当前设计中，项目名称应保持唯一。  
原因是项目属于平台基础配置，不应存在多个同名项目，否则会导致用户选择、规则配置和统计结果产生歧义。

---

### description

项目说明。

用于首页项目卡片展示，帮助用户理解该运动项目的目标和含义。

示例：
```text
把通勤、散步和碎片运动都变成稳定积分。
记录有氧强度，持续拉高身体活力曲线。
用训练日历沉淀力量、柔韧和核心能力。
```
该字段允许为空。  
如果为空，前端可以不展示项目说明或展示默认文案。

---

### icon_url

项目图标地址。

---

### status

项目状态。

取值说明：
```text
1 = 启用
0 = 停用
```
保留该字段的原因是：项目一旦被赛季规则、用户锁定记录、上传凭证或历史统计引用，就不建议物理删除。

如果某个项目后续不再开放给用户选择，可以将其状态改为停用。

---

## MySQL 建表语句
```sql
CREATE TABLE project (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '项目ID',
  name VARCHAR(64) NOT NULL COMMENT '项目名称',
  description VARCHAR(255) DEFAULT NULL COMMENT '项目说明',
  icon_url VARCHAR(255) DEFAULT NULL COMMENT '项目图标地址',
  status TINYINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '状态：1启用，0停用',
  PRIMARY KEY (id),
  UNIQUE KEY uk_project_name (name),
  KEY idx_project_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='项目表';
```
