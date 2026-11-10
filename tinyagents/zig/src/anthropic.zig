const std = @import("std");
const types = @import("types.zig");
const http = @import("provider_http.zig");
const json = @import("provider_json.zig");
const history = @import("history.zig");
const Value = std.json.Value;
const Allocator = std.mem.Allocator;

pub const AnthropicProvider = struct {
    api_key: []const u8,
    endpoint: []const u8 = "https://api.anthropic.com/v1/messages",
    timeout_ms: u32 = 180_000,
    max_tokens: u32 = 8192,

    pub fn provider(self: *AnthropicProvider) types.Provider {
        return .{ .context = self, .stream = stream };
    }

    fn stream(context: ?*anyopaque, allocator: Allocator, io: std.Io, request: types.ProviderRequest, on_text: types.TextHandler) !types.ProviderTurn {
        const self: *AnthropicProvider = @ptrCast(@alignCast(context.?));
        if (self.api_key.len == 0) return error.MissingApiKey;
        if (self.max_tokens == 0) return error.InvalidMaxTokens;
        for (request.history) |message| {
            if (message != .assistant or message.assistant.replay.len == 0) continue;
            if (message.assistant.replay.len != 1) return error.InvalidHistory;
            const content = json.field(message.assistant.replay[0], "content") catch return error.InvalidHistory;
            if (content != .array) return error.InvalidHistory;
        }
        const body = try std.json.Stringify.valueAlloc(allocator, Request{ .value = request, .max_tokens = self.max_tokens }, .{});
        defer allocator.free(body);
        return http.stream(Response, allocator, io, .{
            .endpoint = self.endpoint,
            .headers = &.{
                .{ .name = "x-api-key", .value = self.api_key },
                .{ .name = "anthropic-version", .value = "2023-06-01" },
            },
            .timeout_ms = self.timeout_ms,
        }, body, on_text);
    }
};

const Request = struct {
    value: types.ProviderRequest,
    max_tokens: u32,

    pub fn jsonStringify(self: Request, out: *std.json.Stringify) !void {
        try out.beginObject();
        try out.objectField("model");
        try out.write(self.value.model);
        if (self.value.instructions.len != 0) {
            try out.objectField("system");
            try out.write(self.value.instructions);
        }
        try out.objectField("max_tokens");
        try out.write(self.max_tokens);
        try out.objectField("stream");
        try out.write(true);
        if (self.value.tools.len != 0) {
            try out.objectField("tools");
            try out.beginArray();
            for (self.value.tools) |tool| try out.write(.{
                .name = tool.name,
                .description = tool.description,
                .input_schema = tool.parameters,
            });
            try out.endArray();
        }
        try out.objectField("messages");
        try out.beginArray();
        var index: usize = 0;
        while (index < self.value.history.len) : (index += 1) {
            const message = self.value.history[index];
            try out.beginObject();
            try out.objectField("role");
            try out.write(if (message == .assistant) "assistant" else "user");
            try out.objectField("content");
            if (message == .assistant and message.assistant.replay.len != 0) {
                try out.write(message.assistant.replay[0].object.get("content").?);
            } else {
                try out.beginArray();
                switch (message) {
                    .user => |text| try out.write(.{ .type = "text", .text = text }),
                    .assistant => |assistant| {
                        if (assistant.text.len != 0) try out.write(.{ .type = "text", .text = assistant.text });
                        for (assistant.tool_calls) |call| try out.write(.{
                            .type = "tool_use",
                            .id = call.call_id,
                            .name = call.name,
                            .input = call.arguments,
                        });
                    },
                    .tool_result => {
                        while (true) {
                            const result = self.value.history[index].tool_result;
                            try out.write(.{
                                .type = "tool_result",
                                .tool_use_id = result.call_id,
                                .content = result.output,
                                .is_error = result.is_error,
                            });
                            if (index + 1 == self.value.history.len or self.value.history[index + 1] != .tool_result) break;
                            index += 1;
                        }
                    },
                }
                try out.endArray();
            }
            try out.endObject();
        }
        try out.endArray();
        try out.endObject();
    }
};

const Block = struct {
    value: Value,
    kind: []const u8,
    open: bool = true,
    text: std.ArrayList(u8) = .empty,
    thinking: std.ArrayList(u8) = .empty,
    signature: std.ArrayList(u8) = .empty,
    arguments: std.ArrayList(u8) = .empty,
};

