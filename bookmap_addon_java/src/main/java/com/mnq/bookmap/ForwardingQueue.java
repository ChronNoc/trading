package com.mnq.bookmap;

import java.util.Objects;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

/** Bounded queue that reports data gaps instead of buffering without limit. */
public final class ForwardingQueue {
    /** Emit at most one data_gap marker per second so gap reporting can
     *  never flood the very queue it protects; the marker still carries the
     *  cumulative dropped count. */
    private static final long GAP_MARKER_INTERVAL_NANOS = 1_000_000_000L;

    private final ArrayBlockingQueue<String> queue;
    private final MessageFactory messageFactory;
    private final AtomicLong droppedCount = new AtomicLong();
    private final AtomicLong sentCount = new AtomicLong();
    private final AtomicLong lastGapMarkerNanos = new AtomicLong(Long.MIN_VALUE);
    private final AtomicBoolean gapMarkerPending = new AtomicBoolean();

    public ForwardingQueue(int capacity, MessageFactory messageFactory) {
        if (capacity <= 0) {
            throw new IllegalArgumentException("capacity must be greater than zero");
        }
        this.queue = new ArrayBlockingQueue<>(capacity);
        this.messageFactory = Objects.requireNonNull(messageFactory, "messageFactory");
    }

    public boolean enqueue(String payload) {
        if (queue.offer(payload)) {
            return true;
        }
        droppedCount.incrementAndGet();
        gapMarkerPending.set(true);
        return false;
    }

    /** Delivers a rate-limited data_gap marker ahead of queued messages when
     *  drops occurred. The marker is synthesized at take() time, so it can
     *  never be evicted by further overflow and never displaces real data. */
    public String take(long timeout, TimeUnit unit) throws InterruptedException {
        if (gapMarkerPending.get()) {
            long now = System.nanoTime();
            long last = lastGapMarkerNanos.get();
            boolean firstMarker = last == Long.MIN_VALUE;
            if ((firstMarker || now - last >= GAP_MARKER_INTERVAL_NANOS)
                    && lastGapMarkerNanos.compareAndSet(last, now)) {
                gapMarkerPending.set(false);
                return messageFactory.dataGap(droppedCount.get(), "bounded queue overflow");
            }
        }
        return queue.poll(timeout, unit);
    }

    public long droppedCount() {
        return droppedCount.get();
    }

    public long sentCount() {
        return sentCount.get();
    }

    public void markSent() {
        sentCount.incrementAndGet();
    }

    public int size() {
        return queue.size();
    }
}
