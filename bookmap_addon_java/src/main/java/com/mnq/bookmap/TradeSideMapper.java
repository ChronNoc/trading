package com.mnq.bookmap;

/** Maps Bookmap trade aggressor flags to receiver-side aggressor labels. */
public final class TradeSideMapper {
    private TradeSideMapper() {
    }

    public static String fromBidAggressor(boolean isBidAggressor) {
        return isBidAggressor ? "buy" : "sell";
    }
}
