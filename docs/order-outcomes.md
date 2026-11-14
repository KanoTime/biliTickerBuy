# Order outcome reliability

This change records the outcome of an order attempt before payment helpers and
notifications run. A valid order ID is evidence that an order was created; it is
not evidence of payment, admission entitlement, or completion of a multi-person
purchase goal.

## Stream and runner contract

| Status | Meaning | Required next action |
| --- | --- | --- |
| `order_created` | A success response contained a valid positive decimal order ID. This purchase stream stops creating orders. | Check the official order and complete payment where required. |
| `order_result_unknown` | A create request may have been sent, but its response or order ID cannot be trusted. | Stop automatic creation and reconcile the official order state before another attempt. |
| `needs_order_review` | The platform reported a purchase limit, an unfinished order, or a duplicate order (`100003`, `100048`, `100079`). | Check the relevant official orders and eligibility; do not count the response as purchased tickets. |

Runners consume structured stream state, including events that have no log
message. Text such as “抢票成功” is no longer a success detector. The old
`succeeded` and `duplicate_order` strings remain recognized when reading existing
managed-run records; new purchase streams emit the statuses above.

For a known order, the order ID is emitted before retrieving payment information,
opening a browser, displaying a QR code, or sending notifications. Failures in
those optional steps retain the order outcome and do not re-enter creation.
Managed runners write the outcome to `status.json` before writing the event log.
Runner errors after an order outcome preserve it in the returned result.

`ok` describes whether a runner/API operation completed normally. In particular,
`ok: true` with `order_result_unknown` is not a successful purchase. Likewise,
generic `completed` means a stream ended, not that any ticket was paid for.

An order outcome and a process exit are different events: the process may still
be preparing payment information or delivering notifications. Task lifecycle
operations must preserve the order outcome while dealing with the actual process.

## Transport behavior

The protected route is the POST to
`https://show.bilibili.com/api/ticket/order/createV2`. Query parameters do not
change this classification. Other endpoints, including order preparation, keep
their existing recovery behavior.

For that create route, read/write/protocol/decode uncertainty stops transparent
replay and raises `BiliOrderResultUnknownError`. The default HTTPX transport can
retry a confirmed connection/pool failure once before any request was sent.
Custom clients or transport options are treated conservatively because their
exception names do not necessarily prove that nothing was sent.

This protects the wrapper's retry boundary. It does not make the server operation
idempotent and does not remove internal concurrent sends from the optional
`local_fanout` client. It is not a global exactly-once guarantee.

## Scope of this patch

The following are still separate implementation work:

- A durable purchase-goal ledger keyed by the real attendee and admission target,
  shared across account executors.
- Saving a submission intent before the network call, reconciling uncertain
  orders, and recovering safely after a process or machine restart.
- Coordinating multiple accounts so their submitted attendee sets never overlap.
- Trying the preferred group size first, then using only explicitly authorized
  smaller groups after a confirmed no-order stock shortage, with a fresh prepare
  for each account/person/count change.
- Automatically continuing the same goal for returning inventory and purchasing
  only attendees who are neither fulfilled nor reserved by an unresolved order.
- Fixed target and price checks. The existing `100034` branch can still update
  `pay_money` from the response; it has not been changed here.

The per-run status files introduced or updated by the runner are not that shared
goal ledger. Starting a second independent process or clearing those files is
not an order-reconciliation mechanism.

## Verification

The regression tests exercise the actual purchase stream with offline fake
requests and auxiliary services, the HTTP recovery boundary with fake clients,
and runner entry points with temporary run directories. They do not create live
platform orders or demonstrate real-event ticket availability.

```bash
python -m pytest -q \
  tests/test_buy_order_outcomes.py \
  tests/test_buy_prepare_token.py \
  tests/test_payment_result_fields.py \
  tests/test_notifier_exit_ordering.py \
  tests/test_execution_order_outcomes.py \
  tests/test_managed_order_lifecycle.py \
  tests/test_bili_request_h2_recovery.py \
  tests/test_bili_request_rate_limit.py \
  tests/test_bili_request_maintenance.py
ruff check .
ruff format --check .
git diff --check
```

Use Ruff 0.12.0, matching the repository's workflow. Full Gradio/Mac behavior,
account login, live order reconciliation, and paid-ticket outcomes require their
own verification before a personal coordinated purchasing version is ready.
