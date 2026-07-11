package com.mnq.bookmap;

import java.io.IOException;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.Locale;
import java.util.concurrent.TimeUnit;

/** Plain Java test runner used by Gradle's test task without external test dependencies. */
public final class TestRunner {
    private int testsRun;

    public static void main(String[] args) throws Exception {
        TestRunner runner = new TestRunner();
        runner.run();
        System.out.println("Java bridge tests passed: " + runner.testsRun);
    }

    private void run() throws Exception {
        testPriceConversion();
        testDepthTrackingAndRemoval();
        testTradeSideAndSequence();
        testJsonSchemas();
        testQueueDropAndDataGap();
        testReconnectBackoff();
        testLoopbackPolicy();
        testRuntimeReplayLiveLifecycle();
        testRuntimeFormatsDepthAndTrade();
        testSourceHasNoBrokerExecutionReferences();
    }

    private void testPriceConversion() {
        PriceConverter converter = new PriceConverter(100.0);
        assertEquals("100.25", converter.toPriceString(10025), "price conversion");
        assertEquals("100", converter.toPriceString(10000), "whole price conversion");
        assertThrows(() -> new PriceConverter(0.0), "invalid pips");
        testsRun++;
    }

    private void testDepthTrackingAndRemoval() {
        DepthBookTracker tracker = new DepthBookTracker();
        DepthUpdate first = tracker.update(true, "100.25", 12);
        DepthUpdate second = tracker.update(true, "100.25", 7);
        DepthUpdate removal = tracker.update(true, "100.25", 0);

        assertEquals("bid", first.side(), "bid side");
        assertEquals(0, first.previousSize(), "first previous");
        assertEquals(12, second.previousSize(), "second previous");
        assertEquals(7, removal.previousSize(), "remove previous");
        assertEquals(0, tracker.sizeAt("bid", "100.25"), "removed size");
        testsRun++;
    }

    private void testTradeSideAndSequence() {
        SequenceGenerator generator = new SequenceGenerator();

        assertEquals("buy", TradeSideMapper.fromBidAggressor(true), "bid aggressor");
        assertEquals("sell", TradeSideMapper.fromBidAggressor(false), "ask aggressor");
        assertEquals(1L, generator.next(), "first sequence");
        assertEquals(2L, generator.next(), "second sequence");
        testsRun++;
    }

    private void testJsonSchemas() {
        MessageFactory factory = factory();
        String depth = factory.depthUpdate(123L, new DepthUpdate("bid", "100.25", 4, 8));
        String trade = factory.trade(456L, 100.25, 3, "sell", 42L);
        String heartbeat = factory.heartbeat(789L, "live", 2L, 10L);

        assertContains(depth, "\"type\":\"depth_update\"", "depth type");
        assertContains(depth, "\"previous_size\":\"4\"", "depth previous");
        assertContains(trade, "\"type\":\"trade\"", "trade type");
        assertContains(trade, "\"sequence_id\":42", "trade sequence");
        assertContains(heartbeat, "\"session_id\":\"session_test\"", "heartbeat session");
        assertContains(heartbeat, "\"addon_version\":\"" + BridgeConfig.ADDON_VERSION + "\"", "heartbeat version");
        testsRun++;
    }

    private void testQueueDropAndDataGap() throws Exception {
        ForwardingQueue queue = new ForwardingQueue(1, factory());

        assertTrue(queue.enqueue("{\"type\":\"first\"}"), "first enqueue");
        assertFalse(queue.enqueue("{\"type\":\"second\"}"), "second enqueue");
        assertEquals(1L, queue.droppedCount(), "drop count");
        String payload = queue.take(1, TimeUnit.SECONDS);
        assertContains(payload, "\"type\":\"data_gap\"", "data gap message");
        testsRun++;
    }

    private void testReconnectBackoff() {
        ReconnectState state = new ReconnectState(Duration.ofMillis(10), Duration.ofMillis(40));

        assertEquals(10L, state.nextDelayMillis(), "first delay");
        assertEquals(20L, state.nextDelayMillis(), "second delay");
        assertEquals(40L, state.nextDelayMillis(), "capped delay");
        assertEquals(40L, state.nextDelayMillis(), "still capped");
        state.reset();
        assertEquals(10L, state.nextDelayMillis(), "reset delay");
        testsRun++;
    }

    private void testLoopbackPolicy() {
        assertTrue(EndpointPolicy.isAllowed(URI.create("ws://127.0.0.1:8765/bookmap"), false), "127 loopback");
        assertTrue(EndpointPolicy.isAllowed(URI.create("ws://localhost:8765/bookmap"), false), "localhost loopback");
        assertFalse(EndpointPolicy.isAllowed(URI.create("ws://192.168.0.50:8765/bookmap"), false), "remote rejected");
        assertTrue(EndpointPolicy.isAllowed(URI.create("ws://192.168.0.50:8765/bookmap"), true), "remote override");
        testsRun++;
    }

