package dev.tinyagent;

import java.io.IOException;

public final class ProviderException extends IOException {
    private static final long serialVersionUID = 1L;

    public ProviderException(String message) {
        super(message);
    }

    public ProviderException(String message, Throwable cause) {
        super(message, cause);
    }
}
