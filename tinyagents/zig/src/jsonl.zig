const std = @import("std");

pub const max_line_bytes = 1_048_576;

/// Own one framed JSON value. Count first so a huge value cannot create an
/// unbounded serialization buffer. The line limit excludes the trailing LF.
pub fn encode(allocator: std.mem.Allocator, value: anytype, options: std.json.Stringify.Options) ![]u8 {
    var buffer: [256]u8 = undefined;
    var count: std.Io.Writer.Discarding = .init(&buffer);
    try std.json.Stringify.value(value, options, &count.writer);
    if (count.fullCount() > max_line_bytes) return error.OutputTooLarge;
    const line = try allocator.alloc(u8, @intCast(count.fullCount() + 1));
    errdefer allocator.free(line);
    var writer: std.Io.Writer = .fixed(line);
    try std.json.Stringify.value(value, options, &writer);
    try writer.writeByte('\n');
    return line;
}
