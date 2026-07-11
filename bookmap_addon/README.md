# DEPRECATED: Python Bookmap WebSocket Forwarder

This unverified Python adapter has been replaced by the loadable Java L1/Simplified
Bookmap add-on in `bookmap_addon_java/`. Keep this folder only as a historical
message-format reference and for older isolated parser tests. New Bookmap setup
should use:

```text
bookmap_addon_java\build\libs\mnq-bookmap-forwarder-all.jar
```

# MNQ Bookmap WebSocket Forwarder

This is a minimal Bookmap-side bridge. It listens for Bookmap depth and trade callbacks, formats events with the exact Task 5 schemas, and forwards them to the Python app over a local WebSocket.

Default endpoint:

```text
ws://127.0.0.1:8765/bookmap
```

## Event Schemas

Depth update:

```json
{
  "type": "depth_update",
  "timestamp": 123456789,
  "symbol": "MNQ",
  "side": "bid",
  "price": "100.25",
  "previous_size": "7",
  "new_size": "12"
}
```

Trade:

```json
{
  "timestamp_ns": 123456790,
  "price": "100.25",
  "size": "4",
  "aggressor_side": "sell",
  "instrument": "MNQ",
  "sequence_id": 42
}
```

Prices and sizes are serialized as strings so the receiving app can convert them to `Decimal` without going through float math.

## Manual Bookmap Setup

1. In the Python environment used by Bookmap's Python add-on runner, install the runtime dependency:

   ```powershell
   python -m pip install websockets
   ```

2. Copy the `bookmap_addon/` folder into the location Bookmap expects for Python add-ons.

3. In Bookmap, open the add-ons manager from the UI, load the Python add-on, and point it at `bookmap_addon.addon:create_addon` or the included `addon.json`, depending on the loader style exposed by your Bookmap build.

4. Configure the add-on for instrument `MNQ` and WebSocket URL `ws://127.0.0.1:8765/bookmap`.

5. Start the receiving Python app's local WebSocket server before enabling the add-on. The forwarder retries if the receiver is not available, but it does not persist events across Bookmap restarts.

6. Enable the add-on only in observe/data-forwarding mode. This bridge does not submit orders and does not contain broker credentials.

## Bookmap API Notes

Bookmap's Python add-on API is alpha-stage. The public API reference pages were not available in this workspace, so the Bookmap-specific integration point is intentionally isolated in `addon.py`.

The adapter exposes these callback-style methods:

- `on_depth_update(timestamp, side, price, new_size, previous_size=None, symbol=None)`
- `on_market_depth(timestamp, is_bid, price, size, symbol=None)`
- `on_trade(timestamp_ns, price, size, aggressor_side, instrument=None, sequence_id=None)`
- `on_trade_event(timestamp_ns, price, size, aggressor_side, instrument=None, sequence_id=None)`

If your installed Bookmap docs use different callback names or positional arguments, keep `events.py` and `websocket_forwarder.py` unchanged and adjust only `addon.py` to map Bookmap's callback objects into `format_depth_update(...)` and `format_trade(...)`.

## Local Testing

The repository tests only validate message formatting, parsing, and adapter forwarding with an in-memory publisher. They do not open Bookmap or connect to a real WebSocket.
