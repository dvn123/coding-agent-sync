// src/pi.ts
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
var HOOK = join(dirname(fileURLToPath(import.meta.url)), "shell-unwrap.js");
var TIMEOUT_MS = 1e4;
var UNAVAILABLE = "shell-unwrap could not run, so its guards were not checked";
function runHook(command, hook = HOOK) {
  return new Promise((resolve, reject) => {
    const child = spawn("bun", [hook, "codex"], {
      cwd: dirname(hook),
      stdio: ["pipe", "pipe", "ignore"],
      timeout: TIMEOUT_MS
    });
    let output = "";
    child.stdout.on("data", (chunk) => output += chunk);
    child.on("error", reject);
    child.on("close", (code) => code === 0 ? resolve(output) : reject(new Error(`exit ${code}`)));
    child.stdin.end(JSON.stringify({ tool_name: "Bash", tool_input: { command } }));
  });
}
async function review(command, hook = HOOK) {
  try {
    const output = (await runHook(command, hook)).trim();
    if (!output)
      return;
    return JSON.parse(output).hookSpecificOutput?.permissionDecisionReason ?? UNAVAILABLE;
  } catch {
    return UNAVAILABLE;
  }
}
function shellUnwrap(pi) {
  pi.on("tool_call", async (event) => {
    const command = event.input.command;
    if (event.toolName !== "bash" || typeof command !== "string")
      return;
    const reason = await review(command);
    return reason ? { block: true, reason } : undefined;
  });
}
export {
  shellUnwrap as default,
  review
};
