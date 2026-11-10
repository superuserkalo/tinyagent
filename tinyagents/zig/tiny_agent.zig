const std = @import("std");
const builtin = @import("builtin");
const sdk = @import("tinyagent");
const Io = std.Io;

var interrupted: std.atomic.Value(bool) = .init(false);

extern "kernel32" fn SetConsoleCtrlHandler(?*const fn (u32) callconv(.winapi) c_int, c_int) callconv(.winapi) c_int;

fn consoleInterrupt(kind: u32) callconv(.winapi) c_int {
    if (kind != 0 and kind != 1) return 0;
    interrupted.store(true, .release);
    return 1;
}

fn interrupt(_: std.posix.SIG) callconv(.c) void {
    interrupted.store(true, .release);
}

fn watch(io: Io, cancellation: *Io.Event) !void {
    while (!interrupted.load(.acquire)) try io.sleep(.fromMilliseconds(5), .awake);
    cancellation.set(io);
}

pub fn main(init: std.process.Init) void {
    run(init) catch |err| {
        sdk.cli.reportError(init.io, err);
        std.process.exit(1);
    };
}

fn run(init: std.process.Init) !void {
    const args = try init.minimal.args.toSlice(init.arena.allocator());
    if (builtin.os.tag == .windows) {
        if (SetConsoleCtrlHandler(consoleInterrupt, 1) == 0) return error.SignalHandlerFailed;
        defer _ = SetConsoleCtrlHandler(consoleInterrupt, 0);
        return runCli(init, args[1..]);
    } else {
        const action: std.posix.Sigaction = .{ .handler = .{ .handler = interrupt }, .mask = std.posix.sigemptyset(), .flags = 0 };
        var previous_int: std.posix.Sigaction = undefined;
        var previous_term: std.posix.Sigaction = undefined;
        std.posix.sigaction(.INT, &action, &previous_int);
        defer std.posix.sigaction(.INT, &previous_int, null);
        std.posix.sigaction(.TERM, &action, &previous_term);
        defer std.posix.sigaction(.TERM, &previous_term, null);
        return runCli(init, args[1..]);
    }
}

fn runCli(init: std.process.Init, args: []const []const u8) !void {
    var cancellation: Io.Event = .unset;
    const Work = struct {
        result: ?anyerror!void = null,

        fn perform(self: *@This(), context: std.process.Init, arguments: []const []const u8, signal: *Io.Event) void {
            self.result = sdk.cli.run(context, arguments, signal);
        }
    };
    var work: Work = .{};
    const Outcome = union(enum) { work: void, signal: Io.Cancelable!void };
    var outcomes: [2]Outcome = undefined;
    var select: Io.Select(Outcome) = .init(init.io, &outcomes);
    defer {
        cancellation.set(init.io);
        select.cancelDiscard();
    }
    try select.concurrent(.work, Work.perform, .{ &work, init, args, &cancellation });
    try select.concurrent(.signal, watch, .{ init.io, &cancellation });
    _ = select.await() catch {};
    cancellation.set(init.io);
    select.cancelDiscard();
    work.result.? catch |err| if (err != error.Canceled) return err;
}
