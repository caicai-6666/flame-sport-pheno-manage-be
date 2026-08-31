# 赛季补传资格表：season_supplement_eligibility

## 表介绍

`season_supplement_eligibility` 记录结算中赛季按凭证开放的补传资格，以及补交后从待初审到待终审的流程状态。每行对应一条 `proof_record`；同一凭证最多存在一行资格。

资格首次创建时同时固化项目和规则上下文。后续赛季即使修改当前项目指标，旧赛季补交记录仍使用原快照初审；同一资格重新开放时不得覆盖既有快照。

## 字段介绍

| 字段名 | 类型 | 是否必填 | 默认值 | 说明 |
| --- | --- | ---: | ---: | --- |
| `id` | `BIGINT UNSIGNED` | 是 | 自增 | 补传资格主键 ID |
| `season_user_id` | `BIGINT UNSIGNED` | 是 | 无 | 赛季用户记录 ID，关联 `season_user.id` |
| `proof_record_id` | `BIGINT UNSIGNED` | 是 | 无 | 允许原位补交的凭证 ID，关联 `proof_record.id` |
| `preliminary_review_context_snapshot` | `JSON` | 否 | `NULL` | 补交专用初审上下文；新建资格必须写入，允许迁移期间暂时为空 |
| `status` | `TINYINT UNSIGNED` | 是 | `1` | 资格及审核流程状态 |

### 初审上下文快照

`preliminary_review_context_snapshot` 使用以下固定结构：

```json
{
  "projectId": 3,
  "projectName": "跑步",
  "levelId": 2,
  "recordType": "日常记录",
  "ruleContent": [
    {
      "label": "单次距离",
      "value": "不少于 5 公里"
    }
  ],
  "ruleNote": "按用户实际运动数据判断"
}
```

管理端创建资格时从凭证、赛季用户、项目、上传配置和当前启用规则组合该快照。`projectId` 用于追溯来源；客户端补交初审使用其余字段作为模型上下文。快照缺失或非法时不得退回实时规则。

### 状态

| 值 | 含义 | 是否允许用户再次补传 |
| --- | --- | --- |
| `0` | 资格已关闭 | 否 |
| `1` | 资格开放，等待用户补传 | 是 |
| `2` | 用户已补交，等待补交专用初审 | 是 |
| `3` | 补交初审通过，等待管理员终审 | 是 |

初审通过时常规状态流转为 `1 → 2 → 3 → 0`；初审失败时执行 `2 → 1`。终审通过前，用户可从任一非零状态再次补传并统一转回 `2`。流程终止时可以从任一非零状态关闭为 `0`。资格重新开放只允许把状态 `0` 更新为 `1`，不得覆盖既有快照或回退状态 `2`、`3`。

## 索引与约束

- `proof_record_id` 唯一索引保证资格写入幂等。
- `(season_user_id, status, id)` 索引用于用户资格和结算任务扫描。
- `status` 通过 `CHECK` 约束限制为 `0`、`1`、`2` 或 `3`。
- 两个外键分别保证赛季用户和凭证存在；应用层同时保证二者归属一致。

## 建表语句

```sql
CREATE TABLE season_supplement_eligibility (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT
    COMMENT '赛季补传资格记录ID',
  season_user_id BIGINT UNSIGNED NOT NULL
    COMMENT '赛季用户记录ID',
  proof_record_id BIGINT UNSIGNED NOT NULL
    COMMENT '允许补传的凭证记录ID',
  preliminary_review_context_snapshot JSON DEFAULT NULL
    COMMENT '补交初审上下文快照，资格重开时不得覆盖',
  status TINYINT UNSIGNED NOT NULL DEFAULT 1
    COMMENT '资格状态：0关闭，1可补传，2待初审，3初审通过',
  PRIMARY KEY (id),
  UNIQUE KEY uk_season_supplement_proof_record (proof_record_id),
  KEY idx_season_supplement_user_status
    (season_user_id, status, id),
  CONSTRAINT chk_season_supplement_status
    CHECK (status IN (0, 1, 2, 3)),
  CONSTRAINT fk_season_supplement_season_user
    FOREIGN KEY (season_user_id) REFERENCES season_user(id),
  CONSTRAINT fk_season_supplement_proof_record
    FOREIGN KEY (proof_record_id) REFERENCES proof_record(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='赛季补传资格表';
```

## 表关系

```text
season_user 1 : N season_supplement_eligibility
proof_record 1 : 0..1 season_supplement_eligibility
```
