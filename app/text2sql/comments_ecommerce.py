"""演示库（Kaggle Brazilian E-Commerce / Olist）表与字段的中文注释字典。

SQLite 没有原生的表/字段注释（COMMENT），M-Schema 生成时无法从库里读到中文含义。
这里用一份手工维护的注释字典补齐：表名 → {表注释 + 字段注释}。

约定（对齐 SQLBot 的 custom_comment 语义，见 backend/apps/datasource/crud/table.py）：
- 只对确有业务含义的字段写注释；字典里没有的字段，M-Schema 输出时省略注释部分，**不编造**。
- 字段名严格对齐 Olist 公开数据集的真实列名（含官方拼写错误，如 lenght）。
- 六张表覆盖 REQUIREMENTS §5 演示场景：orders / order_items / customers / products / sellers / payments。
"""
from __future__ import annotations

# 结构：{表名: {"comment": 表注释, "fields": {字段名: 字段注释}}}
# 生成器只在此字典命中时输出注释，未命中即留空 —— 宁缺毋造。
ECOMMERCE_COMMENTS: dict[str, dict] = {
    "orders": {
        "comment": "订单表",
        "fields": {
            "order_id": "订单ID",
            "customer_id": "客户ID（关联 customers.customer_id）",
            "order_status": "订单状态（delivered/shipped/canceled 等）",
            "order_purchase_timestamp": "下单时间",
            "order_approved_at": "支付核准时间",
            "order_delivered_carrier_date": "交付承运商时间",
            "order_delivered_customer_date": "客户签收时间",
            "order_estimated_delivery_date": "预计送达时间",
        },
    },
    "order_items": {
        "comment": "订单明细表（一个订单可含多个商品项）",
        "fields": {
            "order_id": "订单ID（关联 orders.order_id）",
            "order_item_id": "订单内商品项序号",
            "product_id": "商品ID（关联 products.product_id）",
            "seller_id": "卖家ID（关联 sellers.seller_id）",
            "shipping_limit_date": "发货截止时间",
            "price": "商品单价",
            "freight_value": "运费",
        },
    },
    "customers": {
        "comment": "客户表",
        "fields": {
            "customer_id": "客户ID（每个订单一个，关联 orders）",
            "customer_unique_id": "客户唯一标识（跨订单去重用）",
            "customer_zip_code_prefix": "客户邮编前缀",
            "customer_city": "客户所在城市",
            "customer_state": "客户所在州",
        },
    },
    "products": {
        "comment": "商品表",
        "fields": {
            "product_id": "商品ID",
            "product_category_name": "商品类目名称（葡萄牙语）",
            "product_name_lenght": "商品名称长度（字符数，官方列名拼写为 lenght）",
            "product_description_lenght": "商品描述长度（字符数）",
            "product_photos_qty": "商品图片数量",
            "product_weight_g": "商品重量（克）",
            "product_length_cm": "商品长度（厘米）",
            "product_height_cm": "商品高度（厘米）",
            "product_width_cm": "商品宽度（厘米）",
        },
    },
    "sellers": {
        "comment": "卖家表",
        "fields": {
            "seller_id": "卖家ID",
            "seller_zip_code_prefix": "卖家邮编前缀",
            "seller_city": "卖家所在城市",
            "seller_state": "卖家所在州",
        },
    },
    "payments": {
        "comment": "支付表（一个订单可含多笔支付）",
        "fields": {
            "order_id": "订单ID（关联 orders.order_id）",
            "payment_sequential": "支付序号",
            "payment_type": "支付方式（credit_card/boleto/voucher 等）",
            "payment_installments": "分期期数",
            "payment_value": "支付金额",
        },
    },
}


def table_comment(table: str) -> str:
    """取表注释，缺失返回空串"""
    return ECOMMERCE_COMMENTS.get(table, {}).get("comment", "")


def field_comment(table: str, field: str) -> str:
    """取字段注释，缺失返回空串"""
    return ECOMMERCE_COMMENTS.get(table, {}).get("fields", {}).get(field, "")
