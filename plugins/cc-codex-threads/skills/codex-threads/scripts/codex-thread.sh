#!/usr/bin/env bash
# cc-codex-threads: run OpenAI Codex CLI as named, resumable worker threads.
#
# Each named thread (e.g. "luna", "sol") maps to one Codex session id, stored
# per project. Sending to a known thread resumes that exact session by id, so
# several threads can coexist without stealing each other's context.
#
# Locking (all per project):
#   fd 7  project gate   send = shared, reset = exclusive (no reset mid-run)
#   fd 8  registry lock  held only around read-modify-write of the registry
#   fd 9  thread lock    one turn at a time per thread
# `timeout` inherits the locks, so they stay held while Codex runs even if this
# wrapper is killed; only the Codex process itself closes them.
#
# Usage: codex-thread.sh help
set -uo pipefail

VERSION="1.1.0"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
STATE_DIR="${CCT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/cc-codex-threads}"
USER_PROFILES="${CCT_PROFILES:-${XDG_CONFIG_HOME:-$HOME/.config}/cc-codex-threads/profiles.json}"
DEFAULT_PROFILES="$SCRIPT_DIR/profiles.json"
STDIN_TIMEOUT="${CCT_STDIN_TIMEOUT:-60}"

die() { local code="$1"; shift; echo "[cct] ERROR: $*" >&2; exit "$code"; }
[[ "$STDIN_TIMEOUT" =~ ^0*[1-9][0-9]*$ ]] || die 2 "CCT_STDIN_TIMEOUT must be a positive number of seconds"

usage() {
  cat <<'EOF'
codex-thread.sh — named, resumable Codex worker threads

COMMANDS
  send <thread> [options] [--] [prompt...]
      Send a prompt to <thread>. Resumes the thread's session if it exists in
      this project, otherwise starts a new one. The prompt is the trailing
      arguments (put "--" before a prompt that starts with "-"), or stdin when
      no prompt argument is given.
      Options:
        --profile <name>   settings preset for a NEW thread (default: <thread>)
        --model <id>       override model        (new thread only)
        --effort <level>   override effort       (new thread only)
        --sandbox <mode>   read-only | workspace-write | danger-full-access
                           (new thread only)
        --new              discard the registered session, start fresh
        --cd <dir>         project dir (default: current dir)
        --timeout <sec>    hard timeout, > 0 (default depends on effort)
  list   [--cd <dir>]                 threads registered for this project
  show   <thread> [--cd <dir>]        one thread's record
  reset  <thread> [--cd <dir>]        forget one thread (Codex history kept)
  reset  --all [--cd <dir>]           forget all threads of this project
  profiles                            show effective profiles
  help | version

EXIT CODES
  0 ok | 2 usage | 3 busy | 4 failed (codex or local state) | 5 timeout | 6 empty reply
EOF
}

need() { command -v "$1" >/dev/null 2>&1 || die 2 "required command not found: $1"; }
need codex; need jq; need flock; need timeout
if command -v sha1sum >/dev/null 2>&1; then HASH_CMD=(sha1sum)
elif command -v shasum >/dev/null 2>&1; then HASH_CMD=(shasum -a 1)
else die 2 "required command not found: sha1sum or shasum"; fi

