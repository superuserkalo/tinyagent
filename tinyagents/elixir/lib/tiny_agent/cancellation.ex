defmodule TinyAgent.Cancelled do
  defexception message: "Request cancelled.", reason: :aborted
end

defmodule TinyAgent.CancellationToken do
  @moduledoc "A cancellation signal that can be shared with another process."
  @enforce_keys [:flag]
  defstruct [:flag, :parent, :deadline]

  def new, do: %__MODULE__{flag: :atomics.new(1, signed: false)}

  def cancel(%__MODULE__{flag: flag}) do
    :atomics.put(flag, 1, 1)
    :ok
  end

  def cancelled?(token), do: reason(token) != nil

  def check!(token) do
    case reason(token) do
      nil ->
        :ok

      :timeout ->
        raise TinyAgent.Cancelled, message: "Provider request timed out.", reason: :timeout

      :aborted ->
        raise TinyAgent.Cancelled
    end
  end

  @doc false
  def child(%__MODULE__{} = parent), do: %{new() | parent: parent}

  @doc false
  def with_timeout(%__MODULE__{} = parent, milliseconds)
      when is_integer(milliseconds) and milliseconds > 0 do
    %{child(parent) | deadline: System.monotonic_time(:millisecond) + milliseconds}
  end

  @doc false
  def reason(%__MODULE__{flag: flag, parent: parent, deadline: deadline}) do
    cond do
      :atomics.get(flag, 1) != 0 -> :aborted
      parent != nil and cancelled?(parent) -> :aborted
      deadline != nil and System.monotonic_time(:millisecond) >= deadline -> :timeout
      true -> nil
    end
  end

  @doc false
  def remaining(%__MODULE__{deadline: deadline}) when is_integer(deadline),
    do: max(1, deadline - System.monotonic_time(:millisecond))
end
