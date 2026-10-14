# cc-codex-threads

A Claude Code plugin that runs [OpenAI Codex CLI](https://github.com/openai/codex) as **named, resumable worker threads**.

一个 Claude Code 插件：把 Codex CLI 作为**具名、可按 ID 续接的工作线程**来调用。例如 `luna` 线程负责快速执行，`sol` 线程负责较复杂的执行与方案讨论，两条线程各自保留上下文、互不串线。

## Why

- **Resume by session id, not "last".** Every thread name maps to its own Codex session id, so `luna` and `sol` never steal each other's context.
- **Any model / reasoning effort**, including `max`, via profiles.
- **Built on `codex exec`**, Codex's stable non-interactive CLI, with no app-server or daemon.
- **Hardened wrapper:** stdin is always closed (no silent hangs), per-effort timeouts, per-thread and per-project locks, atomic registry writes, transient reconnects are tolerated, and failed/empty turns produce clear exit codes.
- **No build step.** It is one Bash script plus a skill file.

## Requirements

`codex` (logged in), `jq`, `flock` (util-linux), GNU `timeout` (coreutils), `sha1sum` or `shasum`, and Bash 4+. Tested on Linux. On macOS, install `flock` and `coreutils` with Homebrew first.

## Tests

`tests/run.sh` runs an offline suite against a fake `codex`, so it makes no API calls.

## Install

```bash
claude plugin marketplace add luck7x/cc-codex-threads
claude plugin install cc-codex-threads@cc-codex-threads
```

## Usage

Claude uses it through the `codex-threads` skill. You can also run it directly:

```bash
T="$(ls -d ~/.claude/plugins/cache/cc-codex-threads/cc-codex-threads/*/skills/codex-threads/scripts/codex-thread.sh | sort -V | tail -1)"
"$T" send luna -- "Add a --verbose flag to cli.py; run pytest; reply with a summary"
"$T" send luna -- "Also update README"     # resumes the same luna session
"$T" send sol  -- "Review the approach in docs/plan.md and list risks"
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

Thread registry and JSONL logs are stored in `~/.local/state/cc-codex-threads/`, or `$CCT_STATE_DIR` if set. Registrations are scoped per project (git top-level, else the directory).

## Credits

Derived from [skills-directory/skill-codex](https://github.com/skills-directory/skill-codex) (MIT).
