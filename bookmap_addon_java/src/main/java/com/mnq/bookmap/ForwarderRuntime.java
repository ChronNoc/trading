package com.mnq.bookmap;

import java.io.Closeable;
import java.time.Clock;
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
                () -> enqueue(messageFactory.heartbeat(nowNs(), sourceMode.get(), queue.droppedCount(), queue.sentCount())),
                config.heartbeatInterval().toMillis(),
                config.heartbeatInterval().toMillis(),
                TimeUnit.MILLISECONDS);
    }

    public void publishConnected() {
        enqueue(messageFactory.control("connected", nowNs(), sourceMode.get(), queue.droppedCount()));
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
        enqueue(messageFactory.trade(timestampNs, price, size, TradeSideMapper.fromBidAggressor(isBidAggressor), sequenceId));
    }

    public ForwardingQueue queue() {
        return queue;
    }

    @Override
    public void close() {
        if (!running.compareAndSet(true, false)) {
            return;
        }
        publishSessionEnded();
        if (executor != null) {
            executor.shutdownNow();
            executor = null;
        }
        transport.close();
    }

    private void sendLoop() {
        while (running.get()) {
            try {
                ensureConnected();
                String payload = queue.take(250, TimeUnit.MILLISECONDS);
                if (payload != null && transport.send(payload)) {
                    queue.markSent();
                }
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                return;
            } catch (RuntimeException error) {
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
        transport.connect(config.websocketUri());
    }

    private void enqueue(String payload) {
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
