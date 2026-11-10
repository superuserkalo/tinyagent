defmodule TinyAgent.HttpServer do
  @moduledoc "In-memory HTTP sessions around one configured agent. The caller owns its provider."
  use GenServer, restart: :temporary, shutdown: :infinity
  alias TinyAgent.{Agent, HttpConnection, HttpJson, HttpTransport, Session, SessionError}

  def start_link(%Agent{} = agent, options \\ []) do
    options =
      Keyword.validate!(options, host: "127.0.0.1", port: 8080, token: nil, allow_remote: false)

    {:ok, ip} = :inet.parse_strict_address(String.to_charlist(options[:host]))
    token = options[:token]

    unless is_integer(options[:port]) and options[:port] in 0..65_535 and
             (token == nil or
                (is_binary(token) and token != "" and
                   Enum.all?(:binary.bin_to_list(token), &(&1 in 33..126)))),
           do: raise(ArgumentError, "Invalid HTTP port or bearer token")

    loopback = match?({127, _, _, _}, ip) or ip == {0, 0, 0, 0, 0, 0, 0, 1}

    unless loopback or (options[:allow_remote] == true and token != nil),
      do: raise(ArgumentError, "Remote HTTP binding requires allow_remote and a bearer token")

    GenServer.start_link(__MODULE__, {agent, options, ip})
  end

  def url(server), do: GenServer.call(server, :url)

  @doc "Requests shutdown without waiting. Safe in provider callbacks."
  def stop(server) do
    GenServer.call(server, :stop, :infinity)
  catch
    :exit, {reason, {GenServer, :call, _}} when reason in [:noproc, :normal, :shutdown] -> :ok
  end

  @doc "Joins shutdown; returns false in a run callback to avoid waiting on itself."
  def close(server) do
    monitor = Process.monitor(server)
    stop(server)

    if Process.get(HttpConnection) do
      Process.demonitor(monitor, [:flush])
      false
    else
      receive do
        {:DOWN, ^monitor, :process, ^server, _} -> true
      end
    end
  end

  @doc false
  def register(server, socket) do
    GenServer.call(server, {:register, socket}, :infinity)
  catch
    :exit, _ ->
      :gen_tcp.close(socket)
      false
  end

  @impl true
  def init({agent, options, ip}) do
    Process.flag(:trap_exit, true)
    owner = self()
    plug = fn conn, _options -> route(conn, owner, options[:token]) end

    case Bandit.start_link(
           plug: plug,
           ip: ip,
           port: options[:port],
           startup_log: false,
           http_options: [compress: false],
           http_1_options: [max_requests: 1],
           http_2_options: [enabled: false],
           thousand_island_options: [
             num_acceptors: 2,
             shutdown_timeout: :infinity,
             transport_module: HttpTransport,
             transport_options: [
               tiny_agent_server: owner,
               inet_backend: :socket,
               send_timeout: :infinity
             ]
           ]
         ) do
      {:ok, listener} ->
        {:ok, {address, port}} = ThousandIsland.listener_info(listener)
        host = to_string(:inet.ntoa(address))
        host = if tuple_size(address) == 8, do: "[#{host}]", else: host

        {:ok,
         %{
           agent: agent,
           listener: listener,
           url: "http://#{host}:#{port}",
           sessions: %{},
           deletions: %{},
           connections: %{},
           shutdown: nil
         }}

      {:error, reason} ->
        {:stop, reason}
    end
  end

  @impl true
  def handle_call(:url, _from, state), do: {:reply, state.url, state}

  def handle_call(:stop, _from, %{shutdown: nil} = state) do
    prepare_shutdown(state)
    {pid, monitor} = spawn_monitor(fn -> finish_shutdown(state) end)
    {:reply, :ok, %{state | shutdown: {pid, monitor}}}
  end

  def handle_call(:stop, _from, state), do: {:reply, :ok, state}

  def handle_call({:register, socket}, {pid, _}, state) do
    if state.shutdown do
      :gen_tcp.close(socket)
      {:reply, false, state}
    else
      monitor = Process.monitor(pid)
      connection = %{socket: socket, active: false, monitor: monitor}
      {:reply, true, %{state | connections: Map.put(state.connections, pid, connection)}}
    end
  end

  def handle_call(_request, _from, %{shutdown: shutdown} = state) when shutdown != nil,
    do: {:reply, {:error, :closed}, state}

  def handle_call(:create, _from, state) do
    {:ok, session} = Session.start_link(state.agent)
    id = "s_" <> Base.encode16(:crypto.strong_rand_bytes(16), case: :lower)
    {:reply, id, %{state | sessions: Map.put(state.sessions, id, session)}}
  end

  def handle_call({:session, id}, _from, state) do
    session = if not Map.has_key?(state.deletions, id), do: state.sessions[id]
    {:reply, session, state}
  end

  def handle_call(:activate, {pid, _}, state) do
    connections = Map.update!(state.connections, pid, &%{&1 | active: true})
    {:reply, :ok, %{state | connections: connections}}
  end

  def handle_call({:cancel, id}, _from, state) do
    if session = state.sessions[id] do
      if Map.has_key?(state.deletions, id),
        do: {:reply, {:error, :closed}, state},
        else: {:reply, {:ok, Session.cancel(session)}, state}
    else
      {:reply, {:error, :closed}, state}
    end
  end

  def handle_call({:delete, id}, from, state) do
    cond do
      deletion = state.deletions[id] ->
        deletion = %{deletion | waiters: [from | deletion.waiters]}
        {:noreply, %{state | deletions: Map.put(state.deletions, id, deletion)}}

      session = state.sessions[id] ->
        {pid, monitor} = spawn_monitor(fn -> close_session(session) end)
        deletion = %{pid: pid, monitor: monitor, waiters: [from]}
        {:noreply, %{state | deletions: Map.put(state.deletions, id, deletion)}}

      true ->
        {:reply, {:error, :closed}, state}
    end
  end

  @impl true
  def handle_info({:DOWN, monitor, :process, pid, _}, %{shutdown: {pid, monitor}} = state),
    do: {:stop, :normal, %{state | shutdown: :finished}}

  def handle_info({:DOWN, monitor, :process, pid, _}, state) do
    case Enum.find(state.deletions, fn {_, value} -> value.monitor == monitor end) do
      {id, deletion} ->
        for from <- deletion.waiters, do: GenServer.reply(from, :ok)

        {:noreply,
         %{
           state
           | sessions: Map.delete(state.sessions, id),
             deletions: Map.delete(state.deletions, id)
         }}

      nil ->
        {:noreply, %{state | connections: Map.delete(state.connections, pid)}}
    end
  end

  def handle_info({:EXIT, listener, reason}, %{listener: listener, shutdown: nil} = state),
    do: {:stop, reason, state}

  def handle_info(_message, state), do: {:noreply, state}

  @impl true
  def terminate(_reason, %{shutdown: :finished}), do: :ok

  def terminate(_reason, %{shutdown: {pid, monitor}}) do
    receive do
      {:DOWN, ^monitor, :process, ^pid, _} -> :ok
    end
  end

  def terminate(_reason, state) do
    prepare_shutdown(state)
    finish_shutdown(state)
  end

  defp prepare_shutdown(state) do
    for session <- Map.values(state.sessions) do
      try do
        Session.stop(session)
      rescue
        SessionError -> :ok
      end
    end

    for {_, connection} <- state.connections,
        not connection.active,
        do: :gen_tcp.close(connection.socket)
  end

  defp finish_shutdown(state) do
    ThousandIsland.suspend(state.listener)
    for session <- Map.values(state.sessions), do: close_session(session)

    for {_, deletion} <- state.deletions do
      monitor = Process.monitor(deletion.pid)

      receive do
        {:DOWN, ^monitor, :process, _, _} -> :ok
      end
    end

    ThousandIsland.stop(state.listener, :infinity)
  end

  defp close_session(session) do
    monitor = Process.monitor(session)

    try do
      Session.close(session)
    rescue
      SessionError -> :ok
    end

    receive do
      {:DOWN, ^monitor, :process, ^session, _} -> :ok
    end
  end

  defp request(server, message) do
    case GenServer.call(server, message, :infinity) do
      {:error, :closed} -> HttpJson.fail(404, "session_not_found", "Unknown session")
      result -> result
    end
  end

  defp route(conn, server, token) do
    HttpJson.authenticate!(conn, token)
    {body, conn} = HttpJson.object(conn, String.ends_with?(conn.request_path, "/prompts"))

    case {conn.method, conn.path_info} do
      {"GET", ["health"]} ->
        HttpJson.respond(conn, 200, %{"status" => "ok", "protocolVersion" => 1})

      {"POST", ["sessions"]} ->
        HttpJson.respond(conn, 201, %{"sessionId" => request(server, :create)})

      {"DELETE", ["sessions", id]} ->
        :ok = request(server, {:delete, id})
        HttpJson.respond(conn, 204)

      {"POST", ["sessions", id, "cancel"]} ->
        {:ok, run_id} = request(server, {:cancel, id})
        HttpJson.respond(conn, 200, %{"accepted" => run_id != nil, "runId" => run_id})

      {"POST", ["sessions", id, "prompts"]} ->
        prompt = body["message"]

        unless is_binary(prompt) and prompt != "",
          do: HttpJson.fail(400, "invalid_request", "message must be a non-empty string")

        session =
          request(server, {:session, id}) ||
            HttpJson.fail(404, "session_not_found", "Unknown session")

        :ok = request(server, :activate)
        HttpConnection.stream(conn, session, prompt)

      _ ->
        HttpJson.fail(404, "not_found", "Unknown route or session")
    end
  rescue
    error in TinyAgent.HttpError ->
      HttpJson.error(conn, error)

    error in SessionError ->
      {status, code} =
        if error.reason == :busy, do: {409, "session_busy"}, else: {404, "session_not_found"}

      HttpJson.error(conn, %TinyAgent.HttpError{
        status: status,
        code: code,
        message: error.message
      })
  end
end
