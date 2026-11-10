<?php
declare(strict_types=1);

namespace TinyAgent;

/** @internal Only SDK-owned fibers yield automatically during I/O and process waits. */
final class Cooperative
{
    /** @var ?\WeakMap<\Fiber, true> */
    private static ?\WeakMap $fibers = null;
    /** @var ?\WeakMap<\Throwable, true> */
    private static ?\WeakMap $interruptions = null;

    public static function fiber(callable $callback): \Fiber
    {
        $fiber = new \Fiber($callback);
        self::$fibers ??= new \WeakMap();
        self::$fibers[$fiber] = true;
        return $fiber;
    }

    public static function pause(): bool
    {
        $fiber = \Fiber::getCurrent();
        if ($fiber === null || !isset(self::$fibers[$fiber])) {
            return false;
        }
        try {
            \Fiber::suspend();
        } catch (\Throwable $error) {
            // Fiber::throw is caller control flow, even when a provider wraps the exception.
            self::$interruptions ??= new \WeakMap();
            self::$interruptions[$error] = true;
            throw $error;
        }
        return true;
    }

    public static function interruption(\Throwable $error): ?\Throwable
    {
        do {
            if (isset(self::$interruptions[$error])) {
                return $error;
            }
            $error = $error->getPrevious();
        } while ($error !== null);
        return null;
    }
}
