package com.mnq.bookmap;

import java.io.Closeable;
import java.time.Clock;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

/** Owns the asynchronous WebSocket sender, queue, heartbeat, and reconnect loop. */
public final class ForwarderRuntime implements Closeable {
    private final BridgeConfig config;
    private final InstrumentContext instrument;
    private final MessageFactory messageFactory;
    private final JsonTransport transport;
    private final ForwardingQueue queue;
    private final ReconnectState reconnectState;
    private final Clock clock;
    private final AtomicBoolean running = new AtomicBoolean(false);
    private final AtomicBoolean reconnecting = new AtomicBoolean(false);
    private final AtomicBoolean everConnected = new AtomicBoolean(false);
    /** Bounded budget for draining the queue on shutdown. */
    private static final long SHUTDOWN_DRAIN_MILLIS = 2_000L;
    /** True when the last close() could not deliver every queued message. */
    private volatile boolean uncleanShutdown;
    private final AtomicReference<String> sourceMode = new AtomicReference<>("historical");
    private ScheduledExecutorService executor;

    public ForwarderRuntime(
            BridgeConfig config,
            InstrumentContext instrument,
            JsonTransport transport,
            Clock clock) {
        this.config = Objects.requireNonNull(config, "config");
        this.instrument = Objects.requireNonNull(instrument, "instrument");
        this.transport = Objects.requireNonNull(transport, "transport");
        this.clock = Objects.requireNonNull(clock, "clock");
        this.messageFactory = new MessageFactory(instrument);
        this.queue = new ForwardingQueue(config.queueCapacity(), messageFactory);
        this.reconnectState = new ReconnectState(config.reconnectInitialDelay(), config.reconnectMaxDelay());
    }

    public ForwarderRuntime(BridgeConfig config, InstrumentContext instrument, JsonTransport transport) {
        this(config, instrument, transport, Clock.systemUTC());
    }

    public void start() {
        if (!running.compareAndSet(false, true)) {
            return;
        }
        executor = Executors.newScheduledThreadPool(2, new NamedDaemonThreadFactory("mnq-bookmap-forwarder"));
        executor.execute(this::sendLoop);
        executor.scheduleAtFixedRate(
                () -> enqueue(messageFactory.heartbeat(
                        nowNs(), sourceMode.get(), queue.droppedCount(),
                        queue.sentCount(), queue.sentFrameCount())),
                config.heartbeatInterval().toMillis(),
                config.heartbeatInterval().toMillis(),
                TimeUnit.MILLISECONDS);
    }

    public void publishConnected() {
        enqueue(messageFactory.connected(nowNs(), sourceMode.get(), queue.droppedCount()));
        enqueue(messageFactory.control("replay_started", nowNs(), "historical", queue.droppedCount()));
    }

    public void publishRealtimeStarted() {
        sourceMode.set("live");
        enqueue(messageFactory.control("realtime_started", nowNs(), sourceMode.get(), queue.droppedCount()));
    }

    public void publishDisconnected(String reason) {
        enqueue(messageFactory.control("disconnected", nowNs(), sourceMode.get(), queue.droppedCount(), reason));
    }

    public void publishSessionEnded() {
        enqueue(messageFactory.control("session_ended", nowNs(), sourceMode.get(), queue.droppedCount()));
    }

    public void publishDepth(long timestampNs, boolean isBid, int price, int size) {
        if (!instrument.shouldForward()) {
            return;
        }
        DepthUpdate update = instrument.depthTracker().update(isBid, instrument.priceConverter().toPriceString(price), size);
        enqueue(messageFactory.depthUpdate(timestampNs, update));
    }

    public void publishTrade(long timestampNs, double price, int size, boolean isBidAggressor) {
        if (!instrument.shouldForward()) {
            return;
        }
        long sequenceId = instrument.sequenceGenerator().next();
        String realPrice = instrument.priceConverter().toPriceString(price);
        enqueue(messageFactory.trade(timestampNs, realPrice, size, TradeSideMapper.fromBidAggressor(isBidAggressor), sequenceId));
    }

    public ForwardingQueue queue() {
        return queue;
    }

    @Override
    /**
     * Shut down with a bounded graceful drain. Idempotent.
     *
     * <p>Order matters. The send loop runs {@code while (running.get())}, so the
     * previous implementation flipped {@code running} to false, enqueued
     * {@code session_ended} into a queue nobody was draining any more, and then
     * called {@code shutdownNow()} - the terminal marker was never delivered and
     * Python could not tell a clean close from a crash.
     *
     * <p>Now: stop the periodic tasks, let the loop finish its current send,
     * enqueue the terminal marker, then drain the queue synchronously on this
     * thread while the transport is still open. If the drain cannot complete
     * within the deadline the session is marked UNCLEAN and we say so explicitly
     * rather than implying a complete recording.
     */
    public void close() {
        if (!running.compareAndSet(true, false)) {
            return; // idempotent: a second close is a no-op
        }
        if (executor != null) {
            executor.shutdown(); // stop heartbeats; let sendLoop exit its while()
            try {
                if (!executor.awaitTermination(SHUTDOWN_DRAIN_MILLIS, TimeUnit.MILLISECONDS)) {
                    executor.shutdownNow();
                }
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                executor.shutdownNow();
            }
            executor = null;
        }

        // The send loop has stopped; drain what is left on THIS thread so the
        // terminal marker is genuinely delivered.
        long undelivered = drainRemaining(SHUTDOWN_DRAIN_MILLIS);
        boolean drained = undelivered == 0;
        if (!drained) {
            // Loud, explicit: never let an undrained queue look like a clean end.
            trySend(messageFactory.control("data_gap", nowNs(), sourceMode.get(),
                    queue.droppedCount(), "unclean shutdown: " + undelivered + " message(s) undelivered"));
        }
        // The terminal marker must actually be DELIVERED. A close that could not
        // tell the consumer the session ended is unclean by definition - the
        // consumer would otherwise be unable to distinguish it from a crash.
        boolean markerDelivered = trySend(messageFactory.control(
                "session_ended", nowNs(), sourceMode.get(), queue.droppedCount(),
                drained ? "clean shutdown" : "unclean shutdown"));
        uncleanShutdown = !drained || !markerDelivered;
        transport.close();
    }

