#!/usr/bin/env bash
# cc-codex-threads: run OpenAI Codex CLI as named, resumable worker threads.
#
# Each named thread (e.g. "luna", "sol") maps to one Codex session id, stored
# per project. Sending to a known thread resumes that exact session by id, so
# several threads can coexist without stealing each other's context.
#
# Usage: codex-thread.sh help
set -uo pipefail

VERSION="1.0.0"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${CCT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/cc-codex-threads}"
USER_PROFILES="${CCT_PROFILES:-${XDG_CONFIG_HOME:-$HOME/.config}/cc-codex-threads/profiles.json}"
DEFAULT_PROFILES="$SCRIPT_DIR/profiles.json"

die() { local code="$1"; shift; echo "[cct] ERROR: $*" >&2; exit "$code"; }
warn() { echo "[cct] WARN: $*" >&2; }

usage() {
  cat <<'EOF'
codex-thread.sh — named, resumable Codex worker threads

COMMANDS
  send <thread> [options] [--] [prompt...]
      Send a prompt to <thread>. Resumes the thread's session if it exists in
      this project, otherwise starts a new one. Prompt comes from the trailing
      arguments, or from stdin when none are given (or when it is "-").
      Options:
        --profile <name>   settings preset for a NEW thread (default: <thread>)
        --model <id>       override model        (new thread only)
        --effort <level>   override effort       (new thread only)
        --sandbox <mode>   read-only | workspace-write | danger-full-access
                           (new thread only)
        --new              discard the registered session, start fresh
        --cd <dir>         project dir (default: current dir)
        --timeout <sec>    hard timeout (default depends on effort)
  list   [--cd <dir>]           threads registered for this project
  show   <thread> [--cd <dir>]  one thread's record
  reset  <thread>|--all [--cd <dir>]  forget thread(s) (Codex history kept)
  profiles                      show effective profiles
  help

EXIT CODES
  0 ok | 2 usage | 3 thread busy | 4 codex failed | 5 timeout | 6 empty reply
EOF
}

need() { command -v "$1" >/dev/null 2>&1 || die 2 "required command not found: $1"; }
need codex; need jq; need flock; need timeout

project_root() {
  local dir="$1" root
  root="$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null)" || root="$(cd "$dir" && pwd -P)"
  printf '%s' "$root"
}

registry_file() {
  local key; key="$(printf '%s' "$1" | sha1sum | cut -c1-16)"
  printf '%s/projects/%s.json' "$STATE_DIR" "$key"
}

load_registry() {
  local f="$1" root="$2"
  if [[ -s "$f" ]] && jq -e . "$f" >/dev/null 2>&1; then cat "$f"
  else jq -n --arg root "$root" '{root:$root, threads:{}}'; fi
}

save_registry() {
  local f="$1" json="$2" tmp
  mkdir -p "$(dirname "$f")"
  tmp="$(mktemp "$f.XXXXXX")" && printf '%s\n' "$json" > "$tmp" && mv -f "$tmp" "$f"
}

profiles_json() {
  local base='{}' user='{}'
  [[ -f "$DEFAULT_PROFILES" ]] && base="$(cat "$DEFAULT_PROFILES")"
  [[ -f "$USER_PROFILES" ]] && user="$(cat "$USER_PROFILES")"
  jq -n --argjson a "$base" --argjson b "$user" '$a * $b' 2>/dev/null \
    || die 2 "invalid profiles JSON ($DEFAULT_PROFILES or $USER_PROFILES)"
}

default_timeout() {
  case "$1" in
    none|minimal|low) echo 300 ;; medium) echo 600 ;; high) echo 1200 ;;
    xhigh|max) echo 2400 ;; *) echo 3600 ;;
  esac
}

