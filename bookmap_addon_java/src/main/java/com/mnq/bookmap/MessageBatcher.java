package com.mnq.bookmap;

import java.util.List;
import java.util.Objects;

/** Builds the bounded additive wire envelope used for event micro-batches. */
public final class MessageBatcher {
    private MessageBatcher() {
    }

    /**
     * Return one legacy payload unchanged or wrap multiple ordered payloads in
     * a protocol 1.x event-batch envelope.
     */
    public static String encode(List<String> payloads) {
        Objects.requireNonNull(payloads, "payloads");
        if (payloads.isEmpty()) {
            throw new IllegalArgumentException("payloads must not be empty");
        }
        if (payloads.size() > BridgeConfig.BATCH_MAX_EVENTS) {
            throw new IllegalArgumentException("payloads exceeds bounded batch size");
        }
        if (payloads.size() == 1) {
            return Objects.requireNonNull(payloads.get(0), "payload");
        }

        StringBuilder builder = new StringBuilder(payloads.size() * 192);
        builder.append("{\"type\":\"event_batch\",\"protocol_version\":\"")
                .append(BridgeConfig.PROTOCOL_VERSION)
                .append("\",\"event_count\":")
                .append(payloads.size())
                .append(",\"events\":[");
        for (int index = 0; index < payloads.size(); index++) {
            if (index > 0) {
                builder.append(',');
            }
            builder.append(Objects.requireNonNull(payloads.get(index), "payload"));
        }
        return builder.append("]}").toString();
    }
}
