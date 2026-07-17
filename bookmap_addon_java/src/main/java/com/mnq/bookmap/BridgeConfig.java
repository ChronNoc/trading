package com.mnq.bookmap;

import java.net.URI;
import java.time.Duration;
import java.util.Objects;

/** Runtime configuration for the Bookmap WebSocket forwarder. */
public final class BridgeConfig {
    public static final String DEFAULT_URL = "ws://127.0.0.1:8765/bookmap";
    public static final String DEFAULT_ALLOWED_SYMBOL = "MNQ";
    public static final String ADDON_VERSION = "0.1.0";
    /**
     * Wire-protocol version. Bumped only on a breaking envelope change, so the
     * Python receiver can detect and refuse an incompatible bridge instead of
     * silently misparsing. It is carried on the handshake and every control
     * message; the strict market-event schemas are intentionally unchanged.
     */
    public static final String PROTOCOL_VERSION = "1.0";
    /**
     * What this bridge genuinely delivers. It exposes aggregated depth, trades,
     * and the verified aggressor side, but NOT order-by-order (MBO) data, so the
     * receiver can disable only the setups that need what is absent.
     */
    public static final String CAPABILITIES = "aggregated_depth,trades,aggressor_side,source_timestamps";

    private final URI websocketUri;
    private final boolean allowNonLoopback;
    private final String allowedSymbol;
    private final boolean allowAnySymbol;
    private final int queueCapacity;
    private final Duration heartbeatInterval;
    private final Duration reconnectInitialDelay;
    private final Duration reconnectMaxDelay;

    public BridgeConfig(
            URI websocketUri,
            boolean allowNonLoopback,
            String allowedSymbol,
            boolean allowAnySymbol,
            int queueCapacity,
            Duration heartbeatInterval,
            Duration reconnectInitialDelay,
            Duration reconnectMaxDelay) {
        this.websocketUri = Objects.requireNonNull(websocketUri, "websocketUri");
        this.allowNonLoopback = allowNonLoopback;
        this.allowedSymbol = normalizeSymbol(allowedSymbol);
        this.allowAnySymbol = allowAnySymbol;
        this.queueCapacity = requirePositive(queueCapacity, "queueCapacity");
        this.heartbeatInterval = Objects.requireNonNull(heartbeatInterval, "heartbeatInterval");
        this.reconnectInitialDelay = Objects.requireNonNull(reconnectInitialDelay, "reconnectInitialDelay");
        this.reconnectMaxDelay = Objects.requireNonNull(reconnectMaxDelay, "reconnectMaxDelay");
        EndpointPolicy.requireAllowed(websocketUri, allowNonLoopback);
    }

    public static BridgeConfig defaults() {
        return new BridgeConfig(
                URI.create(DEFAULT_URL),
                false,
                DEFAULT_ALLOWED_SYMBOL,
                false,
                10_000,
                Duration.ofSeconds(2),
                Duration.ofMillis(250),
                Duration.ofSeconds(10));
    }

    public static BridgeConfig fromSettings(ForwarderSettings settings) {
        ForwarderSettings safeSettings = ForwarderSettings.sanitized(settings);
        return new BridgeConfig(
                URI.create(safeSettings.websocketUrl),
                safeSettings.allowNonLoopback,
                safeSettings.allowedSymbol,
                safeSettings.allowAnySymbol,
                safeSettings.queueCapacity,
                Duration.ofSeconds(2),
                Duration.ofMillis(250),
                Duration.ofSeconds(10));
    }

    public URI websocketUri() {
        return websocketUri;
    }

    public boolean allowNonLoopback() {
        return allowNonLoopback;
    }

    public String allowedSymbol() {
        return allowedSymbol;
    }

    public boolean allowAnySymbol() {
        return allowAnySymbol;
    }

    public int queueCapacity() {
        return queueCapacity;
    }

    public Duration heartbeatInterval() {
        return heartbeatInterval;
    }

    public Duration reconnectInitialDelay() {
        return reconnectInitialDelay;
    }

    public Duration reconnectMaxDelay() {
        return reconnectMaxDelay;
    }

    public boolean allowsSymbol(String symbol) {
        if (allowAnySymbol) {
            return true;
        }
        return normalizeSymbol(symbol).contains(allowedSymbol);
    }

    private static int requirePositive(int value, String name) {
        if (value <= 0) {
            throw new IllegalArgumentException(name + " must be greater than zero");
        }
        return value;
    }

    private static String normalizeSymbol(String value) {
        String normalized = value == null ? "" : value.trim().toUpperCase();
        if (normalized.isEmpty()) {
            throw new IllegalArgumentException("allowedSymbol is required");
        }
        return normalized;
    }
}