    /** Returns true when the last close() could not deliver every queued message. */
    public boolean isUncleanShutdown() {
        return uncleanShutdown;
    }

    /**
     * Sends queued messages until the queue is empty or the deadline passes.
     *
     * @return the number of messages still undelivered (0 means a full drain).
     */
    private long drainRemaining(long budgetMillis) {
        long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(budgetMillis);
        while (System.nanoTime() < deadline) {
            OutboundBatch batch;
            try {
                batch = nextBatch(10, TimeUnit.MILLISECONDS, false);
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                break;
            }
            if (batch == null) {
                return 0; // queue drained
            }
            if (!trySend(batch.payload())) {
                queue.markTransportDrop(batch.eventCount());
                return queue.size() + batch.eventCount();
            }
            queue.markSent(batch.eventCount());
        }
        return queue.size();
    }

    private boolean trySend(String payload) {
        try {
            return transport.send(payload);
        } catch (RuntimeException error) {
            return false;
        }
    }

    private void sendLoop() {
        while (running.get()) {
            OutboundBatch batch = null;
            try {
                ensureConnected();
                batch = nextBatch(250, TimeUnit.MILLISECONDS, true);
                if (batch != null) {
                    if (transport.send(batch.payload())) {
                        queue.markSent(batch.eventCount());
                    } else {
                        queue.markTransportDrop(batch.eventCount());
                        transport.close();
                    }
                }
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                return;
            } catch (RuntimeException error) {
                if (batch != null) {
                    // The send outcome is uncertain. Fail closed: count it as
                    // loss so Python invalidates the segment rather than
                    // pretending conservation held.
                    queue.markTransportDrop(batch.eventCount());
                }
                publishDisconnected(error.getMessage());
                transport.close();
                sleep(reconnectState.nextDelayMillis());
            }
        }
    }

    private void ensureConnected() {
        if (transport.isOpen()) {
            reconnectState.reset();
            return;
        }
        if (!everConnected.get()) {
            transport.connect(config.websocketUri());
            everConnected.set(true);
            reconnectState.reset();
            return;
        }

        // A new WebSocket handler requires a new protocol handshake before it
        // will accept market events. Anything queued for the dead socket is an
        // explicitly accounted causal gap, never replayed across the boundary.
        reconnecting.set(true);
        try {
            queue.discardForReconnect();
            messageFactory.rotateConnection();
            transport.connect(config.websocketUri());
            if (!transport.send(messageFactory.connected(
                    nowNs(), sourceMode.get(), queue.droppedCount()))) {
                throw new IllegalStateException("reconnect handshake was not confirmed");
            }
            queue.markSent();
            reconnectState.reset();
        } finally {
            reconnecting.set(false);
        }
    }

    /** Collect one bounded low-latency frame while preserving queue order. */
    private OutboundBatch nextBatch(long timeout, TimeUnit unit, boolean waitForWindow)
            throws InterruptedException {
        String first = queue.take(timeout, unit);
        if (first == null) {
            return null;
        }
        List<String> payloads = new ArrayList<>(BridgeConfig.BATCH_MAX_EVENTS);
        payloads.add(first);
        if (!waitForWindow) {
            while (payloads.size() < BridgeConfig.BATCH_MAX_EVENTS) {
                String next = queue.take(0L, TimeUnit.NANOSECONDS);
                if (next == null) {
                    break;
                }
                payloads.add(next);
            }
            return new OutboundBatch(MessageBatcher.encode(payloads), payloads.size());
        }
        long deadline = System.nanoTime() + TimeUnit.MICROSECONDS.toNanos(
                BridgeConfig.BATCH_WINDOW_MICROS);
        while (payloads.size() < BridgeConfig.BATCH_MAX_EVENTS) {
            long remaining = deadline - System.nanoTime();
            if (remaining <= 0L) {
                break;
            }
            String next = queue.take(remaining, TimeUnit.NANOSECONDS);
            if (next == null) {
                break;
            }
            payloads.add(next);
        }
        return new OutboundBatch(MessageBatcher.encode(payloads), payloads.size());
    }

    /** One confirmed WebSocket frame and its exact contained-event count. */
    private record OutboundBatch(String payload, int eventCount) {
    }

    private void enqueue(String payload) {
        if (reconnecting.get()) {
            queue.markTransportDrop();
            return;
        }
        queue.enqueue(payload);
    }

    private long nowNs() {
        return TimeUnit.MILLISECONDS.toNanos(clock.millis());
    }

    private static void sleep(long millis) {
        try {
            Thread.sleep(millis);
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
        }
    }

    private static final class NamedDaemonThreadFactory implements ThreadFactory {
        private final String baseName;
        private int count;

        private NamedDaemonThreadFactory(String baseName) {
            this.baseName = baseName;
        }

        @Override
        public Thread newThread(Runnable runnable) {
            Thread thread = new Thread(runnable, baseName + "-" + ++count);
            thread.setDaemon(true);
            return thread;
        }
    }
}
