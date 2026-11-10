<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

require getenv('TINYAGENT_PHP_AUTOLOAD') ?: (is_file(__DIR__ . '/../vendor/autoload.php')
    ? __DIR__ . '/../vendor/autoload.php' : __DIR__ . '/../autoload.php');
require __DIR__ . '/FixtureClient.php';
require __DIR__ . '/ProviderChecks.php';
require __DIR__ . '/LifecycleChecks.php';
require __DIR__ . '/CoreChecks.php';
require __DIR__ . '/CoreLifecycleChecks.php';
require __DIR__ . '/SessionChecks.php';
require __DIR__ . '/SessionLifecycleChecks.php';
require __DIR__ . '/RpcProcess.php';
require __DIR__ . '/RpcChecks.php';
require __DIR__ . '/RpcEmbeddedChecks.php';
require __DIR__ . '/ProviderTcpChecks.php';
require __DIR__ . '/ProcessFixtures.php';
require __DIR__ . '/BashChecks.php';
require __DIR__ . '/CliChecks.php';
require __DIR__ . '/HttpProcess.php';
require __DIR__ . '/HttpChecks.php';
require __DIR__ . '/ToolChecks.php';

$args = array_slice($argv, 1);
if ($args !== [] && $args !== ['--offline'] && $args !== ['--http-only'] && $args !== ['--tools-only']) {
    fwrite(STDERR, "Usage: php tests/check.php [--offline|--http-only|--tools-only]\n");
    exit(2);
}

