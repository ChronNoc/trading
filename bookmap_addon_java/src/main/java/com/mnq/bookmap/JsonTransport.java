package com.mnq.bookmap;

import java.net.URI;

/** Minimal transport interface used by the asynchronous sender. */
public interface JsonTransport {
    void connect(URI uri);

    boolean send(String payload);

    boolean isOpen();

    void close();
}
