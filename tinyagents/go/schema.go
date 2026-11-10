package tinyagent

import (
	"embed"
	"encoding/json"

	"github.com/kaptinlin/jsonschema"
)

//go:embed schemas/*.json
var metaSchemas embed.FS

func schemaCompiler() (*jsonschema.Compiler, error) {
	compiler := jsonschema.NewCompiler()
	clear(compiler.Loaders)
	entries, err := metaSchemas.ReadDir("schemas")
	if err != nil {
		return nil, err
	}
	resources := make(map[string][]byte)
	for _, entry := range entries {
		data, err := metaSchemas.ReadFile("schemas/" + entry.Name())
		if err != nil {
			return nil, err
		}
		var header struct {
			ID string `json:"$id"`
		}
		if err := json.Unmarshal(data, &header); err != nil {
			return nil, err
		}
		resources[header.ID] = data
	}
	if _, err := compiler.CompileBatch(resources); err != nil {
		return nil, err
	}
	return compiler, nil
}
