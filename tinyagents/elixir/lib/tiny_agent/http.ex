defmodule TinyAgent.HTTP do
  @moduledoc false
  alias TinyAgent.{CancellationToken, JSON, ProviderError}
  @enforce_keys [:uri, :timeout, :connect]
  defstruct [:uri, :timeout, :connect]

  def new(endpoint, timeout, connect) do
    uri = URI.parse(endpoint)

    unless uri.scheme in ["http", "https"] and is_binary(uri.host) and uri.host != "" and
             uri.userinfo == nil and uri.fragment == nil and
             not Regex.match?(~r/[\x00-\x20\x7f]/, endpoint),
           do: raise(ArgumentError, "Provider endpoint must be an HTTP or HTTPS URL.")

    unless is_integer(timeout) and timeout > 0 and timeout <= 4_294_967_295,
      do: raise(ArgumentError, "Timeout must be a positive 32-bit millisecond value.")

    unless is_function(connect, 4),
      do: raise(ArgumentError, "connect must be a four-argument function.")

    %__MODULE__{uri: uri, timeout: timeout, connect: connect}
  end

  def stream(http, headers, payload, cancellation, state, consume) do
    CancellationToken.check!(cancellation)
    scheme = if http.uri.scheme == "https", do: :https, else: :http
    body = JSON.encode!(payload)
    timeout = min(30_000, CancellationToken.remaining(cancellation))

    options = [
      protocols: [:http1],
      mode: :active,
      transport_opts: [timeout: timeout, send_timeout: timeout]
    ]

    connection =
      case http.connect.(scheme, http.uri.host, http.uri.port, options) do
        {:ok, connection} ->
          connection

        {:error, error} ->
          raise ProviderError, message: "Provider connection failed.", cause: error
      end

    socket = Mint.HTTP.get_socket(connection)

    try do
      CancellationToken.check!(cancellation)
      path = if http.uri.path in [nil, ""], do: "/", else: http.uri.path
      path = if http.uri.query == nil, do: path, else: path <> "?" <> http.uri.query
      headers = [{"content-type", "application/json"}, {"accept", "text/event-stream"} | headers]

      case Mint.HTTP.request(connection, "POST", path, headers, body) do
        {:ok, connection, reference} ->
          receive_body(
            connection,
            socket,
            reference,
            cancellation,
            %{status: nil, value: state},
            consume
          )

        {:error, _connection, error} ->
          raise ProviderError, message: "Provider request failed.", cause: error
      end
    after
      Mint.HTTP.close(connection)
      flush_socket(socket)
    end
  end

  defp receive_body(connection, socket, reference, cancellation, state, consume) do
    CancellationToken.check!(cancellation)

    receive do
      {tag, ^socket, _data} = message when tag in [:tcp, :ssl, :tcp_error, :ssl_error] ->
        handle_message(connection, socket, reference, cancellation, state, consume, message)

      {tag, ^socket} = message when tag in [:tcp_closed, :ssl_closed] ->
        handle_message(connection, socket, reference, cancellation, state, consume, message)
    after
      min(50, CancellationToken.remaining(cancellation)) ->
        receive_body(connection, socket, reference, cancellation, state, consume)
    end
  end

  defp handle_message(connection, socket, reference, cancellation, state, consume, message) do
    {connection, responses, failure} =
      case Mint.HTTP.stream(connection, message) do
        {:ok, connection, responses} -> {connection, responses, nil}
        {:error, connection, error, responses} -> {connection, responses, error}
      end

    case responses(responses, reference, cancellation, state, consume) do
      {:cont, state} ->
        if failure != nil,
          do: raise(ProviderError, message: "Provider response failed.", cause: failure)

        receive_body(connection, socket, reference, cancellation, state, consume)

      finished ->
        finished
    end
  end

  defp responses([], _reference, _cancellation, state, _consume), do: {:cont, state}

  defp responses([response | rest], reference, cancellation, state, consume) do
    CancellationToken.check!(cancellation)

    case response do
      {:status, ^reference, status} when status in 100..199 and status != 101 ->
        responses(rest, reference, cancellation, state, consume)

      {:status, ^reference, status} when status in 200..299 ->
        responses(rest, reference, cancellation, %{state | status: status}, consume)

      {:status, ^reference, status} ->
        raise ProviderError, message: "Provider HTTP #{status}."

      {:headers, ^reference, _headers} ->
        responses(rest, reference, cancellation, state, consume)

      {:data, ^reference, bytes} when state.status != nil ->
        case consume.(bytes, state.value) do
          {:cont, value} ->
            responses(rest, reference, cancellation, %{state | value: value}, consume)

          {:halt, result} ->
            {:halt, result}
        end

      {:done, ^reference} ->
        {:done, state.value}

      _ ->
        raise ProviderError, message: "Unexpected provider HTTP response."
    end
  end

  defp flush_socket(socket) do
    receive do
      {tag, ^socket, _} when tag in [:tcp, :ssl, :tcp_error, :ssl_error] -> flush_socket(socket)
      {tag, ^socket} when tag in [:tcp_closed, :ssl_closed] -> flush_socket(socket)
    after
      0 -> :ok
    end
  end
end
