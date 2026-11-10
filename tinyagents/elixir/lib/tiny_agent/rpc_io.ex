defmodule TinyAgent.RpcIO do
  @moduledoc false
  @on_load :load

  def load do
    case :os.type() do
      {:unix, platform} when platform in [:darwin, :linux] ->
        path =
          __MODULE__
          |> :code.which()
          |> to_string()
          |> Path.dirname()
          |> Path.join("../priv/tiny_agent_io")
          |> Path.expand()
          |> String.to_charlist()

        :erlang.load_nif(path, 0)

      _platform ->
        :ok
    end
  end

  # Descriptors are borrowed exclusively until close restores their flags.
  # Only the creating process may operate on a handle; owner exit closes it.
  def open(_descriptor, _writing), do: :erlang.nif_error(:not_loaded)
  def read(_handle), do: :erlang.nif_error(:not_loaded)
  def write(_handle, _bytes, _offset), do: :erlang.nif_error(:not_loaded)
  def watch_interrupt, do: :erlang.nif_error(:not_loaded)
  def interrupted?(_handle), do: :erlang.nif_error(:not_loaded)
  def close(_handle), do: :erlang.nif_error(:not_loaded)
end
