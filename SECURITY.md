# Security

## TinyAgent runs shell commands

The coding CLIs give the model a `bash` tool. Each implementation rejects a
short list of obviously destructive commands before running them. That list is a
guardrail against accidents. It is not a sandbox, and a model can get around it
through aliases, interpreters, expansion, or downloaded programs.

Run the CLIs only where model-written commands are acceptable. For anything you
do not fully trust, use a disposable container or virtual machine.

The `Agent` core has no tools enabled by default. A tool you register runs with
your process's permissions.

## The HTTP server

`serve` binds to `127.0.0.1` by default. Binding to another address requires an
explicit opt-in and a bearer token (`TINYAGENT_TOKEN`). The server does not
terminate TLS; put it behind a reverse proxy you trust before exposing it.

## Reporting a vulnerability

Please report security problems privately through GitHub's "Report a
vulnerability" button on the repository's Security tab. Do not open a public
issue for something exploitable.

Include the language implementation, the version or commit, and the smallest
steps that reproduce the problem. Python and TypeScript are the supported
implementations and get fixes first. The other languages are experimental
reference code and are fixed on a best-effort basis.
