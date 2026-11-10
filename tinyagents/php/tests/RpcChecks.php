<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

function rpcSharedWire(): void
{
    $fixture = json_decode(file_get_contents(__DIR__ . '/../../../conformance/rpc_v1.json'), flags: JSON_THROW_ON_ERROR);
    $child = new RpcProcess();
    try {
        foreach ($fixture->scenarios as $scenario) {
            if (!isset($scenario->expected)) {
                continue;
            }
            foreach ($scenario->commands as $command) {
                $child->send($command);
            }
            foreach ($scenario->expected as $expected) {
                same(true, $expected == $child->read());
            }
        }
        $child->write("{broken}\n{\"extra\":1e999,\"type\":\"get_state\"}\n\n[]\n");
        foreach (['invalid_json', 'invalid_json', 'invalid_json', 'invalid_command'] as $code) {
            same($code, $child->read()->error->code);
        }
        $child->write('{"id":"café","type":"get_state"}');
        $child->closeInput();
        same('café', $child->read()->id);
        same(0, $child->finish()->entered);
    } finally {
        $child->close();
    }
}

function rpcRunsAndHistory(): void
{
    foreach (['script', 'openai', 'anthropic'] as $mode) {
        $child = new RpcProcess($mode);
        try {
            foreach ([1, 3] as $count) {
                $child->send((object) ['id' => 'prompt', 'type' => 'prompt', 'message' => 'go']);
                $ack = $child->read();
                same('prompt', $ack->id);
                same(true, $ack->success);
                $values = $child->through('agent_end');
                same('agent_start', $values[0]->type);
                same(range(1, count($values)), array_column($values, 'sequence'));
                same([$ack->data->runId], array_values(array_unique(array_column($values, 'runId'))));
                same('completed', end($values)->status);
                same($mode === 'script' ? 'history:' . $count : "done\ncafé", end($values)->text);
                same($count + 1, $child->state()->messageCount);
            }
            $child->send((object) ['type' => 'new_session']);
            same(false, $child->read()->data->abortedRun);
            same(0, $child->state()->messageCount);
            $stats = $child->finish();
            same($stats->entered, $stats->closed);
            if ($mode !== 'script') {
                same([true, true], $stats->responsesClosed);
            }
        } finally {
            $child->close();
        }
    }
}

function rpcAbortAndReset(): void
{
    foreach (['abort', 'new_session'] as $command) {
        $child = new RpcProcess();
        try {
            $child->send((object) ['type' => 'prompt', 'message' => 'cleanup']);
            $child->through('text_delta');
            $child->send((object) ['id' => 'busy', 'type' => 'prompt', 'message' => 'go']);
            same('busy', $child->read()->error->code);
            $child->send((object) ['id' => 'cancel', 'type' => $command]);
            $values = [$child->read(), $child->read()];
            $response = $values[$values[0]->type === 'response' ? 0 : 1];
            $terminal = $values[$values[0]->type === 'agent_end' ? 0 : 1];
            same('cancel', $response->id);
            same('aborted', $terminal->status);
            same(true, $command === 'abort' ? $response->data->accepted : $response->data->abortedRun);
            same(0, $child->state()->messageCount);
            $child->send((object) ['type' => 'prompt', 'message' => 'fail']);
            $failed = $child->through('agent_end');
            same('failed', end($failed)->status);
            same(0, $child->state()->messageCount);
            $stats = $child->finish();
            same(2, $stats->entered);
            same(2, $stats->closed);
        } finally {
            $child->close();
        }
    }
}

function rpcShutdown(): void
{
    foreach (['eof', 'signal', 'broken'] as $mode) {
        $child = new RpcProcess();
        try {
            $child->send((object) ['type' => 'prompt', 'message' => 'cleanup']);
            $child->through('text_delta');
            if ($mode === 'signal') {
                $child->signal();
                same('aborted', $child->read()->status);
            } elseif ($mode === 'broken') {
                $child->closeOutput();
                $child->send((object) ['type' => 'abort']);
            }
            $stats = $child->finish($mode === 'broken' ? 1 : 0);
            same(1, $stats->entered);
            same(1, $stats->closed);
            same($mode === 'broken' ? 'output_error' : null, $stats->error);
        } finally {
            $child->close();
        }
    }
}

function rpcInputLimits(): void
{
    foreach (['utf8', 'limit', 'valid'] as $mode) {
        $child = new RpcProcess();
        try {
            $line = '{"id":"limit","type":"get_state","padding":""}';
            $line = substr($line, 0, -2) . str_repeat('a', 1_048_576 - strlen($line)) . '"}';
            if ($mode === 'utf8') {
                $child->write("\xff\n");
            } else {
                $child->write($line . ($mode === 'limit' ? ' ' : '') . "\n");
            }
            $response = $child->read();
            same($mode === 'valid' ? 'response' : 'protocol_error', $response->type);
            if ($mode !== 'valid') {
                same($mode === 'utf8' ? 'invalid_utf8' : 'line_too_large', $response->error->code);
            }
            $child->finish();
        } finally {
            $child->close();
        }
    }
}

function rpcOversizedOutput(): void
{
    foreach (['large_delta', 'large_final'] as $prompt) {
        $child = new RpcProcess();
        try {
            $child->send((object) ['type' => 'prompt', 'message' => $prompt]);
            $values = $child->through('protocol_error');
            same('output_too_large', end($values)->error->code);
            same(0, $child->state()->messageCount);
            $child->send((object) ['type' => 'prompt', 'message' => 'go']);
            $values = $child->through('agent_end');
            same('completed', end($values)->status);
            same(2, $child->state()->messageCount);
            same(2, $child->finish()->closed);
        } finally {
            $child->close();
        }
    }
}

function rpcBlockedOutput(): void
{
    foreach (['eof', 'abort', 'signal'] as $mode) {
        $child = new RpcProcess();
        try {
            $child->send((object) ['type' => 'prompt', 'message' => 'blocked']);
            $child->through('agent_start');
            $child->waitForPartialOutput();
            if ($mode === 'abort') {
                $child->send((object) ['type' => 'abort']);
            } elseif ($mode === 'signal') {
                $child->signal();
            }
            $stats = $child->finish(1, drain: false);
            same('output_stalled', $stats->error);
            same($stats->entered, $stats->closed);
        } finally {
            $child->close();
        }
    }
}

function rpcQueueOverflow(): void
{
    $child = new RpcProcess();
    try {
        // Each legal response is roughly half a MiB. The peer deliberately stops reading.
        $command = (object) ['id' => str_repeat('x', 500_000), 'type' => 'get_state'];
        for ($i = 0; $i < 9; $i++) {
            $child->send($command);
        }
        same('output_queue_full', $child->finish(1, drain: false)->error);
    } finally {
        $child->close();
    }
}

function rpcOversizedReply(): void
{
    $child = new RpcProcess();
    try {
        $child->send((object) ['type' => 'prompt', 'message' => 'wait']);
        $child->through('text_delta');
        $line = '{"id":"","type":"get_state"}';
        $id = str_repeat('x', 1_048_576 - strlen($line));
        $child->send((object) ['id' => $id, 'type' => 'get_state']);
        same('output_too_large', $child->read()->error->code);
        same('aborted', $child->read()->status);
        same(0, $child->state()->messageCount);
        $child->send((object) ['type' => 'prompt', 'message' => 'go']);
        $values = $child->through('agent_end');
        same('completed', end($values)->status);
        same(2, $child->state()->messageCount);
        same(2, $child->finish()->closed);
    } finally {
        $child->close();
    }
}
