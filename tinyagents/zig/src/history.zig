const std = @import("std");
const types = @import("types.zig");
const Allocator = std.mem.Allocator;
const Value = std.json.Value;

pub fn text(allocator: Allocator, value: []const u8) ![]const u8 {
    if (!std.unicode.utf8ValidateSlice(value)) return error.InvalidUtf8;
    return allocator.dupe(u8, value);
}

pub fn message(allocator: Allocator, value: types.HistoryMessage) !types.HistoryMessage {
    return switch (value) {
        .user => |value_text| .{ .user = try text(allocator, value_text) },
        .assistant => |value_assistant| .{ .assistant = try assistant(allocator, value_assistant) },
        .tool_result => |result| .{ .tool_result = .{
            .call_id = try identifier(allocator, result.call_id),
            .name = try identifier(allocator, result.name),
            .output = try text(allocator, result.output),
            .is_error = result.is_error,
        } },
    };
}

pub fn identifier(allocator: Allocator, value: []const u8) ![]const u8 {
    if (value.len == 0) return error.EmptyIdentifier;
    return text(allocator, value);
}

pub fn assistant(allocator: Allocator, value: types.AssistantMessage) !types.AssistantMessage {
    const calls = try allocator.alloc(types.ToolCall, value.tool_calls.len);
    for (value.tool_calls, calls) |call, *copy| {
        if (call.arguments != .object) return error.InvalidToolArguments;
        copy.* = .{
            .call_id = try identifier(allocator, call.call_id),
            .name = try identifier(allocator, call.name),
            .arguments = try json(allocator, call.arguments, 0),
        };
    }
    return .{
        .text = try text(allocator, value.text),
        .tool_calls = calls,
        .replay = try values(allocator, value.replay),
    };
}

pub fn values(allocator: Allocator, value: []const Value) ![]const Value {
    const copies = try allocator.alloc(Value, value.len);
    for (value, copies) |item, *copy| copy.* = try json(allocator, item, 0);
    return copies;
}

pub fn cloneJson(allocator: Allocator, value: Value) !Value {
    return json(allocator, value, 0);
}

// Caller-owned histories and provider replay data must not alias the result.
// This routine uses a run arena; the caller releases partial copies on error.
fn json(allocator: Allocator, value: Value, depth: usize) anyerror!Value {
    if (depth > 128) return error.JsonTooDeep;
    return switch (value) {
        .null, .bool, .integer => value,
        .float => |number| if (std.math.isFinite(number)) value else error.InvalidJsonNumber,
        .string => |string| .{ .string = try text(allocator, string) },
        .number_string => |number| blk: {
            const parsed = std.json.parseFromSlice(Value, allocator, number, .{
                .parse_numbers = false,
            }) catch |err| return if (err == error.OutOfMemory) err else error.InvalidJsonNumber;
            defer parsed.deinit();
            if (parsed.value != .number_string) return error.InvalidJsonNumber;
            break :blk .{ .number_string = try allocator.dupe(u8, number) };
        },
        .array => |array| blk: {
            var copy = std.json.Array.init(allocator);
            try copy.ensureTotalCapacity(array.items.len);
            for (array.items) |item| try copy.append(try json(allocator, item, depth + 1));
            break :blk .{ .array = copy };
        },
        .object => |object| blk: {
            var copy: std.json.ObjectMap = .empty;
            try copy.ensureTotalCapacity(allocator, object.count());
            var entries = object.iterator();
            while (entries.next()) |entry| {
                try copy.put(allocator, try text(allocator, entry.key_ptr.*), try json(allocator, entry.value_ptr.*, depth + 1));
            }
            break :blk .{ .object = copy };
        },
    };
}
