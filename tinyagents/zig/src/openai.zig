const std = @import("std");
const types = @import("types.zig");
const http = @import("provider_http.zig");
const json = @import("provider_json.zig");
const history = @import("history.zig");
const Value = std.json.Value;
const Allocator = std.mem.Allocator;

pub const OpenAIProvider = struct {
    api_key: []const u8,
    endpoint: []const u8 = "https://api.openai.com/v1/responses",
    timeout_ms: u32 = 180_000,

    pub fn provider(self: *OpenAIProvider) types.Provider {
        return .{ .context = self, .stream = stream };
    }

    fn stream(context: ?*anyopaque, allocator: Allocator, io: std.Io, request: types.ProviderRequest, on_text: types.TextHandler) !types.ProviderTurn {
        const self: *OpenAIProvider = @ptrCast(@alignCast(context.?));
        if (self.api_key.len == 0) return error.MissingApiKey;
        const body = try std.json.Stringify.valueAlloc(allocator, Request{ .value = request, .allocator = allocator }, .{});
        defer allocator.free(body);
        const authorization = try std.fmt.allocPrint(allocator, "Bearer {s}", .{self.api_key});
        defer allocator.free(authorization);
        return http.stream(Response, allocator, io, .{
            .endpoint = self.endpoint,
            .headers = &.{.{ .name = "Authorization", .value = authorization }},
            .timeout_ms = self.timeout_ms,
        }, body, on_text);
    }
};

const Request = struct {
    value: types.ProviderRequest,
    allocator: Allocator,

    pub fn jsonStringify(self: Request, out: *std.json.Stringify) !void {
        try out.beginObject();
        try out.objectField("model");
        try out.write(self.value.model);
        try out.objectField("instructions");
        try out.write(self.value.instructions);
        try out.objectField("stream");
        try out.write(true);
        try out.objectField("store");
        try out.write(false);
        try out.objectField("include");
        try out.write([_][]const u8{"reasoning.encrypted_content"});
        try out.objectField("parallel_tool_calls");
        try out.write(false);
        try out.objectField("tools");
        try out.beginArray();
        for (self.value.tools) |tool| try out.write(.{
            .type = "function",
            .name = tool.name,
            .description = tool.description,
            .parameters = tool.parameters,
            .strict = true,
        });
        try out.endArray();
        try out.objectField("input");
        try out.beginArray();
        for (self.value.history) |message| switch (message) {
            .user => |text| try out.write(.{ .role = "user", .content = text }),
            .assistant => |assistant| {
                if (assistant.replay.len != 0) {
                    for (assistant.replay) |item| try out.write(item);
                } else {
                    if (assistant.text.len != 0) try out.write(.{ .role = "assistant", .content = assistant.text });
                    for (assistant.tool_calls) |call| {
                        try out.beginObject();
                        try out.objectField("type");
                        try out.write("function_call");
                        try out.objectField("call_id");
                        try out.write(call.call_id);
                        try out.objectField("name");
                        try out.write(call.name);
                        // OpenAI embeds the JSON argument object in a string.
                        var argument_writer = std.Io.Writer.Allocating.init(self.allocator);
                        defer argument_writer.deinit();
                        try std.json.Stringify.value(call.arguments, .{}, &argument_writer.writer);
                        try out.objectField("arguments");
                        try out.write(argument_writer.written());
                        try out.endObject();
                    }
                }
            },
            .tool_result => |result| try out.write(.{
                .type = "function_call_output",
                .call_id = result.call_id,
                .output = result.output,
            }),
        };
        try out.endArray();
        try out.endObject();
    }
};

