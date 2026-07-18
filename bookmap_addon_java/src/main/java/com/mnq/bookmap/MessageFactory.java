package com.mnq.bookmap;

import java.math.BigDecimal;
import java.time.Clock;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import java.util.UUID;

/** Creates JSON messages sent from Bookmap to the Python receiver. */
public final class MessageFactory {
    private final InstrumentContext instrument;
    /** Every ordinary wire event receives one sequence, across depth/trade/control. */
    private final AtomicLong streamSequence = new AtomicLong(1L);
    private final AtomicReference<String> connectionId;

    public MessageFactory(InstrumentContext instrument) {
        this.instrument = instrument;
        this.connectionId = new AtomicReference<>(instrument.connectionId());
    }

    /** Start a new transport connection identity while preserving stream identity. */
    public String rotateConnection() {
        String next = UUID.randomUUID().toString();
        connectionId.set(next);
        return next;
    }

    public String depthUpdate(long timestampNs, DepthUpdate update) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("type", "depth_update");
        payload.put("timestamp", timestampNs);
        payload.put("symbol", instrument.symbol());
        payload.put("side", update.side());
        payload.put("price", update.price());
        payload.put("previous_size", Integer.toString(update.previousSize()));
        payload.put("new_size", Integer.toString(update.newSize()));
        payload.put("stream_sequence", nextStreamSequence());
        return toJson(payload);
    }

    public String trade(long timestampNs, String price, int size, String aggressorSide, long sequenceId) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("type", "trade");
        payload.put("timestamp_ns", timestampNs);
        payload.put("price", price);
        payload.put("size", Integer.toString(size));
        payload.put("aggressor_side", aggressorSide);
        payload.put("instrument", instrument.symbol());
        payload.put("sequence_id", sequenceId);
        payload.put("stream_sequence", nextStreamSequence());
        return toJson(payload);
    }

    public String heartbeat(long timestampNs, String sourceMode, long droppedCount, long sentCount) {
        Map<String, Object> payload = controlMap("heartbeat", timestampNs, sourceMode, droppedCount);
        payload.put("sent_count", sentCount);
        return toJson(payload);
    }

    public String control(String type, long timestampNs, String sourceMode, long droppedCount) {
        return toJson(controlMap(type, timestampNs, sourceMode, droppedCount));
    }

    /**
     * The handshake. It is a normal "connected" control message plus the feed's
     * capability declaration, so the receiver learns on connect what this bridge
     * can and cannot supply (no MBO) and can gate setups accordingly.
     */
    public String connected(long timestampNs, String sourceMode, long droppedCount) {
        Map<String, Object> payload = controlMap("connected", timestampNs, sourceMode, droppedCount);
        payload.put("capabilities", BridgeConfig.CAPABILITIES);
        payload.put("provider", "bookmap");
        return toJson(payload);
    }

    public String control(String type, long timestampNs, String sourceMode, long droppedCount, String reason) {
        Map<String, Object> payload = controlMap(type, timestampNs, sourceMode, droppedCount);
        payload.put("reason", reason == null ? "unspecified" : reason);
        return toJson(payload);
    }

    public String dataGap(long droppedCount, String reason) {
        // A gap marker is synthesized ahead of older queued events. It must not
        // consume a stream sequence or it would itself make the wire order look
        // reversed. The next ordinary event exposes the missing sequence(s).
        Map<String, Object> payload = controlMapWithoutSequence(
                "data_gap", Clock.systemUTC().millis() * 1_000_000L, "unknown", droppedCount);
        payload.put("reason", reason);
        return toJson(payload);
    }

    private Map<String, Object> controlMap(String type, long timestampNs, String sourceMode, long droppedCount) {
        Map<String, Object> payload = controlMapWithoutSequence(type, timestampNs, sourceMode, droppedCount);
        payload.put("stream_sequence", nextStreamSequence());
        return payload;
    }

    private Map<String, Object> controlMapWithoutSequence(
            String type, long timestampNs, String sourceMode, long droppedCount) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("type", type);
        payload.put("timestamp_ns", timestampNs);
        payload.put("protocol_version", BridgeConfig.PROTOCOL_VERSION);
        payload.put("stream_id", instrument.streamId());
        payload.put("connection_id", connectionId.get());
        payload.put("session_id", instrument.sessionId());
        payload.put("alias", instrument.alias());
        payload.put("symbol", instrument.symbol());
        payload.put("requested_symbol", instrument.requestedSymbol());
        payload.put("source_mode", sourceMode);
        payload.put("addon_name", "MNQ WebSocket Forwarder");
        payload.put("addon_version", BridgeConfig.ADDON_VERSION);
        payload.put("dropped_message_count", droppedCount);
        return payload;
    }

    private long nextStreamSequence() {
        return streamSequence.getAndIncrement();
    }

    private static String toJson(Map<String, Object> payload) {
        StringBuilder builder = new StringBuilder();
        builder.append('{');
        boolean first = true;
        for (Map.Entry<String, Object> entry : payload.entrySet()) {
            if (!first) {
                builder.append(',');
            }
            first = false;
            builder.append('"').append(escape(entry.getKey())).append('"').append(':');
            Object value = entry.getValue();
            if (value instanceof Number || value instanceof Boolean) {
                builder.append(value);
            } else {
                builder.append('"').append(escape(String.valueOf(value))).append('"');
            }
        }
        builder.append('}');
        return builder.toString();
    }

    private static String escape(String value) {
        StringBuilder builder = new StringBuilder(value.length());
        for (int index = 0; index < value.length(); index++) {
            char character = value.charAt(index);
            switch (character) {
                case '"' -> builder.append("\\\"");
                case '\\' -> builder.append("\\\\");
                case '\b' -> builder.append("\\b");
                case '\f' -> builder.append("\\f");
                case '\n' -> builder.append("\\n");
                case '\r' -> builder.append("\\r");
                case '\t' -> builder.append("\\t");
                default -> {
                    if (character < 0x20) {
                        builder.append(String.format("\\u%04x", (int) character));
                    } else {
                        builder.append(character);
                    }
                }
            }
        }
        return builder.toString();
    }

    private static String decimalString(double value) {
        return BigDecimal.valueOf(value).stripTrailingZeros().toPlainString();
    }
}
