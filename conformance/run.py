#!/usr/bin/env python3
"""Run native checks without treating an unavailable SDK as a passing SDK."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIMES = {
    "python": sys.executable,
    "typescript": "node",
    "go": "go",
    "rust": "cargo",
    "zig": "zig",
    "java": "java",
    "elixir": "elixir",
    "csharp": "dotnet",
    "php": "php",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "languages", nargs="*", help="Languages to check; defaults to all nine"
    )
    args = parser.parse_args()
    selected = args.languages or list(RUNTIMES)
    unknown = set(selected) - RUNTIMES.keys()
    if unknown:
        parser.error(f"Unknown languages: {', '.join(sorted(unknown))}")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "TINYAGENT_PROVIDER",
            "TINYAGENT_MODEL",
            "TINYAGENT_HOST",
            "TINYAGENT_PORT",
            "TINYAGENT_TOKEN",
            "TINYAGENT_ALLOW_REMOTE",
        }
    }
    results = {}
    for language in dict.fromkeys(selected):
        runtime = shutil.which(RUNTIMES[language])
        if runtime is None:
            results[language] = f"INCOMPLETE: missing {RUNTIMES[language]}"
            continue
        directory = ROOT / "tinyagents" / language
        if language in {"go", "rust"}:
            shared = ROOT / "conformance"
            bundled = directory / "conformance"
            fixture_paths = {
                path.relative_to(root)
                for root in (shared, bundled)
                for path in root.rglob("*")
                if path.suffix in {".json", ".sse"}
            }
            stale = [
                str(path)
                for path in sorted(fixture_paths)
                if not (shared / path).is_file()
                or not (bundled / path).is_file()
                or (shared / path).read_bytes() != (bundled / path).read_bytes()
            ]
            protocol = directory / "PROTOCOL.md"
            if (
                not protocol.is_file()
                or protocol.read_bytes() != (ROOT / "PROTOCOL.md").read_bytes()
            ):
                stale.append("PROTOCOL.md")
            if not fixture_paths or stale:
                results[language] = (
                    "FAILED: packaged fixtures and protocol must match workspace sources: "
                    + (", ".join(stale) or "no fixtures found")
                )
                continue
        if language == "python":
            commands = [
                [runtime, "-m", "unittest", "discover", "-p", "test_*.py"],
                [runtime, "tiny_agent.py", "--self-test"],
            ]
        elif language == "typescript":
            compiler = directory / "node_modules/typescript/bin/tsc"
            if not compiler.is_file():
                results[language] = (
                    "INCOMPLETE: run npm ci --ignore-scripts in tinyagents/typescript"
                )
                continue
            commands = [
                [runtime, str(compiler), "--noEmit"],
                [runtime, str(compiler), "-p", "tsconfig.test.json"],
                [
                    runtime,
                    "--test",
                    *[path.name for path in sorted(directory.glob("*.test.ts"))],
                ],
                [runtime, "tiny_agent.ts", "--self-test"],
            ]
        elif language == "go":
            commands = [
                [runtime, "test", "-race", "-timeout", "90s", "./..."],
                [runtime, "vet", "./..."],
                [runtime, "run", "./cmd/tinyagent", "--self-test"],
            ]
        elif language == "rust":
            commands = [
                [runtime, "test", "--locked", "--no-fail-fast"],
                [
                    runtime,
                    "clippy",
                    "--locked",
                    "--all-targets",
                    "--",
                    "-D",
                    "warnings",
                ],
                [runtime, "run", "--quiet", "--locked", "--example", "offline"],
                [runtime, "run", "--quiet", "--locked", "--", "--self-test"],
            ]
        elif language == "zig":
            cargo = shutil.which("cargo")
            if cargo is None:
                results[language] = "INCOMPLETE: missing Cargo for the native schema library"
                continue
            fixtures = ROOT / "conformance/providers"
            bundled = directory / "test/fixtures"
            names = {
                path.name
                for root in (fixtures, bundled)
                for path in root.glob("*.sse")
            }
            if not names or any(
                not (fixtures / name).is_file()
                or not (bundled / name).is_file()
                or (fixtures / name).read_bytes() != (bundled / name).read_bytes()
                for name in names
            ):
                results[language] = (
                    "FAILED: packaged Zig SSE fixtures must match shared sources"
                )
                continue
            if any(
                not (bundled / name).is_file()
                or (bundled / name).read_bytes() != (ROOT / "conformance" / name).read_bytes()
                for name in ("rpc_v1.json", "tool_arguments.json")
            ):
                results[language] = "FAILED: packaged Zig RPC/tool fixtures must match shared sources"
                continue
            commands = [
                [cargo, "fmt", "--manifest-path", "schema-native/Cargo.toml", "--check"],
                [cargo, "clippy", "--manifest-path", "schema-native/Cargo.toml", "--locked",
                 "--target-dir", ".zig-cache/schema-check", "--", "-D", "warnings"],
                [
                    runtime, "fmt", "--check", "build.zig", "build.zig.zon",
                    "tiny_agent.zig", "src", "test", "examples",
                ],
                [runtime, "build", "test", "example", "-j1", "--summary", "all"],
            ]
        elif language == "java":
            commands = [[sys.executable, "check.py"]]
        elif language == "csharp":
            commands = [[sys.executable, "check.py"]]
        elif language == "php":
            commands = [[runtime, "check.php"]]
        elif language == "elixir":
            (directory / "dist").mkdir(exist_ok=True)
            commands = [
                [runtime, "check.exs"],
                ["mix", "hex.build", "--output", "dist/tiny_agent_sdk-0.0.1.tar"],
                [
                    runtime,
                    "-pa",
                    "_build/direct/lib/tiny_agent_sdk/ebin",
                    "tiny_agent.exs",
                    "--self-test",
                ],
            ]
        else:
            results[language] = (
                "INCOMPLETE: native SDK conformance suite is not implemented"
            )
            continue
        passed = True
        for command in commands:
            print(f"\n[{language}] {' '.join(command)}", flush=True)
            try:
                result = subprocess.run(
                    command, cwd=directory, env=environment, timeout=180, check=False
                )
                passed = result.returncode == 0 and passed
            except (OSError, subprocess.TimeoutExpired) as error:
                print(f"Check could not finish: {error}", flush=True)
                passed = False
        results[language] = "CHECKS PASSED" if passed else "FAILED"
    print("\nNative check results", flush=True)
    for language, status in results.items():
        print(f"  {language}: {status}")
    print("The finish contract also requires packaging and a final acceptance audit.")
    return 0 if all(status == "CHECKS PASSED" for status in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
