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

## Personal goal integration

The separate [personal goal entry point](personal-goal.md) now adds a durable
SQLite attendee ledger, submission intents, cross-account reservations, explicit
group-size reduction, fixed target/price checks, and a returning-stock loop. Its
bounded adapter reuses the prepare/create helpers and this transport protection.

The legacy `Buy`/UI entry point is still a single-task flow. Its per-run status
files are not the shared goal ledger, and its `100034` price-update branch remains
unchanged. The personal-goal adapter stops on a price change and does not use
`local_fanout`. Use the goal entry point for coordinated multi-account behavior.

The goal ledger preserves unknown submissions on restart. Automatic recognition
is currently limited to known, durably bound order IDs with sufficient matching
platform fields. Unknown candidates and insufficient query schemas require an
explicit local official-order check; see the [API evidence](goal-api-evidence.md).

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
