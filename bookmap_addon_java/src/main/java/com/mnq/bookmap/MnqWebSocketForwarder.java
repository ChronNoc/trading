package com.mnq.bookmap;

import java.util.concurrent.TimeUnit;
import velox.api.layer1.annotations.Layer1ApiPublic;
import velox.api.layer1.annotations.Layer1ApiVersion;
import velox.api.layer1.annotations.Layer1ApiVersionValue;
import velox.api.layer1.annotations.Layer1SimpleAttachable;
import velox.api.layer1.annotations.Layer1StrategyName;
import velox.api.layer1.data.InstrumentInfo;
import velox.api.layer1.data.TradeInfo;
import velox.api.layer1.simplified.Api;
import velox.api.layer1.simplified.CustomModule;
import velox.api.layer1.simplified.DepthDataListener;
import velox.api.layer1.simplified.HistoricalModeListener;
import velox.api.layer1.simplified.InitialState;
import velox.api.layer1.simplified.TimeListener;
import velox.api.layer1.simplified.TradeDataListener;

/** Loadable Bookmap L1/Simplified add-on that forwards MNQ market data locally. */
@Layer1ApiPublic
@Layer1SimpleAttachable
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
@Layer1StrategyName(value = "MNQ WebSocket Forwarder", localizationKey = "mnq.websocket.forwarder")
public final class MnqWebSocketForwarder implements CustomModule, DepthDataListener, TradeDataListener, TimeListener, HistoricalModeListener {
    private volatile long latestTimestampNs = TimeUnit.MILLISECONDS.toNanos(System.currentTimeMillis());
    private volatile ForwarderRuntime runtime;

    @Override
    public void initialize(String alias, InstrumentInfo instrumentInfo, Api api, InitialState initialState) {
        if (initialState != null && initialState.getCurrentTime() > 0) {
            latestTimestampNs = initialState.getCurrentTime();
        }
        ForwarderSettings settings = api.getSettings(ForwarderSettings.class);
        if (settings == null) {
            settings = new ForwarderSettings();
            api.setSettings(settings);
        }
        BridgeConfig config = BridgeConfig.fromSettings(settings);
        InstrumentContext instrument = InstrumentContext.from(alias, instrumentInfo, config);
        runtime = new ForwarderRuntime(config, instrument, new WebSocketTransport());
        runtime.start();
        runtime.publishConnected();
        api.addTimeListeners(this);
        api.addDepthDataListeners(this);
        api.addTradeDataListeners(this);
        api.addHistoricalModeListeners(this);
    }

    @Override
    public void stop() {
        ForwarderRuntime activeRuntime = runtime;
        runtime = null;
        if (activeRuntime != null) {
            activeRuntime.close();
        }
    }

    @Override
    public void onTimestamp(long timestamp) {
        if (timestamp > 0) {
            latestTimestampNs = timestamp;
        }
    }

    @Override
    public void onDepth(boolean isBid, int price, int size) {
        ForwarderRuntime activeRuntime = runtime;
        if (activeRuntime != null) {
            activeRuntime.publishDepth(latestTimestampNs, isBid, price, size);
        }
    }

    @Override
    public void onTrade(double price, int size, TradeInfo tradeInfo) {
        ForwarderRuntime activeRuntime = runtime;
        if (activeRuntime != null) {
            boolean isBidAggressor = tradeInfo != null && tradeInfo.isBidAggressor;
            activeRuntime.publishTrade(latestTimestampNs, price, size, isBidAggressor);
        }
    }

    @Override
    public void onRealtimeStart() {
        ForwarderRuntime activeRuntime = runtime;
        if (activeRuntime != null) {
            activeRuntime.publishRealtimeStarted();
        }
    }
}
