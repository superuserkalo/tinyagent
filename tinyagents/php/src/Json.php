<?php
declare(strict_types=1);

namespace TinyAgent;

/** @internal JSON objects remain distinct from arrays, including empty objects. */
final class Json
{
    public const MAX_BYTES = 1_048_576;

    public static function text(string $value, string $name): string
    {
        if (preg_match('//u', $value) !== 1) {
            throw new \InvalidArgumentException("{$name} must be valid UTF-8.");
        }
        return $value;
    }

    public static function object(mixed $value, string $name = 'JSON'): \stdClass
    {
        if (!$value instanceof \stdClass) {
            throw new ProviderException("Missing or invalid object: {$name}");
        }
        return $value;
    }

    /** @return list<mixed> */
    public static function list(mixed $value, string $name): array
    {
        if (!is_array($value) || !array_is_list($value)) {
            throw new ProviderException("Missing or invalid array: {$name}");
        }
        return $value;
    }

    public static function string(\stdClass $value, string $field, bool $nonempty = false): string
    {
        $text = $value->{$field} ?? null;
        if (!is_string($text) || ($nonempty && $text === '') || preg_match('//u', $text) !== 1) {
            throw new ProviderException("Missing or invalid string: {$field}");
        }
        return $text;
    }

    public static function index(mixed $value): int
    {
        if (!is_int($value) || $value < 0) {
            throw new ProviderException('Missing or invalid block index.');
        }
        return $value;
    }

    public static function encode(mixed $value): string
    {
        try {
            return json_encode($value, JSON_THROW_ON_ERROR | JSON_UNESCAPED_UNICODE | JSON_PRESERVE_ZERO_FRACTION, 100);
        } catch (\JsonException $error) {
            throw new ProviderException('Invalid JSON value.', previous: $error);
        }
    }

    public static function parseObject(string $source): \stdClass
    {
        try {
            $value = json_decode($source, false, 100, JSON_THROW_ON_ERROR);
            // The decoder can represent a syntactically valid exponent as infinity.
            self::encode($value);
            return self::object($value);
        } catch (\JsonException $error) {
            throw new ProviderException('Invalid provider JSON.', previous: $error);
        }
    }

    public static function copyObject(\stdClass $value): \stdClass
    {
        return self::parseObject(self::encode($value));
    }

    /** @param list<\stdClass> $values
     *  @return list<\stdClass>
     */
    public static function copyObjects(array $values): array
    {
        self::list($values, 'objects');
        return array_map(self::copyObject(...), $values);
    }
}

/** @internal Counts decoded UTF-8 argument bytes across SSE events. */
final class Arguments
{
    private string $value = '';

    public function append(string $fragment): void
    {
        if (strlen($fragment) > Json::MAX_BYTES - strlen($this->value)) {
            throw new ProviderException('Tool arguments exceed their byte limit.');
        }
        $this->value .= $fragment;
    }

    public function set(string $value): void
    {
        $this->value = '';
        $this->append($value);
    }

    public function text(): string
    {
        return $this->value;
    }
}
