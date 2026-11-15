# 2026 FES 11 月 28 日普通票公开目标快照

2026 年 11 月 14 日 **23:29:04 +08:00（Asia/Shanghai）**，公开活动详情返回了本场目标：活动 `1006859`，11 月 28 日单票场次 `1035758`，普通入场券票档 `960028`，票面 `19800` 分。该快照用于预填本场模板；执行时仍须重新核对场次日期、票档名称和金额。

## 来源与请求

- [官方活动页面](https://show.bilibili.com/platform/detail.html?id=1006859)
- 详情接口：`POST https://mall.bilibili.com/mall-search-items/items_detail/info`
- 该端点及请求字段来自本仓库 `interface/project.py` 的 `_fetch_project_payload_new`。
- 本次详情请求未发送 Cookie 或 Authorization，没有使用登录会话，没有调用订单准备或创建接口。
- 返回 **HTTP 200**，业务返回码读取结果为 **0**，`success` 为 **true**。

请求体：

```json
{
  "itemsId": 1006859,
  "itemsDetailPageType": 3
}
```

## 公开字段摘录

以下使用归档分组保留读取到的目标字段，不代表平台响应的完整外层结构。仅包含公开活动、场次和目标票档信息。

```json
{
  "observed_at": "2026-11-14T23:29:04+08:00",
  "http_status": 200,
  "business_code": 0,
  "success": true,
  "project": {
    "projectId": 1006859,
    "itemsId": 1006859,
    "projectName": "杭州·绝区零 2026FES 布梦嘉年华"
  },
  "screen": {
    "id": 1035758,
    "name": "【2026年11月28日 单票】",
    "start_time": 1795822200,
    "start_time_str": "2026-11-28",
    "sale_start": 1794736800
  },
  "ticket": {
    "id": 960028,
    "desc": "普通入场券",
    "price": 19800,
    "sale_start": "2026-11-15 18:00:00",
    "saleStart": 1794736800,
    "sale_flag_number": 1,
    "clickable": false
  }
}
```

`sale_flag_number=1` 和 `clickable=false` 仅是读取时的售卖状态，**不是库存数量**，也不能证明已售罄、还剩几张或后续一定有票。该快照没有验证任何账号的资格、实际订单或付款结果。

本场模板已预填这组公开目标参数；账号 UID、两账号对应的购票人 ID 和回流结束时刻仍在本机填写。公开页面的后续变更以运行时校验为准，不能因已有快照而跳过核对。
