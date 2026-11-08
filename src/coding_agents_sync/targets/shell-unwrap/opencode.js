// @bun
// src/opencode.ts
var BINARY = `${import.meta.dir}/../shell-unwrap${process.platform === "win32" ? ".exe" : ""}`;
async function ask(binary, operation, request) {
  const child = Bun.spawn([binary, "opencode", operation], {
    stdin: new Blob([JSON.stringify(request)]),
    stdout: "pipe",
    stderr: "pipe"
  });
  const [output, code] = await Promise.all([new Response(child.stdout).text(), child.exited]);
  if (code !== 0)
    throw new Error(`shell-unwrap exited with ${code}`);
  return JSON.parse(output);
}
async function readReasons(path) {
  try {
    const reasons = (await Bun.file(path).json())?.reasons;
    if (!reasons || typeof reasons !== "object")
      return {};
    return Object.fromEntries(Object.entries(reasons).filter(([, reason]) => typeof reason === "string"));
  } catch {
    return {};
  }
}
async function review(event, rules, reasons = {}, binary = BINARY) {
  if (event.action !== "shell" || event.effect === "deny")
    return;
  const request = { action: event.action, resources: event.resources, effect: event.effect, reasons };
  let verdict;
  try {
    verdict = await ask(binary, "review", request);
    if (verdict.needsRules) {
      let loaded;
      try {
        loaded = await rules();
      } catch (cause) {
        event.effect = "deny";
        event.message = `shell-unwrap could not load the shell rules: ${cause instanceof Error ? cause.message : cause}`;
        return;
      }
      verdict = await ask(binary, "review", { ...request, rules: loaded });
    }
  } catch (cause) {
    event.effect = "deny";
    event.message = `shell-unwrap could not run: ${cause instanceof Error ? cause.message : cause}`;
    return;
  }
  if (verdict.effect)
    event.effect = verdict.effect;
  if (verdict.message)
    event.message = verdict.message;
}
async function explain(resources, rules, reasons, binary = BINARY) {
  const { reason } = await ask(binary, "explain", { resources, rules, reasons });
  return reason || undefined;
}
var plugin = {
  id: "shell-unwrap",
  async setup(ctx) {
    const reasons = await readReasons(`${import.meta.dir}/../shell-unwrap.json`);
    async function rules(sessionID, agent) {
      const session = await ctx.session.get({ sessionID });
      const agentID = agent ?? session.agent;
      if (!agentID)
        throw new Error("the session names no agent");
      const loaded = await ctx.agent.get({ agentID });
      return [...loaded.data.permissions, ...session.permissions ?? []];
    }
    await ctx.permission.hook("evaluate", (event) => review(event, () => rules(event.sessionID, event.agent), reasons));
    if (Object.keys(reasons).length === 0)
      return;
    await ctx.tool.hook("execute.after", async (event) => {
      if (event.tool !== "shell" || event.status !== "error")
        return;
      const blocked = event.error.error;
      if (blocked?._tag !== "Permission.BlockedError" || !blocked.rules || !blocked.resources)
        return;
      const reason = await explain(blocked.resources, blocked.rules, reasons).catch(() => {
        return;
      });
      if (reason)
        blocked.reason = reason;
    });
  }
};
var opencode_default = plugin;
export {
  opencode_default as default,
  explain,
  readReasons,
  review
};
