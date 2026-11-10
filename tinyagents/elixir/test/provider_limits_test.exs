defmodule TinyAgent.ProviderLimitsTest do
  use ExUnit.Case, async: true
  import TinyAgent.TestSupport
  alias TinyAgent.{FixtureTransport, OpenAIProvider, ProviderError}

  test "Mint rejects unfinished response lines and excessive chunk-size digits" do
    head = "HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n"

    cases = [
      {["HTTP/1.1 200 ", String.duplicate("x", 262_145)], :response_line_too_long},
      {[head <> "1;", String.duplicate("x", 262_145)], :response_line_too_long},
      {[head, String.duplicate("f", 17)], :invalid_chunk_size}
    ]

    for {fragments, expected} <- cases do
      {provider, socket} = provider(OpenAIProvider, "", fragments: fragments, eof: false)
      error = assert_raise ProviderError, fn -> stream(provider, request()) end
      assert %Mint.HTTPError{reason: reason} = error.cause
      assert if(is_tuple(reason), do: elem(reason, 0), else: reason) == expected
      assert FixtureTransport.snapshot(socket).closed
    end
  end
end
