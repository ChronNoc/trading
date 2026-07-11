package com.mnq.bookmap;

import java.util.concurrent.atomic.AtomicLong;

/** Monotonic sequence id generator for forwarded trade events. */
public final class SequenceGenerator {
    private final AtomicLong next = new AtomicLong(1);

    public long next() {
        return next.getAndIncrement();
    }
}
