const std = @import("std");
const sdk = @import("tinyagent");
const Io = std.Io;

var interrupted: std.atomic.Value(bool) = .init(false);

fn interrupt(_: std.posix.SIG) callconv(.c) void {
    interrupted.store(true, .release);
}

fn watch(io: Io, cancellation: *Io.Event) !void {
    while (!interrupted.load(.acquire)) try io.sleep(.fromMilliseconds(5), .awake);
    cancellation.set(io);
}

const Provider = struct {
    bash_command: []const u8,
    active: usize = 0,

    fn stream(context: ?*anyopaque, allocator: std.mem.Allocator, io: Io, request: sdk.ProviderRequest, text: sdk.TextHandler) !sdk.ProviderTurn {
        const self: *Provider = @ptrCast(@alignCast(context.?));
        self.active += 1;
        defer self.active -= 1;
        const last = request.history[request.history.len - 1];
        if (last == .tool_result) return .{ .assistant = .{ .text = "tool done" } };
        const prompt = last.user;
        if (std.mem.eql(u8, prompt, "wait")) {
            try text.emit(io, "waiting");
            var forever: Io.Event = .unset;
            try forever.wait(io);
        }
        if (std.mem.eql(u8, prompt, "large") or std.mem.eql(u8, prompt, "final_large")) {
            const value = try allocator.alloc(u8, 1_000_000);
            @memset(value, 'x');
            if (std.mem.eql(u8, prompt, "large")) try text.emit(io, value);
            return .{ .assistant = .{ .text = value } };
        }
        if (std.mem.eql(u8, prompt, "bash") or std.mem.eql(u8, prompt, "tool_start") or std.mem.eql(u8, prompt, "tool_end")) {
            const command = if (std.mem.eql(u8, prompt, "bash")) self.bash_command else if (std.mem.eql(u8, prompt, "tool_end")) "printf '%100000s' ''" else blk: {
                const padding = try allocator.alloc(u8, 131_072);
                @memset(padding, ' ');
                break :blk try std.fmt.allocPrint(allocator, "printf {s}x", .{padding});
            };
            var arguments: std.json.ObjectMap = .empty;
            try arguments.put(allocator, "command", .{ .string = command });
            const calls = try allocator.alloc(sdk.ToolCall, 1);
            calls[0] = .{ .call_id = "one", .name = "bash", .arguments = .{ .object = arguments } };
            return .{ .assistant = .{ .text = "", .tool_calls = calls } };
        }
        const value = if (prompt.len > 32) "accepted" else prompt;
        try text.emit(io, value);
        return .{ .assistant = .{ .text = value } };
    }
};

pub fn main(init: std.process.Init) !void {
    const action: std.posix.Sigaction = .{ .handler = .{ .handler = interrupt }, .mask = std.posix.sigemptyset(), .flags = 0 };
    var previous: std.posix.Sigaction = undefined;
    std.posix.sigaction(.INT, &action, &previous);
    defer std.posix.sigaction(.INT, &previous, null);
    var cancellation: Io.Event = .unset;
    var monitor = try init.io.concurrent(watch, .{ init.io, &cancellation });
    defer monitor.cancel(init.io) catch {};
    var provider: Provider = .{ .bash_command = init.environ_map.get("TINYAGENT_TEST_BASH") orelse "printf observation" };
    var tool = try sdk.bashTool(init.gpa);
    defer tool.deinit();
    const agent: sdk.Agent = .{ .provider = .{ .context = &provider, .stream = Provider.stream }, .model = "fixture", .tools = &.{&tool} };
    const result = sdk.console.run(init.gpa, init.io, agent, .{ .input = .stdin(), .output = .stdout(), .diagnostics = .stderr() }, &cancellation);
    if (provider.active != 0) return error.ProviderLeaked;
    result catch |err| {
        sdk.cli.reportError(init.io, err);
        std.process.exit(1);
    };
}
