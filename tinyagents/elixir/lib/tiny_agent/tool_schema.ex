defmodule TinyAgent.ToolSchema do
  @moduledoc false
  @dialect "https://json-schema.org/draft/2020-12/schema"
  @options [formats: false, atoms: false]
  @meta JSV.build!(JSV.Resolver.Embedded.Draft202012.Schema.json_schema(), @options)

  def build(parameters) do
    unless match?({:ok, _}, JSV.validate(parameters, @meta, cast: false)),
      do: raise(ArgumentError, "Tool parameters must be a valid Draft 2020-12 schema.")

    if is_boolean(parameters) do
      JSV.build!(parameters, @options)
    else
      nodes = collect(parameters, [], :root)
      resources = MapSet.new(nodes, fn {_path, namespace, _schema} -> namespace end)

      for {_path, namespace, schema} <- nodes,
          keyword <- ["$ref", "$dynamicRef"],
          reference = schema[keyword] do
        ref = JSV.Ref.parse!(reference, namespace)

        unless MapSet.member?(resources, ref.ns),
          do:
            raise(
              ArgumentError,
              "Tool schema references must resolve within the supplied schema."
            )
      end

      {key, _raw, context} = JSV.build_add!(JSV.build_init!(@options), parameters)

      # Build every schema position, including unused definitions. JSV owns
      # URI resolution, anchors, keyword compilation, and argument validation.
      context =
        Enum.reduce(nodes, context, fn {path, _namespace, _schema}, context ->
          {_key, context} =
            JSV.build_key!(context, if(path == [], do: key, else: JSV.Ref.pointer!(path, key)))

          context
        end)

      root = JSV.to_root!(context, key)
      Enum.each(Map.keys(root.validators), &check_key(&1, root.validators, MapSet.new()))
      root
    end
  end

  def valid?(schema, arguments),
    do: match?({:ok, _}, JSV.validate(arguments, schema, cast: false))

  defp collect(schema, path, namespace) when is_map(schema) do
    if Map.has_key?(schema, "$schema") and schema["$schema"] != @dialect,
      do: raise(ArgumentError, "Tool schemas must use Draft 2020-12.")

    namespace = if id = schema["$id"], do: JSV.Ref.parse!(id, namespace).ns, else: namespace

    children =
      Enum.flat_map(schema, fn {keyword, value} ->
        cond do
          keyword in [
            "$defs",
            "definitions",
            "properties",
            "patternProperties",
            "dependentSchemas"
          ] ->
            Enum.flat_map(value, fn {name, child} ->
              collect(child, path ++ [keyword, name], namespace)
            end)

          keyword in ["allOf", "anyOf", "oneOf", "prefixItems"] ->
            value
            |> Enum.with_index()
            |> Enum.flat_map(fn {child, index} ->
              collect(child, path ++ [keyword, index], namespace)
            end)

          keyword in [
            "not",
            "if",
            "then",
            "else",
            "items",
            "contains",
            "additionalProperties",
            "unevaluatedProperties",
            "unevaluatedItems",
            "propertyNames",
            "contentSchema"
          ] ->
            collect(value, path ++ [keyword], namespace)

          true ->
            []
        end
      end)

    [{path, namespace, schema} | children]
  end

  defp collect(_boolean, _path, _namespace), do: []

  # Follow only edges evaluating the same input. A property/item boundary
  # permits recursion; every compiled schema is checked independently above.
  defp check_key(key, validators, active) do
    if MapSet.member?(active, key),
      do: raise(ArgumentError, "Tool schema reference cycle does not descend into its input.")

    check_schema(Map.fetch!(validators, key), validators, MapSet.put(active, key))
  end

  defp check_schema({:alias_of, key}, validators, active), do: check_key(key, validators, active)
  defp check_schema(%JSV.BooleanSchema{}, _validators, _active), do: :ok

  defp check_schema(%JSV.Subschema{validators: vocabularies}, validators, active) do
    for {_module, keywords} <- vocabularies, keyword <- keywords do
      case keyword do
        {:ref, _kind, {:dynamic_anchor, _namespace, anchor}} ->
          for {:dynamic_anchor, _namespace, ^anchor} = key <- Map.keys(validators),
              do: check_key(key, validators, active)

        {:ref, _kind, key} ->
          check_key(key, validators, active)

        {kind, schemas} when kind in [:allOf, :anyOf, :oneOf] ->
          Enum.each(schemas, &check_schema(&1, validators, active))

        {:dependentSchemas, schemas} ->
          Enum.each(Map.values(schemas), &check_schema(&1, validators, active))

        {:jsv@if, schemas} ->
          schemas
          |> Tuple.to_list()
          |> Enum.reject(&is_nil/1)
          |> Enum.each(&check_schema(&1, validators, active))

        {:not, schema} ->
          check_schema(schema, validators, active)

        _ ->
          :ok
      end
    end
  end
end
