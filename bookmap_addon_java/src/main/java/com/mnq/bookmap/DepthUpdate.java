package com.mnq.bookmap;

/** Normalized depth update from Bookmap before JSON serialization. */
public record DepthUpdate(String side, String price, int previousSize, int newSize) {
}
