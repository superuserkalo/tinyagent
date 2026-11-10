#!/usr/bin/env python3
"""Build and package the C# SDK, then check its native runtime and original CLI."""

import argparse
import os
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline", action="store_true", help="Exclude the four real TCP test groups"
    )
    args = parser.parse_args()
    dotnet = shutil.which("dotnet")
    if dotnet is None:
        parser.error("The .NET 8 SDK or later is required")
    config = ROOT / "obj/check/NuGet.Config"
    config.parent.mkdir(parents=True, exist_ok=True)
    # A local feed supports reviewed, cached dependencies in disconnected environments.
    feed = escape(os.environ.get("TINYAGENT_NUGET_SOURCE", "https://api.nuget.org/v3/index.json"), {'"': "&quot;"})
    config.write_text(
        f'<configuration><packageSources><clear /><add key="tinyagent" value="{feed}" /></packageSources></configuration>\n',
        encoding="utf-8",
    )
    projects = [
        "tests/TinyAgent.Checks/TinyAgent.Checks.csproj",
        "cli/TinyAgent.Cli.csproj",
    ]
    environment = os.environ.copy()
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        environment.pop(name, None)

    def run(arguments: list[str]) -> int:
        return subprocess.run(
            [dotnet, *arguments], cwd=ROOT, env=environment, check=False, timeout=120
        ).returncode

    try:
        for project in projects:
            if run(
                [
                    "restore",
                    project,
                    "--configfile",
                    str(config),
                    "--nologo",
                    "--disable-parallel",
                    "--locked-mode",
                    "-p:BuildInParallel=false",
                    "-m:1",
                    "-nr:false",
                ]
            ):
                return 1
            if run(
                [
                    "build",
                    project,
                    "--no-restore",
                    "--configuration",
                    "Release",
                    "--nologo",
                    "-p:UseSharedCompilation=false",
                    "-p:BuildInParallel=false",
                    "-m:1",
                    "-nr:false",
                ]
            ):
                return 1
        for package in ("TinyAgent", "TinyAgent.Http"):
            if run(
                [
                    "pack",
                    f"src/{package}/{package}.csproj",
                    "--no-build",
                    "--no-restore",
                    "--configuration",
                    "Release",
                    "--output",
                    "artifacts",
                    "--nologo",
                ]
            ):
                return 1
        sdk_checks = run(
            [
                "tests/TinyAgent.Checks/bin/Release/net8.0/TinyAgent.Checks.dll",
                *(["--offline"] if args.offline else []),
            ]
        )
        cli = run(["cli/bin/Release/net8.0/TinyAgent.Cli.dll", "--self-test"])
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"C# checks could not finish: {error}", file=sys.stderr)
        return 1
    return 0 if sdk_checks == 0 and cli == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
