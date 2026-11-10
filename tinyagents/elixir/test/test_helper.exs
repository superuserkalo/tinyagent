# Native test groups launch additional BEAMs. Bound parallel groups so VM
# startup does not consume their I/O and process-cleanup deadlines.
ExUnit.start(max_cases: 2, exclude: if("--offline" in System.argv(), do: [:tcp], else: []))
Code.require_file("fixture_transport.exs", __DIR__)
Code.require_file("core_support.exs", __DIR__)
Code.require_file("session_support.exs", __DIR__)
Code.require_file("http_support.exs", __DIR__)

defmodule TinyAgent.TestSupport do
  import ExUnit.Callbacks

  alias TinyAgent.{
    AnthropicProvider,
    FixtureTransport,
    OpenAIProvider,
    ProviderRequest,
    ToolDefinition,
    UserMessage
  }

  def fixture(name),
    do: File.read!(Path.expand("../../../conformance/providers/#{name}.sse", __DIR__))

  def events(values), do: Enum.map_join(values, &"data: #{Jason.encode!(&1)}\n\n")

  def request(history \\ [UserMessage.new("go")]) do
    ProviderRequest.new("fixture", "Instructions", history, [
      ToolDefinition.new("echo", "Echo", %{
        "type" => "object",
        "properties" => %{"text" => %{"type" => "string"}},
        "required" => ["text"]
      })
    ])
  end

  def provider(module, body, options \\ []) do
    fragments =
      Keyword.get_lazy(options, :fragments, fn -> FixtureTransport.response(body, options) end)

    socket =
      start_supervised!(%{
        id: make_ref(),
        start:
          {FixtureTransport, :start_link,
           [
             [
               fragments: fragments,
               eof: Keyword.get(options, :eof, true),
               observer: self(),
               close: options[:close]
             ]
           ]}
      })

    instance =
      module.new(
        api_key: "fixture-key",
        endpoint: "http://fixture.invalid/api?mode=test",
        timeout: Keyword.get(options, :timeout, 2_000),
        connect: FixtureTransport.connector(socket)
      )

    {instance, socket}
  end

  def stream(provider, request, options \\ [])

  def stream(%OpenAIProvider{} = provider, request, options),
    do: OpenAIProvider.stream(provider, request, options)

  def stream(%AnthropicProvider{} = provider, request, options),
    do: AnthropicProvider.stream(provider, request, options)

  def sent(socket) do
    [headers, body] =
      String.split(FixtureTransport.snapshot(socket).request, "\r\n\r\n", parts: 2)

    {headers, Jason.decode!(body)}
  end
end
