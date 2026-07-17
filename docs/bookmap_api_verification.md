# Bookmap API semantics — verified against the installed jars

Verified **2026-07-17** against the **installed Bookmap 7.7.0 build 22** jars
(`C:\Program Files\Bookmap\lib\bm-l1api.jar`,
`bm-simplified-api-wrapper.jar`) by decompiling with
`javap -p -c` (JDK 17.0.19). This is primary evidence from the exact binaries the
add-on compiles against — not documentation, memory, or guesswork.

## 1. `TradeInfo.isBidAggressor` — true means BUY (aggressive buyer)

**Evidence.** `velox.api.layer1.simplified.InstanceWrapper.onTrade(...)` reads
`TradeInfo.isBidAggressor` and passes it to
`velox.api.layer1.simplified.Bar.addTrade(boolean, long, double)`.
Decompiling `Bar.addTrade`:

```
58: iload_1                      // isBidAggressor
59: ifeq          89             // false -> jump to 89
62: ...
64: getfield      volumeBuy:J    // TRUE  -> volumeBuy  += size
...
89: ...
91: getfield      volumeSell:J   // FALSE -> volumeSell += size
```

Bookmap's own bar aggregation counts `isBidAggressor == true` as **buy** volume.

**Conclusion.** `TradeSideMapper.fromBidAggressor(true) -> "buy"` is **CORRECT**.
CVD, aggressive-buy/sell bubbles, absorption, and every direction-dependent rule
are therefore **not inverted**. Pinned by
`tests/test_bookmap_semantics.py` and the Java `TestRunner` mapper test.

## 2. Simplified `onTrade(double price, ...)` — price is in PIP units

**Evidence.** In `InstanceWrapper.onTrade(String, double, int, TradeInfo, boolean)`
the dispatch to the listener is:

```
147: aload   9                   // TradeDataListener
149: dload_2                     // the raw L1 `double price`, UNMODIFIED
150: iload   4                   // size
152: aload   5                   // TradeInfo
154: invokeinterface TradeDataListener.onTrade:(DILvelox/api/layer1/data/TradeInfo;)V
```

There is **no `dmul`** (no pip conversion) anywhere on this path: the simplified
API forwards the raw Layer-1 price unchanged, and Layer-1 trade prices are
pip-denominated (as are `onDepth`'s `int` prices).

**Corroboration from real data.** MNQ `pips = 0.25`. Recorded sessions contain
prices ~29,268–29,500 — i.e. `rawPrice * 0.25`. Had the simplified API delivered
real prices, our conversion would have produced ~7,375. It does not.

**Conclusion.** `PriceConverter.toPriceString` multiplying by `pips` is **CORRECT
for both** the `int` depth callback and the `double` trade callback.

## Scope / limits

- Verified: aggressor semantics, trade/depth price units.
- NOT re-verified this pass: initial depth-state semantics, historical/delayed
  mode boundaries, and reconnect boundaries (see `docs/IMPLEMENTATION_STATE.md`).
