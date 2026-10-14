#!/usr/bin/env python3
"""Fake `codex app-server` (stdio JSON-RPC) for offline tests.

Turn behaviour is chosen by a MODE:<x> token in the turn input:
  ok (default) | sleep:<sec> (one command per second; steerable, interruptible)
  fail | empty | reconnect | nophase | crash | hang | interleave
  earlydone (turn completes before the turn/start response) | itemsonly
  (the answer only appears inside the completed Turn)
  request:<cmd|file|perm|input|elicit|tool|auth|attest|legacy|unknown>
Thread-level behaviour comes from the environment:
  FAKE_RESUME_OTHERID=1  thread/resume answers with another thread id
  FAKE_SANDBOX=<type>    report this sandbox type instead of the requested one
  FAKE_SETTINGS_MODEL=m  announce a model change (thread/settings/updated) mid-turn
  FAKE_EARLY_USAGE=1     send the thread's usage baseline before the resume response
  FAKE_STUBBORN=1        ignore SIGTERM and stdin EOF (a worker that will not die)
  FAKE_IGNORE_INTERRUPT=1  never complete a turn after turn/interrupt
Threads persist in $FAKE_STORE (one file per id), so resume works across
processes and unknown ids fail like the real server. Every client request is
appended to $FAKE_ARGS_LOG as one JSON line.
"""
import json
import os
import re
import sys
import threading
import time
import uuid

STORE = os.environ.get("FAKE_STORE", "/tmp/fake-codex-store")
os.makedirs(STORE, exist_ok=True)
out_lock = threading.Lock()
state = {"steer": [], "interrupt": False, "turn": None, "thread": None, "replies": {}, "done": False}
cond = threading.Condition()


def send(obj):
    with out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def note(method, params):
    send({"method": method, "params": params})


def log(msg):
    path = os.environ.get("FAKE_ARGS_LOG")
    if path:
        with open(path, "a") as f:
            f.write(json.dumps({"method": msg.get("method"), "params": msg.get("params")}) + "\n")


SANDBOX = {"read-only": "readOnly", "workspace-write": "workspaceWrite", "danger-full-access": "dangerFullAccess"}


def thread_result(tid, p):
    thread = {"id": tid, "ephemeral": bool(p.get("ephemeral")), "cliVersion": "0.0.0-fake", "createdAt": 0,
              "updatedAt": 0, "cwd": p.get("cwd") or os.getcwd(), "modelProvider": "fake", "preview": "",
              "projectId": None, "sessionId": tid, "source": "appServer", "status": {"type": "idle"}, "turns": []}
    return {"thread": thread, "model": p.get("model") or "default",
            "modelProvider": "fake", "cwd": p.get("cwd") or os.getcwd(),
            "approvalPolicy": p.get("approvalPolicy") or "on-request", "approvalsReviewer": "user",
            "sandbox": {"type": os.environ.get("FAKE_SANDBOX") or SANDBOX.get(p.get("sandbox"), "readOnly")},
            "reasoningEffort": "medium"}


REQUESTS = {
    "cmd": ("item/commandExecution/requestApproval", {"itemId": "i", "threadId": "t", "turnId": "u", "command": "rm -rf x"},
            lambda r: r.get("result") == {"decision": "decline"}),
    "file": ("item/fileChange/requestApproval", {"itemId": "i", "threadId": "t", "turnId": "u"},
             lambda r: r.get("result") == {"decision": "decline"}),
    "perm": ("item/permissions/requestApproval", {"itemId": "i", "threadId": "t", "turnId": "u", "permissions": {}},
             lambda r: r.get("result") == {"permissions": {}, "scope": "turn"}),
    "input": ("item/tool/requestUserInput", {"itemId": "i", "threadId": "t", "turnId": "u", "isBlocking": True,
                                             "questions": [{"id": "q1", "header": "h", "question": "?"},
                                                           {"id": "q2", "header": "h", "question": "?"}]},
              lambda r: r.get("result") == {"answers": {"q1": {"answers": []}, "q2": {"answers": []}}}),
    "elicit": ("mcpServer/elicitation/request", {"serverName": "x", "threadId": "t", "turnId": "u", "mode": "form",
                                                 "message": "?", "requestedSchema": {"type": "object", "properties": {}}},
               lambda r: r.get("result") == {"action": "decline", "content": None}),
    "tool": ("item/tool/call", {"tool": "x", "callId": "c", "threadId": "t", "turnId": "u", "arguments": {}},
             lambda r: (r.get("result") or {}).get("success") is False),
    "auth": ("account/chatgptAuthTokens/refresh", {}, lambda r: "error" in r),
    "attest": ("attestation/generate", {}, lambda r: "error" in r),
    "legacy": ("execCommandApproval", {"conversationId": "t", "callId": "c", "command": ["ls"], "cwd": "/",
                                       "parsedCmd": []}, lambda r: r.get("result") == {"decision": "abort"}),
    "unknown": ("brand/new/request", {}, lambda r: (r.get("error") or {}).get("code") == -32601),
}