    private void testRuntimeReplayLiveLifecycle() throws Exception {
        BridgeConfig config = BridgeConfig.defaults();
        ForwarderRuntime runtime = new ForwarderRuntime(
                config,
                InstrumentContext.synthetic("MNQ", "MNQ", 100.0, config),
                new CaptureTransport(),
                Clock.fixed(Instant.parse("2026-07-10T14:30:00Z"), ZoneOffset.UTC));

        runtime.publishConnected();
        runtime.publishRealtimeStarted();
        runtime.publishSessionEnded();

        assertContains(runtime.queue().take(1, TimeUnit.SECONDS), "\"type\":\"connected\"", "connected event");
        assertContains(runtime.queue().take(1, TimeUnit.SECONDS), "\"type\":\"replay_started\"", "replay event");
        assertContains(runtime.queue().take(1, TimeUnit.SECONDS), "\"type\":\"realtime_started\"", "live event");
        assertContains(runtime.queue().take(1, TimeUnit.SECONDS), "\"type\":\"session_ended\"", "ended event");
        testsRun++;
    }

    private void testRuntimeFormatsDepthAndTrade() throws Exception {
        BridgeConfig config = BridgeConfig.defaults();
        ForwarderRuntime runtime = new ForwarderRuntime(
                config,
                InstrumentContext.synthetic("MNQ", "MNQ", 100.0, config),
                new CaptureTransport(),
                Clock.fixed(Instant.parse("2026-07-10T14:30:00Z"), ZoneOffset.UTC));

        runtime.publishDepth(100L, true, 10025, 12);
        runtime.publishTrade(200L, 100.25, 3, true);

        assertContains(runtime.queue().take(1, TimeUnit.SECONDS), "\"price\":\"100.25\"", "depth price");
        String trade = runtime.queue().take(1, TimeUnit.SECONDS);
        assertContains(trade, "\"type\":\"trade\"", "trade type");
        assertContains(trade, "\"aggressor_side\":\"buy\"", "trade side");
        assertContains(trade, "\"sequence_id\":1", "trade sequence");
        testsRun++;
    }

    private void testSourceHasNoBrokerExecutionReferences() throws IOException {
        Path sourceRoot = Path.of("src", "main", "java");
        StringBuilder allSource = new StringBuilder();
        try (var stream = Files.walk(sourceRoot)) {
            for (Path path : stream.filter(path -> path.toString().endsWith(".java")).toList()) {
                allSource.append(Files.readString(path, StandardCharsets.UTF_8)).append('\n');
            }
        }
        String normalized = allSource.toString().toLowerCase(Locale.ROOT);
        assertFalse(normalized.contains("tradovate"), "tradovate reference");
        assertFalse(normalized.contains("broker"), "broker reference");
        assertFalse(normalized.contains("sendorder"), "send order reference");
        assertFalse(normalized.contains("updateorder"), "update order reference");
        assertFalse(normalized.contains("execution"), "execution reference");
        testsRun++;
    }

    private static MessageFactory factory() {
        return new MessageFactory(InstrumentContext.synthetic("MNQ", "MNQ", 100.0, BridgeConfig.defaults()));
    }

    private static void assertEquals(Object expected, Object actual, String label) {
        if (!expected.equals(actual)) {
            throw new AssertionError(label + ": expected " + expected + " but got " + actual);
        }
    }

    private static void assertTrue(boolean value, String label) {
        if (!value) {
            throw new AssertionError(label + ": expected true");
        }
    }

    private static void assertFalse(boolean value, String label) {
        if (value) {
            throw new AssertionError(label + ": expected false");
        }
    }

    private static void assertContains(String value, String expected, String label) {
        if (value == null || !value.contains(expected)) {
            throw new AssertionError(label + ": missing " + expected + " in " + value);
        }
    }

    private static void assertThrows(ThrowingRunnable runnable, String label) {
        try {
            runnable.run();
        } catch (RuntimeException expected) {
            return;
        }
        throw new AssertionError(label + ": expected RuntimeException");
    }

    @FunctionalInterface
    private interface ThrowingRunnable {
        void run();
    }

    private static final class CaptureTransport implements JsonTransport {
        @Override
        public void connect(URI uri) {
        }

        @Override
        public boolean send(String payload) {
            return true;
        }

        @Override
        public boolean isOpen() {
            return true;
        }

        @Override
        public void close() {
        }
    }
}
