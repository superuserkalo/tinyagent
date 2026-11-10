defmodule TinyAgent.ScriptedProvider do
  @behaviour TinyAgent.Provider
  @enforce_keys [:run]
  defstruct [:run]

  @impl true
  def stream(provider, request, options), do: provider.run.(request, options)
end

defmodule TinyAgent.CoreSupport do
  def scripted(run), do: %TinyAgent.ScriptedProvider{run: run}

  def capture(run) do
    {:ok, run.()}
  catch
    kind, reason -> {kind, reason}
  end

  def collect do
    receive do
      {:core_event, event} -> [event | collect()]
    after
      0 -> []
    end
  end

  def record do
    owner = self()
    fn event, _token -> send(owner, {:core_event, event}) end
  end

  def event_keys do
    Process.get_keys() |> Enum.filter(&match?({TinyAgent.RunEvents, _}, &1)) |> Enum.sort()
  end
end