now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
valid_name() { [[ "$1" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || die 2 "invalid thread name: '$1' (use A-Z a-z 0-9 . _ -)"; }
valid_tid() { [[ "$1" =~ ^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$ ]]; }
abs_dir() { [[ -d "$1" ]] || die 2 "not a directory: $1"; (cd -- "$1" && pwd -P) || die 2 "cannot enter directory: $1"; }

project_root() {
  local dir="$1" root
  root="$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null)" && [[ -n "$root" ]] || root="$dir"
  (cd -- "$root" && pwd -P)
}

registry_file() {
  local key
  key="$(printf '%s' "$1" | "${HASH_CMD[@]}" | cut -c1-16)" || return 4
  [[ "$key" =~ ^[0-9a-f]{16}$ ]] || return 4
  printf '%s/projects/%s.json' "$STATE_DIR" "$key"
}

# Missing file -> empty registry. Present but invalid (empty, several JSON
# values, wrong shape) -> failure; never silently reset.
load_registry() {
  local f="$1" root="$2"
  if [[ ! -e "$f" ]]; then jq -n --arg root "$root" '{root:$root, threads:{}}' || return 4; return; fi
  jq -ces --arg root "$root" '
    if length == 1 then .[0] else error("expected exactly one registry object") end
    | if type=="object" and .root==$root and (.threads|type=="object")
        and all(.threads[];
          ([.thread_id,.model,.effort,.sandbox,.cwd] | all(type=="string" and length>0))
          and (.turns | type=="number" and . >= 0 and floor == .))
      then . else error("invalid registry") end
  ' "$f" 2>/dev/null || return 4
}

save_registry() {
  local f="$1" json="$2" tmp
  mkdir -p -- "$(dirname -- "$f")" || return 4
  tmp="$(mktemp "$f.XXXXXX")" || return 4
  if ! printf '%s\n' "$json" > "$tmp" || ! mv -f -- "$tmp" "$f"; then rm -f -- "$tmp"; return 4; fi
}

lock_paths() {  # sets GATE_LOCK REG_LOCK for a registry file
  local base; base="$STATE_DIR/locks/$(basename -- "$1" .json)"
  mkdir -p -- "$STATE_DIR/locks" || die 4 "cannot create $STATE_DIR/locks"
  GATE_LOCK="$base.gate.lock"; REG_LOCK="$base.registry.lock"
}

open_registry_lock() {
  exec 8>"$REG_LOCK" || die 4 "cannot open registry lock"
  flock -w 15 8 || die 3 "registry lock busy (another cct process is stuck?)"
}

profiles_json() {
  local base='{}' user='{}'
  if [[ -f "$DEFAULT_PROFILES" ]]; then base="$(cat -- "$DEFAULT_PROFILES")" || return 2; fi
  if [[ -f "$USER_PROFILES" ]]; then user="$(cat -- "$USER_PROFILES")" || return 2; fi
  jq -n --argjson a "$base" --argjson b "$user" '$a * $b' 2>/dev/null || return 2
}

default_timeout() {
  case "$1" in
    none|minimal|low) echo 300 ;; medium) echo 600 ;; high) echo 1200 ;;
    xhigh|max) echo 2400 ;; *) echo 3600 ;;
  esac
}

