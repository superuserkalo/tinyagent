<?php
declare(strict_types=1);

namespace TinyAgent;

final class CurlClient implements HttpClient
{
    public function stream(HttpRequest $request, CancellationToken $cancellation, callable $consume): void
    {
        $cancellation->throwIfCancelled();
        $curl = curl_init($request->endpoint);
        if ($curl === false) {
            throw new ProviderException('Could not initialize cURL.');
        }
        $multi = curl_multi_init();
        $added = false;
        $stopped = false;
        $failure = null;
        try {
            $headers = ['Content-Type: application/json'];
            if (!array_key_exists('accept', array_change_key_case($request->headers, CASE_LOWER))) {
                $headers[] = 'Accept: text/event-stream';
            }
            foreach ($request->headers as $name => $value) {
                $headers[] = "{$name}: {$value}";
            }
            if (!curl_setopt_array($curl, [
                CURLOPT_POST => true,
                CURLOPT_POSTFIELDS => $request->body,
                CURLOPT_HTTPHEADER => $headers,
                CURLOPT_PROTOCOLS => CURLPROTO_HTTP | CURLPROTO_HTTPS,
                CURLOPT_FOLLOWLOCATION => false,
                CURLOPT_FORBID_REUSE => true,
                CURLOPT_CONNECTTIMEOUT_MS => (int) ceil(min(30, $request->timeout) * 1000),
                CURLOPT_TIMEOUT_MS => max(1, (int) ceil($request->timeout * 1000)),
                CURLOPT_WRITEFUNCTION => static function (\CurlHandle $handle, string $bytes) use (
                    $cancellation, $consume, &$stopped, &$failure,
                ): int {
                    try {
                        $cancellation->throwIfCancelled();
                        $status = curl_getinfo($handle, CURLINFO_RESPONSE_CODE);
                        if ($status < 200 || $status >= 300) {
                            throw new ProviderException("Provider HTTP {$status}.");
                        }
                        if (!$consume($bytes)) {
                            $stopped = true;
                            return 0;
                        }
                        $cancellation->throwIfCancelled();
                        return strlen($bytes);
                    } catch (\Throwable $error) {
                        $failure = $error;
                        return 0;
                    }
                },
            ])) {
                throw new ProviderException('Could not configure cURL request.');
            }
            if (curl_multi_add_handle($multi, $curl) !== CURLM_OK) {
                throw new ProviderException('Could not start cURL request.');
            }
            $added = true;
            do {
                $cancellation->throwIfCancelled();
                $state = curl_multi_exec($multi, $running);
                $cancellation->throwIfCancelled();
                if ($failure !== null) {
                    throw $failure;
                }
                if ($stopped) {
                    break;
                }
                if ($state !== CURLM_OK) {
                    throw new ProviderException('cURL failed: ' . curl_multi_strerror($state));
                }
                $done = curl_multi_info_read($multi);
                if ($done !== false && $done['result'] !== CURLE_OK) {
                    if ($done['result'] === CURLE_OPERATION_TIMEDOUT) {
                        throw new ProviderException('Provider request timed out.');
                    }
                    throw new ProviderException('Provider request failed: ' . curl_strerror($done['result']));
                }
                if ($running === 0) {
                    $status = curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
                    if ($status < 200 || $status >= 300) {
                        throw new ProviderException("Provider HTTP {$status}.");
                    }
                    break;
                }
                if (Cooperative::pause()) {
                    continue;
                }
                $ready = curl_multi_select($multi, 0.05);
                $cancellation->throwIfCancelled();
                if ($ready < 0) {
                    throw new ProviderException('cURL could not wait for network activity.');
                }
                if ($ready === 0) {
                    usleep(1000);
                }
            } while ($running > 0);
        } finally {
            if ($added) {
                curl_multi_remove_handle($multi, $curl);
            }
            // Release callbacks even if a captured exception retains its native callback arguments.
            curl_reset($curl);
            unset($curl, $multi);
        }
        $cancellation->throwIfCancelled();
    }
}
