package velox.api.layer1.simplified;

import velox.api.layer1.data.InstrumentInfo;

/** Compile-time stub matching Bookmap API 7.6.0.20. */
public interface CustomModule {
    void initialize(String alias, InstrumentInfo instrumentInfo, Api api, InitialState initialState);

    void stop();
}
