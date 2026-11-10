const std = @import("std");
const Io = std.Io;

/// Cancellation may stop a producer, but its pending frame still gets one
/// second to drain. Return whether this wait consumed native cancellation;
/// the caller restores it after inspecting the delivery result.
pub fn wait(done: *Io.Event, io: Io, draining: bool) !bool {
    var cancelled = false;
    if (!draining) done.wait(io) catch {
        cancelled = true;
    };
    if (draining or cancelled) try drain(done, io);
    return cancelled;
}

pub fn drain(done: *Io.Event, io: Io) !void {
    const protection = io.swapCancelProtection(.blocked);
    defer _ = io.swapCancelProtection(protection);
    const deadline = Io.Clock.Timestamp.fromNow(io, .{ .raw = .fromSeconds(1), .clock = .awake });
    while (!done.isSet()) {
        done.waitTimeout(io, .{ .deadline = deadline }) catch |err| switch (err) {
            error.Canceled => unreachable,
            error.Timeout => if (deadline.durationFromNow(io).raw.nanoseconds <= 0) return error.OutputDrainTimeout,
        };
    }
}
