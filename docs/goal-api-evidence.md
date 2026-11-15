# 双账号三人目标：账号适配器的接口证据与边界

研究和离线验证日期：2026-11-14，2026-11-15 更新实际尝试路径。对应实现为 `task/goal_adapter.py`，故障用例为 `tests/test_goal_adapter.py`。

## 本轮完成了什么

适配器提供一个账号的一次有界尝试。协调器负责决定哪个账号尝试哪一组人、等待多久以及何时停止。明确验证成功的 UID 和逐人购票映射在同一账号实例内最多复用 30 秒，使用 monotonic 时钟，失败后清除，重启不保留。每次仍重新读取固定目标的日期、票档和价格，为本次人组重新 prepare，最后最多调用一次 create 请求逻辑。默认客户端按账号独立创建，复用本仓库的标准 `BiliRequest` 和 `buy_helpers`。

这一版采用串行、有界的请求流程。已减少刚通过身份校验后的冗余读取，并调整订单轮询与剩余人组的调度顺序；尚未测量用户 Mac 上的实际接口延迟，没有“双账号并发使成功率加倍”的验证或承诺。[本轮审查记录](goal-readiness-review.md)给出原版与新版的实际离线调用数量对照；它不能证明热门场次一定能买到。

显式 `preflight()` 强制新读登录、固定目标及全员购票人信息；`preflight(person_keys=...)` 可限定在线人员范围，空元组只核对登录和目标。恢复初始化使用账本中尚可购买的人组，实际 attempt 只核验本批人员，避免已购人员的在线资格提示阻塞剩余人。预检不调用 prepare 或 create；新版详情查询虽然使用 HTTP POST，但它读取的是商品详情。`attempt(person_keys, before_submit)` 在 prepare 成功且最终载荷核对完成后、create POST 之前调用回调。回调异常直接传播，保证停止或无法落账时不提交。付款页提示由上层处理；适配器拿到可信订单号即返回，不等待二维码、浏览器或通知步骤。

本轮没有使用用户 Cookie，没有提交真实订单，没有完成真实付款。测试内账号、订单、票档和实名信息均为合成数据；它们不是目标 FES 的已核实 ID 或现场响应。

## 请求路径及来源

| 用途 | 适配器使用的路径 | 实际可核对的来源 |
| --- | --- | --- |
| 当前登录身份 | `GET https://api.bilibili.com/x/web-interface/nav` | 本仓库现有 Cookie / 登录逻辑；要求 `code=0`、`data.isLogin=true`、`data.mid` 与配置 UID 一致 |
| 活动与场次详情 | `POST https://mall.bilibili.com/mall-search-items/items_detail/info`；必要时原有旧接口 `GET https://show.bilibili.com/api/ticket/project/getV2` | 本仓库 `interface/project.py` 中的详情查询和字段归一化 |
| 当前账号购票人 | `GET https://show.bilibili.com/api/ticket/buyer/list?is_default&projectId=...` | 本仓库 `interface/project.py` 的购票人读取 |
| 准备订单 | `POST https://show.bilibili.com/api/ticket/order/prepare?project_id=...` | 本仓库 `task/buy.py`、`task/buy_helpers.py` |
| 创建订单 | `POST https://show.bilibili.com/api/ticket/order/createV2?project_id=...` | 本仓库 `task/buy_helpers.py`；使用已有令牌与载荷构建逻辑 |
| 精确订单详情 | `GET https://show.bilibili.com/api/ticket/order/info?order_id=...` | BHYG Legacy 的 `order_status`，Amorter 的订单详情函数，见下方固定提交链接 |
| 订单列表备选读取 | `GET https://show.bilibili.com/api/ticket/order/list?page=0&page_size=20` | Amorter 的列表实现与 BiliTicketRush 的列表结构，见下方链接 |

