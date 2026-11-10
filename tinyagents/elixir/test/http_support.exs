defmodule TinyAgent.HttpSupport do
  import ExUnit.Assertions
  import ExUnit.Callbacks
  import TinyAgent.CoreSupport
  alias TinyAgent.{Agent, HttpServer, ProviderTurn}

  def server(run \\ fn _, _ -> ProviderTurn.new("done", [], [], []) end, options \\ []) do
    {:ok, server} =
      HttpServer.start_link(Agent.new(scripted(run), "fixture"), [port: 0] ++ options)

    on_exit(fn -> HttpServer.close(server) end)
    {server, URI.parse(HttpServer.url(server))}
  end

  def connect(url, options \\ []) do
    {:ok, ip} = :inet.parse_address(String.to_charlist(url.host))
    family = if tuple_size(ip) == 8, do: [:inet6], else: []

    {:ok, socket} =
      :gen_tcp.connect(ip, url.port, [:binary, active: false] ++ family ++ options, 2_000)

    socket
  end

  def open(url, method, path, body \\ "", headers \\ [], options \\ []) do
    socket = connect(url, options)
    bytes = if is_binary(body), do: body, else: Jason.encode!(body)

    headers =
      if is_binary(body), do: headers, else: [{"content-type", "application/json"} | headers]

    :ok =
      :gen_tcp.send(socket, [
        method,
        " ",
        path,
        " HTTP/1.1\r\nhost: fixture\r\n",
        "content-length: ",
        to_string(byte_size(bytes)),
        "\r\n",
        Enum.map(headers, fn {key, value} -> [key, ": ", value, "\r\n"] end),
        "\r\n",
        bytes
      ])

    socket
  end

  def request(url, method, path, body \\ "", headers \\ []) do
    socket = open(url, method, path, body, headers)
    response(socket)
  end

  def response(socket, prefix \\ "") do
    bytes = receive_all(socket, prefix)
    [head, body] = String.split(bytes, "\r\n\r\n", parts: 2)
    [status | headers] = String.split(head, "\r\n")
    [_, status, _] = String.split(status, " ", parts: 3)

    headers =
      Enum.map(headers, fn line ->
        [name, value] = String.split(line, ":", parts: 2)
        {String.downcase(name), String.trim(value)}
      end)

    body = if {"transfer-encoding", "chunked"} in headers, do: chunks(body, ""), else: body
    %{status: String.to_integer(status), headers: headers, body: body}
  end

  defp receive_all(socket, bytes) do
    case :gen_tcp.recv(socket, 0, 5_000) do
      {:ok, more} -> receive_all(socket, bytes <> more)
      {:error, :closed} -> bytes
      other -> flunk("HTTP response did not close: #{inspect(other)}")
    end
  end

  defp chunks("0\r\n\r\n", body), do: body

  defp chunks(bytes, body) do
    [size, rest] = String.split(bytes, "\r\n", parts: 2)
    size = String.to_integer(size, 16)
    <<part::binary-size(^size), "\r\n", rest::binary>> = rest
    chunks(rest, body <> part)
  end

  def create(url) do
    %{status: 201, body: body} = request(url, "POST", "/sessions")
    Jason.decode!(body)["sessionId"]
  end

  def events(%{status: 200, body: body}) do
    body |> String.split("\n", trim: true) |> Enum.map(&Jason.decode!/1)
  end

  def wait_until(check, remaining \\ 200) do
    cond do
      check.() ->
        :ok

      remaining == 0 ->
        flunk("Condition did not become true")

      true ->
        Process.sleep(10)
        wait_until(check, remaining - 1)
    end
  end
end
