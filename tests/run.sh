#!/usr/bin/env bash
# Offline test suite for cc-codex-threads, using tests/fake-codex (a fake
# `codex exec` plus a fake `codex app-server`). No API calls.
set -uo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
T="$HERE/../plugins/cc-codex-threads/skills/codex-threads/scripts/codex-thread.sh"
WORK="$(mktemp -d)"; trap 'pkill -f "$WORK" 2>/dev/null; rm -rf -- "$WORK"' EXIT
mkdir -p "$WORK/bin" "$WORK/proj" "$WORK/other" "$WORK/dir with space"
ln -s "$HERE/fake-codex" "$WORK/bin/codex"
export PATH="$WORK/bin:$PATH" CCT_STATE_DIR="$WORK/state" CCT_PROFILES="$WORK/profiles.json"
export FAKE_ARGS_LOG="$WORK/args.log" FAKE_STORE="$WORK/store" CCT_SOCK_DIR="$WORK/sock" CCT_STOP_GRACE=3
unset CCT_ENGINE FAKE_RESUME_OTHERID FAKE_SANDBOX FAKE_IGNORE_INTERRUPT
cd "$WORK/proj" && git init -q . || exit 1
PROJ_ROOT="$(pwd -P)"

pass=0 fail=0
ok()  { pass=$((pass+1)); echo "  ok   $1"; }
bad() { fail=$((fail+1)); echo "  FAIL $1"; [[ -n "${2:-}" ]] && echo "       $2"; }
check() { if eval "$1"; then ok "$2"; else bad "$2" "${3:-}"; fi; }
run() { OUT="$(timeout 60 "$T" "$@" 2>"$WORK/err")"; RC=$?; ERR="$(cat "$WORK/err")"; }
expect_rc() { if [[ "$RC" == "$1" ]]; then ok "$2"; else bad "$2" "rc=$RC want=$1 out=${OUT:0:300} err=$ERR"; fi; }
regfile() { local f; for f in "$CCT_STATE_DIR"/projects/*.json; do
  jq -e --arg r "$PROJ_ROOT" '.root == $r' "$f" >/dev/null 2>&1 && { echo "$f"; return; }; done; }
reg() { jq -r "$1" "$(regfile)" 2>/dev/null; }
lastreq() { grep "\"method\": \"$1\"" "$FAKE_ARGS_LOG" | tail -1; }
wait_running() { local i; for i in $(seq 1 50); do "$T" status "$1" 2>/dev/null | grep -q '"state": "running"' && return 0; sleep 0.1; done; return 1; }

echo "send / resume (app-server)"
run send luna -- "hello MODE:ok";               expect_rc 0 "new thread from profile"
check '[[ "$(reg .threads.luna.model)/$(reg .threads.luna.effort)" == gpt-6-luna/max ]]' "profile recorded"
check '[[ "$OUT" == *"reply from "*": hello MODE:ok"* ]]' "final answer printed"
check 'lastreq thread/start | jq -e ".params.ephemeral == false and .params.approvalPolicy == \"never\" and .params.sandbox == \"workspace-write\"" >/dev/null' "thread/start is persistent, never asks, sandboxed"
id1="$(reg .threads.luna.thread_id)"
run send luna -- "again";                       expect_rc 0 "resume"
check 'lastreq thread/resume | jq -e --arg id "$id1" ".params.threadId == \$id and .params.excludeTurns == true and .params.model == \"gpt-6-luna\"" >/dev/null' "resume by id with model, without history"
check 'lastreq turn/start | jq -e ".params.effort == \"max\"" >/dev/null' "effort sent with the turn"
check '[[ "$(reg .threads.luna.turns)" == 2 ]]' "turn counter"
run send sol -- "x";                            expect_rc 0 "second thread"
check '[[ "$(reg .threads.sol.thread_id)" != "$id1" ]]' "threads have distinct sessions"
run send luna --new -- "fresh";                 expect_rc 0 "--new"
check '[[ "$(reg .threads.luna.thread_id)" != "$id1" && "$(reg .threads.luna.turns)" == 1 ]]' "--new replaces the thread"
PROMPT=$'multi\nline $HOME `x` "q" \'s\' $(touch '"$WORK"'/pwned)\nCCT_PROMPT_END\nend'
OUT="$(printf '%s' "$PROMPT" | timeout 60 "$T" send luna 2>&1)"
check '[[ "$OUT" == *"$PROMPT"* ]]' "stdin prompt preserved verbatim"
printf '%s' "$PROMPT" > "$WORK/p.txt"; run send luna --prompt-file "$WORK/p.txt"; expect_rc 0 "--prompt-file"
check '[[ "$OUT" == *"$PROMPT"* && ! -e "$WORK/pwned" ]]' "prompt file verbatim, nothing executed"
run send luna --progress -- "MODE:ok"
check '[[ "$OUT" == *"\$ ls -la"* && "$OUT" == *"↳ exit 2"* && "$OUT" == *"💭 Planning the change"* && "$OUT" == *"✎ update src/a.py"* && "$OUT" == *"✓ done"* ]]' "--progress renders commands, reasoning, edits" "$OUT"
check '[[ "$OUT" != *noise* ]]' "deltas are not rendered"

echo "steer / interrupt"
"$T" send luna -- "MODE:sleep:20" > "$WORK/o1" 2>&1 & bg=$!
wait_running luna
run steer luna -- "change of plan";             expect_rc 0 "steer delivered"
wait "$bg"; RC=$?;                              expect_rc 0 "steered run completes"
check 'grep -q "STEERED: change of plan" "$WORK/o1"' "steer text reached the turn"
"$T" send luna -- "MODE:sleep:20" > "$WORK/o2" 2>&1 & bg=$!
wait_running luna
run interrupt luna;                             expect_rc 0 "interrupt sent"
wait "$bg"; RC=$?;                              expect_rc 7 "interrupted run -> 7"
run result luna;                                expect_rc 7 "result reports the interruption"
check '[[ "$OUT" == *"partial work before stop"* ]]' "partial reply kept"
run steer luna -- x;                            expect_rc 8 "steer without a running turn -> 8"
run interrupt luna;                             expect_rc 8 "interrupt without a running turn -> 8"
run steer luna --run 000000000000 -- x;         expect_rc 2 "steer to a foreign run id refused"
"$T" send luna -- "MODE:sleep:20" > /dev/null 2>&1 & bg=$!
wait_running luna; rid="$("$T" status luna | jq -r .run_id)"
run steer sol --run "$rid" -- x;                expect_rc 2 "run id of another thread refused"
kill -TERM "$bg"; wait "$bg"; RC=$?;            expect_rc 7 "SIGTERM -> graceful interrupt (7)"
check '"$T" status luna | jq -e ".state == \"done\" and .exit_code == 7" >/dev/null' "run recorded as done"

echo "concurrency and cleanup"
for i in $(seq 1 12); do "$T" send "p$i" --profile luna -- "MODE:sleep:1" >/dev/null 2>&1 & done; wait
check '[[ "$(reg ".threads | length")" == 14 ]]' "12 parallel threads all registered" "$(reg '.threads|keys')"
"$T" send luna -- "MODE:sleep:20" >/dev/null 2>&1 & bg=$!
wait_running luna
run send luna -- "x";                           expect_rc 3 "same thread busy"
run reset sol;                                  expect_rc 3 "reset refused while a thread runs"
srv="$(pgrep -P "$bg" | head -1)"
kill -KILL "$bg"; wait "$bg" 2>/dev/null; sleep 1
check '[[ -n "$srv" ]] && ! kill -0 "$srv" 2>/dev/null' "runner SIGKILLed -> app-server exits (no orphan)" "srv=$srv"
check '"$T" status luna | jq -e ".state == \"lost\" and .alive == false" >/dev/null' "killed run shows as lost"
run send luna -- "x";                           expect_rc 0 "lock released after runner death"
run result luna;                                expect_rc 0 "result of the latest run"

echo "failures"
run send luna -- "MODE:reconnect";              expect_rc 0 "transient reconnect tolerated"
check '[[ "$OUT" == *"retries=1"* ]]' "retry counted"
run send f1 --profile luna -- "MODE:fail";      expect_rc 4 "failed turn -> 4"
check '[[ "$ERR" == *"simulated model failure"* ]]' "failure reason reported"
check '[[ "$(reg ".threads.f1.thread_id // \"none\"")" != none ]]' "thread registered before its first turn"
run send f1 -- "MODE:ok";                       expect_rc 0 "failed first turn can be continued"
run send luna -- "MODE:empty";                  expect_rc 6 "empty reply -> 6"
run send luna -- "MODE:nophase";                expect_rc 0 "reply without phase accepted"
check '[[ "$OUT" == *"reply from"* ]]' "phase-less reply printed"
run send luna --timeout 2 -- "MODE:hang";       expect_rc 5 "timeout -> interrupt -> 5"
FAKE_IGNORE_INTERRUPT=1 run send luna --timeout 1 -- "MODE:hang"; expect_rc 5 "timeout when the turn ignores interrupt"
run send luna -- "MODE:crash";                  expect_rc 4 "app-server crash -> 4"
run send luna -- "MODE:interleave";             expect_rc 0 "server request before turn/start response"
for k in cmd file perm input elicit tool auth attest legacy unknown; do
  run send luna -- "MODE:request:$k";           expect_rc 0 "server request '$k' answered correctly"
done
OUT="$(CCT_STDIN_TIMEOUT=2 timeout 20 "$T" send luna < <(sleep 15) 2>&1)"; RC=$?
expect_rc 5 "never-closed stdin bounded"

echo "settings"
run send ro --profile luna --sandbox read-only -- x; expect_rc 0 "read-only thread"
run send ro --sandbox workspace-write -- y;     expect_rc 2 "loosening a resumed thread refused"
run send luna --sandbox read-only -- y;         expect_rc 0 "tighten a writable thread for one run"
check 'lastreq thread/resume | jq -e ".params.sandbox == \"read-only\"" >/dev/null' "read-only applied on resume"
check '[[ "$(reg .threads.luna.sandbox)" == workspace-write ]]' "registry keeps the thread's own sandbox"
FAKE_SANDBOX=dangerFullAccess run send luna -- x; expect_rc 4 "server applying another sandbox -> 4"
check '[[ "$(lastreq turn/start | jq -r .params.input[0].text)" != x ]]' "no turn started after a settings mismatch"
FAKE_RESUME_OTHERID=1 run send luna -- x;       expect_rc 4 "resume returning another thread -> 4"
rm -f "$FAKE_STORE/$(reg .threads.sol.thread_id)"
run send sol -- x;                              expect_rc 4 "resume of a missing thread -> 4"
check '[[ "$ERR" == *"--new"* ]]' "missing thread suggests --new"

echo "usage errors"
OUT="$(CCT_STDIN_TIMEOUT=0 "$T" send luna -- x 2>&1)"; RC=$?; expect_rc 2 "CCT_STDIN_TIMEOUT=0 rejected"
echo '{"num":{"model":123,"effort":456}}' > "$CCT_PROFILES"
run send n1 --profile num -- x;                 expect_rc 2 "non-string profile fields"
run send n2 --model m --effort bogus -- x;      expect_rc 2 "unsupported effort"
rm -f "$CCT_PROFILES"
run send luna --model;                          expect_rc 2 "missing option value"
run send luna --timeout 0 -- x;                 expect_rc 2 "--timeout 0 rejected"
run send luna --timeout 08 -- "MODE:ok";        expect_rc 0 "--timeout 08 is decimal"
run send luna --timeout 99999999999 -- x;       expect_rc 2 "absurd --timeout rejected"
run send luna -dash;                            expect_rc 2 "dash prompt needs --"
run send luna compare - x;                      expect_rc 0 "mid-prompt dash"
check '[[ "$OUT" == *": compare - x"* ]]' "mid-prompt dash preserved"
run send nope -- x;                             expect_rc 2 "unknown profile"
echo '{"bad":{"model":"m"}}' > "$CCT_PROFILES"
run send b1 --profile bad -- x;                 expect_rc 2 "profile without effort"
echo '{not json' > "$CCT_PROFILES"
run send b2 --profile luna -- x;                expect_rc 2 "invalid user profiles JSON"
rm -f "$CCT_PROFILES"
run reset --all --bogus;                        expect_rc 2 "reset rejects unknown args"
run list --cd;                                  expect_rc 2 "--cd without value"
run watch luna --wait abc;                      expect_rc 2 "watch --wait must be a number"

echo "state"
mkdir "$WORK/proj/gone"; run send g --profile luna --cd "$WORK/proj/gone" -- x; expect_rc 0 "thread in subdir"
rm -rf "$WORK/proj/gone"
run send g -- x;                                expect_rc 4 "deleted cwd -> 4 (no silent switch)"
( cd "$WORK/other" && "$T" send luna -- x >/dev/null 2>&1 ); id_other="$(jq -r 'select(.root|endswith("/other")) | .threads.luna.thread_id' "$CCT_STATE_DIR"/projects/*.json)"
check '[[ -n "$id_other" && "$id_other" != "$(reg .threads.luna.thread_id)" ]]' "projects isolated"
f="$(regfile)"; good="$(cat "$f")"
echo '{oops' > "$f";  run send luna -- x;       expect_rc 4 "corrupt registry -> 4 (not silently reset)"
: > "$f";             run send luna -- x;       expect_rc 4 "empty registry file -> 4"
jq '.threads.luna.turns="1"' <<<"$good" > "$f"; run send luna -- x; expect_rc 4 "string turns -> 4"
jq '.threads.luna.thread_id="deadbeef"' <<<"$good" > "$f"; run send luna -- x; expect_rc 4 "invalid thread id -> 4"
run reset --all;                                expect_rc 0 "reset --all recovers"
run list;                                       expect_rc 0 "list after recovery"

echo "watch"
"$T" send w1 --profile luna -- "MODE:sleep:2" >/dev/null 2>&1 &
W="$(timeout 30 "$T" watch w1 --wait 10)"; RC=$?; wait
expect_rc 0 "watch follows a live run and exits when it ends"
check '[[ "$W" == *"● session"* && "$W" == *"✓ done"* && "$W" == *"run ended"* ]]' "watch renders progress" "$W"
W="$(timeout 30 "$T" watch w1 --wait 0)"; RC=$?
expect_rc 0 "watch replays the last run"
check '[[ "$W" == *"✓ done"* ]]' "replay shows completion"
run watch nothere --wait 0;                     expect_rc 2 "watch of unknown thread -> 2"

echo "relay"
eval "$(cd "$WORK/proj" && "$T" relay-init luna | sed 's/^/R_/')"
check '[[ -f "$R_STATE" && -n "$R_MSG" ]]' "relay-init prints state and message paths"
printf '@cct thread=r1 read-only cd="%s"\nfirst task MODE:ok\n$(touch %s/pwned2)' "$WORK/dir with space" "$WORK" > "$R_MSG"
run relay "$R_STATE";                           expect_rc 0 "relay starts a run"
check '[[ "$OUT" == *"✓ done"* && "$OUT" == *"RUN FINISHED (exit 0)"* && ! -e "$WORK/pwned2" ]]' "relay output has progress, no shell expansion"
check '[[ ! -e "$R_MSG" ]]' "message file consumed"
run relay-result "$R_STATE";                    expect_rc 0 "relay-result"
check '[[ "$OUT" == *"first task MODE:ok"* && "$OUT" != *"@cct"* && "$OUT" != *"✓ done"* ]]' "relay-result is the reply without directive or progress" "$OUT"
run relay-result "$R_STATE"
check '[[ "$OUT" == *"nothing new"* ]]' "each result reported once"
check '[[ "$(jq -r .threads.r1.sandbox "$CCT_STATE_DIR"/projects/*.json | grep -v null)" == read-only ]]' "directive: thread, read-only and cd with spaces applied"
printf 'MODE:sleep:20' > "$R_MSG"; "$T" relay "$R_STATE" > "$WORK/r1" 2>&1 & bg=$!
for i in $(seq 1 50); do grep -q "turn started" "$WORK/r1" && break; sleep 0.1; done
printf 'please change course' > "$R_MSG"; run relay "$R_STATE"
check '[[ "$OUT" == *"steering input"* ]]' "message during a run becomes steering"
wait "$bg"
run relay-result "$R_STATE"
check '[[ "$OUT" == *"steering input"* && "$OUT" == *"STEERED: please change course"* ]]' "steer note and final reply both reported" "$OUT"
printf 'MODE:sleep:20' > "$R_MSG"; "$T" relay "$R_STATE" > "$WORK/r2" 2>&1 & bg=$!
for i in $(seq 1 50); do grep -q "turn started" "$WORK/r2" && break; sleep 0.1; done
printf '@cct interrupt\n' > "$R_MSG"; run relay "$R_STATE"
check '[[ "$OUT" == *"interrupt sent"* ]]' "@cct interrupt"
wait "$bg"; RC=$?;                              expect_rc 7 "interrupted relay run -> 7"
printf '@cct bogus\nx' > "$R_MSG"; run relay "$R_STATE"; expect_rc 2 "unknown directive rejected"
run relay-result "$R_STATE"
check '[[ "$OUT" == *"unknown @cct directive"* ]]' "rejection is reported through relay-result"
run relay /etc/passwd;                          expect_rc 2 "relay refuses foreign state files"

echo "exec engine"
export CCT_ENGINE=exec
run send x1 --profile luna -- "MODE:ok";        expect_rc 0 "exec: new thread"
xid="$(reg .threads.x1.thread_id)"
run send x1 -- "again";                         expect_rc 0 "exec: resume"
check 'grep -q "exec resume $xid .*-m gpt-6-luna" "$FAKE_ARGS_LOG"' "exec: resume by id"
run send x1 --timeout 1 -- "MODE:sleep:5";      expect_rc 5 "exec: timeout -> 5"
"$T" send x1 -- "MODE:sleep:20" >/dev/null 2>&1 & bg=$!
wait_running x1
run steer x1 -- x;                              expect_rc 2 "exec: steering refused"
run interrupt x1;                               expect_rc 0 "exec: interrupt"
wait "$bg"; RC=$?;                              expect_rc 7 "exec: interrupted -> 7"
unset CCT_ENGINE
run send x1 -- "MODE:ok";                       expect_rc 0 "exec thread resumes under app-server"

echo; echo "passed=$pass failed=$fail"
((fail == 0))
