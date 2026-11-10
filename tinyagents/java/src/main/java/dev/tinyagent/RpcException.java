package dev.tinyagent;

import java.io.IOException;

/** A local RPC transport failure, with the same code used on the wire. */
public final class RpcException extends IOException {
    private static final long serialVersionUID = 1L;
    private final String code;

    RpcException(String code, String message) {
        super(message);
        this.code = code;
    }

    public String code() { return code; }
}
