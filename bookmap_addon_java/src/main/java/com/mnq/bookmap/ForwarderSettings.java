package com.mnq.bookmap;

import velox.api.layer1.simplified.Parameter;

/** Bookmap user settings for the MNQ forwarder. */
public final class ForwarderSettings {
    public String websocketUrl = BridgeConfig.DEFAULT_URL;
    public String allowedSymbol = BridgeConfig.DEFAULT_ALLOWED_SYMBOL;

    @Parameter(name = "Allow non-loopback URL", minimum = 0, maximum = 1, step = 1)
    public boolean allowNonLoopback = false;

    @Parameter(name = "Allow non-MNQ symbols", minimum = 0, maximum = 1, step = 1)
    public boolean allowAnySymbol = false;

    @Parameter(name = "Queue capacity", minimum = 100, maximum = 100000, step = 100)
    public int queueCapacity = 10_000;
}
