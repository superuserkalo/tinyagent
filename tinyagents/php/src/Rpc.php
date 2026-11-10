<?php
declare(strict_types=1);

namespace TinyAgent;

/** Native POSIX JSONL transport for a configured agent. Requires PCNTL. */
final class Rpc
{
    public static function runStdio(Agent $agent, ?CancellationToken $cancellation = null): void
    {
        self::run($agent, STDIN, STDOUT, $cancellation, handleInterrupt: true);
    }

    /** Takes ownership of both streams. The caller continues to own the agent.
     *  @param resource $input
     *  @param resource $output
     */
    public static function run(
        Agent $agent,
        mixed $input,
        mixed $output,
        ?CancellationToken $cancellation = null,
        bool $handleInterrupt = false,
    ): void {
        $cancellation?->throwIfCancelled();
        $io = new RpcIO($input, $output);
        $signals = null;
        $error = null;
        try {
            $signals = new RpcSignals($handleInterrupt);
            self::drive($agent, $io, $signals, $cancellation);
        } catch (\Throwable $failure) {
            $error = $failure;
        } finally {
            try {
                $io->close();
            } catch (\Throwable $failure) {
                $error ??= $failure;
            } finally {
                $signals?->close();
            }
        }
        if ($error !== null) {
            throw $error;
        }
    }

    private static function drive(Agent $agent, RpcIO $io, RpcSignals $signals, ?CancellationToken $cancellation): void
    {
        $stopping = new CancellationToken();
        $output = new RpcOutput($io, $stopping);
        $session = new RpcSession($agent, $output->emit(...));
        $readError = null;
        $reader = new \Fiber(static function () use ($io, $session, $output, $stopping, &$readError): void {
            try {
                RpcInput::read($io, $session, $output, $stopping);
            } catch (\Throwable $error) {
                $readError = $error;
            } finally {
                $stopping->cancel();
            }
        });
        $error = null;
        do {
            try {
                pcntl_signal_dispatch();
                if ($signals->interrupted || $cancellation?->isCancelled()) {
                    $stopping->cancel();
                }
                if (!$reader->isStarted()) {
                    $reader->start();
                } elseif ($reader->isSuspended()) {
                    $reader->resume();
                }
                if ($stopping->isCancelled()) {
                    $session->stop();
                    $output->cancel();
                }
                $session->advance();
                $output->advance();
                $pending = !$reader->isTerminated() || $session->state()->runId !== null || $output->pending();
                if ($pending) {
                    if (\Fiber::getCurrent() !== null) {
                        \Fiber::suspend();
                    } else {
                        usleep(1000);
                    }
                }
            } catch (\Throwable $failure) {
                // An embedding fiber may be interrupted while cleanup is still owned here.
                $error ??= $failure;
                $stopping->cancel();
                $session->stop();
                $output->cancel();
                $pending = true;
            }
        } while ($pending);
        $error ??= $readError ?? $output->failure();
        if ($error !== null) {
            throw $error;
        }
        $cancellation?->throwIfCancelled();
    }
}