parse_cd_only() {  # parse trailing [--cd <dir>] for list/show/reset; sets DIR
  DIR="$PWD"
  while (($#)); do
    case "$1" in
      --cd) (($# >= 2)) && [[ -n "$2" ]] || die 2 "--cd requires a directory"; DIR="$2"; shift 2 ;;
      *) die 2 "unknown argument: $1" ;;
    esac
  done
  DIR="$(abs_dir "$DIR")" || exit 2
}

cmd_send() {
  local thread="" profile="" model="" effort="" sandbox="" force_new=0 dir="$PWD" tmo=""
  local -a words=()
  while (($#)); do
    case "$1" in
      --profile|--model|--effort|--sandbox|--cd|--timeout)
        (($# >= 2)) && [[ -n "$2" ]] || die 2 "$1 requires a non-empty value" ;;
    esac
    case "$1" in
      --profile) profile="$2"; shift 2 ;;
      --model)   model="$2"; shift 2 ;;
      --effort)  effort="$2"; shift 2 ;;
      --sandbox) sandbox="$2"; shift 2 ;;
      --cd)      dir="$2"; shift 2 ;;
      --timeout) tmo="$2"; shift 2 ;;
      --new)     force_new=1; shift ;;
      --)        shift; words+=("$@"); break ;;
      -)         ((${#words[@]})) && words+=("$1"); shift ;;  # leading "-" = read stdin; later "-" is prompt text
      -*)        die 2 "unknown option: $1 (put '--' before a prompt that starts with '-')" ;;
      *)         if [[ -z "$thread" ]]; then thread="$1"; else words+=("$1"); fi; shift ;;
    esac
  done
  [[ -n "$thread" ]] || die 2 "send: missing <thread>"
  valid_name "$thread"
  [[ -z "$profile" ]] || valid_name "$profile"
  [[ -z "$tmo" || "$tmo" =~ ^0*[1-9][0-9]*$ ]] || die 2 "--timeout must be a positive number of seconds"
  dir="$(abs_dir "$dir")" || exit 2

  # Prompt: trailing args, else stdin (bounded, so a never-closed pipe cannot hang us).
  local prompt read_rc
  if ((${#words[@]})); then prompt="${words[*]}"
  else
    [[ -t 0 ]] && die 2 "send: no prompt given (pass it as arguments or on stdin)"
    prompt="$(timeout -k 3 "$STDIN_TIMEOUT" cat)"; read_rc=$?
    ((read_rc == 124)) && die 5 "reading the prompt from stdin timed out after ${STDIN_TIMEOUT}s"
    ((read_rc == 0)) || die 4 "reading the prompt from stdin failed"
  fi
  [[ -n "${prompt//[[:space:]]/}" ]] || die 2 "send: prompt is empty"

  local root reg_file reg rec
  root="$(project_root "$dir")" || die 4 "cannot resolve project root for $dir"
  reg_file="$(registry_file "$root")" || die 4 "cannot compute registry path"
  mkdir -p -- "$STATE_DIR/logs" || die 4 "cannot create $STATE_DIR/logs"
  lock_paths "$reg_file"

  exec 7>"$GATE_LOCK" || die 4 "cannot open project gate"
  flock -s -w 15 7 || die 3 "a reset is in progress for this project; retry shortly"
  exec 9>"$STATE_DIR/locks/$(basename -- "$reg_file" .json)-$thread.lock" || die 4 "cannot open thread lock"
  flock -n 9 || die 3 "thread '$thread' is already running in this project; wait for it or use another thread name"

  reg="$(load_registry "$reg_file" "$root")" || die 4 "registry is corrupt: $reg_file (inspect it, or remove it to forget all threads of this project)"
  rec="$(jq -c --arg t "$thread" '.threads[$t] // empty' <<<"$reg")" || die 4 "cannot read registry"

  local mode="new" tid="" cwd="$dir"
  if [[ -n "$rec" && $force_new -eq 0 ]]; then
    mode="resume"
    tid="$(jq -r '.thread_id' <<<"$rec")"; model="$(jq -r '.model' <<<"$rec")"
    effort="$(jq -r '.effort' <<<"$rec")"; sandbox="$(jq -r '.sandbox' <<<"$rec")"
    cwd="$(jq -r '.cwd' <<<"$rec")"
    valid_tid "$tid" || die 4 "registered session id for '$thread' is invalid; use --new"
    [[ "$cwd" == /* && -d "$cwd" ]] || die 4 "working directory of '$thread' no longer exists ($cwd); use --new"
  else
    local profs prof
    profile="${profile:-$thread}"
    profs="$(profiles_json)" || die 2 "invalid profiles JSON ($DEFAULT_PROFILES or $USER_PROFILES)"
    prof="$(jq -c --arg p "$profile" '.[$p] // empty' <<<"$profs")" || die 2 "invalid profiles JSON"
    if [[ -z "$prof" && ( -z "$model" || -z "$effort" ) ]]; then
      die 2 "no profile named '$profile'; pass --profile <name> or both --model and --effort (see: codex-thread.sh profiles)"
    fi
    [[ -n "$prof" ]] || prof='{}'
    jq -e 'type=="object" and all([.model,.effort,.sandbox][]; .==null or type=="string")' <<<"$prof" >/dev/null \
      || die 2 "profile '$profile': model, effort and sandbox must be strings"
    [[ -n "$model"   ]] || model="$(jq -r '.model // empty' <<<"$prof")"
    [[ -n "$effort"  ]] || effort="$(jq -r '.effort // empty' <<<"$prof")"
    [[ -n "$sandbox" ]] || sandbox="$(jq -r '.sandbox // "workspace-write"' <<<"$prof")"
  fi
  [[ -n "$model" && -n "$effort" ]] || die 2 "profile '$profile' must define model and effort"
  [[ "$model" =~ ^[A-Za-z0-9._:/-]+$ ]] || die 2 "invalid model: $model"
  case "$effort" in none|minimal|low|medium|high|xhigh|max|ultra) ;; *) die 2 "unsupported effort: $effort" ;; esac
  case "$sandbox" in read-only|workspace-write|danger-full-access) ;; *) die 2 "invalid sandbox: $sandbox" ;; esac
  [[ -n "$tmo" ]] || tmo="$(default_timeout "$effort")"

  local log last pfile
  log="$STATE_DIR/logs/$thread-$(date -u +%Y%m%dT%H%M%SZ)-$$.jsonl"
  # Globals so the EXIT trap can still see them after this function returns.
  CCT_LAST="$(mktemp)" && CCT_PROMPT="$(mktemp)" || die 4 "cannot create temp files"
  trap 'rm -f -- "$CCT_LAST" "$CCT_PROMPT"' EXIT
  last="$CCT_LAST"; pfile="$CCT_PROMPT"
  printf '%s' "$prompt" > "$pfile" || die 4 "cannot write prompt file"

  local -a common=(--json --skip-git-repo-check -m "$model"
    -c "model_reasoning_effort=\"$effort\"" -c 'approval_policy="never"' -o "$last")
  local -a cmd
  if [[ "$mode" == "resume" ]]; then
    cmd=(codex exec resume "$tid" "${common[@]}" -c "sandbox_mode=\"$sandbox\"" -)
  else
    cmd=(codex exec "${common[@]}" -s "$sandbox" -)
  fi

  local t0 rc; t0="$(date +%s)"
  # At the deadline, timeout SIGKILLs the whole process group (Codex's launcher
  # and its native child). Only that KILL message counts as a timeout.
  ( cd -- "$cwd" && LC_ALL=C timeout -v -s KILL "$tmo" \
      bash -c 'exec 7>&- 8>&- 9>&-; exec "$@"' cct-child "${cmd[@]}" \
      < "$pfile" > "$log" 2> "$log.stderr" )
  rc=$?
  local elapsed=$(( $(date +%s) - t0 ))

  if ((rc == 137)) && grep -q '^timeout: sending signal KILL ' "$log.stderr" 2>/dev/null; then
    die 5 "timed out after ${tmo}s (thread=$thread, log=$log). Split the task or raise --timeout."
  fi

  # Top-level "error" events are often transient (e.g. "Reconnecting... 1/5")
  # and Codex retries them itself, so only a failed or missing turn counts.
  local failmsg completed retries new_tid
  new_tid="$(jq -r 'select(.type=="thread.started") | .thread_id' "$log" 2>/dev/null | head -1)"
  completed="$(jq -r 'select(.type=="turn.completed") | "y"' "$log" 2>/dev/null | head -1)"
  retries="$(jq -r 'select(.type=="error") | .message' "$log" 2>/dev/null | grep -c . || true)"
  failmsg="$(jq -r 'select(.type=="turn.failed") | (.error.message // "turn failed")' "$log" 2>/dev/null | head -3)"
  if [[ -z "$failmsg" && -z "$completed" ]]; then
    failmsg="no turn.completed event; last errors: $(jq -r 'select(.type=="error") | .message' "$log" 2>/dev/null | tail -2) $(tail -3 "$log.stderr" 2>/dev/null)"
  fi
  if ((rc != 0)) || [[ -n "$failmsg" ]]; then
    ((rc == 137)) && failmsg="killed by SIGKILL (out of memory or external kill). $failmsg"
    if [[ "$mode" == "resume" ]]; then
      die 4 "codex resume failed (exit $rc, thread=$thread, id=$tid): $failmsg
       If the session is gone or corrupted, start over with: send $thread --new ...  (log=$log)"
    fi
    die 4 "codex failed (exit $rc, thread=$thread): $failmsg  (log=$log)"
  fi
  valid_tid "$new_tid" || die 4 "codex did not report a valid session id (log=$log)"
  if [[ "$mode" == "resume" ]]; then
    [[ "${new_tid,,}" == "${tid,,}" ]] || die 4 "codex resumed a different session ($new_tid, expected $tid; log=$log)"
  else
    tid="$new_tid"
  fi
  [[ -s "$last" ]] || die 6 "codex finished but returned an empty reply (thread=$thread, id=$tid, log=$log)"

  # Persist registration only after a successful turn, under the registry lock.
  local now; now="$(now_utc)"
  open_registry_lock
  reg="$(load_registry "$reg_file" "$root")" || die 4 "registry became corrupt: $reg_file (session id was $tid)"
  reg="$(jq --arg t "$thread" --arg id "$tid" --arg m "$model" --arg e "$effort" \
            --arg s "$sandbox" --arg c "$cwd" --arg now "$now" --arg mode "$mode" '
    .threads[$t] = ((if $mode=="new" then {created:$now, turns:0} else (.threads[$t] // {created:$now, turns:0}) end)
      + {thread_id:$id, model:$m, effort:$e, sandbox:$s, cwd:$c, updated:$now})
    | .threads[$t].turns += 1' <<<"$reg")" || die 4 "registry update failed (session id was $tid)"
  save_registry "$reg_file" "$reg" || die 4 "registry write failed: $reg_file (session id was $tid)"
  flock -u 8

  local usage_line
  usage_line="$(jq -r 'select(.type=="turn.completed") | .usage | "in=\(.input_tokens) cached=\(.cached_input_tokens) out=\(.output_tokens) reasoning=\(.reasoning_output_tokens)"' "$log" 2>/dev/null | tail -1)"
  cat -- "$last"; echo
  echo "---"
  echo "[cct] thread=$thread mode=$mode id=$tid model=$model effort=$effort sandbox=$sandbox elapsed=${elapsed}s ${usage_line} retries=${retries:-0} log=$log"
}

cmd_list() {
  parse_cd_only "$@"
  local root f reg; root="$(project_root "$DIR")" || die 4 "cannot resolve project root"
  f="$(registry_file "$root")" || die 4 "cannot compute registry path"
  reg="$(load_registry "$f" "$root")" || die 4 "registry is corrupt: $f"
  echo "project: $root"
  jq -r '.threads | if length==0 then "  (no threads)" else
    to_entries[] | "  \(.key)\t\(.value.model)/\(.value.effort)\t\(.value.sandbox)\tturns=\(.value.turns)\tupdated=\(.value.updated)\tid=\(.value.thread_id)" end' <<<"$reg"
}

cmd_show() {
  local thread="${1:-}"; (($#)) && shift
  [[ -n "$thread" && "$thread" != -* ]] || die 2 "show: missing <thread>"
  parse_cd_only "$@"
  local root f reg; root="$(project_root "$DIR")" || die 4 "cannot resolve project root"
  f="$(registry_file "$root")" || die 4 "cannot compute registry path"
  reg="$(load_registry "$f" "$root")" || die 4 "registry is corrupt: $f"
  jq --arg t "$thread" -e '.threads[$t]' <<<"$reg" || die 2 "no thread '$thread' in $root"
}

cmd_reset() {
  local target="${1:-}"; (($#)) && shift
  [[ -n "$target" ]] || die 2 "reset: missing <thread> or --all"
  [[ "$target" == "--all" ]] || valid_name "$target"
  parse_cd_only "$@"
  local root f reg; root="$(project_root "$DIR")" || die 4 "cannot resolve project root"
  f="$(registry_file "$root")" || die 4 "cannot compute registry path"
  lock_paths "$f"
  exec 7>"$GATE_LOCK" || die 4 "cannot open project gate"
  flock -x -n 7 || die 3 "a thread is running in this project; reset after it finishes"
  open_registry_lock
  if [[ "$target" == "--all" ]]; then
    reg="$(jq -n --arg root "$root" '{root:$root, threads:{}}')" || die 4 "cannot build empty registry"  # also recovers a corrupt one
  else
    reg="$(load_registry "$f" "$root")" || die 4 "registry is corrupt: $f (use: reset --all)"
    reg="$(jq --arg t "$target" 'del(.threads[$t])' <<<"$reg")" || die 4 "registry update failed"
  fi
  save_registry "$f" "$reg" || die 4 "registry write failed: $f"
  echo "[cct] reset $target in $root"
}

case "${1:-help}" in
  send)     shift; cmd_send "$@" ;;
  list)     shift; cmd_list "$@" ;;
  show)     shift; cmd_show "$@" ;;
  reset)    shift; cmd_reset "$@" ;;
  profiles) p="$(profiles_json)" || die 2 "invalid profiles JSON"; jq . <<<"$p" ;;
  version|--version) echo "cc-codex-threads $VERSION" ;;
  help|-h|--help) usage ;;
  *) usage >&2; exit 2 ;;
esac