def server_request(method, params, timeout=10):
    rid = "srv-" + uuid.uuid4().hex[:8]
    send({"id": rid, "method": method, "params": params})
    with cond:
        cond.wait_for(lambda: rid in state["replies"], timeout)
        return state["replies"].pop(rid, None)


def agent(tid, turn, text, phase="final_answer"):
    item = {"type": "agentMessage", "id": uuid.uuid4().hex[:8], "text": text, "phase": phase}
    note("item/completed", {"threadId": tid, "turnId": turn, "item": item})


def usage(tid, turn, total):
    u = {"inputTokens": total, "cachedInputTokens": 2, "outputTokens": 3, "reasoningOutputTokens": 1, "totalTokens": total + 3}
    note("thread/tokenUsage/updated", {"threadId": tid, "turnId": turn,
                                       "tokenUsage": {"total": u, "last": u, "modelContextWindow": 1000}})


def complete(tid, turn, status, error=None, items=None):
    state["done"] = True
    note("thread/tokenUsage/updated", {"threadId": tid, "turnId": turn, "tokenUsage": {
        "total": {"inputTokens": 10, "cachedInputTokens": 2, "outputTokens": 3, "reasoningOutputTokens": 1, "totalTokens": 13},
        "last": {"inputTokens": 10, "cachedInputTokens": 2, "outputTokens": 3, "reasoningOutputTokens": 1, "totalTokens": 13},
        "modelContextWindow": 1000}})
    t = {"id": turn, "status": status, "items": items or [], "error": {"message": error} if error else None}
    note("turn/completed", {"threadId": tid, "turn": t})


def run_turn(tid, turn, prompt):
    m = re.search(r"MODE:([a-z]+(?::[a-z0-9]+)?)", prompt)
    mode = m.group(1) if m else "ok"
    note("turn/started", {"threadId": tid, "turn": {"id": turn, "status": "inProgress", "items": []}})
    note("item/started", {"threadId": tid, "turnId": turn, "item": {"type": "commandExecution", "id": "c1",
                                                                     "command": "/bin/bash -lc 'ls -la'", "status": "inProgress"}})
    note("item/completed", {"threadId": tid, "turnId": turn, "item": {"type": "commandExecution", "id": "c1",
                                                                       "command": "/bin/bash -lc 'ls -la'", "exitCode": 2}})
    note("item/completed", {"threadId": tid, "turnId": turn, "item": {"type": "reasoning", "id": "r1",
                                                                       "summary": ["**Planning the change**\n\nbody"], "content": []}})
    note("item/completed", {"threadId": tid, "turnId": turn, "item": {"type": "fileChange", "id": "f1", "status": "completed",
                                                                       "changes": [{"path": "src/a.py", "kind": {"type": "update"}, "diff": ""}]}})
    note("item/agentMessage/delta", {"threadId": tid, "turnId": turn, "itemId": "x", "delta": "noise"})
    agent(tid, turn, "Looking around first.", "commentary")
    if os.environ.get("FAKE_SETTINGS_MODEL"):
        note("thread/settings/updated", {"threadId": tid, "threadSettings": {
            "model": os.environ["FAKE_SETTINGS_MODEL"], "approvalPolicy": "never", "approvalsReviewer": "user",
            "cwd": os.getcwd(), "modelProvider": "fake", "collaborationMode": {}, "sandboxPolicy": {"type": "workspaceWrite"}}})
        while not state["interrupt"]:
            time.sleep(0.1)
        return complete(tid, turn, "interrupted")
    if mode == "reconnect":
        note("error", {"threadId": tid, "turnId": turn, "willRetry": True,
                       "error": {"message": "Reconnecting... 1/5 (stream disconnected)"}})
    elif mode == "fail":
        note("error", {"threadId": tid, "turnId": turn, "willRetry": False, "error": {"message": "simulated model failure"}})
        return complete(tid, turn, "failed", "simulated model failure")
    elif mode == "crash":
        os._exit(3)
    elif mode == "hang":
        while not state["interrupt"]:
            time.sleep(0.1)
        if os.environ.get("FAKE_IGNORE_INTERRUPT"):
            time.sleep(3600)
        return complete(tid, turn, "interrupted")
    elif mode.startswith("sleep:"):
        for i in range(int(mode.split(":")[1])):
            if state["interrupt"]:
                agent(tid, turn, "partial work before stop", "commentary")
                return complete(tid, turn, "interrupted")
            if state["steer"]:
                text = state["steer"].pop(0)
                agent(tid, turn, "STEERED: %s" % text)
                return complete(tid, turn, "completed")
            note("item/started", {"threadId": tid, "turnId": turn, "item": {"type": "commandExecution", "id": "s%d" % i,
                                                                             "command": "sleep 1; echo %d" % i}})
            time.sleep(1)
    elif mode.startswith("request:"):
        method, params, check = REQUESTS[mode.split(":")[1]]
        r = server_request(method, params)
        if r is None or not check(r):
            return complete(tid, turn, "failed", "bad reply to %s: %s" % (method, json.dumps(r)))
    if mode == "itemsonly":
        return complete(tid, turn, "completed", items=[{"type": "agentMessage", "id": "fin", "phase": "final_answer",
                                                         "text": "reply from %s: %s" % (tid, prompt)}])
    if mode == "empty":
        agent(tid, turn, "")
    elif mode == "nophase":
        agent(tid, turn, "reply from %s: %s" % (tid, prompt), None)
    else:
        agent(tid, turn, "reply from %s: %s" % (tid, prompt))
    complete(tid, turn, "completed")


