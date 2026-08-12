# 商品表：product

## 表作用

`product` 表用于存储积分商城中可兑换的商品信息。

当前平台中，商品与赛季脱钩。  
商品可以由后台自由加入商城列表，也可以由后台调整兑换所需积分。

商品表只描述商品本身及其兑换积分，不维护库存，也不按赛季单独配置价格。

---

## 字段说明

| 字段名          | 类型             | 是否必填 | 默认值 | 说明                                 |
| --------------- | ---------------- | -------: | -----: | ------------------------------------ |
| id              | BIGINT UNSIGNED  |       是 |   自增 | 商品主键 ID                          |
| name            | VARCHAR(128)     |       是 |     无 | 商品名称                             |
| description     | VARCHAR(255)     |       否 |   NULL | 商品说明，用于商城卡片展示           |
| points_required | INT UNSIGNED     |       是 |     无 | 兑换该商品所需积分                   |
| image_url       | VARCHAR(255)     |       否 |   NULL | 商品图片地址                         |
| status          | TINYINT UNSIGNED |       是 |      1 | 商品状态：`1` 表示上架，`0` 表示下架 |

---

## 字段设计说明

### id

商品的唯一标识。

使用 `BIGINT UNSIGNED AUTO_INCREMENT` 作为主键。  
后续商品兑换记录表会通过 `product_id` 关联到该字段。

---

### name

商品名称。

用于商城页面展示和后台管理识别。

示例：
```text
运动毛巾
羽毛球袜
筋膜球
跳绳
运动水杯
瑜伽垫
```

当前设计中，不强制商品名称唯一。

原因是后续可能出现：

- 同名但不同规格的商品
- 同名但不同批次的商品
- 后台重复上架相似商品

系统应通过 `id` 唯一识别商品，而不是通过 `name` 识别商品。

---

### description

商品说明。

用于商城卡片展示，帮助用户理解商品内容。

示例：
```text
训练后快速吸汗
透气耐磨日常款
居家拉伸训练
```
该字段允许为空。  
如果为空，前端可以不展示商品说明或展示默认文案。

---

### points_required

兑换该商品所需积分。

该字段表示商品的积分价格，例如：
```text
20
30
50
```
之所以使用 `points_required`，而不是 `price`，是因为该商品不是使用人民币购买，而是使用平台积分兑换。

商品积分价格可以由后台调整。  
需要注意的是，后续兑换记录表中应额外保存用户兑换时实际消耗的积分，例如：

```text
points_spent
```
这样即使商品价格后续发生变化，历史兑换记录也不会受到影响。

---

### image_url

商品图片地址。

该字段允许为空。  
如果商品没有配置图片，前端可以展示默认图片或占位图。

---

### status

商品状态。

取值说明：
```text
1 = 上架
0 = 下架
```
保留该字段的原因是：商品一旦产生兑换记录，就不建议物理删除。

如果某个商品后续不再开放兑换，可以将其状态改为下架。  
下架商品不再展示给普通用户，但历史兑换记录仍然保留。

---

## MySQL 建表语句
```sql
CREATE TABLE product (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '商品ID',
  name VARCHAR(128) NOT NULL COMMENT '商品名称',
  description VARCHAR(255) DEFAULT NULL COMMENT '商品说明',
  points_required INT UNSIGNED NOT NULL COMMENT '兑换所需积分',
  image_url VARCHAR(255) DEFAULT NULL COMMENT '商品图片地址',
  status TINYINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '状态：1上架，0下架',
  PRIMARY KEY (id),
  KEY idx_product_status (status),
  KEY idx_product_points_required (points_required)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品表';
```
