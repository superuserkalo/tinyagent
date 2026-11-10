const std = @import("std");
const Value = std.json.Value;

pub const max_bytes = 1024 * 1024;

pub fn field(value: Value, name: []const u8) !Value {
    if (value != .object) return error.InvalidProviderResponse;
    return value.object.get(name) orelse error.InvalidProviderResponse;
}

pub fn string(value: Value) ![]const u8 {
    if (value != .string) return error.InvalidProviderResponse;
    return value.string;
}

pub fn namedString(value: Value, name: []const u8) ![]const u8 {
    return string(try field(value, name));
}

pub fn identifier(value: Value, name: []const u8) ![]const u8 {
    const result = try namedString(value, name);
    if (result.len == 0) return error.InvalidProviderResponse;
    return result;
}

pub fn index(value: Value, name: []const u8) !usize {
    return switch (try field(value, name)) {
        .integer => |number| std.math.cast(usize, number) orelse error.InvalidProviderResponse,
        .number_string => |number| std.fmt.parseInt(usize, number, 10) catch error.InvalidProviderResponse,
        else => error.InvalidProviderResponse,
    };
}

pub fn parse(allocator: std.mem.Allocator, bytes: []const u8) !Value {
    if (bytes.len > max_bytes) return error.ProviderDataTooLarge;
    if (!std.unicode.utf8ValidateSlice(bytes)) return error.InvalidUtf8;
    return std.json.parseFromSliceLeaky(Value, allocator, bytes, .{
        .parse_numbers = false,
        .allocate = .alloc_always,
        .max_value_len = max_bytes,
    }) catch |err| return if (err == error.OutOfMemory) err else error.InvalidProviderResponse;
}

pub fn append(allocator: std.mem.Allocator, buffer: *std.ArrayList(u8), bytes: []const u8) !void {
    if (bytes.len > max_bytes - buffer.items.len) return error.ProviderDataTooLarge;
    try buffer.appendSlice(allocator, bytes);
}

pub fn equal(a: []const u8, b: []const u8) bool {
    return std.mem.eql(u8, a, b);
}
