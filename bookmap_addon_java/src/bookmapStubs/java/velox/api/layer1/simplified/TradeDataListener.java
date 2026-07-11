package velox.api.layer1.simplified;

import velox.api.layer1.data.TradeInfo;

/** Compile-time stub matching Bookmap API 7.6.0.20. */
public interface TradeDataListener {
    void onTrade(double price, int size, TradeInfo tradeInfo);
}
