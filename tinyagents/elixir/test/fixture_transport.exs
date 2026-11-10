defmodule TinyAgent.FixtureTransport do
  @moduledoc false
  @behaviour Mint.Core.Transport

  # Mint still serializes requests and parses HTTP. Only the socket is replaced.
  def start_link(options) do
    Agent.start_link(fn ->
      %{
        owner: nil,
        observer: options[:observer],
        fragments: options[:fragments] || [],
        eof: Keyword.get(options, :eof, true),
        armed: false,
        started: false,
        closed: false,
        close: options[:close],
        request: ""
      }
    end)
  end

  def connector(socket) do
    fn _scheme, host, port, options ->
      options = Keyword.update!(options, :transport_opts, &Keyword.put(&1, :fixture, socket))
      Mint.HTTP.connect(__MODULE__, host, port, options)
    end
  end

  @impl true
  def connect(_address, _port, options) do
    socket = Keyword.fetch!(options, :fixture)
    owner = self()
    Agent.update(socket, &%{&1 | owner: owner})
    {:ok, socket}
  end

  @impl true
  def send(socket, payload) do
    Agent.update(socket, fn state ->
      state = %{state | request: state.request <> IO.iodata_to_binary(payload), started: true}
      deliver(socket, state)
    end)

    :ok
  end

  @impl true
  def setopts(socket, options) do
    if options[:active] == :once do
      Agent.update(socket, &deliver(socket, %{&1 | armed: true}))
    end

    :ok
  end

  defp deliver(socket, %{armed: true, started: true, closed: false} = state) do
    case state.fragments do
      [bytes | rest] ->
        Kernel.send(state.owner, {:tcp, socket, bytes})
        %{state | fragments: rest, armed: false}

      [] ->
        if state.eof, do: Kernel.send(state.owner, {:tcp_closed, socket})
        if state.observer, do: Kernel.send(state.observer, {:waiting, socket})
        %{state | armed: false}
    end
  end

  defp deliver(_socket, state), do: state

  @impl true
  def close(socket) do
    callback =
      Agent.get_and_update(socket, fn state ->
        {if(state.closed, do: nil, else: state.close), %{state | closed: true}}
      end)

    if callback, do: callback.()
    :ok
  end

  @impl true
  def getopts(_socket, keys), do: {:ok, Enum.map(keys, &{&1, 16_384})}
  @impl true
  def controlling_process(socket, owner), do: Agent.update(socket, &%{&1 | owner: owner})
  @impl true
  def negotiated_protocol(_socket), do: {:error, :protocol_not_negotiated}
  @impl true
  def wrap_error(reason), do: %Mint.TransportError{reason: reason}
  @impl true
  def recv(_socket, _bytes, _timeout), do: {:error, wrap_error(:unexpected_passive_read)}
  @impl true
  def upgrade(_socket, _scheme, _host, _port, _options), do: {:error, wrap_error(:unsupported)}

  def snapshot(socket), do: Agent.get(socket, & &1)

  def response(body, options \\ []) do
    status = Keyword.get(options, :status, 200)
    prefix = Keyword.get(options, :prefix, "")

    wire =
      case Keyword.get(options, :framing, :length) do
        :length ->
          "content-length: #{byte_size(body)}\r\n\r\n" <> body

        :chunked ->
          "transfer-encoding: chunked\r\n\r\n" <>
            Integer.to_string(byte_size(body), 16) <> "\r\n" <> body <> "\r\n0\r\n\r\n"

        :open ->
          "transfer-encoding: chunked\r\n\r\n" <>
            Integer.to_string(byte_size(body), 16) <> "\r\n" <> body <> "\r\n"
      end

    bytes = prefix <> "HTTP/1.1 #{status} Fixture\r\ncontent-type: text/event-stream\r\n" <> wire
    split(bytes, Keyword.get(options, :fragment_size, 7))
  end

  defp split("", _size), do: []

  defp split(bytes, size) do
    length = min(byte_size(bytes), size)

    [
      binary_part(bytes, 0, length)
      | split(binary_part(bytes, length, byte_size(bytes) - length), size)
    ]
  end
end
