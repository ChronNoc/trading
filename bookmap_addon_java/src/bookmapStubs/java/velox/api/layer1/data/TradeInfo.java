package velox.api.layer1.data;

import java.io.Serializable;

/** Compile-time stub matching the fields used from Bookmap API 7.6.0.20. */
public class TradeInfo implements Serializable {
    public final boolean isOtc;
    public final boolean isBidAggressor;
    public final boolean isExecutionStart;
    public final boolean isExecutionEnd;
    public final String aggressorOrderId;
    public final String passiveOrderId;

    public TradeInfo(boolean isOtc, boolean isBidAggressor) {
        this(isOtc, isBidAggressor, false, false, "", "");
    }

    public TradeInfo(
            boolean isOtc,
            boolean isBidAggressor,
            boolean isExecutionStart,
            boolean isExecutionEnd,
            String aggressorOrderId,
            String passiveOrderId) {
        this.isOtc = isOtc;
        this.isBidAggressor = isBidAggressor;
        this.isExecutionStart = isExecutionStart;
        this.isExecutionEnd = isExecutionEnd;
        this.aggressorOrderId = aggressorOrderId;
        this.passiveOrderId = passiveOrderId;
    }
}
