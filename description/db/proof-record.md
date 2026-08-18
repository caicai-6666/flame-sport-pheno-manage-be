# 凭证记录表：proof_record

## 表作用

`proof_record` 表用于记录用户在赛季期间上传的运动凭证。

当前平台中，用户在选择并锁定项目后，可以针对不同项目上传运动凭证。  
初审通过后，凭证会先推进项目完成进度；管理员在赛季期间按日常工作节奏持续终审。终审失败时，系统撤销该凭证当前实际贡献的进度，并使用其他尚未完全分配进度的有效凭证回补空缺。

当前设计中，一次上传只对应一张图片，因此图片路径直接存储在本表中，不单独设计图片文件表。

本表不单独存储 BMI。  
减重挑战中，BMI 应根据用户表中的身高和体重凭证中的体重信息计算得到，不作为通用凭证字段存储。

---

## 字段说明

| 字段名         | 类型             | 是否必填 |      默认值 | 说明                                   |
| -------------- | ---------------- | -------: | ----------: | -------------------------------------- |
| id             | BIGINT UNSIGNED  |       是 |        自增 | 凭证记录主键 ID                        |
| season_user_id | BIGINT UNSIGNED  |       是 |          无 | 赛季用户记录 ID，关联 `season_user.id` |
| project_id     | BIGINT UNSIGNED  |       是 |          无 | 项目 ID，关联 `project.id`             |
| project_upload_config_id | BIGINT UNSIGNED | 是 | 无 | 项目上传配置 ID，关联 `project_upload_config.id` |
| image_url      | VARCHAR(500)     |       是 |          无 | 上传图片路径                           |
| note           | VARCHAR(255)     |       否 |        NULL | 用户备注                               |
| proof_date     | DATE             |       是 |          无 | 凭证对应的实际运动日期                 |
| review_status  | VARCHAR(32)      |       是 |     pending | 初审与终审状态                         |
| review_comment | VARCHAR(500)     |       否 |        NULL | 初审或终审的审核说明，终审时可覆盖初审意见 |
| progress_delta | DECIMAL(5,4) | 是 | 0.0000 | 大模型初审给出的原始项目进度增量 |
| increase       | DECIMAL(5,4) | 是 | 0.0000 | 当前凭证实际分配到项目进度条的贡献 |
| status         | TINYINT UNSIGNED |       是 |           1 | 记录状态：`1` 正常，`0` 无效/删除      |
| created_at     | DATETIME         |       是 | CURRENT_TIMESTAMP | 上传时间                         |

---

## 字段设计说明

### id

凭证记录的唯一标识。

使用 `BIGINT UNSIGNED AUTO_INCREMENT` 作为主键。

---

### season_user_id

赛季用户记录 ID。

该字段关联 `season_user.id`。

通过 `season_user_id` 可以确定：
```text
用户是谁
属于哪个赛季
用户选择了哪个项目等级
```

因此本表不再重复存储 `season_id` 和 `user_id`，避免数据冗余。

---

### project_id

项目 ID。

该字段关联 `project.id`，表示这条凭证属于哪个运动项目。

示例：
```text
日常步数
跑步/快走
健身打卡
减重挑战
```
上传凭证时，后端应校验该项目是否属于用户当前赛季已锁定的项目。

即需要存在有效记录：
```text
season_user_project.season_user_id = 当前 season_user_id
season_user_project.project_id = 当前 project_id
season_user_project.status = 1
```
---

### project_upload_config_id

项目上传配置 ID。

该字段关联 `project_upload_config.id`，用于标识本条凭证是按照哪一条上传配置提交的。

上传配置中维护：

```text
project_id
record_type
upload_hint
note_example
sort_order
status
```

因此 `proof_record` 不再单独存储 `record_type` 字符串，而是通过 `project_upload_config_id` 关联到具体的凭证类型配置。

这样设计的原因：

- 避免用户上传记录只依赖凭证类型文本
- 避免不同项目下相同 `record_type` 字符串产生歧义
- 避免凭证类型文案调整时破坏历史关联
- 让数据库层直接约束凭证类型必须来自已有上传配置

