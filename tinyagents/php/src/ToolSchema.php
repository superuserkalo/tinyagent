<?php
declare(strict_types=1);

namespace TinyAgent;

use Opis\JsonSchema\Parsers\SchemaParser;
use Opis\JsonSchema\Resolvers\SchemaResolver;
use Opis\JsonSchema\Schema;
use Opis\JsonSchema\SchemaLoader;
use Opis\JsonSchema\Schemas\ExceptionSchema;
use Opis\JsonSchema\Uri;
use Opis\JsonSchema\Validator;

/** @internal Opis owns keyword validation; this boundary fixes its dialect and reference policy. */
final readonly class ToolSchema
{
    private const DIALECT = 'https://json-schema.org/draft/2020-12/schema';
    private Validator $validator;
    private Schema $schema;

    public function __construct(\stdClass|bool $parameters)
    {
        $options = array_fill_keys([
            'allowFilters', 'allowFormats', 'allowMappers', 'allowTemplates', 'allowGlobals', 'allowDefaults',
            'allowSlots', 'allowKeywordValidators', 'allowPragmas', 'allowDataKeyword',
            'allowRelativeJsonPointerInRef', 'allowExclusiveMinMaxAsBool', 'keepDependenciesKeyword', 'keepAdditionalItemsKeyword',
        ], false);
        $resolver = new SchemaResolver();
        foreach (glob(dirname(__DIR__) . '/resources/draft202012/*.json') as $path) {
            $resolver->registerRaw(json_decode(file_get_contents($path), flags: JSON_THROW_ON_ERROR));
        }
        $meta = new Validator(new SchemaLoader(new SchemaParser([], $options), $resolver));
        if (!$meta->validate($parameters, self::DIALECT)->isValid()) {
            throw new \InvalidArgumentException('Tool parameters must be a valid Draft 2020-12 schema.');
        }

        // This loader has no resolver: only schemas in this document are available.
        $loader = new SchemaLoader(new SchemaParser([], $options));
        $this->validator = new Validator($loader);
        $this->schema = is_bool($parameters) ? $loader->loadBooleanSchema($parameters) : $loader->loadObjectSchema($parameters);
        $this->checkReferences($loader);
    }

    public function valid(\stdClass $arguments): bool
    {
        return $this->validator->validate($arguments, $this->schema)->isValid();
    }

    private function checkReferences(SchemaLoader $loader): void
    {
        $nodes = [];
        $edges = [];
        $anchors = [];
        $references = [];
        $visit = function (Schema $schema) use (&$visit, &$nodes, &$edges, &$anchors, &$references, $loader): void {
            if ($schema instanceof ExceptionSchema) {
                throw new \InvalidArgumentException('Tool schema could not be compiled.');
            }
            $info = $schema->info();
            $data = $info->data();
            if (is_bool($data) || isset($nodes[spl_object_id($data)])) {
                return;
            }
            if ($info->draft() !== '2020-12' || (isset($data->{'$schema'}) && $data->{'$schema'} !== self::DIALECT)) {
                throw new \InvalidArgumentException('Tool schemas must use Draft 2020-12.');
            }
            $id = spl_object_id($data);
            $nodes[$id] = $schema;
            $edges[$id] = [];
            foreach (['$anchor', '$dynamicAnchor'] as $name) {
                if (isset($data->{$name})) {
                    $anchors[(string) $info->idBaseRoot()->resolveRef('#' . $data->{$name})] = $schema;
                }
            }
            foreach (['$ref', '$dynamicRef'] as $name) {
                if (isset($data->{$name})) {
                    $references[] = [$id, Uri::merge($data->{$name}, $info->idBaseRoot(), true)];
                }
            }
            foreach (get_object_vars($data) as $name => $value) {
                $children = match ($name) {
                    '$defs', 'definitions', 'properties', 'patternProperties', 'dependentSchemas' => (array) $value,
                    'allOf', 'anyOf', 'oneOf', 'prefixItems' => $value,
                    'not', 'if', 'then', 'else', 'items', 'contains', 'additionalProperties',
                    'unevaluatedProperties', 'unevaluatedItems', 'propertyNames', 'contentSchema' => [$value],
                    default => [],
                };
                foreach ($children as $child) {
                    if (!$child instanceof \stdClass) {
                        continue;
                    }
                    $next = $loader->loadObjectSchema($child);
                    $visit($next);
                    if (in_array($name, ['allOf', 'anyOf', 'oneOf', 'not', 'if', 'then', 'else', 'dependentSchemas'], true)) {
                        $edges[$id][] = spl_object_id($child);
                    }
                }
            }
        };
        $visit($this->schema);
        for ($index = 0; $index < count($references); ++$index) {
            [$id, $uri] = $references[$index];
            $target = $uri === null ? null : ($loader->loadSchemaById($uri) ?? $anchors[(string) $uri] ?? null);
            if ($target === null) {
                throw new \InvalidArgumentException('Tool schema reference is not available locally: ' . $uri);
            }
            $visit($target);
            if ($target->info()->isObject()) {
                $edges[$id][] = spl_object_id($target->info()->data());
            }
        }
        $active = [];
        $finished = [];
        $checkCycle = function (int $id) use (&$checkCycle, &$active, &$finished, $edges): void {
            if (isset($active[$id])) {
                throw new \InvalidArgumentException('Tool schema reference cycle does not descend into its input.');
            }
            if (isset($finished[$id])) {
                return;
            }
            $active[$id] = true;
            foreach ($edges[$id] as $child) {
                $checkCycle($child);
            }
            unset($active[$id]);
            $finished[$id] = true;
        };
        foreach (array_keys($nodes) as $id) {
            $checkCycle($id);
        }
    }
}