def main():
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, rid, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method is None and rid is not None:        # reply to a server request
            with cond:
                state["replies"][rid] = msg
                cond.notify_all()
            continue
        log(msg)
        if rid is None:
            continue
        if method == "initialize":
            send({"id": rid, "result": {"codexHome": "/x", "platformFamily": "unix", "platformOs": "linux", "userAgent": "fake"}})
        elif method == "thread/start":
            tid = str(uuid.uuid4())
            open(os.path.join(STORE, tid), "w").close()
            note("thread/started", {"thread": {"id": tid}})
            send({"id": rid, "result": thread_result(tid, p)})
        elif method == "thread/resume":
            tid = p.get("threadId")
            if not os.path.exists(os.path.join(STORE, str(tid))):
                send({"id": rid, "error": {"code": -32600, "message": "no rollout found for thread id %s" % tid}})
                continue
            if os.environ.get("FAKE_RESUME_OTHERID"):
                tid = str(uuid.uuid4())
            if os.environ.get("FAKE_EARLY_USAGE"):
                usage(tid, "old-turn", 4)
            send({"id": rid, "result": thread_result(tid, p)})
            if not os.environ.get("FAKE_EARLY_USAGE"):
                usage(tid, "old-turn", 4)
        elif method == "turn/start":
            tid = p["threadId"]
            turn = "turn-" + uuid.uuid4().hex[:6]
            state["turn"], state["thread"], state["done"] = turn, tid, False
            prompt = "".join(i.get("text", "") for i in p.get("input") or [])
            if "MODE:earlydone" in prompt:
                note("turn/started", {"threadId": tid, "turn": {"id": turn, "status": "inProgress", "items": []}})
                usage(tid, turn, 10)
                agent(tid, turn, "reply from %s: %s" % (tid, prompt))
                note("turn/completed", {"threadId": tid, "turn": {"id": turn, "status": "completed", "items": []}})
                send({"id": rid, "result": {"turn": {"id": turn, "status": "inProgress", "items": []}}})
                continue
            if "MODE:interleave" in prompt:
                note("warning", {"message": "before response"})
                threading.Thread(target=server_request, args=("item/fileChange/requestApproval", {"itemId": "early"}),
                                 daemon=True).start()
                time.sleep(0.3)
            send({"id": rid, "result": {"turn": {"id": turn, "status": "inProgress", "items": []}}})
            threading.Thread(target=run_turn, args=(tid, turn, prompt), daemon=True).start()
        elif method == "turn/steer":
            if state["done"]:
                send({"id": rid, "error": {"code": -32600, "message": "no active turn to steer"}})
            elif p.get("expectedTurnId") != state["turn"]:
                send({"id": rid, "error": {"code": -32600, "message": "expected turn mismatch"}})
            else:
                state["steer"].append("".join(i.get("text", "") for i in p.get("input") or []))
                send({"id": rid, "result": {"turnId": state["turn"]}})
        elif method == "turn/interrupt":
            state["interrupt"] = True
            send({"id": rid, "result": {}})
        else:
            send({"id": rid, "error": {"code": -32601, "message": "unknown method %s" % method}})


if __name__ == "__main__":
    if os.environ.get("FAKE_STUBBORN"):
        import signal
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    main()
    if os.environ.get("FAKE_STUBBORN"):
        time.sleep(3600)
