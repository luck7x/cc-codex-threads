---
name: codex-threads
description: Delegate a well-defined task or a discussion to OpenAI Codex CLI as a named, resumable worker thread (e.g. "luna" for fast execution, "sol" for harder work and discussion). Use when the main agent has already settled the plan and wants Codex to execute or to give a second opinion, or when the user asks to use Codex. Each thread keeps its own Codex session, resumed by id, so several threads coexist without mixing context.
---

# Codex Threads

Run Codex through one helper script. Never hand-roll `codex exec` commands: the script handles session ids, stdin, timeouts, locking and failure detection.

Script (use the first path that exists):
- `${CLAUDE_PLUGIN_ROOT}/skills/codex-threads/scripts/codex-thread.sh`
- `scripts/codex-thread.sh` under this skill's base directory

## Threads and profiles

A thread is a name you choose; a profile is the model/effort/sandbox it starts with. With no `--profile`, the profile with the same name as the thread is used. Built-in profiles (`scripts/profiles.json`, user overrides in `~/.config/cc-codex-threads/profiles.json`, merged per key):

| profile | model | effort | sandbox | use for |
|---|---|---|---|---|
| `luna` | `gpt-6-luna` | `max` | workspace-write | plan is settled and concrete; fast execution |
| `sol` | `gpt-6.1-sol` | `xhigh` | workspace-write | task is defined but needs real reasoning; design discussion, review, second opinion |

The user's CLAUDE.md may define which thread to use when; it takes precedence over this table.

## Commands

```bash
T="${CLAUDE_PLUGIN_ROOT}/skills/codex-threads/scripts/codex-thread.sh"

"$T" send luna "<prompt>"                  # resume thread 'luna', or start it from profile 'luna'
"$T" send sol  <<'EOF'                      # long prompt via stdin (preferred for multi-line)
<prompt>
EOF
"$T" send sol --new "<prompt>"             # drop the old session, start fresh
"$T" send review-x --profile sol "<prompt>"  # extra thread with sol's settings
"$T" send luna --sandbox read-only --new "<prompt>"  # read-only run (overrides apply to new threads only)
"$T" list                                  # threads registered for this project
"$T" reset luna | "$T" reset --all         # forget registrations (Codex history stays on disk)
```

Threads are registered per project (git top-level, else the current directory). Run the script from the project directory or pass `--cd <dir>`.

## Rules

1. **Resume by default.** Follow-ups on the same line of work go to the same thread name. Use `--new` only when the topic changes completely or the session went wrong. On resume the thread keeps its original model, effort, sandbox and working directory; override flags are ignored.
2. **Write self-contained prompts.** Codex sees no Claude context. On a new thread, give the goal, the relevant file paths, concrete steps, constraints, the acceptance criteria and the expected reply format. On a resumed thread, send only what is new.
3. **One task per thread at a time.** A busy thread exits with code 3. For parallel work, use different thread names; never two writers on the same files.
4. **Long runs:** max/xhigh can take many minutes. Run the Bash call with `run_in_background: true` and do not set a shorter Bash timeout than the script's own (default 2400s for xhigh/max; `--timeout <sec>` to change).
5. **Verify.** Codex output is a colleague's work, not ground truth. Check the diff (`git diff`) and run tests before reporting success. If Codex is wrong, say so, and if useful resume the thread with evidence, identifying yourself as Claude.
6. **Do not loop on failures.** On a non-zero exit, report the error line to the user; retry at most once, and only for timeouts or transient network errors.

## Output and exit codes

stdout is Codex's final reply, then a line starting with `[cct]` that has the thread, mode (new/resume), session id, model, effort, elapsed time, token usage, transient retries and the full JSONL log path. Errors go to stderr as `[cct] ERROR: ...`.

| code | meaning | action |
|---|---|---|
| 0 | ok | verify the work |
| 2 | usage error (bad flag, unknown profile, empty prompt) | fix the call |
| 3 | thread busy | wait for it, or use another thread name |
| 4 | Codex failed | read the message and log; if a resume failed because the session is gone, use `--new` |
| 5 | timeout | split the task or raise `--timeout` |
| 6 | empty reply | inspect the log; resume with a clearer prompt |
