# cc-codex-threads

A Claude Code plugin that runs [OpenAI Codex CLI](https://github.com/openai/codex) as **named, resumable worker threads**.

一个 Claude Code 插件：把 Codex CLI 作为**具名、可按 ID 续接的工作线程**来调用。例如 `luna` 线程负责快速执行，`sol` 线程负责较复杂的执行与方案讨论，两条线程各自保留上下文、互不串线。运行中的任务可以实时查看进度，也可以中途引导（steer）或打断。

## Why

- **Resume by thread id, not "last".** Every thread name maps to its own Codex thread id, so `luna` and `sol` never steal each other's context.
- **Steer and interrupt a running turn**, like the interactive Codex CLI: add instructions mid-turn, or stop it and keep the partial reply.
- **Live progress without noise.** The relay agent streams Codex's commands, edits and reasoning headings into its background task, and wakes the main session only when the reply is ready.
- **Any model / reasoning effort**, including `max`, via profiles.
- **Private `codex app-server` per run.** No shared daemon. Every approval, permission, user-input, elicitation or tool request from the server gets a fixed refusal, so a run never hangs waiting for an answer. The legacy `codex exec` engine remains available with `CCT_ENGINE=exec`.
- **Hardened:** per-thread and per-project locks, atomic state writes, settings verified before a turn starts, timeouts that interrupt and then kill, no orphaned processes, and clear exit codes.
- **No build step.** One Python 3 script (standard library only) plus a skill and two agent files.

## Requirements

`codex` (logged in; tested with codex-cli 0.162.1) and Python 3.8+. Tested on Linux. macOS should work, but process clean-up after a crash relies on the Linux parent-death signal there.

## Tests

`tests/run.sh` runs an offline suite (124 checks) against a fake `codex exec` and a fake `codex app-server`, so it makes no API calls. It needs `jq` and `git`.

## Install

```bash
claude plugin marketplace add luck7x/cc-codex-threads
claude plugin install cc-codex-threads@cc-codex-threads
```

## Usage

Claude delegates through two relay subagents: `codex-luna` and `codex-sol`. The relay runs on Haiku at low effort and only forwards text: it writes the message to a file (no shell quoting involved) and starts one background command. Open that background task to watch Codex live. The agent view shows the exact prompt and the verbatim reply.

- A message sent to the agent after the reply continues the same Codex thread.
- A message sent while Codex is still working is delivered into the running turn as steering input.
- A message whose first line is `@cct interrupt` stops the turn.

Example progress stream:

```
12:03:46 ● run 3f2a…: thread=sol resume gpt-6.1-sol/xhigh workspace-write engine=app-server
12:03:47 ● resumed 01a139a8-… (gpt-6.1-sol/xhigh, workspace-write)
12:03:47 ● turn started
12:03:55 💬 I'll review the rewrite read-only and check lock ordering…
12:03:56 $ nl -ba plugins/…/cct.py
12:04:30 💭 Reviewing registry validation
12:05:02 ↪ steer: also check the relay directive parser
12:06:10 ✎ update src/cli.py
12:07:41 ✓ done
```

You can also run the script directly:

```bash
T="$(ls -d ~/.claude/plugins/cache/cc-codex-threads/cc-codex-threads/*/skills/codex-threads/scripts/codex-thread.sh | sort -V | tail -1)"
"$T" send luna -- "Add a --verbose flag to cli.py; run pytest; reply with a summary"
"$T" send luna -- "Also update README"     # resumes the same luna session
"$T" send sol  -- "Review the approach in docs/plan.md and list risks"
"$T" steer luna -- "Also handle the empty-input case"   # while luna is running
"$T" interrupt luna                        # stop the running turn
"$T" watch luna                            # follow a running thread live
"$T" list
```

Run `codex-thread.sh help` to see all commands and exit codes.

## Profiles

Defaults are in `plugins/cc-codex-threads/skills/codex-threads/scripts/profiles.json`:

```json
{
  "luna": { "model": "gpt-6-luna",  "effort": "max",   "sandbox": "workspace-write" },
  "sol":  { "model": "gpt-6.1-sol", "effort": "xhigh", "sandbox": "workspace-write" }
}
```

To override or add profiles, use `~/.config/cc-codex-threads/profiles.json`. It is merged per key.

## State

The thread registry and per-run directories are stored in `~/.local/state/cc-codex-threads/`, or `$CCT_STATE_DIR` if set. Each run directory holds the prompt, the raw protocol log, the rendered progress and the reply. Runs older than 14 days are pruned. Registrations are scoped per project (git top-level, else the directory). Control sockets live in a private per-user directory under the system temp directory.

Threads created by v1 (`codex exec`) resume unchanged under v2.

## Credits

Derived from [skills-directory/skill-codex](https://github.com/skills-directory/skill-codex) (MIT).
