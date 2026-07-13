package com.mnq.bookmap;

import java.io.IOException;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.lang.reflect.Proxy;
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
import velox.api.layer1.annotations.Layer1ApiVersion;
import velox.api.layer1.annotations.Layer1SimpleAttachable;
import velox.api.layer1.annotations.Layer1StrategyName;
import velox.api.layer1.data.InstrumentInfo;
import velox.api.layer1.settings.StrategySettingsVersion;
import velox.api.layer1.simplified.Api;
import velox.api.layer1.simplified.CustomModule;
import velox.api.layer1.simplified.DepthDataListener;
import velox.api.layer1.simplified.HistoricalDataListener;
import velox.api.layer1.simplified.HistoricalModeListener;
import velox.api.layer1.simplified.InitialState;
import velox.api.layer1.simplified.NoAutosubscription;
import velox.api.layer1.simplified.TimeListener;
import velox.api.layer1.simplified.TradeDataListener;

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
        testQueueGapMarkersAreRateLimited();
        testReconnectBackoff();
        testLoopbackPolicy();
        testRuntimeReplayLiveLifecycle();
        testRuntimeFormatsDepthAndTrade();
        testForwarderBookmapAnnotations();
        testForwarderSettingsVersionAndDefaults();
        testForwarderSettingsMigrationAndEndpointFallback();
        testForwarderSettingsFieldsAreSerializableOnly();
        testLoadSettingsSafelyHandlesBookmapSettingsErrors();
        testForwarderUsesAutomaticSubscription();
        testInitializeDoesNotCallManualListenerRegistration();
        testStopIsSafeBeforeAndAfterInitialize();
        testSourceHasNoBrokerExecutionReferences();
    }

    private void testPriceConversion() {
        PriceConverter converter = new PriceConverter(0.25);
        assertEquals("29771.25", converter.toPriceString(119085), "MNQ price conversion");
        assertEquals("29771", converter.toPriceString(119084), "whole price conversion");
        assertEquals("29771.125", converter.toPriceString(119084.5d), "trade double conversion");
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
        String trade = factory.trade(456L, "100.25", 3, "sell", 42L);
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

    private void testQueueGapMarkersAreRateLimited() throws Exception {
        ForwardingQueue queue = new ForwardingQueue(1, factory());

        assertTrue(queue.enqueue("{\"type\":\"first\"}"), "first enqueue");
        long failures = 0;
        for (int i = 0; i < 500; i++) {
            if (!queue.enqueue("{\"type\":\"burst\"}")) {
                failures++;
            }
        }
        assertTrue(failures > 0, "burst caused drops");
        assertEquals(failures, queue.droppedCount(), "every failed enqueue counted");

        long gapMarkers = 0;
        String payload;
        while ((payload = queue.take(50, TimeUnit.MILLISECONDS)) != null) {
            if (payload.contains("\"type\":\"data_gap\"")) {
                gapMarkers++;
            }
        }
        assertEquals(1L, gapMarkers, "one rate-limited gap marker for the burst");
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
                InstrumentContext.synthetic("MNQ", "MNQ", 0.25, config),
                new CaptureTransport(),
                Clock.fixed(Instant.parse("2026-07-10T14:30:00Z"), ZoneOffset.UTC));

        runtime.publishDepth(100L, true, 119085, 12);
        runtime.publishTrade(200L, 119084.5d, 3, true);

        assertContains(runtime.queue().take(1, TimeUnit.SECONDS), "\"price\":\"29771.25\"", "depth price");
        String trade = runtime.queue().take(1, TimeUnit.SECONDS);
        assertContains(trade, "\"type\":\"trade\"", "trade type");
        assertContains(trade, "\"price\":\"29771.125\"", "trade real price");
        assertContains(trade, "\"aggressor_side\":\"buy\"", "trade side");
        assertContains(trade, "\"sequence_id\":1", "trade sequence");
        testsRun++;
    }

    private void testForwarderBookmapAnnotations() {
        Class<MnqWebSocketForwarder> type = MnqWebSocketForwarder.class;
        assertTrue(type.isAnnotationPresent(Layer1SimpleAttachable.class), "Layer1SimpleAttachable annotation");
        assertTrue(type.isAnnotationPresent(Layer1ApiVersion.class), "Layer1ApiVersion annotation");

        Layer1StrategyName strategyName = type.getAnnotation(Layer1StrategyName.class);
        assertTrue(strategyName != null, "Layer1StrategyName annotation");
        assertEquals("MNQ WebSocket Forwarder", strategyName.value(), "literal strategy name");
        assertEquals("", strategyName.localizationKey(), "empty localization key");
        testsRun++;
    }

    private void testForwarderSettingsVersionAndDefaults() throws Exception {
        StrategySettingsVersion version = ForwarderSettings.class.getAnnotation(StrategySettingsVersion.class);
        assertTrue(version != null, "StrategySettingsVersion annotation");
        assertEquals(ForwarderSettings.SETTINGS_VERSION, version.currentVersion(), "settings current version");
        assertEquals(0, version.compatibleVersions().length, "settings compatible versions");

        Constructor<ForwarderSettings> constructor = ForwarderSettings.class.getConstructor();
        assertTrue(Modifier.isPublic(constructor.getModifiers()), "public no-argument constructor");
        ForwarderSettings settings = constructor.newInstance();
        assertEquals(BridgeConfig.DEFAULT_URL, settings.websocketUrl, "default websocket URL");
        assertEquals(BridgeConfig.DEFAULT_ALLOWED_SYMBOL, settings.allowedSymbol, "default allowed symbol");
        assertFalse(settings.allowNonLoopback, "non-loopback default");
        assertFalse(settings.allowAnySymbol, "allow-any-symbol default");
        assertEquals(50_000, settings.queueCapacity, "default queue capacity");
        testsRun++;
    }

    private void testForwarderSettingsMigrationAndEndpointFallback() {
        ForwarderSettings stale = new ForwarderSettings();
        stale.websocketUrl = null;
        stale.allowedSymbol = null;
        stale.allowNonLoopback = true;
        stale.queueCapacity = -1;

        ForwarderSettings sanitized = ForwarderSettings.sanitized(stale);
        assertEquals(BridgeConfig.DEFAULT_URL, sanitized.websocketUrl, "missing URL fallback");
        assertFalse(sanitized.allowNonLoopback, "fallback remains loopback-only");
        assertEquals(BridgeConfig.DEFAULT_ALLOWED_SYMBOL, sanitized.allowedSymbol, "missing symbol fallback");
        assertEquals(ForwarderSettings.MIN_QUEUE_CAPACITY, sanitized.queueCapacity, "low queue clamp");

        ForwarderSettings remoteWithoutPermission = new ForwarderSettings();
        remoteWithoutPermission.websocketUrl = "ws://192.168.0.50:8765/bookmap";
        remoteWithoutPermission.allowNonLoopback = false;
        ForwarderSettings sanitizedRemote = ForwarderSettings.sanitized(remoteWithoutPermission);
        assertEquals(BridgeConfig.DEFAULT_URL, sanitizedRemote.websocketUrl, "remote URL fallback");
        assertFalse(sanitizedRemote.allowNonLoopback, "remote fallback does not enable non-loopback");

        ForwarderSettings invalid = new ForwarderSettings();
        invalid.websocketUrl = "not a websocket url";
        BridgeConfig config = BridgeConfig.fromSettings(invalid);
        assertEquals(URI.create(BridgeConfig.DEFAULT_URL), config.websocketUri(), "invalid endpoint config fallback");
        testsRun++;
    }

    private void testForwarderSettingsFieldsAreSerializableOnly() {
        for (Field field : ForwarderSettings.class.getDeclaredFields()) {
            if (Modifier.isStatic(field.getModifiers())) {
                continue;
            }
            Class<?> type = field.getType();
            boolean supported = type == String.class || type == boolean.class || type == int.class;
            assertTrue(supported, "Bookmap-serializable field type for " + field.getName());
            String typeName = type.getName().toLowerCase(Locale.ROOT);
            assertFalse(typeName.contains("socket"), "no socket field");
            assertFalse(typeName.contains("thread"), "no thread field");
            assertFalse(typeName.contains("executor"), "no executor field");
            assertFalse(typeName.contains("path"), "no path field");
            assertFalse(typeName.contains("callback"), "no callback field");
        }
        testsRun++;
    }

    private void testLoadSettingsSafelyHandlesBookmapSettingsErrors() {
        Object[] savedSettings = new Object[1];
        Api throwingApi = (Api) Proxy.newProxyInstance(
                Api.class.getClassLoader(),
                new Class<?>[]{Api.class},
                (proxy, method, args) -> {
                    if ("getSettings".equals(method.getName())) {
                        throw new RuntimeException("stale settings");
                    }
                    if ("setSettings".equals(method.getName())) {
                        savedSettings[0] = args[0];
                    }
                    return defaultReturnValue(method.getReturnType());
                });

        ForwarderSettings settings = MnqWebSocketForwarder.loadSettingsSafely(throwingApi);
        assertEquals(BridgeConfig.DEFAULT_URL, settings.websocketUrl, "getSettings exception URL fallback");
        assertEquals(BridgeConfig.DEFAULT_ALLOWED_SYMBOL, settings.allowedSymbol, "getSettings exception symbol fallback");
        assertTrue(savedSettings[0] instanceof ForwarderSettings, "sanitized settings saved after getSettings failure");
        testsRun++;
    }

    private void testForwarderUsesAutomaticSubscription() throws IOException {
        Class<MnqWebSocketForwarder> type = MnqWebSocketForwarder.class;
        assertTrue(CustomModule.class.isAssignableFrom(type), "CustomModule implementation");
        assertTrue(TimeListener.class.isAssignableFrom(type), "TimeListener implementation");
        assertTrue(DepthDataListener.class.isAssignableFrom(type), "DepthDataListener implementation");
        assertTrue(TradeDataListener.class.isAssignableFrom(type), "TradeDataListener implementation");
        assertTrue(HistoricalDataListener.class.isAssignableFrom(type), "HistoricalDataListener implementation");
        assertTrue(HistoricalModeListener.class.isAssignableFrom(type), "HistoricalModeListener implementation");
        assertFalse(type.isAnnotationPresent(NoAutosubscription.class), "NoAutosubscription must not be present");
        assertSourceDoesNotContainManualListenerRegistration();
        testsRun++;
    }

    private void testInitializeDoesNotCallManualListenerRegistration() {
        MnqWebSocketForwarder forwarder = new MnqWebSocketForwarder();
        Api api = apiThatFailsOnManualListenerRegistration(new ForwarderSettings());
        try {
            forwarder.initialize("MNQ", testInstrumentInfo(), api, new InitialState());
        } finally {
            forwarder.stop();
        }
        testsRun++;
    }

    private void testStopIsSafeBeforeAndAfterInitialize() {
        MnqWebSocketForwarder forwarder = new MnqWebSocketForwarder();
        forwarder.stop();
        forwarder.stop();
        Api api = apiThatFailsOnManualListenerRegistration(new ForwarderSettings());
        forwarder.initialize("MNQ", testInstrumentInfo(), api, new InitialState());
        forwarder.stop();
        forwarder.stop();
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

    private static void assertSourceDoesNotContainManualListenerRegistration() throws IOException {
        Path sourcePath = Path.of("src", "main", "java", "com", "mnq", "bookmap", "MnqWebSocketForwarder.java");
        String source = Files.readString(sourcePath, StandardCharsets.UTF_8);
        assertFalse(source.contains("api.addTimeListeners("), "manual time listener registration");
        assertFalse(source.contains("api.addDepthDataListeners("), "manual depth listener registration");
        assertFalse(source.contains("api.addTradeDataListeners("), "manual trade listener registration");
        assertFalse(source.contains("api.addHistoricalModeListeners("), "manual historical mode listener registration");
        assertFalse(source.contains("api.addHistoricalDataListeners("), "manual historical data listener registration");
        assertFalse(source.contains("@NoAutosubscription"), "NoAutosubscription annotation");
    }

    private static MessageFactory factory() {
        return new MessageFactory(InstrumentContext.synthetic("MNQ", "MNQ", 100.0, BridgeConfig.defaults()));
    }

    private static InstrumentInfo testInstrumentInfo() {
        return new InstrumentInfo("MNQ", "MNQ", "MNQ", 100.0, 1.0, "MNQ", true, 1.0);
    }

    private static Api apiThatFailsOnManualListenerRegistration(ForwarderSettings settings) {
        return (Api) Proxy.newProxyInstance(
                Api.class.getClassLoader(),
                new Class<?>[]{Api.class},
                (proxy, method, args) -> {
                    if (method.getName().startsWith("add") && method.getName().endsWith("Listeners")) {
                        throw new AssertionError("manual listener registration called: " + method.getName());
                    }
                    if ("getSettings".equals(method.getName())) {
                        return settings;
                    }
                    if ("setSettings".equals(method.getName())) {
                        return null;
                    }
                    return defaultReturnValue(method.getReturnType());
                });
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

    private static Object defaultReturnValue(Class<?> returnType) {
        if (returnType == void.class) {
            return null;
        }
        if (returnType == boolean.class) {
            return false;
        }
        if (returnType == int.class) {
            return 0;
        }
        if (returnType == double.class) {
            return 0.0;
        }
        if (returnType == long.class) {
            return 0L;
        }
        return null;
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
