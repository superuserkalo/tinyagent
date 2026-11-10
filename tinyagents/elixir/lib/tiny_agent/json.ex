defmodule TinyAgent.JSON do
  @moduledoc false
  alias TinyAgent.ProviderError
  @max_bytes 1_048_576

  def max_bytes, do: @max_bytes

  def object!(value, name \\ "JSON")
  def object!(value, _name) when is_map(value) and not is_struct(value), do: value

  def object!(_value, name),
    do: raise(ProviderError, message: "Missing or invalid object: #{name}")

  def list!(value, name)
  def list!(value, _name) when is_list(value), do: value
  def list!(_value, name), do: raise(ProviderError, message: "Missing or invalid array: #{name}")

  def string!(value, field, nonempty \\ false) do
    text = Map.get(value, field)

    if is_binary(text) and String.valid?(text) and (not nonempty or text != "") do
      text
    else
      raise ProviderError, message: "Missing or invalid string: #{field}"
    end
  end

  def index!(value) when is_integer(value) and value >= 0, do: value
  def index!(_value), do: raise(ProviderError, message: "Missing or invalid block index.")

  def encode!(value) do
    Jason.encode!(value)
  rescue
    error -> raise ProviderError, message: "Invalid JSON value.", cause: error
  end

  def parse_object!(value) when is_binary(value) do
    value |> Jason.decode!(strings: :copy) |> object!()
  rescue
    error in Jason.DecodeError ->
      raise ProviderError, message: "Invalid provider JSON.", cause: error
  end

  def normalize_object!(value), do: value |> object!() |> encode!() |> parse_object!()

  def append_arguments!(previous, fragment) when is_binary(previous) and is_binary(fragment) do
    if byte_size(fragment) > @max_bytes - byte_size(previous),
      do: raise(ProviderError, message: "Tool arguments exceed their byte limit.")

    previous <> fragment
  end
end
