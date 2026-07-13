package com.mnq.bookmap;

import java.math.BigDecimal;
import java.math.MathContext;

/** Converts Bookmap pips-unit prices to real decimal-string wire prices.
 *  Bookmap's Simplified API reports prices as counts of InstrumentInfo.pips,
 *  so the real price is rawPrice MULTIPLIED by pips (MNQ: 119085 * 0.25 = 29771.25). */
public final class PriceConverter {
    private final BigDecimal pips;

    public PriceConverter(double pips) {
        if (!Double.isFinite(pips) || pips <= 0.0d) {
            throw new IllegalArgumentException("pips must be positive and finite");
        }
        this.pips = BigDecimal.valueOf(pips);
    }

    public String toPriceString(int rawPrice) {
        BigDecimal price = BigDecimal.valueOf(rawPrice).multiply(pips, MathContext.DECIMAL64);
        return price.stripTrailingZeros().toPlainString();
    }

    public String toPriceString(double rawPrice) {
        if (!Double.isFinite(rawPrice)) {
            throw new IllegalArgumentException("rawPrice must be finite");
        }
        BigDecimal price = BigDecimal.valueOf(rawPrice).multiply(pips, MathContext.DECIMAL64);
        return price.stripTrailingZeros().toPlainString();
    }
}
