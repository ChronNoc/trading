package com.mnq.bookmap;

import java.math.BigDecimal;
import java.math.MathContext;

/** Converts Bookmap integer prices to decimal-string wire prices using InstrumentInfo.pips. */
public final class PriceConverter {
    private final BigDecimal pips;

    public PriceConverter(double pips) {
        if (!Double.isFinite(pips) || pips <= 0.0d) {
            throw new IllegalArgumentException("pips must be positive and finite");
        }
        this.pips = BigDecimal.valueOf(pips);
    }

    public String toPriceString(int rawPrice) {
        BigDecimal price = BigDecimal.valueOf(rawPrice).divide(pips, MathContext.DECIMAL64);
        return price.stripTrailingZeros().toPlainString();
    }
}
