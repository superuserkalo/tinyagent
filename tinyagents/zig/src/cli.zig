const std = @import("std");
const Agent = @import("agent.zig").Agent;
const OpenAIProvider = @import("openai.zig").OpenAIProvider;
const AnthropicProvider = @import("anthropic.zig").AnthropicProvider;
const bash = @import("bash.zig");
const console = @import("console.zig");
const rpc = @import("rpc.zig");
const http = @import("http.zig");
const Io = std.Io;

pub const usage = "usage: tiny-agent [--self-test | --mode rpc | serve [--host IP] [--port PORT] [--token TOKEN] [--allow-remote]]";
const instructions =
    \\You are TinyAgent, a coding agent in the current directory.
    \\Inspect before changing files. Use bash for shell and file work. Each call is a fresh process.
;

pub const Mode = union(enum) { console, rpc, http: http.Options, self_test };

pub fn parse(args: []const []const u8, token: ?[]const u8) !Mode {
    if (args.len == 0) return .console;
    if (args.len == 1 and std.mem.eql(u8, args[0], "--self-test")) return .self_test;
    if (args.len == 2 and std.mem.eql(u8, args[0], "--mode") and std.mem.eql(u8, args[1], "rpc")) return .rpc;
    if (!std.mem.eql(u8, args[0], "serve")) return error.InvalidArguments;
    var options: http.Options = .{ .token = if (token) |value| if (value.len == 0) null else value else null };
    var index: usize = 1;
    while (index < args.len) : (index += 1) {
        const flag = args[index];
        if (std.mem.eql(u8, flag, "--allow-remote")) {
            options.allow_remote = true;
            continue;
        }
        if (index + 1 == args.len) return error.InvalidArguments;
        index += 1;
        const value = args[index];
        if (std.mem.eql(u8, flag, "--host")) {
            options.host = if (std.mem.eql(u8, value, "localhost")) "127.0.0.1" else value;
            _ = Io.net.IpAddress.parse(options.host, 0) catch return error.InvalidHost;
        } else if (std.mem.eql(u8, flag, "--port")) {
            if (value.len == 0) return error.InvalidPort;
            for (value) |byte| if (!std.ascii.isDigit(byte)) return error.InvalidPort;
            options.port = std.fmt.parseInt(u16, value, 10) catch return error.InvalidPort;
        } else if (std.mem.eql(u8, flag, "--token")) {
            options.token = value;
        } else return error.InvalidArguments;
    }
    return .{ .http = options };
}

/// Configure the native coding agent, then dispatch to the shared console,
/// RPC, or HTTP adapter. The executable owns signals and cancellation.
pub fn run(init: std.process.Init, args: []const []const u8, cancellation: *Io.Event) !void {
    const mode = try parse(args, init.environ_map.get("TINYAGENT_TOKEN"));
    if (mode == .self_test) {
        if (try bash.deniedReason(init.gpa, "printf 'hello\\n'") != null) return error.SafeCommandDenied;
        for ([_][]const u8{ "rm -rf /", "r\"\"m -rf /", "sudo echo hi", "git reset --hard HEAD", "find . -delete", "mkfs.ext4 /dev/sda" }) |command| {
            if (try bash.deniedReason(init.gpa, command) == null) return error.UnsafeCommandAllowed;
        }
        return console.write(init.io, .stdout(), "self-test passed\n", cancellation.isSet());
    }
    const provider_name = init.environ_map.get("TINYAGENT_PROVIDER") orelse "openai";
    const anthropic = std.mem.eql(u8, provider_name, "anthropic");
    if (!anthropic and provider_name.len != 0 and !std.mem.eql(u8, provider_name, "openai")) return error.UnknownProvider;
    const key_name = if (anthropic) "ANTHROPIC_API_KEY" else "OPENAI_API_KEY";
    const key = init.environ_map.get(key_name) orelse return if (anthropic) error.MissingAnthropicApiKey else error.MissingOpenAiApiKey;
    if (key.len == 0) return if (anthropic) error.MissingAnthropicApiKey else error.MissingOpenAiApiKey;
    var openai: OpenAIProvider = .{ .api_key = key };
    var messages: AnthropicProvider = .{ .api_key = key };
    var tool = try bash.bashTool(init.gpa);
    defer tool.deinit();
    const default_model = if (anthropic) "claude-sonnet-4-6" else "gpt-5.6";
    const configured_model = init.environ_map.get("TINYAGENT_MODEL") orelse default_model;
    const agent: Agent = .{
        .provider = if (anthropic) messages.provider() else openai.provider(),
        .model = if (configured_model.len == 0) default_model else configured_model,
        .instructions = instructions,
        .tools = &.{&tool},
    };
    switch (mode) {
        .console => try console.run(init.gpa, init.io, agent, .{ .input = .stdin(), .output = .stdout(), .diagnostics = .stderr() }, cancellation),
        .rpc => try rpc.run(init.gpa, init.io, agent, .stdin(), .stdout(), .{ .cancellation = cancellation }),
        .http => |options| {
            const server = try http.HttpServer.start(init.gpa, init.io, agent, options);
            defer server.deinit();
            const message = try std.fmt.allocPrint(init.gpa, "TinyAgent listening on {s}\n", .{server.url});
            defer init.gpa.free(message);
            try console.write(init.io, .stderr(), message, cancellation.isSet());
            const Outcome = union(enum) { stopped: anyerror!void, cancelled: Io.Cancelable!void };
            var outcomes: [2]Outcome = undefined;
            var select: Io.Select(Outcome) = .init(init.io, &outcomes);
            defer {
                server.stop();
                select.cancelDiscard();
            }
            try select.concurrent(.stopped, http.HttpServer.wait, .{server});
            try select.concurrent(.cancelled, Io.Event.wait, .{ cancellation, init.io });
            const outcome = select.await() catch Outcome{ .cancelled = error.Canceled };
            server.stop();
            select.cancelDiscard();
            if (outcome == .stopped) try outcome.stopped;
        },
        .self_test => unreachable,
    }
}

pub fn reportError(io: Io, failure: anyerror) void {
    var buffer: [512]u8 = undefined;
    const text = if (failure == error.InvalidArguments) usage else @errorName(failure);
    const message = std.fmt.bufPrint(&buffer, "error: {s}\n", .{text}) catch unreachable;
    console.write(io, .stderr(), message, true) catch {};
}
