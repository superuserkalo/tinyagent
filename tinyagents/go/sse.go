package tinyagent

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"
)

// readSSE stops at the provider's terminal event, even if HTTP remains open.
func readSSE(ctx context.Context, client *http.Client, endpoint string, headers http.Header,
	body []byte, consume func(json.RawMessage) (bool, error)) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return err
	}
	request.Header = headers.Clone()
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "text/event-stream")
	if client == nil {
		client = &http.Client{Timeout: 180 * time.Second}
	}
	response, err := client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		data, err := io.ReadAll(io.LimitReader(response.Body, 65536))
		if err != nil {
			return err
		}
		return fmt.Errorf("provider HTTP %d: %s", response.StatusCode, data)
	}
	scanner := bufio.NewScanner(response.Body)
	scanner.Buffer(make([]byte, 4096), MaxJSONLLineBytes+1)
	var data []string
	dataBytes := 0
	flush := func() (bool, error) {
		if len(data) == 0 {
			return false, nil
		}
		raw := strings.Join(data, "\n")
		data, dataBytes = nil, 0
		if raw == "[DONE]" {
			return false, nil
		}
		if !utf8.ValidString(raw) || !json.Valid([]byte(raw)) {
			return false, fmt.Errorf("invalid UTF-8 JSON in provider stream")
		}
		return consume(json.RawMessage(raw))
	}
	for scanner.Scan() {
		if err := ctx.Err(); err != nil {
			return err
		}
		line := scanner.Text()
		if line == "" {
			if done, err := flush(); done || err != nil {
				return err
			}
		} else if strings.HasPrefix(line, "data:") {
			value := strings.TrimPrefix(line[5:], " ")
			dataBytes += len(value) + 1
			if dataBytes > MaxJSONLLineBytes {
				return fmt.Errorf("provider event exceeds the byte limit")
			}
			data = append(data, value)
		}
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	_, err = flush()
	return err
}
