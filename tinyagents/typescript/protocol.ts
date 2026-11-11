/** Limits are UTF-8 bytes. The JSONL limit excludes the trailing LF. */
export const PROTOCOL_VERSION = 2;
export const MAX_JSONL_LINE_BYTES = 1_048_576;
export const MAX_HTTP_BODY_BYTES = 1_048_576;
export const MAX_QUEUED_OUTPUT_BYTES = 4_194_304;
export const MAX_TOOL_OUTPUT_BYTES = 131_072;

/** The version of this package. */
export const VERSION = "0.2.0";
