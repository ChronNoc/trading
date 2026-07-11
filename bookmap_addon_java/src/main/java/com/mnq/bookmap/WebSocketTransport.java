package com.mnq.bookmap;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.WebSocket;
import java.util.concurrent.TimeUnit;

/** JDK WebSocket transport used by the sender thread. */
public final class WebSocketTransport implements JsonTransport {
    private final HttpClient httpClient = HttpClient.newHttpClient();
    private volatile WebSocket client;
    private volatile boolean open;

    @Override
    public void connect(URI uri) {
        if (isOpen()) {
            return;
        }
        try {
            client = httpClient.newWebSocketBuilder()
                    .buildAsync(uri, new WebSocket.Listener() {
                        @Override
                        public void onOpen(WebSocket webSocket) {
                            open = true;
                            webSocket.request(1);
                        }

                        @Override
                        public java.util.concurrent.CompletionStage<?> onClose(
                                WebSocket webSocket,
                                int statusCode,
                                String reason) {
                            open = false;
                            return WebSocket.Listener.super.onClose(webSocket, statusCode, reason);
                        }

                        @Override
                        public void onError(WebSocket webSocket, Throwable error) {
                            open = false;
                        }
                    })
                    .get(2, TimeUnit.SECONDS);
            open = true;
        } catch (Exception error) {
            open = false;
            throw new IllegalStateException("WebSocket connection was not acknowledged", error);
        }
    }

    @Override
    public boolean send(String payload) {
        WebSocket activeClient = client;
        if (activeClient == null || !isOpen()) {
            return false;
        }
        activeClient.sendText(payload, true).join();
        return true;
    }

    @Override
    public boolean isOpen() {
        return open;
    }

    @Override
    public void close() {
        WebSocket activeClient = client;
        client = null;
        open = false;
        if (activeClient != null) {
            activeClient.sendClose(WebSocket.NORMAL_CLOSURE, "MNQ forwarder stopped");
        }
    }
}
