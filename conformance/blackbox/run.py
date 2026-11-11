#!/usr/bin/env python3
"""Black-box conformance and fault-injection runner for TinyAgent CLIs.

    python conformance/blackbox/run.py [languages...] [--only PATTERN]
        [--sweeps crash,provider,cancel,client,walk|none] [--seed N] [--quick]

Drives each language's CLI from outside (RPC over stdio, the HTTP server, and
one-shot mode) against a scripted fake model server. See README.md.
"""

from __future__ import annotations

import argparse
import fnmatch
import importlib
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness                                        # noqa: E402
from harness import Case, Result, Spec, run_case      # noqa: E402
from targets import TARGETS                           # noqa: E402

SWEEPS = ("crash", "provider", "cancel", "client", "walk")
SCENARIO_MODULES = ("core", "rpc", "http", "durability", "cli")


def collect(target, options) -> list[Spec]:
    """Every case selected by the command line, in a stable order."""
    specs: list[Spec] = []
    if not options.no_scenarios:
        for module in SCENARIO_MODULES:
            importlib.import_module(f"scenarios.{module}")
        specs += [Spec(s.name, "scenarios", s.function, s.wire or "openai", s.variant, 0.0, s.refs, s.quick, s.module)
                  for s in harness.SCENARIOS]
    for sweep in options.sweeps:
        specs += importlib.import_module(f"sweeps.{sweep}").specs(target, options)
    if options.only:
        return [spec for spec in specs if any(fnmatch.fnmatchcase(spec.name, pattern) or pattern in spec.name
                                              for pattern in options.only)]
    return [spec for spec in specs if spec.quick or not options.quick]


def run_language(target, options) -> list[Result]:
    for command in target.build:
        subprocess.run(command, cwd=target.directory, check=True)
    specs = collect(target, options)
    if options.list:
        print("\n".join(f"{spec.group:10} {spec.name}" for spec in specs))
        return []

    def one(spec: Spec) -> Result:
        return run_case(spec.name, spec.function, lambda: Case(target, spec.wire, spec.variant, spec.think), spec.group)

    with ThreadPoolExecutor(options.jobs) as pool:
        return list(pool.map(one, specs))


def reproduce(language: str, result: Result, options) -> str:
    return (f"python conformance/blackbox/run.py {language} --only '{result.name}' --seed {options.seed} --jobs 1"
            + ("" if result.group == "scenarios" else f" --sweeps {result.group} --no-scenarios"))


def report(language: str, results: list[Result], options, seconds: float) -> bool:
    print(f"\n== {language}: {len(results)} cases in {seconds:.1f} s (seed {options.seed}"
          f"{', quick' if options.quick else ''})")
    scenarios = [result for result in results if result.group == "scenarios"]
    if scenarios and (options.verbose or len(scenarios) <= 12):
        width = max(len(result.name) for result in scenarios)
        for result in scenarios:
            print(f"  {result.name:{width}}  {result.status.upper():4}  {result.seconds:5.1f} s")
    print(f"  {'group':10} {'pass':>5} {'fail':>5} {'skip':>5}")
    for group in ("scenarios", *SWEEPS):
        mine = [result for result in results if result.group == group]
        if mine:
            counts = [sum(result.status == status for result in mine) for status in ("pass", "fail", "skip")]
            print(f"  {group:10} {counts[0]:5} {counts[1]:5} {counts[2]:5}")
    failed = [result for result in results if result.status == "fail"]
    for result in failed:
        print(f"\n  FAIL {result.name}\n    {result.detail.strip()}")
        if result.notes:
            print(f"    observed: {result.notes}")
        print(f"    reproduce: {reproduce(language, result, options)}")
    for result in results:
        if result.status == "skip" and options.verbose:
            print(f"  skip {result.name}: {result.detail}")
    return not failed


def divergences(by_language: dict[str, list[Result]]) -> None:
    """Cases where languages ended differently, or took different allowed paths."""
    languages = list(by_language)
    if len(languages) < 2:
        return
    tables = {language: {result.name: result for result in results} for language, results in by_language.items()}
    lines = []
    for name in tables[languages[0]]:
        seen = {language: tables[language].get(name) for language in languages}
        if any(result is None for result in seen.values()):
            continue
        outcomes = {language: (result.status, tuple(sorted((k, v) for k, v in result.notes.items()
                                                            if not k.startswith("~"))))
                    for language, result in seen.items() if result.status != "skip"}
        if len(set(outcomes.values())) > 1:
            lines.append(f"  {name}\n" + "\n".join(
                f"    {language:11} {status.upper()} {dict(notes) or ''}" for language, (status, notes) in outcomes.items()))
    print(f"\n== divergences between {', '.join(languages)}: {len(lines)}")
    print("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("languages", nargs="*", help="default: every language in targets.py")
    parser.add_argument("--only", action="append", metavar="PATTERN", help="case name, substring or glob (repeatable)")
    parser.add_argument("--sweeps", default=",".join(SWEEPS), help="comma-separated, or 'none'")
    parser.add_argument("--no-scenarios", action="store_true", help="run only the sweeps")
    parser.add_argument("--seed", type=int, default=1, help="seed for random kills and the random walk")
    parser.add_argument("--quick", action="store_true", help="the fast subset, for CI")
    parser.add_argument("--jobs", type=int, default=4, help="cases run in parallel (each is fully isolated)")
    parser.add_argument("--list", action="store_true", help="print the selected case names and exit")
    parser.add_argument("--json", metavar="FILE", help="also write every result to FILE")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every scenario and every skip")
    options = parser.parse_args()
    options.sweeps = [] if options.sweeps == "none" else options.sweeps.split(",")
    unknown = [name for name in options.languages if name not in TARGETS] + \
              [name for name in options.sweeps if name not in SWEEPS]
    if unknown:
        parser.error(f"unknown language or sweep: {', '.join(unknown)}")
    ok = True
    by_language: dict[str, list[Result]] = {}
    for name in options.languages or list(TARGETS):
        target = TARGETS[name]
        reason = "not implemented" if not target.enabled else target.missing()
        if reason:
            print(f"\n== {name}: NOT RUN ({reason})")
            ok = ok and name not in options.languages
            continue
        began = time.monotonic()
        by_language[name] = run_language(target, options)
        if not options.list:
            ok = report(name, by_language[name], options, time.monotonic() - began) and ok
    if not options.list:
        divergences(by_language)
        print(f"\n{'PASSED' if ok else 'FAILED'} at {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    if options.json:
        Path(options.json).write_text(json.dumps({name: [vars(result) for result in results]
                                                  for name, results in by_language.items()}, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
