package com.mnq.bookmap;

import java.net.URI;
import java.util.Locale;
import velox.api.layer1.settings.StrategySettingsVersion;
import velox.api.layer1.simplified.Parameter;

/** Bookmap user settings for the MNQ forwarder. */
@StrategySettingsVersion(currentVersion = ForwarderSettings.SETTINGS_VERSION, compatibleVersions = {})
public final class ForwarderSettings {
    public static final int SETTINGS_VERSION = 2;
    public static final int MIN_QUEUE_CAPACITY = 100;
    public static final int MAX_QUEUE_CAPACITY = 100_000;

    public String websocketUrl = BridgeConfig.DEFAULT_URL;
    public String allowedSymbol = BridgeConfig.DEFAULT_ALLOWED_SYMBOL;

    @Parameter(name = "Allow non-loopback URL", minimum = 0, maximum = 1, step = 1)
    public boolean allowNonLoopback = false;

    @Parameter(name = "Allow non-MNQ symbols", minimum = 0, maximum = 1, step = 1)
    public boolean allowAnySymbol = false;

    @Parameter(name = "Queue capacity", minimum = 100, maximum = 100000, step = 100)
    public int queueCapacity = 50_000;

    public ForwarderSettings() {
    }

    public static ForwarderSettings sanitized(ForwarderSettings settings) {
        ForwarderSettings safe = settings == null ? new ForwarderSettings() : settings;
        String safeUrl = safeWebSocketUrl(safe.websocketUrl, safe.allowNonLoopback);
        if (BridgeConfig.DEFAULT_URL.equals(safeUrl)) {
            safe.allowNonLoopback = false;
        }
        safe.websocketUrl = safeUrl;
        safe.allowedSymbol = safeSymbol(safe.allowedSymbol);
        safe.queueCapacity = safeQueueCapacity(safe.queueCapacity);
        return safe;
    }

    private static String safeWebSocketUrl(String value, boolean allowNonLoopback) {
        try {
            URI uri = URI.create(value == null ? "" : value.trim());
            if (EndpointPolicy.isAllowed(uri, allowNonLoopback)) {
                return uri.toString();
            }
        } catch (IllegalArgumentException error) {
            // Fall through to the local default.
        }
        return BridgeConfig.DEFAULT_URL;
    }

    private static String safeSymbol(String value) {
        String normalized = value == null ? "" : value.trim().toUpperCase(Locale.ROOT);
        return normalized.isEmpty() ? BridgeConfig.DEFAULT_ALLOWED_SYMBOL : normalized;
    }

    private static int safeQueueCapacity(int value) {
        if (value < MIN_QUEUE_CAPACITY) {
            return MIN_QUEUE_CAPACITY;
        }
        return Math.min(value, MAX_QUEUE_CAPACITY);
    }
}
