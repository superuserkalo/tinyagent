namespace TinyAgent;

public sealed class RpcException(string code, string message) : IOException(message)
{
    public string Code { get; } = code;
}
