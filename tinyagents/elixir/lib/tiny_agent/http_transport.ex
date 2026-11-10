defmodule TinyAgent.HttpTransport do
  @moduledoc false
  @behaviour ThousandIsland.Transport
  alias ThousandIsland.Transports.TCP

  # Carry our server through accept and ownership transfer. Handshake returns
  # the raw socket, so subsequent native socket messages need no translation.
  @impl true
  def listen(port, options) do
    {server, options} = Keyword.pop!(options, :tiny_agent_server)

    with {:ok, listener} <- TCP.listen(port, options),
         do: {:ok, {listener, server}}
  end

  @impl true
  def accept({listener, server}) do
    with {:ok, socket} <- TCP.accept(listener) do
      {:ok, {socket, server}}
    end
  end

  @impl true
  def controlling_process({socket, _server}, pid), do: TCP.controlling_process(socket, pid)

  @impl true
  def handshake({socket, server}) do
    Process.put(__MODULE__, socket)

    if TinyAgent.HttpServer.register(server, socket),
      do: {:ok, socket},
      else: {:error, :closed}
  end

  def socket, do: Process.get(__MODULE__) || raise("HTTP socket is not available")

  @impl true
  def close({listener, server}) when is_pid(server), do: TCP.close(listener)

  def close(socket), do: TCP.close(socket)

  @impl true
  def sockname({listener, server}) when is_pid(server), do: TCP.sockname(listener)
  def sockname(socket), do: TCP.sockname(socket)

  @impl true
  defdelegate recv(socket, length, timeout), to: TCP
  @impl true
  defdelegate send(socket, data), to: TCP
  @impl true
  defdelegate sendfile(socket, file, offset, length), to: TCP
  @impl true
  defdelegate getopts(socket, options), to: TCP
  @impl true
  defdelegate setopts(socket, options), to: TCP
  @impl true
  defdelegate shutdown(socket, direction), to: TCP
  @impl true
  def peername({socket, server}) when is_pid(server), do: TCP.peername(socket)
  def peername(socket), do: TCP.peername(socket)
  @impl true
  defdelegate peercert(socket), to: TCP
  @impl true
  defdelegate secure?(), to: TCP
  @impl true
  defdelegate getstat(socket), to: TCP
  @impl true
  defdelegate negotiated_protocol(socket), to: TCP
  @impl true
  defdelegate connection_information(socket), to: TCP
  @impl true
  defdelegate upgrade(socket, options), to: TCP
end