上传凭证时，后端应校验：

```text
project_upload_config.id = 用户提交的 project_upload_config_id
project_upload_config.project_id = 当前 project_id
project_upload_config.status = 1
```

校验通过后，才能写入 `proof_record.project_upload_config_id`。

---

### image_url

上传图片路径。

当前平台采用 MySQL + 文件夹存储图片的方式：
```text
MySQL 存储图片路径
服务器文件夹存储图片文件
```
示例路径：
```text
/uploads/proofs/2026/07/user_10001/proof_xxxxx.jpg
```
需要注意：

- 图片文件名应由服务端生成
- 不应直接使用用户上传的原始文件名作为最终文件名
- 文件路径写入数据库前，应确保图片已成功保存
- 如果数据库写入失败，应清理已上传的孤儿文件

---

### note

用户备注。

用于用户补充说明该凭证对应的运动情况。

示例：
```text
晚间快走 4km，用时 38 分钟，配速稳定。
今日累计 8612 步，通勤和饭后散步完成。
```
该字段允许为空。

---

### proof_date

凭证对应的实际运动日期，格式为 `YYYY-MM-DD`。

普通上传与补传均由前端提交该字段；普通上传页面默认选择当天，补传页面可选择赛季内的过去日期。后端必须再次校验：

```text
season.start_date <= proof_date <= min(season.end_date, 今天)
```

`proof_date` 不等于 `created_at`：前者用于“一项目一天一条有效凭证”的判重与前端日期展示，后者始终记录用户实际上传的时间。迁移历史数据时，使用 `DATE(created_at)` 回填本字段。

同一 `season_user_id + project_id + proof_date` 最多保留一条 `status = 1` 的记录。用户再次提交该日期时，系统按重传处理，更新原记录并重新初审。

---

### review_status

审核状态。该字段使用一个 `VARCHAR(32)` 字段记录模型初审和管理员终审结果。

取值：
```text
pending
preliminary_approved
preliminary_rejected
approved
rejected
```
含义如下：
```text
pending              = 待初审
preliminary_approved = 初审通过
preliminary_rejected = 初审失败
approved             = 终审通过
rejected             = 终审失败
```

用户上传凭证或同运动日期重复上传后，默认状态为：
```text
pending
```

定时初审任务根据图片、`note` 和项目等级规则更新为：
```text
preliminary_approved
preliminary_rejected
```
初审通过的凭证计入排行榜；初审失败的凭证不计入，用户可重传后重新初审。管理员在赛季期间持续将初审通过记录更新为 `approved` 或 `rejected`。终审失败的凭证不再贡献项目进度，也不再计入后续排行榜快照。
---

### progress_delta

大模型初审给出的原始项目进度增量，取值范围为 `0.0000`～`1.0000`。

初审通过时保存模型返回的 `progressDelta`，即使当前项目进度已经达到 `1.0000`，也保留该原始值。初审失败、待初审和月初减重基线记录的值为 `0.0000`。同运动日期重传时，本字段随旧初审结果一起清零，等待新版本重新初审。

该字段为终审失败后的进度回补保留原始依据，不会因为进度条封顶而截断。对于迁移前已经存在的历史记录，旧系统没有保存被封顶前的原始值，迁移时保守使用原实际贡献进行回填。

---

### increase

当前凭证实际分配到 `season_user_project.completion_progress` 的进度贡献，取值范围为 `0.0000`～`1.0000`，并始终满足：

```text
0 <= increase <= progress_delta
```

例如当前项目进度为 `0.9000`，一条初审通过凭证的 `progress_delta` 为 `0.2000`，进度条最多只能增加到 `1.0000`，因此该凭证保存：

```text
progress_delta = 0.2000
increase = 0.1000
```

当管理员终审拒绝一条凭证时，该凭证的 `increase` 归零。系统在同一事务中锁定对应的 `season_user_project`，按 `created_at ASC, id ASC` 查询同一用户、同一赛季、同一项目下仍有效且 `progress_delta > increase` 的通过凭证，将释放的进度依次回补到这些凭证的 `increase`，最后同步更新项目进度条。

