# 用户表：user

## 表作用

`user` 表用于存储企业运动平台中的用户基础信息。

当前平台中，用户主要用于：

- 标识参与企业运动活动的员工
- 关联用户所属部门
- 在排行榜中展示用户信息
- 关联用户的赛季参与记录
- 关联用户上传的运动凭证
- 关联用户积分流水
- 关联用户商品兑换记录

当前版本中，用户表只保存基础展示与归属信息，不负责登录账号、权限角色和积分数据。

---

## 字段说明

| 字段名        | 类型             | 是否必填 | 默认值 | 说明                                      |
| ------------- | ---------------- | -------: | -----: | ----------------------------------------- |
| id            | VARCHAR(64)      |       是 |     无 | 用户主键 ID，也是系统内识别用户的唯一标识 |
| name          | VARCHAR(64)      |       是 |     无 | 用户名称，用于页面展示                    |
| department_id | VARCHAR(64)      |       是 |     无 | 用户所属部门 ID，关联 `department.id`     |
| avatar_url    | VARCHAR(255)     |       否 |   NULL | 用户头像地址                              |
| status        | TINYINT UNSIGNED |       是 |      1 | 用户状态：`1` 表示启用，`0` 表示停用      |

---

## 字段设计说明

### id

用户的唯一标识。

使用 `VARCHAR(64)` 作为主键。  
该字段可以直接使用企业内部员工 ID、第三方登录返回的用户 ID 或后端统一生成的字符串 ID。  
该字段由业务系统或用户同步程序写入，不由数据库自增生成。

平台内其他业务表会通过 `user_id` 关联到该字段。

例如：
```text
season_user.user_id
proof_record.user_id
point_ledger.user_id
exchange_order.user_id
```

---

### name

用户名称。

用于页面展示，例如：
```text
james
amy
张三
李四
```
当前设计中，`name` 不作为唯一识别字段。  
原因是企业中可能存在重名用户，系统应通过 `id` 唯一识别用户。

---

### department_id

用户所属部门 ID。

该字段关联 `department.id`，且不允许为空。

设计原因：

- 当前平台排行榜需要展示用户所属部门
- 后续部门统计、部门排行榜会依赖该字段
- 当前业务中用户必须归属于某个部门

关系如下：
```text
department 1 : N user
```
即一个部门可以有多个用户，一个用户只能属于一个部门。

---

### avatar_url

用户头像地址。

该字段允许为空。  
如果用户没有配置头像，前端可以展示默认头像或用户名缩写。

钉钉首次登录初始化时，服务端会将头像下载到 `assets/images/avatar/`，本字段保存本地相对路径而不是钉钉远程 URL，例如：

```text
/james.jpg
```

该字段可用于：

- 排行榜用户头像
- 用户个人信息展示
- 后续首页或历史记录中的用户展示

---

### status

用户状态。

取值说明：
```text
1 = 启用
0 = 停用
```
保留该字段的原因是：用户一旦产生赛季参与记录、上传记录、积分流水或兑换记录，就不建议物理删除。  
如果用户离职或不再参与活动，可以将其状态改为停用。

---

## MySQL 建表语句
```sql
CREATE TABLE `user` (
  id VARCHAR(64) NOT NULL COMMENT '用户ID',
  name VARCHAR(64) NOT NULL COMMENT '用户名称',
  department_id VARCHAR(64) NOT NULL COMMENT '所属部门ID',
  avatar_url VARCHAR(255) DEFAULT NULL COMMENT '头像地址',
  status TINYINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '状态：1启用，0停用',
  PRIMARY KEY (id),
  KEY idx_user_department_id (department_id),
  KEY idx_user_status (status),
  CONSTRAINT fk_user_department
    FOREIGN KEY (department_id) REFERENCES department(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户表';
```

---

## 迁移语句

从保存用户健康资料的旧版本升级时，使用可重复执行的迁移删除已停用的身高字段：

```sql
SET @drop_user_height_cm_sql = (
  SELECT IF(
    EXISTS(
      SELECT 1
      FROM information_schema.columns
      WHERE table_schema = DATABASE()
        AND table_name = 'user'
        AND column_name = 'height_cm'
    ),
    'ALTER TABLE `user` DROP COLUMN `height_cm`',
    'SELECT 1'
  )
);

PREPARE drop_user_height_cm FROM @drop_user_height_cm_sql;
EXECUTE drop_user_height_cm;
DEALLOCATE PREPARE drop_user_height_cm;
```