const Response = struct {
    allocator: Allocator,
    on_text: types.TextHandler,
    items: std.AutoArrayHashMapUnmanaged(usize, Value) = .empty,
    arguments: std.AutoArrayHashMapUnmanaged(usize, std.ArrayList(u8)) = .empty,
    result: ?types.ProviderTurn = null,

    pub fn consume(self: *Response, io: std.Io, event: Value) !void {
        const kind = try json.identifier(event, "type");
        if (json.equal(kind, "response.output_text.delta")) {
            const text = try json.namedString(event, "delta");
            if (text.len != 0) try self.on_text.emit(io, text);
        } else if (json.equal(kind, "response.output_item.added") or json.equal(kind, "response.output_item.done")) {
            const index = try json.index(event, "output_index");
            const item = try json.field(event, "item");
            const item_kind = try json.identifier(item, "type");
            if (json.equal(item_kind, "function_call")) {
                const arguments = try json.namedString(item, "arguments");
                if (arguments.len != 0) try self.updateArguments(index, arguments, true);
            }
            try self.items.put(self.allocator, index, try history.cloneJson(self.allocator, item));
        } else if (json.equal(kind, "response.function_call_arguments.delta")) {
            try self.updateArguments(try json.index(event, "output_index"), try json.namedString(event, "delta"), false);
        } else if (json.equal(kind, "response.function_call_arguments.done")) {
            try self.updateArguments(try json.index(event, "output_index"), try json.namedString(event, "arguments"), true);
        } else if (json.equal(kind, "response.completed")) {
            const response = try json.field(event, "response");
            if (response != .object) return error.InvalidProviderResponse;
            if (response.object.get("status")) |status| {
                if (!json.equal(try json.string(status), "completed")) return error.IncompleteProviderResponse;
            }
            if (response.object.get("output")) |output| {
                if (output != .array) return error.InvalidProviderResponse;
                self.items.clearRetainingCapacity();
                for (output.array.items, 0..) |item, index| try self.items.put(self.allocator, index, try history.cloneJson(self.allocator, item));
            } else if (self.items.count() == 0) return error.IncompleteProviderResponse;
            self.result = try self.complete();
        } else if (json.equal(kind, "response.failed") or json.equal(kind, "response.incomplete") or json.equal(kind, "error")) {
            return error.ProviderStreamFailed;
        }
    }

    fn updateArguments(self: *Response, index: usize, bytes: []const u8, replace: bool) !void {
        const entry = try self.arguments.getOrPut(self.allocator, index);
        if (!entry.found_existing) entry.value_ptr.* = .empty;
        if (replace) entry.value_ptr.clearRetainingCapacity();
        try json.append(self.allocator, entry.value_ptr, bytes);
    }

    fn complete(self: *Response) !types.ProviderTurn {
        const indexes = try self.allocator.dupe(usize, self.items.keys());
        std.mem.sort(usize, indexes, {}, std.sort.asc(usize));
        const output = try self.allocator.alloc(Value, indexes.len);
        var calls: std.ArrayList(types.ToolCall) = .empty;
        var text: std.ArrayList(u8) = .empty;
        for (indexes, output) |index, *item| {
            item.* = self.items.get(index).?;
            const kind = try json.identifier(item.*, "type");
            if (json.equal(kind, "function_call")) {
                const id = try json.identifier(item.*, "call_id");
                const name = try json.identifier(item.*, "name");
                for (calls.items) |call| if (json.equal(call.call_id, id)) return error.DuplicateToolCall;
                var raw = try json.namedString(item.*, "arguments");
                if (raw.len == 0) raw = if (self.arguments.get(index)) |buffer| buffer.items else "";
                const args = try json.parse(self.allocator, raw);
                if (args != .object) return error.InvalidToolArguments;
                try item.object.put(self.allocator, "arguments", .{ .string = raw });
                try calls.append(self.allocator, .{ .call_id = id, .name = name, .arguments = args });
            } else if (json.equal(kind, "message")) {
                const content = try json.field(item.*, "content");
                if (content != .array) return error.InvalidProviderResponse;
                for (content.array.items) |part| {
                    if (json.equal(try json.identifier(part, "type"), "output_text")) try json.append(self.allocator, &text, try json.namedString(part, "text"));
                }
            }
        }
        return .{ .assistant = .{ .text = text.items, .tool_calls = calls.items, .replay = output }, .output = output };
    }
};
