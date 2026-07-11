package velox.api.layer1.data;

/** Compile-time stub matching the fields used from Bookmap API 7.6.0.20. */
public class InstrumentInfo extends InstrumentCoreInfo {
    public static final long UNKNOWN_DELAY = -1L;
    public final double pips;
    public final double multiplier;
    public final String fullName;
    public final boolean isFullDepth;
    public final double sizeMultiplier;
    public final boolean isCrypto;
    public final String recordingTag;
    public final boolean isApiProtected;
    public final boolean isNbboSupported;
    public final long dataDelay;
    public final String requestedSymbol;

    public InstrumentInfo(
            String symbol,
            String exchange,
            String type,
            double pips,
            double multiplier,
            String fullName,
            boolean isFullDepth,
            double sizeMultiplier) {
        super(symbol, exchange, type);
        this.pips = pips;
        this.multiplier = multiplier;
        this.fullName = fullName;
        this.isFullDepth = isFullDepth;
        this.sizeMultiplier = sizeMultiplier;
        this.isCrypto = false;
        this.recordingTag = "";
        this.isApiProtected = false;
        this.isNbboSupported = false;
        this.dataDelay = UNKNOWN_DELAY;
        this.requestedSymbol = symbol;
    }
}