const Response = struct {
    allocator: Allocator,
    on_text: types.TextHandler,
    message: ?Value = null,
    usage: std.json.ObjectMap = .empty,
    blocks: std.AutoArrayHashMapUnmanaged(usize, Block) = .empty,
    result: ?types.ProviderTurn = null,

    pub fn consume(self: *Response, io: std.Io, event: Value) !void {
        const kind = try json.identifier(event, "type");
        if (json.equal(kind, "message_start")) {
            if (self.message != null) return error.InvalidProviderResponse;
            const message = try json.field(event, "message");
            if (message != .object) return error.InvalidProviderResponse;
            if (message.object.get("content")) |content| {
                if (content != .array or content.array.items.len != 0) return error.InvalidProviderResponse;
            }
            self.message = try history.cloneJson(self.allocator, message);
            if (message.object.get("usage")) |usage| try self.mergeUsage(usage);
        } else if (json.equal(kind, "content_block_start")) {
            if (self.message == null) return error.InvalidProviderResponse;
            const index = try json.index(event, "index");
            if (self.blocks.contains(index)) return error.InvalidProviderResponse;
            const value = try history.cloneJson(self.allocator, try json.field(event, "content_block"));
            var block: Block = .{ .value = value, .kind = try json.identifier(value, "type") };
            if (value.object.get("text")) |text| try json.append(self.allocator, &block.text, try json.string(text));
            if (value.object.get("thinking")) |thinking| try json.append(self.allocator, &block.thinking, try json.string(thinking));
            if (value.object.get("signature")) |signature| try json.append(self.allocator, &block.signature, try json.string(signature));
            try self.blocks.put(self.allocator, index, block);
            if (json.equal(block.kind, "text") and block.text.items.len != 0) try self.on_text.emit(io, block.text.items);
        } else if (json.equal(kind, "content_block_delta")) {
            const block = try self.openBlock(event);
            const delta = try json.field(event, "delta");
            const delta_kind = try json.identifier(delta, "type");
            if (json.equal(delta_kind, "text_delta")) {
                if (!json.equal(block.kind, "text")) return error.InvalidProviderResponse;
                const text = try json.namedString(delta, "text");
                try json.append(self.allocator, &block.text, text);
                if (text.len != 0) try self.on_text.emit(io, text);
            } else if (json.equal(delta_kind, "thinking_delta") or json.equal(delta_kind, "signature_delta")) {
                if (!json.equal(block.kind, "thinking")) return error.InvalidProviderResponse;
                if (json.equal(delta_kind, "thinking_delta")) {
                    try json.append(self.allocator, &block.thinking, try json.namedString(delta, "thinking"));
                } else {
                    try json.append(self.allocator, &block.signature, try json.namedString(delta, "signature"));
                }
            } else if (json.equal(delta_kind, "input_json_delta")) {
                if (!json.equal(block.kind, "tool_use")) return error.InvalidProviderResponse;
                try json.append(self.allocator, &block.arguments, try json.namedString(delta, "partial_json"));
            }
        } else if (json.equal(kind, "content_block_stop")) {
            const block = try self.openBlock(event);
            block.open = false;
            if (json.equal(block.kind, "text")) {
                try block.value.object.put(self.allocator, "text", .{ .string = block.text.items });
            } else if (json.equal(block.kind, "thinking")) {
                try block.value.object.put(self.allocator, "thinking", .{ .string = block.thinking.items });
                try block.value.object.put(self.allocator, "signature", .{ .string = block.signature.items });
            } else if (json.equal(block.kind, "tool_use")) {
                const arguments = if (block.arguments.items.len != 0)
                    try json.parse(self.allocator, block.arguments.items)
                else
                    try json.field(block.value, "input");
                if (arguments != .object) return error.InvalidToolArguments;
                try block.value.object.put(self.allocator, "input", arguments);
            }
        } else if (json.equal(kind, "message_delta")) {
            const message = if (self.message) |*value| value else return error.InvalidProviderResponse;
            const delta = try json.field(event, "delta");
            if (delta != .object) return error.InvalidProviderResponse;
            var fields = delta.object.iterator();
            while (fields.next()) |field| try message.object.put(self.allocator, try self.allocator.dupe(u8, field.key_ptr.*), try history.cloneJson(self.allocator, field.value_ptr.*));
            if (event.object.get("usage")) |usage| try self.mergeUsage(usage);
        } else if (json.equal(kind, "message_stop")) {
            self.result = try self.complete();
        } else if (json.equal(kind, "error")) {
            return error.ProviderStreamFailed;
        }
    }

    fn openBlock(self: *Response, event: Value) !*Block {
        const block = self.blocks.getPtr(try json.index(event, "index")) orelse return error.InvalidProviderResponse;
        if (!block.open) return error.InvalidProviderResponse;
        return block;
    }

    fn mergeUsage(self: *Response, value: Value) !void {
        if (value != .object) return error.InvalidProviderResponse;
        var fields = value.object.iterator();
        while (fields.next()) |field| try self.usage.put(self.allocator, try self.allocator.dupe(u8, field.key_ptr.*), try history.cloneJson(self.allocator, field.value_ptr.*));
    }

    fn complete(self: *Response) !types.ProviderTurn {
        var message = self.message orelse return error.IncompleteProviderResponse;
        const reason = try json.identifier(message, "stop_reason");
        if (json.equal(reason, "max_tokens") or json.equal(reason, "model_context_window_exceeded")) return error.IncompleteProviderResponse;
        const indexes = try self.allocator.dupe(usize, self.blocks.keys());
        std.mem.sort(usize, indexes, {}, std.sort.asc(usize));
        const content = try self.allocator.alloc(Value, indexes.len);
        var calls: std.ArrayList(types.ToolCall) = .empty;
        var text: std.ArrayList(u8) = .empty;
        for (indexes, content) |index, *value| {
            const block = self.blocks.get(index).?;
            if (block.open) return error.IncompleteProviderResponse;
            value.* = block.value;
            if (json.equal(block.kind, "text")) {
                try json.append(self.allocator, &text, block.text.items);
            } else if (json.equal(block.kind, "tool_use")) {
                const id = try json.identifier(value.*, "id");
                for (calls.items) |call| if (json.equal(call.call_id, id)) return error.DuplicateToolCall;
                try calls.append(self.allocator, .{
                    .call_id = id,
                    .name = try json.identifier(value.*, "name"),
                    .arguments = try json.field(value.*, "input"),
                });
            }
        }
        try message.object.put(self.allocator, "content", .{ .array = std.json.Array.fromOwnedSlice(self.allocator, content) });
        try message.object.put(self.allocator, "usage", .{ .object = self.usage });
        const replay = try self.allocator.alloc(Value, 1);
        replay[0] = message;
        return .{ .assistant = .{ .text = text.items, .tool_calls = calls.items, .replay = replay }, .output = content };
    }
};
