"""How to launch each language's CLI.

Adding a language is one entry: the command that starts its CLI. The harness
appends `--mode rpc`, `serve` or `-p ...` and any flags itself, and passes all
configuration through the TINYAGENT_* environment variables. A language with
`enabled=False` (or no entry) is reported as "not implemented", never as
passing.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

PACKAGES = Path(__file__).resolve().parents[2] / "tinyagents"


@dataclass
class Target:
    name: str
    command: list[str]                    # the CLI itself; {dir} is the package directory
    enabled: bool = True
    build: list[list[str]] = field(default_factory=list)   # run once in the package directory
    in_package: bool = False              # True when the command must run from the package directory
    env: dict[str, str] = field(default_factory=dict)

    @property
    def directory(self) -> Path:
        return PACKAGES / self.name

    @property
    def cwd(self) -> str | None:
        """None: run in the case's temp directory, so nothing is ever written into the repository."""
        return str(self.directory) if self.in_package else None

    def argv(self, mode: str) -> list[str]:
        base = [part.replace("{dir}", str(self.directory)) for part in self.command]
        return base + {"rpc": ["--mode", "rpc"], "serve": ["serve"], "oneshot": [], "interactive": []}[mode]

    def missing(self) -> str | None:
        """The reason this target cannot run here, or None."""
        program = self.command[0]
        if "{dir}" in program:
            return None if (self.build or Path(program.replace("{dir}", str(self.directory))).exists()) \
                else f"{program} is not built"
        return None if shutil.which(program) else f"missing runtime: {program}"


TARGETS = {target.name: target for target in [
    Target("python", [sys.executable, "{dir}/tiny_agent.py"]),
    Target("typescript", ["node", "{dir}/tiny_agent.ts"]),
    # Not yet ported to protocol version 2. Set enabled=True once the port lands.
    Target("go", ["{dir}/bin/tinyagent"], enabled=False,
           build=[["go", "build", "-o", "bin/tinyagent", "./cmd/tinyagent"]]),
    Target("rust", ["{dir}/target/release/tinyagent"], enabled=False,
           build=[["cargo", "build", "--locked", "--release"]]),
    Target("zig", ["{dir}/zig-out/bin/tinyagent"], enabled=False, build=[["zig", "build"]]),
    Target("csharp", ["dotnet", "run", "--no-build", "--project", "{dir}/cli/TinyAgent.Cli.csproj", "--"],
           enabled=False, build=[["dotnet", "build", "cli/TinyAgent.Cli.csproj"]]),
    Target("java", ["java", "-cp", "{dir}/target/tiny-agent-0.1.0.jar:{dir}/target/dependency/*",
                    "{dir}/TinyAgent.java"], enabled=False, build=[["mvn", "-q", "package"]]),
    Target("php", ["php", "{dir}/tiny_agent.php"], enabled=False),
    Target("elixir", ["elixir", "{dir}/tiny_agent.exs"], enabled=False, in_package=True),
]}
