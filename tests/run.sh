#!/usr/bin/env bash
# Offline test suite for codex-thread.sh, using tests/fake-codex. No API calls.
set -uo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
T="$HERE/../plugins/cc-codex-threads/skills/codex-threads/scripts/codex-thread.sh"
WORK="$(mktemp -d)"; trap 'rm -rf -- "$WORK"' EXIT
mkdir -p "$WORK/bin" "$WORK/proj" "$WORK/other"
ln -s "$HERE/fake-codex" "$WORK/bin/codex"
export PATH="$WORK/bin:$PATH" CCT_STATE_DIR="$WORK/state" CCT_PROFILES="$WORK/profiles.json"
export FAKE_ARGS_LOG="$WORK/args.log"
cd "$WORK/proj" && git init -q . || exit 1
PROJ_ROOT="$(pwd -P)"

pass=0 fail=0
ok()  { pass=$((pass+1)); echo "  ok   $1"; }
bad() { fail=$((fail+1)); echo "  FAIL $1"; [[ -n "${2:-}" ]] && echo "       $2"; }
run() { OUT="$(timeout 60 "$T" "$@" 2>"$WORK/err")"; RC=$?; ERR="$(cat "$WORK/err")"; }
expect_rc() { if [[ "$RC" == "$1" ]]; then ok "$2"; else bad "$2" "rc=$RC want=$1 err=$ERR"; fi; }
regfile() { local f; for f in "$CCT_STATE_DIR"/projects/*.json; do
  jq -e --arg r "$PROJ_ROOT" '.root == $r' "$f" >/dev/null 2>&1 && { echo "$f"; return; }; done; }
reg() { jq -r "$1" "$(regfile)" 2>/dev/null; }

echo "send / resume"
run send luna -- "hello MODE:ok";               expect_rc 0 "new thread from profile"
[[ "$(reg '.threads.luna.model')/$(reg '.threads.luna.effort')" == "gpt-6-luna/max" ]] && ok "profile recorded" || bad "profile recorded"
id1="$(reg '.threads.luna.thread_id')"
run send luna -- "again";                       expect_rc 0 "resume"
grep -q "exec resume $id1 .*-m gpt-6-luna .*model_reasoning_effort=\"max\"" "$FAKE_ARGS_LOG" && ok "resume by id keeps model+effort" || bad "resume args" "$(tail -1 "$FAKE_ARGS_LOG")"
[[ "$(reg '.threads.luna.turns')" == 2 ]] && ok "turn counter" || bad "turn counter"
run send sol -- "x";                            expect_rc 0 "second thread"
[[ "$(reg '.threads.sol.thread_id')" != "$id1" ]] && ok "threads have distinct sessions" || bad "distinct sessions"
run send luna --new -- "fresh";                 expect_rc 0 "--new"
[[ "$(reg '.threads.luna.thread_id')" != "$id1" ]] && ok "--new replaces session" || bad "--new replaces session"
printf 'multi\nline $HOME `x` "q" '"'"'s'"'"'' | { OUT="$(timeout 60 "$T" send luna 2>&1)"; echo "$OUT"; } | grep -qF 'multi
line $HOME `x` "q" '"'"'s'"'"'' && ok "stdin prompt preserved verbatim" || bad "stdin prompt"

echo "concurrency"
for i in $(seq 1 12); do "$T" send "p$i" --profile luna -- "MODE:sleep:1" >/dev/null 2>&1 & done; wait
[[ "$(reg '.threads | length')" == 14 ]] && ok "12 parallel threads all registered (no lost updates)" || bad "parallel registry" "$(reg '.threads|keys')"
"$T" send luna -- "MODE:sleep:4" >/dev/null 2>&1 & wpid=$!; sleep 1; kill -TERM "$wpid"; sleep 0.5
run send luna -- "x";                           expect_rc 3 "wrapper killed: lock still held while codex runs"
run reset luna;                                 expect_rc 3 "wrapper killed: reset still refused"
sleep 4
"$T" send luna -- "MODE:sleep:3" >/dev/null 2>&1 & bgpid=$!; sleep 1
run send luna -- "x";                           expect_rc 3 "same thread busy"
run reset sol;                                  expect_rc 3 "reset refused while a thread runs"
wait "$bgpid"
run send luna -- "MODE:bg";                     expect_rc 0 "straggler child run"
run send luna -- "x";                           expect_rc 0 "lock not held by straggler child"

echo "failures"
run send luna -- "MODE:reconnect";              expect_rc 0 "transient reconnect tolerated"
[[ "$OUT" == *"retries=1"* ]] && ok "retry counted" || bad "retry counted"
run send f1 --profile luna -- "MODE:fail";      expect_rc 4 "turn.failed -> 4"
[[ "$(reg '.threads.f1 // "none"')" == none ]] && ok "failed new thread not registered" || bad "failed not registered"
run send luna -- "MODE:empty";                  expect_rc 6 "empty reply -> 6"
run send luna --timeout 1 -- "MODE:sleep:5"
expect_rc 5 "timeout -> 5"
run send luna -- "MODE:kill";                   expect_rc 4 "SIGKILL -> 4 (not timeout)"
[[ "$ERR" == *SIGKILL* ]] && ok "SIGKILL explained" || bad "SIGKILL explained" "$ERR"
OUT="$(CCT_STDIN_TIMEOUT=2 timeout 20 "$T" send luna < <(sleep 15) 2>&1)"; RC=$?
expect_rc 5 "never-closed stdin bounded"

echo "prompt and settings"
run send luna compare - x;                      expect_rc 0 "mid-prompt dash"
[[ "$OUT" == *": compare - x"* ]] && ok "mid-prompt dash preserved" || bad "mid-prompt dash preserved" "$OUT"
run send luna -- -;                             expect_rc 0 "prompt that is just a dash"
[[ "$OUT" == *": -"* ]] && ok "dash prompt preserved" || bad "dash prompt preserved" "$OUT"
run send ro --profile luna --sandbox read-only -- x; expect_rc 0 "read-only thread"
run send ro --sandbox workspace-write -- y;     expect_rc 0 "resume ignores sandbox override"
grep "exec resume" "$FAKE_ARGS_LOG" | tail -1 | grep -q 'sandbox_mode="read-only"' && ok "stored sandbox re-applied on resume" || bad "stored sandbox" "$(tail -1 "$FAKE_ARGS_LOG")"

echo "usage errors"
OUT="$(CCT_STDIN_TIMEOUT=0 "$T" send luna -- x 2>&1)"; RC=$?; expect_rc 2 "CCT_STDIN_TIMEOUT=0 rejected"
echo '{"num":{"model":123,"effort":456}}' > "$CCT_PROFILES"
run send n1 --profile num -- x;                 expect_rc 2 "non-string profile fields"
run send n2 --model m --effort bogus -- x;      expect_rc 2 "unsupported effort"
rm -f "$CCT_PROFILES"
run send luna --model;                          expect_rc 2 "missing option value (no infinite loop)"
run send luna --timeout 0 -- x;                 expect_rc 2 "--timeout 0 rejected"
run send luna -dash;                            expect_rc 2 "dash prompt needs --"
run send nope -- x;                             expect_rc 2 "unknown profile"
echo '{"bad":{"model":"m"}}' > "$CCT_PROFILES"
run send b1 --profile bad -- x;                 expect_rc 2 "profile without effort"
echo '{not json' > "$CCT_PROFILES"
run send b2 --profile luna -- x;                expect_rc 2 "invalid user profiles JSON"
rm -f "$CCT_PROFILES"
run reset --all --bogus;                        expect_rc 2 "reset rejects unknown args"
run list --cd;                                  expect_rc 2 "--cd without value"

echo "state"
mkdir "$WORK/proj/gone"; run send g --profile luna --cd "$WORK/proj/gone" -- x; expect_rc 0 "thread in subdir"
rm -rf "$WORK/proj/gone"
run send g -- x;                                expect_rc 4 "deleted cwd -> 4 (no silent switch)"
( cd "$WORK/other" && "$T" send luna -- x >/dev/null 2>&1 ); id_other="$(jq -r 'select(.root|endswith("/other")) | .threads.luna.thread_id' "$CCT_STATE_DIR"/projects/*.json)"
[[ -n "$id_other" && "$id_other" != "$(reg '.threads.luna.thread_id')" ]] && ok "projects isolated" || bad "projects isolated"
f="$(regfile)"; good="$(cat "$f")"
echo '{oops' > "$f";  run send luna -- x;       expect_rc 4 "corrupt registry -> 4 (not silently reset)"
: > "$f";             run send luna -- x;       expect_rc 4 "empty registry file -> 4"
printf '%s\n%s\n' "$good" "$good" > "$f"; run send luna -- x; expect_rc 4 "two JSON documents -> 4"
jq '.threads.luna.turns="1"' <<<"$good" > "$f"; run send luna -- x; expect_rc 4 "string turns -> 4"
jq '.threads.luna.thread_id="deadbeef"' <<<"$good" > "$f"; run send luna -- x; expect_rc 4 "non-UUID session id -> 4"
printf '%s\n' "$good" > "$f"; run send luna -- "MODE:otherid"; expect_rc 4 "resume returning another session -> 4"
run reset --all;                                expect_rc 0 "reset --all recovers"
run list;                                       expect_rc 0 "list after recovery"

echo "watch"
"$T" send w1 --profile luna -- "MODE:sleep:2" >/dev/null 2>&1 &
W="$(timeout 30 "$T" watch w1 --wait 10)"; RC=$?; wait
expect_rc 0 "watch follows a live run and exits when it ends"
[[ "$W" == *"● session"* && "$W" == *"✓ done"* && "$W" == *"run ended"* ]] && ok "watch renders progress" || bad "watch renders progress" "$W"
W="$(timeout 30 "$T" watch w1 --wait 0)"; RC=$?
expect_rc 0 "watch replays the last run"
[[ "$W" == *"✓ done"* ]] && ok "replay shows completion" || bad "replay shows completion" "$W"
run watch nothere --wait 0;                     expect_rc 2 "watch of unknown thread -> 2"

echo; echo "passed=$pass failed=$fail"
((fail == 0))
