#!/usr/bin/env node
/**
 * The command-line entry file. The library is sdk.ts, which is what the
 * package exports; it is re-exported here so this file can also be imported.
 * Only this file asks whether it is the program, and only it reads the
 * process's arguments and environment.
 */
import { realpathSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { runCli } from "./sdk.ts";

export * from "./sdk.ts";

/** True when this file is the program, including through an installed command's symlink. */
function isProgram(): boolean {
  try {
    return import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href;
  } catch {
    return false;
  }
}

if (isProgram()) process.exitCode = await runCli({ argv: process.argv.slice(2), env: process.env });
