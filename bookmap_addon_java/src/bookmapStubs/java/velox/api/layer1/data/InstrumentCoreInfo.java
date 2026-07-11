package velox.api.layer1.data;

import java.io.Serializable;

/** Compile-time stub matching the fields used from Bookmap API 7.6.0.20. */
public class InstrumentCoreInfo implements Serializable {
    public final String symbol;
    public final String exchange;
    public final String type;

    public InstrumentCoreInfo(String symbol, String exchange, String type) {
        this.symbol = symbol;
        this.exchange = exchange;
        this.type = type;
    }
}
