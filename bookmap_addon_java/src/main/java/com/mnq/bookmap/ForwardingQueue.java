package com.mnq.bookmap;

import java.util.Objects;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

/** Bounded queue that reports data gaps instead of buffering without limit. */
public final class ForwardingQueue {
    private final ArrayBlockingQueue<String> queue;
    private final MessageFactory messageFactory;
    private final AtomicLong droppedCount = new AtomicLong();
    private final AtomicLong sentCount = new AtomicLong();

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
        long dropped = droppedCount.incrementAndGet();
        queue.poll();
        queue.offer(messageFactory.dataGap(dropped, "bounded queue overflow"));
        return false;
    }

    public String take(long timeout, TimeUnit unit) throws InterruptedException {
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
