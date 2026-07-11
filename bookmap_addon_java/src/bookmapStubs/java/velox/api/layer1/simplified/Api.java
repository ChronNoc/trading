package velox.api.layer1.simplified;

/** Compile-time stub for the methods used from Bookmap API 7.6.0.20. */
public interface Api {
    <T> void setSettings(T settings);

    <T> T getSettings(Class<? extends T> settingsClass);

    void addTimeListeners(TimeListener listener);

    void addDepthDataListeners(DepthDataListener listener);

    void addTradeDataListeners(TradeDataListener listener);

    void addHistoricalModeListeners(HistoricalModeListener listener);
}
