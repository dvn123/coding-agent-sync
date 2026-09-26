// @bun
// src/peel.ts
var ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*\+?=/;
var REDIRECT = /^(?:\d+|&)?(?:>>|>&|>\||<>|<&|>|<)(.*)$/s;
var WRAPPERS = {
  builtin: {},
  command: { lookups: ["-v", "-V"] },
  doas: { values: ["-a", "-C", "-u"] },
  env: {
    values: ["-a", "-C", "-P", "-S", "-u", "--argv0", "--chdir", "--split-string", "--unset"],
    assignments: true
  },
  exec: { values: ["-a"] },
  nice: { values: ["-n", "--adjustment"] },
  nocorrect: {},
  noglob: {},
  nohup: {},
  stdbuf: { values: ["-e", "-i", "-o", "--error", "--input", "--output"] },
  sudo: {
    assignments: true,
    values: [
      "-C",
      "-D",
      "-g",
      "-h",
      "-p",
      "-R",
      "-r",
      "-T",
      "-t",
      "-U",
      "-u",
      "--chdir",
      "--chroot",
      "--close-from",
      "--command-timeout",
      "--group",
      "--host",
      "--other-user",
      "--prompt",
      "--role",
      "--type",
      "--user"
    ]
  },
  time: { values: ["-f", "-o", "--format", "--output"] },
  timeout: {
    values: ["-k", "--kill-after", "-s", "--signal"],
    positionals: 1
  },
  xargs: {
    values: [
      "-a",
      "-d",
      "-E",
      "-I",
      "-J",
      "-L",
      "-n",
      "-P",
      "-R",
      "-S",
      "-s",
      "--arg-file",
      "--delimiter",
      "--max-args",
      "--max-chars",
      "--max-lines",
      "--max-procs",
      "--process-slot-var"
    ]
  }
};
function words(text) {
  const found = [];
  let index = 0;
  while (index < text.length) {
    while (index < text.length && /\s/.test(text[index]))
      index++;
    if (index >= text.length)
      break;
    const start = index;
    let value = "";
    while (index < text.length && !/\s/.test(text[index])) {
      const char = text[index];
      if (char === "'") {
        const close = text.indexOf("'", index + 1);
        const stop = close === -1 ? text.length : close;
        value += text.slice(index + 1, stop);
        index = stop + 1;
      } else if (char === '"') {
        index++;
        while (index < text.length && text[index] !== '"') {
          if (text[index] === "\\" && index + 1 < text.length)
            index++;
          value += text[index];
          index++;
        }
        index++;
      } else if (char === "\\" && index + 1 < text.length) {
        value += text[index + 1];
        index += 2;
      } else {
        value += char;
        index++;
      }
    }
    found.push({ value, start, end: Math.min(index, text.length) });
  }
  return found;
}
function basename(program) {
  return program.slice(program.lastIndexOf("/") + 1);
}
function wrapped(list, wrapper) {
  let index = 1;
  let positionals = wrapper.positionals ?? 0;
  while (index < list.length) {
    const value = list[index].value;
    if (value === "--")
      return index + 1 < list.length ? index + 1 : undefined;
    if (wrapper.lookups?.includes(value))
      return;
    if (value.startsWith("-") && value !== "-") {
      index += wrapper.values?.includes(value) ? 2 : 1;
    } else if (wrapper.assignments && ASSIGNMENT.test(value)) {
      index++;
    } else if (positionals > 0) {
      positionals--;
      index++;
    } else {
      return index;
    }
  }
  return;
}
function peelOnce(text) {
  const list = words(text);
  const head = list[0];
  if (!head)
    return [];
  const raw = text.slice(head.start, head.end);
  const redirect = REDIRECT.exec(raw);
  if (redirect) {
    const next = list[redirect[1] ? 1 : 2];
    return next ? [text.slice(next.start)] : [];
  }
  if (ASSIGNMENT.test(head.value))
    return list[1] ? [text.slice(list[1].start)] : [];
  const found = [];
  const program = basename(head.value);
  if (program && program !== raw)
    found.push(program + text.slice(head.end));
  const wrapper = WRAPPERS[program];
  if (wrapper) {
    const index = wrapped(list, wrapper);
    if (index !== undefined)
      found.push(text.slice(list[index].start));
    if (program === "env") {
      const split = list.findIndex((word) => word.value === "-S" || word.value === "--split-string");
      const payload = split > 0 ? list[split + 1] : undefined;
      if (payload)
        found.push(payload.value + text.slice(payload.end));
    }
  }
  return found.map((spelling) => spelling.trim()).filter(Boolean);
}
function spellings(command) {
  const original = command.trim();
  const seen = new Set([original]);
  const pending = [original];
  while (pending.length > 0) {
    for (const spelling of peelOnce(pending.pop())) {
      if (!seen.has(spelling)) {
        seen.add(spelling);
        pending.push(spelling);
      }
    }
  }
  seen.delete(original);
  return [...seen];
}

// src/rules.ts
var compiled = new Map;
function match(input, pattern) {
  let regex = compiled.get(pattern);
  if (!regex) {
    let escaped = pattern.replaceAll("\\", "/").replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".");
    if (escaped.endsWith(" .*"))
      escaped = escaped.slice(0, -3) + "( .*)?";
    regex = new RegExp("^" + escaped + "$", process.platform === "win32" ? "si" : "s");
    compiled.set(pattern, regex);
  }
  return regex.test(input.replaceAll("\\", "/"));
}
function evaluate(rules, action, resource) {
  return rules.findLast((rule) => match(action, rule.action) && match(resource, rule.resource))?.effect ?? "ask";
}

// src/review.ts
async function review(event, rules) {
  if (event.action !== "shell" || event.effect === "deny")
    return;
  const peeled = event.resources.flatMap(spellings);
  if (peeled.length === 0)
    return;
  let loaded;
  try {
    loaded = await rules();
  } catch (cause) {
    event.effect = "deny";
    event.message = `opencode-unwrap could not load the shell rules: ${cause instanceof Error ? cause.message : cause}`;
    return;
  }
  const denied = peeled.find((spelling) => evaluate(loaded, "shell", spelling) === "deny");
  if (denied) {
    event.effect = "deny";
    event.message = `\`${denied}\` is denied by the shell rules`;
  }
}

// src/index.ts
var plugin = {
  id: "opencode-unwrap",
  async setup(ctx) {
    await ctx.permission.hook("evaluate", (event) => review(event, async () => {
      const session = await ctx.session.get({ sessionID: event.sessionID });
      const agentID = event.agent ?? session.agent;
      if (!agentID)
        throw new Error("the session names no agent");
      const agent = await ctx.agent.get({ agentID });
      return [...agent.data.permissions, ...session.permissions ?? []];
    }));
  }
};
var src_default = plugin;
export {
  src_default as default
};