set_error_handler(static function (int $severity, string $message, string $file, int $line): never {
    throw new \ErrorException($message, 0, $severity, $file, $line);
});
$checks = [
    'OpenAI shared fixtures and replay' => static fn() => sharedFixtures(false),
    'Anthropic shared fixtures and replay' => static fn() => sharedFixtures(true),
    'Immutable provider values and referenced arrays' => immutableValues(...),
    'SSE framing and byte limits' => framingAndBounds(...),
    'Accumulated tool argument limits' => argumentBounds(...),
    'OpenAI malformed and incomplete streams' => malformedOpenAI(...),
    'Anthropic malformed and incomplete streams' => malformedAnthropic(...),
    'Cancellation across callbacks and cleanup' => cancellation(...),
    'Deadlines across callbacks and cleanup' => deadlines(...),
    'Consumer failure releases the response' => callbackFailure(...),
    'Terminal completion stops parsing and waits for cleanup' => completedCleanup(...),
    'Lazy credentials and request options' => credentialsAndOptions(...),
    'Core native providers and ordered events' => coreProviders(...),
    'Core immutable event protocol values' => coreEvents(...),
    'Core immutable history and output' => coreValues(...),
    'Core provider failures and unregistered tools' => coreProviderFailures(...),
    'Core input validation and safe error events' => coreInputValidation(...),
    'Core consumer failure and native cleanup' => coreConsumerFailure(...),
    'Core preserves swallowed consumer failure' => coreSwallowedConsumerFailure(...),
    'Core pre-cancellation and callback cancellation scope' => coreCancellation(...),
    'Core ignored provider cancellation' => coreIgnoredCancellation(...),
    'Core native deadlines through callbacks and cleanup' => coreDeadlines(...),
    'Core held final delivery and cancellation' => coreHeldDelivery(...),
    'Core cancellation waits for provider cleanup' => coreHeldCleanup(...),
    'Core concurrent runs retain independent state' => coreConcurrentRuns(...),
    'Core rejects retained provider callbacks' => coreRetainedCallbacks(...),
    'Core caller interruptions survive provider wrapping and cleanup' => coreCallerInterruption(...),
    'Session shared parsed commands and value snapshots' => sessionSharedCommands(...),
    'Session strict native JSON validation' => sessionInvalidValues(...),
    'Session native providers and history commit' => sessionNativeProviders(...),
    'Session busy, abort, reset, and rollback' => sessionBusyAndRollback(...),
    'Session command consumer failures are not retried' => sessionCommandConsumerFailure(...),
    'Session pending acknowledgement cancellation' => sessionPendingAcknowledgement(...),
    'Session run consumer failures retain history' => sessionRunConsumerFailure(...),
    'Session held terminal delivery and rollback' => sessionHeldFinalDelivery(...),
    'Session reset waits for held cleanup' => static fn() => sessionHeldCleanup(false),
    'Session close waits for held cleanup' => static fn() => sessionHeldCleanup(true),
    'Session callback-initiated cleanup avoids self-wait' => sessionSelfCleanup(...),
    'Session independent fibers retain isolated history' => sessionIndependentRuns(...),
    'Session waiters join only their observed run' => sessionWaitsForObservedRun(...),
    'RPC shared JSONL commands and malformed input' => rpcSharedWire(...),
    'RPC native provider fixtures and history' => rpcRunsAndHistory(...),
    'RPC busy, abort, reset, and failed runs' => rpcAbortAndReset(...),
    'RPC EOF, SIGINT, broken output, and cleanup' => rpcShutdown(...),
    'RPC native input framing and byte limits' => rpcInputLimits(...),
    'RPC oversized events cancel and roll back history' => rpcOversizedOutput(...),
    'RPC cancellation releases blocked native output' => rpcBlockedOutput(...),
    'RPC bounded queue fails under native pipe backpressure' => rpcQueueOverflow(...),
    'RPC oversized command reply cancels only its active run' => rpcOversizedReply(...),
    'RPC embedding cancellation joins held provider cleanup' => rpcEmbeddedCleanup(...),
    'RPC concurrent connections own separate streams and histories' => rpcConcurrentConnections(...),
    'RPC stream validation, pre-cancellation, and failure ownership' => rpcStreamOwnership(...),
    'RPC scoped signal handlers preserve application changes' => rpcSignalOwnership(...),
    'RPC overflow retains its partial frame before the error line' => rpcOverflowFraming(...),
    'RPC cooperative scheduling is limited to owned fibers' => rpcCooperativeScope(...),
    'RPC fibers suspend inside a native cURL file callback' => rpcNativeCurlCallback(...),
    'Bash shared guard and input validation' => bashGuard(...),
    'Bash native stdout, stderr, status, and closed stdin' => bashOutput(...),
    'Bash combined output bounds and infinite output' => bashBounds(...),
    'Bash cancellation joins command cleanup' => static fn() => bashCancellation(false),
    'Bash caller interruption preserves cleanup and original error' => static fn() => bashCancellation(true),
    'Bash independent commands and background cleanup' => bashOwnership(...),
    'Bash scheduling is limited to SDK-owned fibers' => bashCooperativeScope(...),
    'CLI streaming, replay, sequential calls, and immutable history' => cliStreaming(...),
    'CLI shared Bash observations, argument errors, and overflow recovery' => cliBashObservations(...),
    'CLI provider failures and response bounds retain history' => cliFailures(...),
    'CLI consumer failures survive provider wrapping and suppression' => cliConsumerFailures(...),
    'CLI cancellation releases requests and Bash' => cliCancellation(...),
    'CLI terminal text, cancellation, and retained callbacks' => cliTerminal(...),
    'CLI console rollback and borrowed stream ownership' => cliBorrowedStreams(...),
    'CLI native EOF, idle SIGINT, and pending requests' => static fn() => cliInterrupt(['idle', 'request', 'conversation']),
    'CLI native SIGINT joins Bash process cleanup' => static fn() => cliInterrupt(['bash']),
    'CLI native SIGINT releases stalled output and restores flags' => static fn() => cliInterrupt(['stdout', 'nonblocking', 'stderr']),
    'CLI native broken stdout releases provider work' => static fn() => cliInterrupt(['broken']),
];
$toolChecks = [
    'Tools shared schemas and invalid construction' => toolSchemas(...),
    'Tools immutable values, annotations, and no coercion' => toolSnapshots(...),
    'Tools native providers and replay' => toolProviderReplay(...),
    'Tools sequential execution and bounded error recovery' => toolRecovery(...),
    'Tools unknown names reject the whole batch' => toolUnknownBatch(...),
    'Tools cancellation pairs start and end events' => toolCancellation(...),
    'Tools cancellation and caller interruption join held cleanup' => toolHeldCleanup(...),
    'Tools consumer failures and retained callback ownership' => toolCallbackOwnership(...),
    'Tools RPC acknowledgement, history commit, and rollback' => toolSessionCommit(...),
    'Tools native RPC subprocess events and replay' => toolRpcProcess(...),
];
$checks += $toolChecks;
if ($args === ['--tools-only']) {
    $checks = $toolChecks;
}
if ($args === ['--http-only']) {
    $checks = [];
}
if ($args === []) {
    $checks['OpenAI over native cURL and TCP'] = static fn() => providerTcp(false);
    $checks['Anthropic over native cURL and TCP'] = static fn() => providerTcp(true);
}
if ($args !== ['--offline'] && $args !== ['--tools-only']) {
    $checks += [
        'HTTP loopback, IPv6, remote opt-in, and absent CORS' => httpExposure(...),
        'HTTP bearer authentication and duplicate headers' => httpAuthentication(...),
        'HTTP native framing, strict JSON, and UTF-8 fragments' => httpFraming(...),
        'HTTP fixed and chunked body limits' => httpBodyLimits(...),
        'HTTP ordered events, failures, and committed history' => httpHistory(...),
        'HTTP busy, cancellation, and independent sessions' => httpCancellation(...),
        'HTTP oversized events fail and roll back history' => httpOversizedEvents(...),
        'HTTP active and silent disconnect cleanup' => httpDisconnects(...),
        'HTTP cancellation releases stalled native output' => httpBlockedOutput(...),
        'HTTP deletion and shutdown retain cooperative cleanup' => httpHeldCleanup(...),
        'HTTP tool events, cancellation, and history rollback' => toolHttpProcess(...),
    ];
} else {
    echo "Offline checks exclude mandatory provider and HTTP TCP groups.\n";
}
$failed = 0;
foreach ($checks as $name => $run) {
    try {
        AssertionFailure::$first = null;
        $run();
        if (AssertionFailure::$first !== null) {
            throw AssertionFailure::$first;
        }
        echo "PASS {$name}\n";
    } catch (\Throwable $error) {
        $failed++;
        fwrite(STDERR, "FAIL {$name}: {$error}\n");
    }
}
echo count($checks) - $failed, " passed, {$failed} failed.\n";
exit($failed === 0 ? 0 : 1);
