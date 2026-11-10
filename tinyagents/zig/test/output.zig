const std = @import("std");
const output = @import("rpc_output");
const testing = std.testing;
const io = testing.io;

// Keep the worker paused until the entire queue is ready. Then drain every
// owned frame, including when allocation or admission failed partway through.
fn flush(writer: *output.Output) void {
    const file = std.Io.Dir.cwd().openFile(io, "/dev/null", .{ .mode = .write_only }) catch unreachable;
    defer file.close(io);
    writer.closing = true;
    writer.work(file);
}

test "output line limit excludes its LF and counts escaped UTF-8 bytes" {
    var writer: output.Output = .{ .allocator = testing.allocator, .io = io };
    defer flush(&writer);
    const text = try testing.allocator.alloc(u8, output.max_line_bytes - 2);
    defer testing.allocator.free(text);
    @memset(text, 'x');
    const exact = try writer.enqueue(text);
    defer exact.release();
    try testing.expect(!exact.oversized);
    try testing.expectEqual(output.max_line_bytes + 1, exact.line.len);
    text[0] = '\n';
    const oversized = try writer.enqueue(text);
    defer oversized.release();
    try testing.expect(oversized.oversized);
    try testing.expect(std.mem.indexOf(u8, oversized.line, "output_too_large") != null);
    const unicode = try writer.enqueue("café\n");
    defer unicode.release();
    try testing.expectEqualStrings("\"café\\n\"\n", unicode.line);
}

test "queue overflow preserves its bounded diagnostic and closes admission" {
    var writer: output.Output = .{ .allocator = testing.allocator, .io = io };
    defer flush(&writer);
    const text = try testing.allocator.alloc(u8, 262_144);
    defer testing.allocator.free(text);
    @memset(text, 'x');
    for (0..15) |_| {
        const frame = try writer.enqueue(text);
        frame.release();
    }
    try testing.expectError(error.OutputQueueFull, writer.enqueue(text));
    try testing.expect(writer.failed.isSet());
    try testing.expect(writer.bytes <= 4_194_304);
    try testing.expect(std.mem.indexOf(u8, writer.tail.?.line, "output_queue_full") != null);
    try testing.expectError(error.OutputQueueFull, writer.enqueue("later"));
}

fn allocationCase(allocator: std.mem.Allocator, large: []const u8, overflow: bool) !void {
    var writer: output.Output = .{ .allocator = allocator, .io = io };
    defer flush(&writer);
    for (0..if (overflow) @as(usize, 16) else 1) |_| {
        const frame = writer.enqueue(large) catch |err| {
            if (overflow and err == error.OutputQueueFull) return;
            return err;
        };
        frame.release();
    }
}

test "every allocation failure releases an ordinary output frame" {
    try testing.checkAllAllocationFailures(testing.allocator, allocationCase, .{ "small", false });
}

test "every allocation failure releases an oversized replacement" {
    const large = try testing.allocator.alloc(u8, output.max_line_bytes);
    defer testing.allocator.free(large);
    @memset(large, 'x');
    try testing.checkAllAllocationFailures(testing.allocator, allocationCase, .{ large, false });
}

test "every allocation failure releases a full queue and overflow diagnostic" {
    const large = try testing.allocator.alloc(u8, 262_144);
    defer testing.allocator.free(large);
    @memset(large, 'x');
    try testing.checkAllAllocationFailures(testing.allocator, allocationCase, .{ large, true });
}
