#!/usr/bin/env python3
"""Compile the Java SDK, package it, and run native checks against that JAR."""

import argparse
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parent
NS = {"m": "http://maven.apache.org/POM/4.0.0"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline", action="store_true", help="Exclude provider and HTTP server socket checks"
    )
    args = parser.parse_args()
    commands = {name: shutil.which(name) for name in ("java", "javac", "jar")}
    if not all(commands.values()):
        parser.error("Java 21 or later is required, including javac and jar")
    pom = ET.parse(ROOT / "pom.xml").getroot()
    repository = Path(
        os.environ.get("MAVEN_REPO_LOCAL", Path.home() / ".m2/repository")
    )
    dependencies = []
    for dependency in pom.findall("m:dependencies/m:dependency", NS):
        group = dependency.findtext("m:groupId", namespaces=NS)
        artifact = dependency.findtext("m:artifactId", namespaces=NS)
        version = dependency.findtext("m:version", namespaces=NS)
        if not group or not artifact or not version:
            parser.error("Each dependency needs explicit Maven coordinates")
        path = repository / group.replace(".", "/") / artifact / version
        path /= f"{artifact}-{version}.jar"
        if not path.is_file():
            parser.error(
                f"Missing declared dependency: {path}. Resolve pom.xml with Maven first."
            )
        dependencies.append(str(path))
    library = ROOT / "target/dependency"
    if library.exists():
        shutil.rmtree(library)
    library.mkdir(parents=True, exist_ok=True)
    for dependency in dependencies:
        shutil.copyfile(dependency, library / Path(dependency).name)
    classpath = os.pathsep.join(str(library / Path(path).name) for path in dependencies)
    classes = ROOT / "target/classes"
    tests = ROOT / "target/test-classes"
    for directory in (classes, tests):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
    artifact = pom.findtext("m:artifactId", namespaces=NS)
    version = pom.findtext("m:version", namespaces=NS)
    package = ROOT / "target" / f"{artifact}-{version}.jar"
    environment = os.environ.copy()
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        environment.pop(name, None)

    def run(command: list[str]) -> None:
        subprocess.run(command, cwd=ROOT, env=environment, check=True, timeout=120)

    try:
        run(
            [
                commands["javac"],
                "--release",
                "21",
                "-Xlint:all",
                "-Werror",
                "-cp",
                classpath,
                "-d",
                str(classes),
                *map(str, sorted((ROOT / "src/main/java").rglob("*.java"))),
            ]
        )
        metadata = classes / "META-INF"
        metadata.mkdir()
        for name in ("README.md", "LICENSE"):
            shutil.copyfile(ROOT / name, metadata / name)
        run(
            [
                commands["jar"],
                "--create",
                "--file",
                str(package),
                "-C",
                str(classes),
                ".",
            ]
        )
        packaged_classpath = os.pathsep.join([str(package), classpath])
        run(
            [
                commands["javac"],
                "--release",
                "21",
                "-Xlint:all",
                "-Werror",
                "-cp",
                packaged_classpath,
                "-d",
                str(tests),
                str(ROOT / "TinyAgent.java"),
                *map(str, sorted((ROOT / "src/test/cli").glob("*.java"))),
                *map(str, sorted((ROOT / "src/test/java").rglob("*.java"))),
            ]
        )
        run(
            [
                commands["java"],
                "-cp",
                packaged_classpath,
                "TinyAgent.java",
                "--self-test",
            ]
        )
        run(
            [
                commands["java"],
                "-cp",
                os.pathsep.join([str(tests), packaged_classpath]),
                "CliChecks",
            ]
        )
        run(
            [
                commands["java"],
                "-cp",
                os.pathsep.join([str(tests), packaged_classpath]),
                "dev.tinyagent.ProviderChecks",
                *(["--offline"] if args.offline else []),
            ]
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"Java checks failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