现有本仓库代码的固定基线可见：[项目详情读取](https://github.com/KanoTime/biliTickerBuy/blob/d0481d64f920ec5877d7f5d9e025c9afd6dddedd/interface/project.py)、[下单辅助逻辑](https://github.com/KanoTime/biliTickerBuy/blob/d0481d64f920ec5877d7f5d9e025c9afd6dddedd/task/buy_helpers.py)、[业务错误码](https://github.com/KanoTime/biliTickerBuy/blob/d0481d64f920ec5877d7f5d9e025c9afd6dddedd/util/ErrorCodes.py)。

这些是公开实现的源代码证据，并非平台对外发布的稳定 API 合同。不能据此保证接口在实际开票时仍保持同样的字段或权限。

### 原有详情回退的额外保护

原有 `fetch_project_payload` 会捕获新版详情请求的异常再尝试旧接口。目标适配器增加一层只读保护：遇到 HTTP 429 或 HTTP 412 后，该轮不再实际发送旧接口请求。429 的 `Retry-After` 传给协调器；412 需要在官方页面处理。离线测试直接调用原有详情 helper，验证了这两种情况不会被回退逻辑吞掉。

普通暂态网络错误、5xx，以及 HTTP 200 内未能完成只读查询的业务错误或缺少结构，可以以 `PreflightResult.retryable` 返回，初始化不会仅因这次读取失败而永久停用账号。明确未登录或需验证、价格/日期/票名/UID/购票人映射矛盾则停止相应账号或目标。任何无法核实的读取都不会进入 prepare/create。

## 下单前必须核对的内容

在线读取与本地固定配置同时约束提交：活动 ID、场次 ID、SKU ID、完整入场日期、票档名称、以分计的单价及本次人数必须一致。日期只接受完整日期或时间戳；不把缺少年份的“11月28日”猜成目标年份。额外配送费与固定金额不一致时停止。

本次人组中的每个本地人员别名必须已映射到该账号真实返回的唯一购票人 ID，实际姓名和证件字段须与本地导出一致；成功结果最多复用 30 秒。返回 `accountId` 时还核对它与账号 UID；返回本批购票人的错误码时要求为 0。初始配置仍校验完整三人的跨账号身份绑定，在线资格校验不扩散到当前未购买的人。日志和结果仅给出人员别名和差错类型，不输出 Cookie、姓名、证件号、电话或响应正文。

每次组成人数变化都从 `GoalAccount.payload_for()` 重新生成载荷并重新 prepare。提交前再核对目标、人组、人数、总价，确保旧 token 或旧组三人的金额不会被带入一人、两人的请求。只有当前程序成功 create 返回的有效订单号，才构成后续自动订单核验的已知 ID。

## 创建结果的保守分类

| 平台结果 | 本版本行为 | 对人数调整的含义 |
| --- | --- | --- |
| `errno/code=0` 且含有效订单号 | `created`，立即返回订单号等待付款核验 | 已创建人组保持占用，不能重复补买 |
| `100009` | `shortage`，明确所选数量库存不足 | 协调器可以按约定缩小人组 |
| `100001` | `retry`，保持原人数退避 | 现有错误表同时涵盖无票与登录异常，不能用来推断库存只剩一两张 |
| `100034` | 固定目标暂停 | 不自动接受新价格，不改金额再次提交 |
| `100044` 或明确登录问题 | 账号暂停，交由官方页面验证 | 不自动解验证码 |
| `100016` / `100017` | 当前不可售，返回 `retry` 并使该账号等待 5 秒，再读固定目标 | 不把当前不可售解释成永久结束或剩余库存数量，不缩小人组 |
| `100039` | 明确活动结束，暂停目标 | 不继续创建 |
| 重复、未完成或限购订单提示 `100003/100048/100079` | `review`，暂停并核对订单 | 无论 prepare 或 create 阶段，都可能意味着此前已有占用，不能释放同组人员给另一个账号 |
| 已知繁忙、频率或 prepare 过期拒绝码 | 有界返回 `retry`，由协调器退避 | 适配器内部不自旋提交 |
| 结果超时、未知业务码、成功码无订单号、异常 JSON 或可能已送达 | `unknown` | 保留人员占用，核对后再处理 |

该表中业务码的已知含义来自本仓库现有错误表和下单处理逻辑。任何非成功创建响应如果同时带有效订单号，也按结果未知处理，不能忽略订单号继续抢。非成功响应的订单号仅作为供人工核对的候选号，不能当作已证实属于固定目标及本次人组的订单号。标准请求层明确证明为发送前连接失败时才可安全重试；发送状态不明的连接错误必须冻结为未知。每次重试仍需新一轮 prepare 和提交前回调。

## 自动订单核验的两条证据链

### A. 本程序已取得并持久化绑定的订单号

仅当本地记录处于 `pending`、属于当前账号、含有效订单号及已知人组，才尝试自动核验。确认 UID 的成功短缓存仍有效，或重新读取 UID 后，查询精确订单详情；详情结构不足时最多读取第一页 20 条订单，且只考虑唯一的同一订单号。订单状态本身每次新读，不受身份缓存影响。协调器每轮最多核验一笔并轮换，下一次间隔从查询完成时计算；已有可购买的人组优先尝试。

核验至少要求订单 ID、活动 ID、场次 ID、人数和实际应付金额全部匹配。返回 UID、SKU、入场时间或总金额时还要核对这些字段，任何矛盾均不接受。具体购票人及 SKU 的绑定来自这次成功 create 的本地持久记录，不从列表中的人数相同或标题相似推断。

状态只接受以下已知组合：

| 数据组合 | 结果 |
| --- | --- |
| `status=1` 且 `sub_status=1` | `pending` |
| `status=2`，并有 `refund_status=0`、正数 `pay_time` | `paid` |
| `status=4`，并有 `refund_status=0`、`pay_time=0` | `closed` |

`status` 的 1/2/4 解释可在 BHYG 与 BiliTicketRush 的实现中交叉核对。`refund_status`、`pay_time` 的字段存在可在 Amorter 的模型核对；这里对值的要求是额外的保守一致性条件，不能当作已经在当前活动认证回包上验证过的通用规则。字段缺失、字符串格式不同、退款状态或新的业务状态都会保留为未核验，避免猜测。

`reconcile()` 返回未核验 `unknown` 仅表示这轮没有足够的新证据。协调器必须保留已知待付款订单的占用，不能据此把 `pending` 改成可以重买。对于当前平台回包不满足此严格结构的情况，应走本机人工核对；不应为了“自动化率”放宽人组归属和状态证据。

### B. create 可能送达，但没有可信订单号

这种记录保持 `unknown` / 人工复核。已找到的 BiliTicketRush 列表模型没有具体购票人列表或 SKU 字段，不能证明某个列表候选就是刚才那一组人的订单。因此适配器不会自动认领候选订单，也不会因列表为空而宣称未下单。

同理，订单在第一页没有出现不等于不存在；本实现不扫描账号历史以猜测身份。恢复购买必须依靠后续可信的具体订单证据，或上层明确标记来源的本机人工确认流程。

## 订单查询的固定源代码证据

1. [BiliTicketRush：订单列表数据结构，提交 `cccbd6ea`](https://github.com/Violiate/bili_ticket_rush/blob/cccbd6ea6c788f010445b8d4b45aff5d7a2e2082/common/src/show_orderlist.rs)。可以核对 `order_id`、`item_id`、`item_info.screen_id`、`count`、`pay_money`、`status`、`sub_status`；此模型不足以把未知订单关联到具体人组。
2. [BiliTicketRush：订单列表界面状态判断，同一提交](https://github.com/Violiate/bili_ticket_rush/blob/cccbd6ea6c788f010445b8d4b45aff5d7a2e2082/frontend/src/windows/show_orderlist.rs)。用 2 表示已付款/已完成、4 表示取消、1 与子状态 1 表示待付款。
3. [BHYG Legacy：`order_status`，提交 `aa2653d1`](https://github.com/ZianTT/BHYG_Legacy/blob/aa2653d12a9b9f5ea908e8baf70410d0da5bea9d/api.py)。直接展示精确订单详情路径与 1/2/4 的判断；历史实现，不能替代当前平台验证。
4. [Amorter/bili_ticket：Order 模型与详情/列表函数，提交 `b36a8a79`](https://github.com/Amorter/bili_ticket/blob/b36a8a798f1eb859a3907cf2cd458202c7c96ef4/bili_lib/src/lib.rs)。可核对列表分页路径，以及 `uid`、`refund_status`、`pay_time` 等字段的存在。

## 尚未完成的现场验证

2026-11-14 对公开订单详情页面 `https://show.bilibili.com/platform/orderDetail.html` 的一次未登录只读访问返回 HTTP 412。研究停留在这一边界，没有绕过验证，也没有拿到当前账号的认证订单 JSON。

下一步应在用户 Mac 上以真实账号运行只读预检，核对实际 UID、11 月 28 日普通票的真实 ID、单价及两个账号的三人映射。真实订单的自动 `paid` / `closed` 识别需要用允许读取的当前账号回包确认字段；在此之前只能准确描述为“适配器及离线故障测试已实现”，不能称为“本活动已实测通过”或“保证能自动补齐三张”。

本轮离线测试覆盖了目标和身份漂移、三人到两人/一人重新 prepare、回调阻止发送、关键业务码、未知结果不重放、429/412 不继续回退、账号 Cookie 隔离、精确已知订单匹配及空列表不释放。测试可以在不连接平台、不提交真实订单的条件下运行：

```bash
python -m pytest -q tests/test_goal_adapter.py tests/test_goal_config.py
```
