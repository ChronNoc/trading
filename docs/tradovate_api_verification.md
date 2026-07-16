# Tradovate API verification record

Retrieved **2026-07-16** from Tradovate's official example repository
(`github.com/tradovate/example-api-js`, raw tutorial READMEs). The interactive
docs SPA at `api.tradovate.com` serves no static content, so the official
tutorial sources were used.

## Verified (official sources)

| Item | Verified value | Source |
|---|---|---|
| Auth endpoint | `POST {base}/auth/accesstokenrequest` | `tutorial/Access/EX-0-Access-Start/README.md` |
| Auth body fields | `name`, `password`, `appId`, `appVersion`, `cid`, `sec` | same |
| REST environments | demo `https://demo.tradovateapi.com/v1`, live `https://live.tradovateapi.com/v1` (demo constant already present in `app/execution/orders.py`; live used ONLY by the locked live gateway) | repo constants (`DEMO_URL`/`LIVE_URL` indirection) |
| WS frame types | `'o'` open, `'h'` server heartbeat, `'a'` JSON array payload, `'c'` closed | `tutorial/WebSockets/EX-05-WebSockets-Start/README.md` |
| WS authorize | text frame `authorize\n{id}\n\n{accessToken}`; success reply `a[{"s":200,"i":{id}}]` | same |
| Client heartbeat | send the literal string `[]` when ≥ **2500 ms** elapsed since the last message (elapsed-time based, not a fixed timer) | `tutorial/WebSockets/EX-06-Heartbeats/README.md` |
| WS request format | `{url}\n{id}\n{query}\n{body}` — endpoint URL has **no leading slash** (e.g. `executionReport/list\n4\n\n`) | `tutorial/WebSockets/EX-07-Making-Requests/README.md` |
| WS response shape | JSON objects with `s` (HTTP-style status), `i` (request id), `d` (data) | same |
| User-data subscription | initial sync response carries a `users` payload (accounts, positions, orders, ...); subsequent real-time updates are `{entity, entityType, eventType}` objects | same (`socket.subscribe` tutorial) |
| Order REST endpoints | `order/placeOrder`, `order/cancelOrder`, `order/modifyOrder`, `order/placeOSO`, `order/list`, `order/item`, `position/list`, `account/list`, `cashBalance/getcashbalancesnapshot` | endpoint naming per tutorials + existing tested demo module |

## Referenced but NOT independently verified this session

- `auth/renewaccesstoken` (token renewal): referenced by Tradovate materials but
  the exact response contract was not captured from an official page this
  session. The implementation therefore treats **re-authentication via
  `auth/accesstokenrequest` as the verified renewal path** and calls
  `renewaccesstoken` only opportunistically (falling back to full re-auth).
- `user/syncrequest` body details: the subscription flow is verified; the exact
  body (`{"users": [userId]}`) follows the official example repo's socket
  `subscribe({url: 'user/syncrequest', body: {users: [...]}})` pattern.
- Rate limits and penalty windows: Tradovate documents time-penalty responses
  (`p-ticket`/`p-time`); handled defensively (backoff on non-200) but exact
  thresholds were not captured. **LIVE stays blocked regardless.**

## Order-state transitions implemented

`PendingNew → Working → PartiallyFilled → Filled | Canceled | Rejected | Expired`
with partial-fill accumulation, weighted-average fill price, cancel/replace
(modify), fill-vs-cancel races, and post-ambiguity REST snapshot reconciliation
(see `app/execution/user_sync.py` and `app/execution/order_lifecycle.py`).
