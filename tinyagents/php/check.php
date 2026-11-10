<?php
declare(strict_types=1);

$args = array_slice($argv, 1);
if ($args !== [] && $args !== ['--offline']) {
    fwrite(STDERR, "Usage: php check.php [--offline]\n");
    exit(2);
}
$status = 0;
$files = [
    'autoload.php', 'package.php', 'check.php', 'tiny_agent.php',
    ...glob(__DIR__ . '/src/*.php'), ...glob(__DIR__ . '/tests/*.php'),
];
foreach ($files as $file) {
    $process = proc_open([PHP_BINARY, '-l', $file], [STDIN, STDOUT, STDERR], $pipes, __DIR__);
    if (!is_resource($process) || proc_close($process) !== 0) {
        $status = 1;
    }
}
foreach ([['package.php'], ['tests/check.php', ...$args], ['tiny_agent.php', '--self-test']] as $arguments) {
    $process = proc_open([PHP_BINARY, ...$arguments], [STDIN, STDOUT, STDERR], $pipes, __DIR__);
    if (!is_resource($process) || proc_close($process) !== 0) {
        $status = 1;
    }
}
exit($status);
