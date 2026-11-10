using System.Text.Json.Nodes;

namespace TinyAgent;

/// <summary>Immutable run events. Each JSON conversion creates a detached protocol object.</summary>
public abstract class AgentEvent
{
    public string RunId { get; }
    public long Sequence { get; }

    private AgentEvent(string runId, long sequence)
    {
        ArgumentException.ThrowIfNullOrEmpty(runId);
        RunId = Json.Text(runId, nameof(runId));
        if (sequence < 1)
            throw new ArgumentOutOfRangeException(nameof(sequence), "Sequence must be positive.");
        Sequence = sequence;
    }

    public sealed class Start : AgentEvent
    {
        public Start(string runId, long sequence) : base(runId, sequence) { }
    }

    public sealed class TextDelta : AgentEvent
    {
        public string Delta { get; }
        public TextDelta(string runId, long sequence, string delta) : base(runId, sequence) =>
            Delta = Json.Text(delta, nameof(delta));
    }

    public sealed class Completed : AgentEvent
    {
        public string Text { get; }
        public Completed(string runId, long sequence, string text) : base(runId, sequence) =>
            Text = Json.Text(text, nameof(text));
    }

    public sealed class ToolExecutionStart : AgentEvent
    {
        public ToolCall Call { get; }
        public ToolExecutionStart(string runId, long sequence, ToolCall call) : base(runId, sequence)
        {
            ArgumentNullException.ThrowIfNull(call);
            Call = call;
        }
    }

    public sealed class ToolExecutionEnd : AgentEvent
    {
        public string ToolCallId { get; }
        public string ToolName { get; }
        public string Output { get; }
        public bool IsError { get; }
        public ToolExecutionEnd(string runId, long sequence, string toolCallId, string toolName, string output, bool isError)
            : base(runId, sequence)
        {
            ToolCallId = Json.Text(toolCallId, nameof(toolCallId));
            ToolName = Json.Text(toolName, nameof(toolName));
            Output = Json.Text(output, nameof(output));
            IsError = isError;
        }
    }

    public sealed class Failed : AgentEvent
    {
        public string Message { get; }
        public Failed(string runId, long sequence, string message) : base(runId, sequence) =>
            Message = Json.Text(message, nameof(message));
    }

    public sealed class Aborted : AgentEvent
    {
        public Aborted(string runId, long sequence) : base(runId, sequence) { }
    }

    public JsonObject ToJson()
    {
        JsonObject value = new() { ["runId"] = RunId, ["sequence"] = Sequence };
        switch (this)
        {
            case Start:
                value["type"] = "agent_start";
                break;
            case TextDelta text:
                value["type"] = "text_delta";
                value["delta"] = text.Delta;
                break;
            case Completed completed:
                value["type"] = "agent_end";
                value["status"] = "completed";
                value["text"] = completed.Text;
                break;
            case ToolExecutionStart started:
                value["type"] = "tool_execution_start";
                value["toolCallId"] = started.Call.CallId;
                value["toolName"] = started.Call.Name;
                value["arguments"] = Json.Node(started.Call.Arguments);
                break;
            case ToolExecutionEnd ended:
                value["type"] = "tool_execution_end";
                value["toolCallId"] = ended.ToolCallId;
                value["toolName"] = ended.ToolName;
                value["output"] = ended.Output;
                value["isError"] = ended.IsError;
                break;
            case Failed failed:
                value["type"] = "agent_end";
                value["status"] = "failed";
                value["error"] = new JsonObject
                {
                    ["code"] = "provider_error",
                    ["message"] = failed.Message,
                    ["retryable"] = false
                };
                break;
            case Aborted:
                value["type"] = "agent_end";
                value["status"] = "aborted";
                break;
        }
        return value;
    }
}
