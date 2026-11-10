package dev.tinyagent;

import java.util.List;
import java.util.concurrent.CountDownLatch;

/** A native subprocess host for transport checks, never a live provider client. */
public final class RpcFixture {
    private RpcFixture() {}

    public static void main(String[] args) throws Exception {
        Provider provider = (request, token, text) -> {
            if (request.history().getLast() instanceof Message.ToolResult result) {
                return CoreChecks.turn(result.output());
            }
            String prompt = ((Message.User) request.history().getLast()).text();
            if (prompt.startsWith("tool")) {
                var arguments = Json.MAPPER.createObjectNode().put("text", prompt.equals("tool_invalid") ? "" : prompt);
                return new ProviderTurn("", List.of(new ToolCall("call", "echo", arguments)), List.of(), List.of());
            }
            if (prompt.equals("wait")) {
                var cancelled = new CountDownLatch(1);
                var registration = token.onCancel(cancelled::countDown);
                System.err.println("provider-waiting");
                try { cancelled.await(); token.check(); }
                finally { registration.close(); System.err.println("provider-cleaned"); }
            }
            if (prompt.equals("large")) {
                text.accept("x".repeat(RpcProtocol.MAX_LINE_BYTES));
                throw new AssertionError("Oversized output was accepted");
            }
            String answer = prompt.equals("history") ? "messages=" + request.history().size() : "done\ncafé";
            text.accept(answer);
            if (prompt.equals("fail")) throw new ProviderException("fixture failed");
            return new ProviderTurn(answer, List.of(), List.of(), List.of());
        };
        var echo = new Tool("echo", "Return text", Json.parse("""
                {"type":"object","properties":{"text":{"type":"string","minLength":1}},"required":["text"]}
                """), (arguments, token) -> {
            if (arguments.path("text").stringValue().equals("tool_wait")) {
                var cancelled = new CountDownLatch(1);
                var registration = token.onCancel(cancelled::countDown);
                System.err.println("tool-waiting");
                try { cancelled.await(); token.check(); }
                finally { registration.close(); System.err.println("tool-cleaned"); }
            }
            return arguments.path("text").stringValue();
        });
        try {
            Rpc.runStdio(new Agent(provider, "fixture-model", "Fixture instructions", List.of(echo)));
        } catch (Exception failure) {
            System.err.println("RPC failed: " + failure);
            throw failure;
        }
    }
}