valid_name() { [[ "$1" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || die 2 "invalid thread name: $1"; }

cmd_send() {
  local thread="" profile="" model="" effort="" sandbox="" force_new=0 dir="$PWD" tmo=""
  local -a words=()
  while (($#)); do
    case "$1" in
      --profile) profile="${2:-}"; shift 2 ;;
      --model)   model="${2:-}"; shift 2 ;;
      --effort)  effort="${2:-}"; shift 2 ;;
      --sandbox) sandbox="${2:-}"; shift 2 ;;
      --new)     force_new=1; shift ;;
      --cd)      dir="${2:-}"; shift 2 ;;
      --timeout) tmo="${2:-}"; shift 2 ;;
      --)        shift; words+=("$@"); break ;;
      -*)        [[ "$1" == "-" ]] && { words+=("$1"); shift; continue; }
                 die 2 "unknown option: $1" ;;
      *)         if [[ -z "$thread" ]]; then thread="$1"; else words+=("$1"); fi; shift ;;
    esac
  done
  [[ -n "$thread" ]] || die 2 "send: missing <thread>"
  valid_name "$thread"
  [[ -d "$dir" ]] || die 2 "--cd: not a directory: $dir"
  [[ -z "$tmo" || "$tmo" =~ ^[0-9]+$ ]] || die 2 "--timeout must be seconds"

  # Prompt: trailing args, else stdin.
  local prompt
  if ((${#words[@]})) && [[ "${words[*]}" != "-" ]]; then prompt="${words[*]}"
  else
    [[ -t 0 ]] && die 2 "send: no prompt given (pass it as arguments or on stdin)"
    prompt="$(cat)"
  fi
  [[ -n "${prompt//[[:space:]]/}" ]] || die 2 "send: prompt is empty"

  local root reg_file reg rec
  root="$(project_root "$dir")"
  reg_file="$(registry_file "$root")"

  mkdir -p "$STATE_DIR/locks" "$STATE_DIR/logs"
  local lock="$STATE_DIR/locks/$(basename "$reg_file" .json)-$thread.lock"
  exec 9>"$lock"
  flock -n 9 || die 3 "thread '$thread' is already running in this project; wait for it or use another thread name"

  reg="$(load_registry "$reg_file" "$root")"
  rec="$(jq -c --arg t "$thread" '.threads[$t] // empty' <<<"$reg")"

  local mode="new" tid="" cwd="$dir"
  if [[ -n "$rec" && $force_new -eq 0 ]]; then
    mode="resume"
    tid="$(jq -r '.thread_id' <<<"$rec")"
    model="$(jq -r '.model' <<<"$rec")"; effort="$(jq -r '.effort' <<<"$rec")"
    sandbox="$(jq -r '.sandbox' <<<"$rec")"; cwd="$(jq -r '.cwd' <<<"$rec")"
    [[ -d "$cwd" ]] || cwd="$dir"
  else
    local prof; profile="${profile:-$thread}"
    prof="$(profiles_json | jq -c --arg p "$profile" '.[$p] // empty')"
    if [[ -z "$prof" && ( -z "$model" || -z "$effort" ) ]]; then
      die 2 "no profile named '$profile'; pass --profile <name> or both --model and --effort (see: codex-thread.sh profiles)"
    fi
    [[ -n "$model"   ]] || model="$(jq -r '.model'   <<<"$prof")"
    [[ -n "$effort"  ]] || effort="$(jq -r '.effort'  <<<"$prof")"
    [[ -n "$sandbox" ]] || sandbox="$(jq -r '.sandbox // "workspace-write"' <<<"${prof:-null}")"
  fi
  case "$sandbox" in read-only|workspace-write|danger-full-access) ;; *) die 2 "invalid sandbox: $sandbox" ;; esac
  [[ -n "$tmo" ]] || tmo="$(default_timeout "$effort")"

  local stamp log
  stamp="$(date +%Y%m%dT%H%M%S)-$$"
  log="$STATE_DIR/logs/$thread-$stamp.jsonl"
  # Globals so the EXIT trap can still see them after this function returns.
  CCT_LAST="$(mktemp)"; CCT_PROMPT="$(mktemp)"
  trap 'rm -f "$CCT_LAST" "$CCT_PROMPT"' EXIT
  local last="$CCT_LAST" pfile="$CCT_PROMPT"
  printf '%s' "$prompt" > "$pfile"

  local -a common=(--json --skip-git-repo-check -m "$model"
    -c "model_reasoning_effort=\"$effort\"" -c 'approval_policy="never"' -o "$last")
  local -a cmd
  if [[ "$mode" == "resume" ]]; then
    cmd=(codex exec resume "$tid" "${common[@]}" -c "sandbox_mode=\"$sandbox\"" -)
  else
    cmd=(codex exec "${common[@]}" -s "$sandbox" -)
  fi

  local t0 rc; t0="$(date +%s)"
  ( cd "$cwd" && timeout -k 30 "$tmo" "${cmd[@]}" < "$pfile" > "$log" 2> "$log.stderr" )
  rc=$?
  local elapsed=$(( $(date +%s) - t0 ))

  local new_tid; new_tid="$(jq -r 'select(.type=="thread.started") | .thread_id' "$log" 2>/dev/null | head -1)"
  [[ "$mode" == "new" ]] && tid="$new_tid"

  if ((rc == 124 || rc == 137)); then
    die 5 "timed out after ${tmo}s (thread=$thread, log=$log). Retry with a larger --timeout or split the task."
  fi
  # Top-level "error" events are often transient (e.g. "Reconnecting... 1/5")
  # and Codex retries them itself, so only a failed or missing turn counts.
  local failmsg="" completed retries
  completed="$(jq -r 'select(.type=="turn.completed") | "y"' "$log" 2>/dev/null | head -1)"
  retries="$(jq -r 'select(.type=="error") | .message' "$log" 2>/dev/null | grep -c . || true)"
  failmsg="$(jq -r 'select(.type=="turn.failed") | (.error.message // "turn failed")' "$log" 2>/dev/null | head -3)"
  if [[ -z "$failmsg" && -z "$completed" ]]; then
    failmsg="no turn.completed event; last errors: $(jq -r 'select(.type=="error") | .message' "$log" 2>/dev/null | tail -2)"
  fi
  if ((rc != 0)) || [[ -n "$failmsg" ]]; then
    [[ -n "$failmsg" ]] || failmsg="$(tail -5 "$log.stderr" 2>/dev/null)"
    if [[ "$mode" == "resume" ]]; then
      die 4 "codex resume failed (exit $rc, thread=$thread, id=$tid): $failmsg
       If the session is gone or corrupted, start over with: send $thread --new ...  (log=$log)"
    fi
    die 4 "codex failed (exit $rc, thread=$thread): $failmsg  (log=$log)"
  fi
  [[ -n "$tid" ]] || die 4 "codex did not report a thread id (log=$log)"
  if [[ ! -s "$last" ]]; then
    die 6 "codex finished but returned an empty reply (thread=$thread, id=$tid, log=$log)"
  fi

  # Persist registration only after a successful turn.
  local now; now="$(date -Is)"
  reg="$(load_registry "$reg_file" "$root")"
  reg="$(jq --arg t "$thread" --arg id "$tid" --arg m "$model" --arg e "$effort" \
            --arg s "$sandbox" --arg c "$cwd" --arg now "$now" --arg mode "$mode" '
    .threads[$t] = ((if $mode=="new" then {created:$now, turns:0} else (.threads[$t] // {created:$now, turns:0}) end)
      + {thread_id:$id, model:$m, effort:$e, sandbox:$s, cwd:$c, updated:$now})
    | .threads[$t].turns += 1' <<<"$reg")"
  save_registry "$reg_file" "$reg"

  local usage_line
  usage_line="$(jq -r 'select(.type=="turn.completed") | .usage | "in=\(.input_tokens) cached=\(.cached_input_tokens) out=\(.output_tokens) reasoning=\(.reasoning_output_tokens)"' "$log" 2>/dev/null | tail -1)"
  cat "$last"; echo
  echo "---"
  echo "[cct] thread=$thread mode=$mode id=$tid model=$model effort=$effort sandbox=$sandbox elapsed=${elapsed}s ${usage_line} retries=${retries:-0} log=$log"
}

cmd_list() {
  local dir="$PWD"; [[ "${1:-}" == "--cd" ]] && dir="${2:?--cd needs a dir}"
  local root f; root="$(project_root "$dir")"; f="$(registry_file "$root")"
  echo "project: $root"
  load_registry "$f" "$root" | jq -r '.threads | if length==0 then "  (no threads)" else
    to_entries[] | "  \(.key)\t\(.value.model)/\(.value.effort)\t\(.value.sandbox)\tturns=\(.value.turns)\tupdated=\(.value.updated)\tid=\(.value.thread_id)" end'
}

cmd_show() {
  local thread="${1:-}"; shift || true
  [[ -n "$thread" ]] || die 2 "show: missing <thread>"
  local dir="$PWD"; [[ "${1:-}" == "--cd" ]] && dir="${2:?--cd needs a dir}"
  local root f; root="$(project_root "$dir")"; f="$(registry_file "$root")"
  load_registry "$f" "$root" | jq --arg t "$thread" -e '.threads[$t]' || die 2 "no thread '$thread' in $root"
}

cmd_reset() {
  local target="${1:-}"; shift || true
  [[ -n "$target" ]] || die 2 "reset: missing <thread> or --all"
  local dir="$PWD"; [[ "${1:-}" == "--cd" ]] && dir="${2:?--cd needs a dir}"
  local root f reg; root="$(project_root "$dir")"; f="$(registry_file "$root")"
  reg="$(load_registry "$f" "$root")"
  if [[ "$target" == "--all" ]]; then reg="$(jq '.threads={}' <<<"$reg")"
  else reg="$(jq --arg t "$target" 'del(.threads[$t])' <<<"$reg")"; fi
  save_registry "$f" "$reg"
  echo "[cct] reset $target in $root"
}

case "${1:-help}" in
  send)     shift; cmd_send "$@" ;;
  list)     shift; cmd_list "$@" ;;
  show)     shift; cmd_show "$@" ;;
  reset)    shift; cmd_reset "$@" ;;
  profiles) profiles_json | jq . ;;
  version|--version) echo "cc-codex-threads $VERSION" ;;
  help|-h|--help) usage ;;
  *) usage >&2; exit 2 ;;
esac
