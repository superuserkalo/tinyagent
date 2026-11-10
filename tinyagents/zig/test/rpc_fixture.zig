const std = @import("std");
const builtin = @import("builtin");
const sdk = @import("tinyagent");
const Io = std.Io;

var interrupted: std.atomic.Value(bool) = .init(false);
var original_io: Io = undefined;

// Observe actual native cancellation without replacing its behavior. Pipe
// tests wait for this marker before allowing a partial event write to finish.
fn observedWait(context: ?*anyopaque, pointer: *const u32, expected: u32, timeout: Io.Timeout) Io.Cancelable!void {
    original_io.vtable.futexWait(context, pointer, expected, timeout) catch |err| {
        Io.File.stderr().writeStreamingAll(original_io, "cancelled wait\n") catch {};
        return err;
    };
}

fn statusFlags(fd: std.posix.fd_t) !c_int {
    const flags = std.posix.system.fcntl(fd, std.posix.F.GETFL, @as(c_int, 0));
    if (flags < 0) return error.DescriptorClosed;
    // Darwin exposes FWASWRITTEN after any successful write. It is a kernel
    // observation, not a caller-set mode: xnu/bsd/sys/fcntl.h and sys_generic.c.
    return if (builtin.os.tag == .macos) flags & ~@as(c_int, 0x10000) else flags;
}

fn interrupt(_: std.posix.SIG) callconv(.c) void {
    interrupted.store(true, .release);
}

fn watch(io: Io, cancellation: *Io.Event) !void {
    while (!interrupted.load(.acquire)) try io.sleep(.fromMilliseconds(5), .awake);
    cancellation.set(io);
}

const Provider = struct {
    cleanup_file: ?Io.File = null,

    fn stream(context: ?*anyopaque, allocator: std.mem.Allocator, io: Io, request: sdk.ProviderRequest, text: sdk.TextHandler) !sdk.ProviderTurn {
        const self: *Provider = @ptrCast(@alignCast(context.?));
        const prompt = request.history[request.history.len - 1].user;
        defer {
            Io.File.stderr().writeStreamingAll(io, "cleanup\n") catch {};
            if (std.mem.eql(u8, prompt, "hold")) {
                const protection = io.swapCancelProtection(.blocked);
                defer _ = io.swapCancelProtection(protection);
                if (self.cleanup_file) |file| {
                    var buffer: [1]u8 = undefined;
                    _ = file.readStreaming(io, &.{&buffer}) catch {};
                }
            }
        }
        if (std.mem.eql(u8, prompt, "wait") or std.mem.eql(u8, prompt, "hold")) {
            try text.emit(io, "waiting");
            var forever: Io.Event = .unset;
            try forever.wait(io);
        }
        if (std.mem.eql(u8, prompt, "fail")) {
            try text.emit(io, "partial");
            return error.ProviderUnavailable;
        }
        if (std.mem.eql(u8, prompt, "history")) {
            const value = try std.fmt.allocPrint(allocator, "{d}", .{request.history.len});
            try text.emit(io, value);
            return .{ .assistant = .{ .text = value } };
        }
        if (std.mem.eql(u8, prompt, "oversize") or std.mem.eql(u8, prompt, "large") or std.mem.eql(u8, prompt, "final_large")) {
            const value = try allocator.alloc(u8, if (std.mem.eql(u8, prompt, "oversize")) 1_048_576 else 262_144);
            @memset(value, 'x');
            if (!std.mem.eql(u8, prompt, "final_large")) try text.emit(io, value);
            return .{ .assistant = .{ .text = value } };
        }
        try text.emit(io, prompt);
        return .{ .assistant = .{ .text = prompt } };
    }
};

pub fn main(init: std.process.Init) !void {
    original_io = init.io;
    var observed_vtable = init.io.vtable.*;
    observed_vtable.futexWait = observedWait;
    const observed_io: Io = .{ .userdata = init.io.userdata, .vtable = &observed_vtable };
    const args = try init.minimal.args.toSlice(init.arena.allocator());
    var provider: Provider = .{};
    if (args.len > 1) provider.cleanup_file = .{ .handle = try std.fmt.parseInt(std.posix.fd_t, args[1], 10), .flags = .{ .nonblocking = false } };
    const agent: sdk.Agent = .{ .provider = .{ .context = &provider, .stream = Provider.stream }, .model = "offline" };
    var cancellation: Io.Event = .unset;
    const action: std.posix.Sigaction = .{ .handler = .{ .handler = interrupt }, .mask = std.posix.sigemptyset(), .flags = 0 };
    var previous: std.posix.Sigaction = undefined;
    std.posix.sigaction(.INT, &action, &previous);
    defer std.posix.sigaction(.INT, &previous, null);
    var monitor = try init.io.concurrent(watch, .{ init.io, &cancellation });
    defer monitor.cancel(init.io) catch {};
    const input_flags = try statusFlags(0);
    const output_flags = try statusFlags(1);
    const input_descriptor = std.posix.system.fcntl(0, std.posix.F.GETFD, @as(c_int, 0));
    const output_descriptor = std.posix.system.fcntl(1, std.posix.F.GETFD, @as(c_int, 0));
    const result = sdk.runRpc(init.gpa, observed_io, agent, .stdin(), .stdout(), .{ .cancellation = &cancellation });
    const after_input = try statusFlags(0);
    const after_output = try statusFlags(1);
    if (input_flags != after_input or output_flags != after_output) {
        var diagnostics = Io.File.stderr().writerStreaming(init.io, &.{});
        try diagnostics.interface.print("flags: input {d} -> {d}, output {d} -> {d}\n", .{ input_flags, after_input, output_flags, after_output });
        return error.DescriptorChanged;
    }
    if (input_descriptor != std.posix.system.fcntl(0, std.posix.F.GETFD, @as(c_int, 0)) or
        output_descriptor != std.posix.system.fcntl(1, std.posix.F.GETFD, @as(c_int, 0))) return error.DescriptorChanged;
    try Io.File.stderr().writeStreamingAll(init.io, "descriptors preserved\n");
    try result;
}
