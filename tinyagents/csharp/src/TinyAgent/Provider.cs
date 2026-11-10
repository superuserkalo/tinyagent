namespace TinyAgent;

public delegate ValueTask TextDeltaHandler(string delta, CancellationToken cancellationToken);

public interface IProvider : IDisposable
{
    Task<ProviderTurn> StreamAsync(
        ProviderRequest request,
        TextDeltaHandler? onText = null,
        CancellationToken cancellationToken = default);
}

public sealed class ProviderException : Exception
{
    public ProviderException(string message) : base(message) { }
    public ProviderException(string message, Exception innerException) : base(message, innerException) { }
}

internal interface IResponseParser
{
    ValueTask<ProviderTurn?> ConsumeAsync(
        System.Text.Json.Nodes.JsonObject value,
        TextDeltaHandler? onText,
        CancellationToken cancellationToken);
}
