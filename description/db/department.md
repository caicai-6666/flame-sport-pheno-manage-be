# 部门表：department

## 表作用

`department` 表用于存储企业内部的部门信息。

当前平台中，部门主要用于：

- 关联用户所属部门
- 在排行榜中展示用户所在部门
- 后续支持按部门统计积分、参与人数、完成情况等数据

当前版本采用扁平化部门结构，不支持上下级部门关系。

---

## 字段说明

| 字段名 | 类型             | 是否必填 | 默认值 | 说明                                         |
| ------ | ---------------- | -------: | -----: | -------------------------------------------- |
| id     | VARCHAR(64)      |       是 |     无 | 部门主键 ID                                  |
| name   | VARCHAR(64)      |       是 |     无 | 部门名称，例如“研发一组”“产品体验”“市场增长” |
| status | TINYINT UNSIGNED |       是 |      1 | 部门状态：`1` 表示启用，`0` 表示停用         |

---

## 字段设计说明

### id

部门的唯一标识。

使用 `VARCHAR(64)` 作为主键，方便直接对接企业内部已有的部门编码或外部系统部门 ID。  
该字段由业务系统或同步程序写入，不由数据库自增生成。

---

### name

部门名称。

当前不支持部门层级，因此部门名称需要保持唯一，避免出现多个同名部门导致用户归属和统计结果不明确。

示例：
```text
产品体验
市场增长
研发一组
运营中心
人力行政
```

---

### status

部门状态。

取值说明：
```text
1 = 启用
0 = 停用
```
保留该字段的原因是：部门一旦被用户引用，不建议直接物理删除。  
如果某个部门后续不再使用，可以将其状态改为停用。

---

## MySQL 建表语句
```sql
CREATE TABLE department (
  id VARCHAR(64) NOT NULL COMMENT '部门ID',
  name VARCHAR(64) NOT NULL COMMENT '部门名称',
  status TINYINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '状态：1启用，0停用',
  PRIMARY KEY (id),
  UNIQUE KEY uk_department_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='部门表';
```
