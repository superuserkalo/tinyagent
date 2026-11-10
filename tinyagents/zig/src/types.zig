const std = @import("std");

pub const ToolCall = struct {
    call_id: []const u8,
    name: []const u8,
    arguments: std.json.Value,
};

/// Schema metadata for provider requests. Construct a Tool to validate and execute it.
pub const ToolDefinition = struct {
    name: []const u8,
    description: []const u8,
    parameters: std.json.Value,
};

pub const AssistantMessage = struct {
    text: []const u8,
    tool_calls: []const ToolCall = &.{},
    replay: []const std.json.Value = &.{},
};

pub const ToolResultMessage = struct {
    call_id: []const u8,
    name: []const u8,
    output: []const u8,
    is_error: bool = false,
};

pub const HistoryMessage = union(enum) {
    user: []const u8,
    assistant: AssistantMessage,
    tool_result: ToolResultMessage,
};

pub const ProviderRequest = struct {
    model: []const u8,
    instructions: []const u8,
    history: []const HistoryMessage,
    tools: []const ToolDefinition = &.{},
};

pub const ProviderTurn = struct {
    assistant: AssistantMessage,
    output: []const std.json.Value = &.{},
};

/// Providers borrow request data. The supplied allocator is the run's arena;
/// providers must not deinitialize it. Allocate returned data with `allocator`,
/// or borrow it from storage that survives until the caller resumes. They call
/// on_text synchronously in order, propagate its errors, and finish cleanup
/// before returning. Use the supplied Io for cancellable I/O, and propagate
/// error.Canceled. The same provider may be called by independent runs.
pub const Provider = struct {
    context: ?*anyopaque = null,
    stream: *const fn (
        context: ?*anyopaque,
        allocator: std.mem.Allocator,
        io: std.Io,
        request: ProviderRequest,
        on_text: TextHandler,
    ) anyerror!ProviderTurn,
};

/// Text and events are borrowed for the duration of the callback. A handler
/// that retains data must copy it. Returning an error stops delivery and the run.
pub const TextHandler = struct {
    context: ?*anyopaque = null,
    callback: *const fn (?*anyopaque, std.Io, []const u8) anyerror!void,

    pub fn emit(self: TextHandler, io: std.Io, text: []const u8) !void {
        try self.callback(self.context, io, text);
    }
};

pub const EventHandler = struct {
    context: ?*anyopaque = null,
    callback: *const fn (?*anyopaque, std.Io, Event) anyerror!void,

    pub fn emit(self: EventHandler, io: std.Io, event: Event) !void {
        try self.callback(self.context, io, event);
    }
};

pub const Event = struct {
    run_id: []const u8,
    sequence: u64,
    kind: union(enum) {
        agent_start,
        text_delta: []const u8,
        tool_execution_start: ToolCall,
        tool_execution_end: ToolResultMessage,
        completed: []const u8,
        failed: anyerror,
        aborted,
    },

    /// Serialize the shared protocol without allocating an intermediate object.
    pub fn jsonStringify(self: Event, json: *std.json.Stringify) !void {
        try json.beginObject();
        try json.objectField("runId");
        try json.write(self.run_id);
        try json.objectField("sequence");
        try json.write(self.sequence);
        try json.objectField("type");
        switch (self.kind) {
            .agent_start => try json.write("agent_start"),
            .text_delta => |text| {
                try json.write("text_delta");
                try json.objectField("delta");
                try json.write(text);
            },
            .tool_execution_start => |call| {
                try json.write("tool_execution_start");
                try json.objectField("toolCallId");
                try json.write(call.call_id);
                try json.objectField("toolName");
                try json.write(call.name);
                try json.objectField("arguments");
                try json.write(call.arguments);
            },
            .tool_execution_end => |result| {
                try json.write("tool_execution_end");
                try json.objectField("toolCallId");
                try json.write(result.call_id);
                try json.objectField("toolName");
                try json.write(result.name);
                try json.objectField("output");
                try json.write(result.output);
                try json.objectField("isError");
                try json.write(result.is_error);
            },
            .completed, .failed, .aborted => {
                try json.write("agent_end");
                try json.objectField("status");
                switch (self.kind) {
                    .completed => |text| {
                        try json.write("completed");
                        try json.objectField("text");
                        try json.write(text);
                    },
                    .failed => |err| {
                        try json.write("failed");
                        try json.objectField("error");
                        try json.write(.{
                            .code = "provider_error",
                            .message = @errorName(err),
                            .retryable = false,
                        });
                    },
                    .aborted => try json.write("aborted"),
                    else => unreachable,
                }
            },
        }
        try json.endObject();
    }
};
