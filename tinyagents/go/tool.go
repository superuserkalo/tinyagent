package tinyagent

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"unicode/utf8"

	"github.com/kaptinlin/jsonschema"
)

type ToolDefinition struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	Parameters  json.RawMessage `json:"parameters"`
}

type ToolHandler func(context.Context, json.RawMessage) (string, error)

// Tool owns a checked schema and one native handler. Use NewTool to construct it.
type Tool struct {
	definition ToolDefinition
	schema     *jsonschema.Schema
	handler    ToolHandler
}

func NewTool(definition ToolDefinition, handler ToolHandler) (*Tool, error) {
	if definition.Name == "" || handler == nil {
		return nil, errors.New("tool name and handler are required")
	}
	var keywords map[string]json.RawMessage
	if err := json.Unmarshal(definition.Parameters, &keywords); err != nil || keywords == nil {
		return nil, errors.New("tool parameters must be a JSON Schema object")
	}
	if async, ok := keywords["$async"]; ok && !bytes.Equal(async, []byte("false")) {
		return nil, errors.New("tool schemas must be synchronous")
	}
	compiler, err := schemaCompiler()
	if err != nil {
		return nil, err
	}
	validation, err := compiler.ValidateSchema(definition.Parameters)
	if err != nil {
		return nil, fmt.Errorf("invalid tool schema: %w", err)
	}
	if !validation.IsValid() {
		return nil, errors.New("invalid tool schema")
	}
	schema, err := compiler.Compile(definition.Parameters)
	if err != nil {
		return nil, fmt.Errorf("invalid tool schema: %w", err)
	}
	if unresolved := schema.UnresolvedReferenceURIs(); len(unresolved) > 0 {
		return nil, fmt.Errorf("unresolved tool schema reference: %s", unresolved[0])
	}
	definition.Parameters = bytes.Clone(definition.Parameters)
	return &Tool{definition: definition, schema: schema, handler: handler}, nil
}

func (t *Tool) Definition() ToolDefinition {
	definition := t.definition
	definition.Parameters = bytes.Clone(definition.Parameters)
	return definition
}

// Execute validates without coercion or defaults, then gives the handler its own argument buffer.
func (t *Tool) Execute(ctx context.Context, arguments json.RawMessage) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", err
	}
	arguments = bytes.TrimSpace(arguments)
	if len(arguments) == 0 || arguments[0] != '{' || !utf8.Valid(arguments) {
		return "", fmt.Errorf("invalid arguments for %s: expected a UTF-8 JSON object", t.definition.Name)
	}
	validation := t.schema.ValidateJSON(arguments)
	if !validation.IsValid() {
		details := validation.DetailedErrors()
		paths := make([]string, 0, len(details))
		for path := range details {
			paths = append(paths, path)
		}
		slices.Sort(paths)
		if len(paths) > 0 {
			return "", fmt.Errorf("invalid arguments for %s%s: %s", t.definition.Name, paths[0], details[paths[0]])
		}
		return "", fmt.Errorf("invalid arguments for %s", t.definition.Name)
	}
	return t.handler(ctx, bytes.Clone(arguments))
}
