package com.mnq.bookmap;

import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.Locale;
import java.util.UUID;
import velox.api.layer1.data.InstrumentInfo;

/** Bookmap instrument metadata used by the message factory and filters. */
public final class InstrumentContext {
    /**
     * Identifies this JVM/add-on load. It is stable across reconnects within one
     * Bookmap run, so the receiver can tell a reconnect (same stream, new
     * connection) from a genuinely new bridge process (new stream).
     */
    private static final String STREAM_ID = UUID.randomUUID().toString();

    private final String alias;
    private final String symbol;
    private final String requestedSymbol;
    private final String sessionId;
    /** New per instrument/connection, so a reconnect is visible to the receiver. */
    private final String connectionId;
    private final boolean shouldForward;
    private final PriceConverter priceConverter;
    private final DepthBookTracker depthTracker;
    private final SequenceGenerator sequenceGenerator;

    private InstrumentContext(
            String alias,
            String symbol,
            String requestedSymbol,
            String sessionId,
            boolean shouldForward,
            PriceConverter priceConverter,
            DepthBookTracker depthTracker,
            SequenceGenerator sequenceGenerator) {
        this.alias = alias;
        this.symbol = symbol;
        this.requestedSymbol = requestedSymbol;
        this.sessionId = sessionId;
        this.connectionId = UUID.randomUUID().toString();
        this.shouldForward = shouldForward;
        this.priceConverter = priceConverter;
        this.depthTracker = depthTracker;
        this.sequenceGenerator = sequenceGenerator;
    }

    public static InstrumentContext from(String alias, InstrumentInfo instrumentInfo, BridgeConfig config) {
        String symbol = safeText(instrumentInfo.symbol, "UNKNOWN");
        String requestedSymbol = safeText(instrumentInfo.requestedSymbol, symbol);
        String effectiveAlias = safeText(alias, requestedSymbol);
        String sessionId = "session_" + DateTimeFormatter.ofPattern("yyyyMMdd'T'HHmmss'Z'")
                .withZone(ZoneOffset.UTC)
                .format(Instant.now()) + "_" + UUID.randomUUID().toString().substring(0, 8);
        boolean allowed = config.allowsSymbol(symbol) || config.allowsSymbol(requestedSymbol) || config.allowsSymbol(effectiveAlias);
        return new InstrumentContext(
                effectiveAlias,
                symbol,
                requestedSymbol,
                sessionId,
                allowed,
                new PriceConverter(instrumentInfo.pips),
                new DepthBookTracker(),
                new SequenceGenerator());
    }

    public static InstrumentContext synthetic(String alias, String symbol, double pips, BridgeConfig config) {
        String normalizedSymbol = safeText(symbol, "MNQ");
        return new InstrumentContext(
                safeText(alias, normalizedSymbol),
                normalizedSymbol,
                normalizedSymbol,
                "session_test",
                config.allowsSymbol(normalizedSymbol),
                new PriceConverter(pips),
                new DepthBookTracker(),
                new SequenceGenerator());
    }

    public String alias() {
        return alias;
    }

    public String symbol() {
        return symbol;
    }

    public String requestedSymbol() {
        return requestedSymbol;
    }

    public String sessionId() {
        return sessionId;
    }

    public String streamId() {
        return STREAM_ID;
    }

    public String connectionId() {
        return connectionId;
    }

    public boolean shouldForward() {
        return shouldForward;
    }

    public PriceConverter priceConverter() {
        return priceConverter;
    }

    public DepthBookTracker depthTracker() {
        return depthTracker;
    }

    public SequenceGenerator sequenceGenerator() {
        return sequenceGenerator;
    }

    private static String safeText(String value, String fallback) {
        if (value == null || value.isBlank()) {
            return fallback;
        }
        return value.trim().toUpperCase(Locale.ROOT);
    }
}
