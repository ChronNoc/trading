package com.mnq.bookmap;

import java.time.Duration;

/** Exponential reconnect delay state capped at a maximum interval. */
public final class ReconnectState {
    private final long initialDelayMillis;
    private final long maxDelayMillis;
    private long currentDelayMillis;

    public ReconnectState(Duration initialDelay, Duration maxDelay) {
        this.initialDelayMillis = positiveMillis(initialDelay, "initialDelay");
        this.maxDelayMillis = positiveMillis(maxDelay, "maxDelay");
        if (initialDelayMillis > maxDelayMillis) {
            throw new IllegalArgumentException("initialDelay must not exceed maxDelay");
        }
        this.currentDelayMillis = initialDelayMillis;
    }

    public long nextDelayMillis() {
        long result = currentDelayMillis;
        currentDelayMillis = Math.min(maxDelayMillis, currentDelayMillis * 2);
        return result;
    }

    public void reset() {
        currentDelayMillis = initialDelayMillis;
    }

    private static long positiveMillis(Duration duration, String name) {
        long millis = duration.toMillis();
        if (millis <= 0) {
            throw new IllegalArgumentException(name + " must be greater than zero");
        }
        return millis;
    }
}
