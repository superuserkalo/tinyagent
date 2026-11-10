const std = @import("std");
const history = @import("history.zig");
const types = @import("types.zig");
const Allocator = std.mem.Allocator;

const Schema = opaque {};
extern fn tinyagent_schema_create([*]const u8, usize, *?*Schema) c_int;
extern fn tinyagent_schema_validate(*const Schema, [*]const u8, usize) c_int;
extern fn tinyagent_schema_destroy(*Schema) void;

/// The handler borrows arguments until it returns and uses the run's arena for
/// output. It may also return borrowed text that survives until the caller resumes.
/// Use the supplied Io for cancellation, propagate error.Canceled, and finish
/// cleanup before returning. Independent runs can call the same handler concurrently.
pub const ToolHandler = struct {
    context: ?*anyopaque = null,
    callback: *const fn (?*anyopaque, Allocator, std.Io, std.json.Value) anyerror![]const u8,
};

/// Owns a checked Draft 2020-12 schema and its definition. The handler context
/// remains borrowed. Do not copy ownership or modify the definition; deinit once,
/// after all executions finish. Validation never fetches external schemas.
pub const Tool = struct {
    arena: *std.heap.ArenaAllocator,
    metadata: types.ToolDefinition,
    schema: *Schema,
    handler: ToolHandler,

    pub fn init(allocator: Allocator, config: types.ToolDefinition, handler: ToolHandler) !Tool {
        if (config.parameters != .object) return error.InvalidToolSchema;
        if (config.parameters.object.contains("$async")) return error.InvalidToolSchema;
        const arena = try allocator.create(std.heap.ArenaAllocator);
        arena.* = .init(allocator);
        errdefer {
            arena.deinit();
            allocator.destroy(arena);
        }
        const memory = arena.allocator();
        const metadata: types.ToolDefinition = .{
            .name = try history.identifier(memory, config.name),
            .description = try history.text(memory, config.description),
            .parameters = try history.cloneJson(memory, config.parameters),
        };
        const encoded = try std.json.Stringify.valueAlloc(memory, metadata.parameters, .{});
        defer memory.free(encoded);
        var schema: ?*Schema = null;
        switch (tinyagent_schema_create(encoded.ptr, encoded.len, &schema)) {
            0 => {},
            1 => return error.InvalidToolSchema,
            else => return error.SchemaValidatorFailed,
        }
        return .{ .arena = arena, .metadata = metadata, .schema = schema.?, .handler = handler };
    }

    pub fn deinit(self: *Tool) void {
        tinyagent_schema_destroy(self.schema);
        const allocator = self.arena.child_allocator;
        self.arena.deinit();
        allocator.destroy(self.arena);
        self.* = undefined;
    }

    /// Returned metadata is borrowed and must not be changed. Providers receive
    /// a separate run-owned copy when the Agent constructs its request.
    pub fn definition(self: Tool) types.ToolDefinition {
        return self.metadata;
    }

    /// Pass a run arena and release it after consuming the result. The handler
    /// receives its own argument copy. Invalid arguments never reach the handler;
    /// outputs exceeding 128 KiB or containing invalid UTF-8 return tool errors.
    pub fn execute(self: Tool, allocator: Allocator, io: std.Io, arguments: std.json.Value) anyerror![]const u8 {
        try io.checkCancel();
        if (arguments != .object) return error.InvalidToolArguments;
        var temporary = std.heap.ArenaAllocator.init(allocator);
        defer temporary.deinit();
        const memory = temporary.allocator();
        const owned = try history.cloneJson(memory, arguments);
        const encoded = try std.json.Stringify.valueAlloc(memory, owned, .{});
        switch (tinyagent_schema_validate(self.schema, encoded.ptr, encoded.len)) {
            0 => {},
            1 => return error.InvalidToolArguments,
            else => return error.SchemaValidatorFailed,
        }
        try io.checkCancel();
        const output = try self.handler.callback(self.handler.context, allocator, io, owned);
        try io.checkCancel();
        if (output.len > 131_072) return error.ToolOutputTooLarge;
        if (!std.unicode.utf8ValidateSlice(output)) return error.InvalidToolOutput;
        return allocator.dupe(u8, output);
    }
};
