defmodule TinyAgent.HttpError do
  @moduledoc false
  defexception [:status, :code, :message]
end

defmodule TinyAgent.HttpJson do
  @moduledoc false
  alias TinyAgent.HttpError
  @limit 1_048_576

  def authenticate!(conn, token) do
    headers = Plug.Conn.get_req_header(conn, "authorization")

    if length(headers) > 1 or (token != nil and headers != ["Bearer " <> token]),
      do: fail(401, "unauthorized", "Invalid bearer token")
  end

  def object(conn, required \\ false) do
    case Plug.Conn.get_req_header(conn, "content-length") do
      [length] ->
        if String.to_integer(length) > @limit,
          do: fail(413, "request_too_large", "Request body exceeds 1048576 bytes")

      _ ->
        :ok
    end

    {bytes, conn} = read(conn, "")
    types = Plug.Conn.get_req_header(conn, "content-type")

    unless bytes == "" and not required and types == [] do
      unless length(types) == 1 and
               types
               |> hd()
               |> String.split(";", parts: 2)
               |> hd()
               |> String.trim()
               |> String.downcase(:ascii) == "application/json",
             do: fail(415, "unsupported_media_type", "Content-Type must be application/json")
    end

    if bytes == "" and not required do
      {%{}, conn}
    else
      case Jason.decode(bytes, strings: :copy) do
        {:ok, object} when is_map(object) -> {object, conn}
        _ -> fail(400, "invalid_json", "Expected a UTF-8 JSON object")
      end
    end
  end

  defp read(conn, bytes) do
    case Plug.Conn.read_body(conn, length: @limit + 1 - byte_size(bytes), read_length: 65_536) do
      {status, part, conn} when status in [:ok, :more] ->
        if byte_size(bytes) + byte_size(part) > @limit,
          do: fail(413, "request_too_large", "Request body exceeds 1048576 bytes")

        if status == :ok, do: {bytes <> part, conn}, else: read(conn, bytes <> part)

      {:error, _} ->
        fail(400, "invalid_request", "Could not read request body")
    end
  end

  def respond(conn, status, value \\ nil) do
    conn
    |> Plug.Conn.put_resp_header("connection", "close")
    |> Plug.Conn.put_resp_content_type("application/json")
    |> Plug.Conn.send_resp(status, if(value == nil, do: "", else: Jason.encode!(value)))
  end

  def error(conn, %HttpError{} = error) do
    respond(conn, error.status, %{
      "error" => %{
        "code" => error.code,
        "message" => error.message,
        "retryable" => error.status in [409, 500]
      }
    })
  end

  def fail(status, code, message),
    do: raise(HttpError, status: status, code: code, message: message)
end
