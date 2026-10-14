---
name: codex-threads
description: Delegate a well-defined task or a discussion to OpenAI Codex CLI as a named, resumable worker thread (e.g. "luna" for fast execution, "sol" for harder work and discussion). Use when the main agent has already settled the plan and wants Codex to execute or to give a second opinion, or when the user asks to use Codex. Each thread keeps its own Codex thread, resumed by id; a running turn can be steered or interrupted.
---

# Codex Threads

Run Codex through one helper script. Never hand-roll `codex exec` or `codex app-server` commands: the script handles thread ids, locking, timeouts, server requests, steering and failure detection.

Script: `scripts/codex-thread.sh` under this skill's base directory, the absolute path shown when the skill loads. `$CLAUDE_PLUGIN_ROOT` is NOT set in Bash, so do not use it. If the base directory is unknown:
`T="$(ls -d ~/.claude/plugins/cache/cc-codex-threads/cc-codex-threads/*/skills/codex-threads/scripts/codex-thread.sh | sort -V | tail -1)"`

## Threads and profiles

A thread is a name you choose; a profile is the model/effort/sandbox it starts with. With no `--profile`, the profile with the same name as the thread is used. Built-in profiles (`scripts/profiles.json`, user overrides in `~/.config/cc-codex-threads/profiles.json`, merged per key):

| profile | model | effort | sandbox | use for |
|---|---|---|---|---|
| `luna` | `gpt-6-luna` | `max` | workspace-write | plan is settled and concrete; fast execution |
| `sol` | `gpt-6.1-sol` | `xhigh` | workspace-write | task is defined but needs real reasoning; design discussion, review, second opinion |

The user's CLAUDE.md may define which thread to use when; it takes precedence over this table.

## Preferred: dispatch through the relay agents

Use the Agent tool with `subagent_type: "cc-codex-threads:codex-luna"` or `"cc-codex-threads:codex-sol"` and `run_in_background: true`. The agent's prompt is forwarded to Codex unchanged, so write it as a complete, self-contained Codex prompt (see Rules 2). The user watches Codex's live progress (commands, edits, reasoning headings) in that agent's background task; the main session is notified only when the agent starts waiting and when the reply is ready.

- **Follow-up:** SendMessage to the same agent. It continues the same Codex thread.
- **Steering:** a SendMessage while Codex is still working is delivered into the running turn (Codex adjusts course without restarting). Only steer with information that changes the task; do not send chatter.
- **Stop:** SendMessage whose first line is `@cct interrupt`. The partial reply then arrives as usual, with exit 7.
- **Directive (optional first line):** `@cct thread=<name> [new] [read-only] [cd=<abs dir>]`, quoted like a shell word when needed (`cd="/a b"`, never evaluated), or JSON: `@cct {"thread":"x","cd":"/a b","new":true,"read_only":true}`. `read-only` on an existing writable thread tightens that run only. The relay remembers `thread` and `cd` for later messages. `@cct status` reports whether the thread is running.

Run the script directly only for `list`, `show`, `status`, `result`, `watch`, `reset`, or when the Agent tool is unavailable.

## Commands

```bash
T="<skill base directory>/scripts/codex-thread.sh"   # absolute path; always quote "$T"

"$T" send luna -- "<prompt>"                # resume thread 'luna', or start it from profile 'luna'
"$T" send sol --prompt-file /path/prompt.txt     # prompt from a file (safest for long text)
"$T" send sol --new -- "<prompt>"           # drop the old thread, start fresh
"$T" send review-x --profile sol --sandbox read-only --new -- "<prompt>"
"$T" send luna --progress -- "<prompt>"     # print live progress lines before the reply
"$T" steer luna -- "<extra instruction>"    # add input to the running turn
"$T" interrupt luna                         # stop the running turn
"$T" status luna                            # current/last run as JSON (state: running/done/lost)
"$T" result luna                            # reply of the last finished run
"$T" watch luna                             # follow live progress until the run ends
"$T" list                                   # threads registered for this project
"$T" show luna                              # one thread's record
"$T" reset luna                             # forget one thread (Codex history stays on disk)
"$T" reset --all                            # forget ALL threads of this project; only when explicitly intended
```

Threads are registered per project (git top-level, else the current directory). Run the script from the project directory or pass `--cd <dir>`.

## Rules

1. **Resume by default.** Follow-ups on the same line of work go to the same thread name. Use `--new` only when the topic changes completely or the thread went wrong. On resume the thread keeps its model, effort, sandbox and working directory; only `--sandbox read-only` is accepted, to tighten one run.
2. **Write self-contained prompts.** Codex sees no Claude context. On a new thread, give the goal, the relevant file paths, concrete steps, constraints, the acceptance criteria and the expected reply format. On a resumed thread, send only what is new.
3. **One turn per thread at a time.** A busy thread refuses a new `send` (exit 3): steer it, wait, or use another thread name. Never two writers on the same files.
4. **Long runs:** max/xhigh can take many minutes. Run in the background and do not set a Bash timeout shorter than the script's own (default 2400s for xhigh/max; `--timeout <sec>`).
5. **Verify.** Codex output is a colleague's work, not ground truth. Check the diff and run tests before reporting success. If Codex is wrong, say so, and if useful resume the thread with evidence, identifying yourself as Claude.
6. **Do not loop on failures.** On a non-zero exit, report the `[cct] ERROR` line to the user; retry at most once, and only for timeouts or transient network errors. Never resend a steer whose outcome is unknown.

## Output and exit codes

stdout is Codex's final reply, then `---` and a `[cct]` status line with the thread, mode (new/resume), thread id, model, effort, sandbox, elapsed time, this turn's token usage, context size, transient retries, run id and the run's log directory. Errors go to stderr as `[cct] ERROR: ...`.

| code | meaning | action |
|---|---|---|
| 0 | ok | verify the work |
| 2 | usage error (bad flag, unknown profile, empty prompt, loosening a thread) | fix the call |
| 3 | busy: thread already running, or a reset while threads run | steer/wait, or use another thread name |
| 4 | Codex failed, or a local state problem (corrupt registry, missing cwd, settings mismatch) | read the message and log; if the thread or its directory is gone, use `--new` |
| 5 | timeout (the turn was interrupted at the deadline) | split the task or raise `--timeout` |
| 6 | empty reply | inspect the log; resume with a clearer prompt |
| 7 | interrupted (by request or signal); a partial reply may be printed | resume with what to do next |
| 8 | steer/interrupt: no running turn | send a normal follow-up instead |

The engine is a private `codex app-server` per run. `CCT_ENGINE=exec` switches to the legacy `codex exec` engine (no steering; interrupt stops the process). Use it only if app-server breaks after a Codex update.
