# 项目上传配置表：project_upload_config

## 表作用

`project_upload_config` 表用于存储不同项目在上传凭证时展示给用户的配置。

当前平台中，用户在成功参与赛季并锁定项目后，可以点击已选择的项目上传运动凭证。  
不同运动项目的凭证类型可能不同：

```text
日常步数：普通凭证
跑步/快走：普通凭证
减重挑战：月初记录、月末记录
```

因此本表采用：

```text
一个项目 + 一个凭证类型 = 一条上传配置
```

这种设计可以让前端根据接口返回的配置动态渲染上传界面，而不是按具体运动项目硬编码 UI。

---

## 字段说明

| 字段名           | 类型             | 是否必填 | 默认值 | 说明                              |
| ---------------- | ---------------- | -------: | -----: | --------------------------------- |
| id               | BIGINT UNSIGNED  |       是 |   自增 | 项目上传配置主键 ID               |
| project_id       | BIGINT UNSIGNED  |       是 |     无 | 项目 ID，关联 `project.id`        |
| record_type      | VARCHAR(64)      |       是 |     无 | 凭证类型展示名称，供前端展示和历史记录展示使用 |
| upload_hint      | VARCHAR(255)     |       是 |     无 | 上传图片下方的凭证类型提示        |
| note_example     | VARCHAR(255)     |       否 |   NULL | 备注输入框中的填写示例            |
| sort_order       | INT UNSIGNED     |       是 |      0 | 展示排序，数值越小越靠前          |
| status           | TINYINT UNSIGNED |       是 |      1 | 状态：`1` 启用，`0` 停用          |

---

## 字段设计说明

### id

项目上传配置的唯一标识。

使用 `BIGINT UNSIGNED AUTO_INCREMENT` 作为主键。

---

### project_id

项目 ID。

该字段关联 `project.id`，表示这条上传配置属于哪个运动项目。

示例：

```text
日常步数
跑步/快走
健身打卡
公司运动
户外登山
减重挑战
```

---

### record_type

凭证类型展示名称。

该字段用于前端展示凭证类型按钮或选项标题。

当前采用简化设计，不再额外拆分“程序编码”和“展示名称”。  
前端提交凭证时，不再提交该字段值作为后端写库依据，而是提交本表的 `id`。

示例：

```text
普通凭证
月初记录
月末记录
```

含义示例：

```text
普通凭证 = 普通运动凭证
月初记录 = 月初体重记录
月末记录 = 月末体重记录
```

后端创建 `proof_record` 时，应将用户提交的 `project_upload_config_id` 写入 `proof_record.project_upload_config_id`。  
`record_type` 只作为展示文案使用。

### upload_hint

上传图片下方的凭证类型提示。

该字段用于告诉用户应该上传什么类型的图片或截图。

示例：

```text
步数截图、手环记录或健康 App 截图
跑步 App 轨迹、距离或配速截图
训练照片、健身房打卡或课程记录
体重秤照片或体重记录截图
```

该字段对应前端上传弹窗中“点击上传图片”下方的小字提示。

---

### note_example

备注输入框中的填写示例。

该字段用于提示用户如何填写凭证备注。

示例：

```text
例如：今日累计 8600 步，含通勤步行和晚饭后散步
例如：晚间快走 4km，用时 38 分钟，配速 9'30"
例如：力量训练 45 分钟，包含深蹲、卧推和拉伸
例如：空腹称重，体重秤放置在同一位置
```

该字段允许为空。  
如果为空，前端可以展示默认备注提示。

---

### sort_order

展示排序。

数值越小，展示越靠前。

该字段主要用于多个凭证类型的项目。

例如减重挑战：

```text
月初记录 sort_order = 10
月末记录 sort_order = 20
```

前端展示凭证类型选项时，应按该字段升序排列。

---

### status

项目上传配置状态。

取值说明：

```text
1 = 启用
0 = 停用
```

保留该字段的原因是：上传配置可能会随着项目调整而停用，但不建议直接物理删除历史配置。

---

## MySQL 建表语句

```sql
CREATE TABLE project_upload_config (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '项目上传配置ID',
  project_id BIGINT UNSIGNED NOT NULL COMMENT '项目ID',
  record_type VARCHAR(64) NOT NULL COMMENT '凭证类型展示名称',
  upload_hint VARCHAR(255) NOT NULL COMMENT '上传图片下方的凭证类型提示',
  note_example VARCHAR(255) DEFAULT NULL COMMENT '备注输入框填写示例',
  sort_order INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '展示排序，数值越小越靠前',
  status TINYINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '状态：1启用，0停用',
  PRIMARY KEY (id),
  UNIQUE KEY uk_project_upload_config_project_record_type (project_id, record_type),
  KEY idx_project_upload_config_project_id (project_id),
  KEY idx_project_upload_config_status (status),
  KEY idx_project_upload_config_sort_order (sort_order),
  CONSTRAINT fk_project_upload_config_project
    FOREIGN KEY (project_id) REFERENCES project(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='项目上传配置表';
```
