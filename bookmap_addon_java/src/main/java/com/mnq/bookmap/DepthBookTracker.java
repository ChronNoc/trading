package com.mnq.bookmap;

import java.util.HashMap;
import java.util.Map;

/** Tracks previous market-by-price depth size by side and price. */
public final class DepthBookTracker {
    private final Map<DepthKey, Integer> sizes = new HashMap<>();

    public DepthUpdate update(boolean isBid, String price, int newSize) {
        String side = isBid ? "bid" : "ask";
        DepthKey key = new DepthKey(side, price);
        int previousSize = sizes.getOrDefault(key, 0);
        if (newSize == 0) {
            sizes.remove(key);
        } else {
            sizes.put(key, newSize);
        }
        return new DepthUpdate(side, price, previousSize, newSize);
    }

    public int sizeAt(String side, String price) {
        return sizes.getOrDefault(new DepthKey(side, price), 0);
    }

    private record DepthKey(String side, String price) {
    }
}
