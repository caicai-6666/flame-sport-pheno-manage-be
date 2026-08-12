



# 项目规则表：project_rule

## 表作用
`project_rule` 表用于存储不同项目在不同挑战等级下的完成规则。
当前平台中，用户在一个赛季中选择 3 个运动项目，并选择一个统一的项目等级。  
系统根据用户选择的项目和等级，找到对应的项目规则，用于前端展示和月末审核判断。

该表只描述挑战规则本身，不记录用户数据，不记录赛季数据，也不记录积分。  
积分由 `project_level` 表中的等级积分字段决定。

当前设计中，项目规则是平台通用规则，不随赛季变化。

当前前端挑战规则以“副描述 + 指标项 + 备注”的形式展示，因此本表将规则拆分为：

```text
sub_desc     = 挑战副描述
rule_content = 规则指标 JSON
rule_note    = 规则备注
```

---

## 字段说明

| 字段名       | 类型             | 是否必填 | 默认值 | 说明                                 |
| ------------ | ---------------- | -------: | -----: | ------------------------------------ |
| id           | BIGINT UNSIGNED  |       是 |   自增 | 项目规则主键 ID                      |
| project_id   | BIGINT UNSIGNED  |       是 |     无 | 项目 ID，关联 `project.id`           |
| level_id     | BIGINT UNSIGNED  |       是 |     无 | 项目等级 ID，关联 `project_level.id` |
| sub_desc     | VARCHAR(128)     |       否 |   NULL | 挑战副描述                           |
| rule_content | JSON             |       是 |     无 | 规则指标内容，JSON 数组              |
| rule_note    | VARCHAR(255)     |       否 |   NULL | 规则备注说明                         |
| status       | TINYINT UNSIGNED |       是 |      1 | 规则状态：`1` 启用，`0` 停用         |

---

## 字段设计说明

### id

项目规则的唯一标识。

使用 `BIGINT UNSIGNED AUTO_INCREMENT` 作为主键。

---

### project_id

项目 ID。

该字段关联 `project.id`，表示这条规则属于哪个运动项目。

示例：

```text
日常步数
跑步/快走
健身打卡
减重挑战
```

---

### level_id

项目等级 ID。

该字段关联 `project_level.id`，表示这条规则对应哪个挑战等级。

示例：

```text
青铜
白银
黄金
```

---

### sub_desc

挑战副描述。

用于展示在挑战等级标题下方，描述该等级挑战的目标或适合人群。

示例：

```text
建立稳定的每日步行习惯
提升有氧容量和节奏控制
完成高频健身挑战
按 BMI 分级设置减重目标
```

该字段允许为空。  
如果为空，前端可以不展示副描述。

---

### rule_content

规则指标内容。

该字段使用 MySQL 8.4 支持的 `JSON` 类型，用于存储前端可直接渲染的规则指标数组。

推荐结构：

```json
[
  {
    "label": "每日步数",
    "value": "8000步/天"
  },
  {
    "label": "达标天数",
    "value": "累计20天"
  }
]
```

对应前端展示：

```text
每日步数：8000步/天
达标天数：累计20天
```

不同项目的规则指标数量和含义可能不同，因此使用 JSON 数组比固定字段更适合当前业务。

示例：

```json
[
  {
    "label": "累计距离",
    "value": "50km"
  },
  {
    "label": "配速要求",
    "value": "≤8'00''"
  }
]
```

```json
[
  {
    "label": "BMI < 24",
    "value": "2kg"
  },
  {
    "label": "24–28",
    "value": "3kg"
  },
  {
    "label": "≥28",
    "value": "4kg"
  }
]
```

MySQL `JSON` 类型会校验字段内容必须是合法 JSON，能减少后台配置时写入非法结构的风险。

---

### rule_note

规则备注说明。

用于展示在挑战规则底部，补充说明审核口径、适用范围或注意事项。

示例：

```text
按自然日统计达标记录
跑步或快走均可累计
适合已有基础运动习惯
以赛季起止体重变化计算
```

该字段允许为空。  
如果为空，前端可以不展示备注。

---

### status

项目规则状态。

取值说明：

```text
1 = 启用
0 = 停用
```

保留该字段的原因是：规则一旦被用户选择、前端展示或历史审核引用，不建议物理删除。  
如果某条规则后续不再使用，可以将其状态改为停用。

---

## MySQL 建表语句

```sql
CREATE TABLE project_rule (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '项目规则ID',
  project_id BIGINT UNSIGNED NOT NULL COMMENT '项目ID',
  level_id BIGINT UNSIGNED NOT NULL COMMENT '项目等级ID',
  sub_desc VARCHAR(128) DEFAULT NULL COMMENT '挑战副描述',
  rule_content JSON NOT NULL COMMENT '规则指标内容，JSON数组',
  rule_note VARCHAR(255) DEFAULT NULL COMMENT '规则备注说明',
  status TINYINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '状态：1启用，0停用',
  PRIMARY KEY (id),
  UNIQUE KEY uk_project_rule_project_level (project_id, level_id),
  KEY idx_project_rule_project_id (project_id),
  KEY idx_project_rule_level_id (level_id),
  KEY idx_project_rule_status (status),
  CONSTRAINT fk_project_rule_project
    FOREIGN KEY (project_id) REFERENCES project(id),
  CONSTRAINT fk_project_rule_level
    FOREIGN KEY (level_id) REFERENCES project_level(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='项目规则表';
```
