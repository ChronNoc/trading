package com.mnq.bookmap;

import java.net.InetAddress;
import java.net.URI;
import java.net.UnknownHostException;
import java.util.Locale;

/** Validates that the forwarder remains local by default. */
public final class EndpointPolicy {
    private EndpointPolicy() {
    }

    public static boolean isAllowed(URI uri, boolean allowNonLoopback) {
        if (uri == null || !"ws".equalsIgnoreCase(uri.getScheme())) {
            return false;
        }
        if (allowNonLoopback) {
            return true;
        }
        String host = uri.getHost();
        if (host == null || host.isBlank()) {
            return false;
        }
        String normalizedHost = host.toLowerCase(Locale.ROOT);
        if ("localhost".equals(normalizedHost) || "127.0.0.1".equals(normalizedHost) || "::1".equals(normalizedHost)) {
            return true;
        }
        try {
            return InetAddress.getByName(host).isLoopbackAddress();
        } catch (UnknownHostException error) {
            return false;
        }
    }

    public static void requireAllowed(URI uri, boolean allowNonLoopback) {
        if (!isAllowed(uri, allowNonLoopback)) {
            throw new IllegalArgumentException("Bookmap forwarder WebSocket URL must be local loopback by default");
        }
    }
}