同运动日期重传也会先撤销旧版本的 `increase`，再将原记录重置为待初审。项目进度应始终满足：

```text
completion_progress = SUM(当前有效通过凭证的 increase)
completion_progress <= 1.0000
```

---

### review_comment

审核评论。

该字段用于保存模型初审或管理员终审的审核说明、判断依据或拒绝原因。管理员终审时可以覆盖初审意见。

示例：
```text
截图清晰，运动时间和距离符合本月挑战要求。
凭证缺少日期信息，无法确认是否属于当前赛季。
体重记录符合月末复测要求。
```

该字段允许为空。

原因是待初审记录通常还没有审核评论；初审或终审通过时也可能不需要额外说明。

---

### status

记录状态。

取值说明：
```text
1 = 正常
0 = 无效/删除
```
该字段用于软删除或后台作废凭证记录。

保留该字段的原因是：凭证一旦上传，通常不建议直接物理删除。  
如果凭证上传错误、用户撤回或后台判定无效，可以将状态改为 `0`。

---

### created_at

上传时间。

该字段用于记录用户提交凭证的时间。

用途包括：

- 历史记录按上传时间倒序展示
- 作为初审结果写回时的重传版本标识
- 判断凭证是否已超过初审最小等待时间
- 后台审核时查看凭证提交时间

该字段由数据库默认写入当前时间。

---

## MySQL 建表语句
```sql
CREATE TABLE proof_record (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '凭证记录ID',
  season_user_id BIGINT UNSIGNED NOT NULL COMMENT '赛季用户记录ID',
  project_id BIGINT UNSIGNED NOT NULL COMMENT '项目ID',
  project_upload_config_id BIGINT UNSIGNED NOT NULL COMMENT '项目上传配置ID',
  image_url VARCHAR(500) NOT NULL COMMENT '上传图片路径',
  note VARCHAR(255) DEFAULT NULL COMMENT '用户备注',
  proof_date DATE NOT NULL COMMENT '凭证对应的实际运动日期',
  review_status VARCHAR(32) NOT NULL DEFAULT 'pending' COMMENT '审核状态：pending待初审，preliminary_approved初审通过，preliminary_rejected初审失败，approved终审通过，rejected终审失败',
  review_comment VARCHAR(500) DEFAULT NULL COMMENT '初审或终审的审核说明，终审时可覆盖初审意见',
  progress_delta DECIMAL(5,4) NOT NULL DEFAULT 0.0000 COMMENT '大模型初审给出的原始项目进度增量',
  `increase` DECIMAL(5,4) NOT NULL DEFAULT 0.0000 COMMENT '当前凭证实际分配到项目进度条的贡献',
  status TINYINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '状态：1正常，0无效/删除',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '上传时间',
  PRIMARY KEY (id),
  KEY idx_proof_record_season_user_id (season_user_id),
  KEY idx_proof_record_project_id (project_id),
  KEY idx_proof_record_project_upload_config_id (project_upload_config_id),
  KEY idx_proof_record_review_status (review_status),
  KEY idx_proof_record_status (status),
  KEY idx_proof_record_season_project_proof_date_status (season_user_id, project_id, proof_date, status),
  KEY idx_proof_record_created_at (created_at),
  CONSTRAINT chk_proof_record_progress_delta
    CHECK (progress_delta >= 0 AND progress_delta <= 1),
  CONSTRAINT chk_proof_record_increase
    CHECK (`increase` >= 0 AND `increase` <= progress_delta),
  CONSTRAINT fk_proof_record_season_user
    FOREIGN KEY (season_user_id) REFERENCES season_user(id),
  CONSTRAINT fk_proof_record_project
    FOREIGN KEY (project_id) REFERENCES project(id),
  CONSTRAINT fk_proof_record_project_upload_config
    FOREIGN KEY (project_upload_config_id) REFERENCES project_upload_config(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='凭证记录表';
```
