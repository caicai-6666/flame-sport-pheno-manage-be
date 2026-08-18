# 赛季补传资格表：season_supplement_eligibility

## 表作用

`season_supplement_eligibility` 表用于记录结算中赛季允许用户补传的凭证记录。

每一行对应一条可补传的 `proof_record`。同一赛季用户存在至少一条有效资格记录时，即表示该用户当前具备补传资格。

---

## 字段说明

| 字段名 | 类型 | 是否必填 | 默认值 | 说明 |
| --- | --- | ---: | ---: | --- |
| `id` | `BIGINT UNSIGNED` | 是 | 自增 | 补传资格记录主键 ID |
| `season_user_id` | `BIGINT UNSIGNED` | 是 | 无 | 赛季用户记录 ID，关联 `season_user.id` |
| `proof_record_id` | `BIGINT UNSIGNED` | 是 | 无 | 允许补传的凭证记录 ID，关联 `proof_record.id` |
| `status` | `TINYINT UNSIGNED` | 是 | `1` | 资格状态：`1` 可补传，`0` 已关闭 |

---

## 字段设计说明

### season_user_id

标识获得补传资格的赛季参与记录。用户和结算赛季通过 `season_user` 关联获取，不重复保存 `user_id` 和 `season_id`。

### proof_record_id

标识允许被补传覆盖的原凭证记录。一个 `proof_record_id` 在本表中最多出现一次，资格重新开放时更新原资格记录，不重复插入。

写入时必须校验：

```text
proof_record.season_user_id = season_supplement_eligibility.season_user_id
```

该归属关系由应用服务在同一事务内校验；两个外键分别保证赛季用户和凭证记录真实存在。

### status

资格状态取值如下：

| 字段值 | 说明 |
| --- | --- |
| `1` | 当前允许补传 |
| `0` | 资格已关闭，不再允许补传 |

用户成功提交该凭证的新版本，或者补传流程整体结束时，将对应记录更新为 `0`。

---

## 数组返回方式

数据库不使用 JSON 数组保存多个凭证 ID。查询用户补传资格时，应用按 `season_user_id` 聚合有效行，对客户端返回数组：

```json
{
  "season_user_id": 82,
  "proof_record_ids": [348, 352]
}
```

采用逐行存储可以直接建立外键和唯一约束，也可以在用户补传其中一条记录时只关闭对应资格，避免并发覆盖整个 JSON 数组。

---

## 索引与约束

- `proof_record_id` 唯一索引防止同一凭证产生重复资格。
- `(season_user_id, status, id)` 索引用于查询某个赛季用户当前可补传的记录。
- `status` 通过 `CHECK` 约束限制为 `0` 或 `1`。
- 两个外键分别关联 `season_user` 和 `proof_record`。

---

## MySQL 建表语句

```sql
CREATE TABLE season_supplement_eligibility (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '赛季补传资格记录ID',
  season_user_id BIGINT UNSIGNED NOT NULL COMMENT '赛季用户记录ID',
  proof_record_id BIGINT UNSIGNED NOT NULL COMMENT '允许补传的凭证记录ID',
  status TINYINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '资格状态：1可补传，0已关闭',
  PRIMARY KEY (id),
  UNIQUE KEY uk_season_supplement_proof_record (proof_record_id),
  KEY idx_season_supplement_user_status
    (season_user_id, status, id),
  CONSTRAINT chk_season_supplement_status
    CHECK (status IN (0, 1)),
  CONSTRAINT fk_season_supplement_season_user
    FOREIGN KEY (season_user_id) REFERENCES season_user(id),
  CONSTRAINT fk_season_supplement_proof_record
    FOREIGN KEY (proof_record_id) REFERENCES proof_record(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='赛季补传资格表';
```

---

## 表关系

```text
season_user 1 : N season_supplement_eligibility
proof_record 1 : 0..1 season_supplement_eligibility
```
