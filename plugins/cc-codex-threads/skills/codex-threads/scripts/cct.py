#!/usr/bin/env python3
"""cc-codex-threads: run OpenAI Codex CLI as named, resumable worker threads.

Each named thread (e.g. "luna", "sol") maps to one Codex thread id, stored per
project. A send to a known thread resumes that exact thread by id, so several
threads coexist without sharing context.

Engine: one private `codex app-server` (stdio JSON-RPC) per run. While a turn
runs, a private unix socket accepts `steer` and `interrupt` commands. The
legacy `codex exec` engine is still available with CCT_ENGINE=exec (no
steering).

Locks (fcntl.flock, per project): the gate is shared by every send and taken
exclusively by reset; each thread has its own lock, held by the runner for the
whole run. The runner owns the app-server: if the runner dies, the server's
stdin closes and it exits, and it also gets SIGTERM (Linux parent-death signal).

Usage: codex-thread.sh help
"""
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import queue
import re
import select
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

VERSION = "2.0.0"
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
HOME = os.path.expanduser("~")
STATE_DIR = os.path.abspath(os.environ.get("CCT_STATE_DIR") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state"), "cc-codex-threads"))
USER_PROFILES = os.environ.get("CCT_PROFILES") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "cc-codex-threads", "profiles.json")
DEFAULT_PROFILES = os.path.join(SCRIPT_DIR, "profiles.json")
CODEX = os.environ.get("CCT_CODEX") or "codex"

OK, USAGE, BUSY, FAILED, TIMEOUT, EMPTY, INTERRUPTED, NOT_RUNNING = 0, 2, 3, 4, 5, 6, 7, 8
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
SANDBOX_TYPES = {"read-only": "readOnly", "workspace-write": "workspaceWrite",
                 "danger-full-access": "dangerFullAccess"}
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
RUN_ID_RE = re.compile(r"^[0-9a-f]{12}$")
SECONDS_RE = re.compile(r"^[0-9]{1,9}$")
MAX_CTL_BYTES = 4 * 1024 * 1024

USAGE_TEXT = """codex-thread.sh — named, resumable Codex worker threads (v%s)

COMMANDS
  send <thread> [options] [--] [prompt...]
      Send a prompt to <thread>: resume its Codex thread in this project, or
      start one. The prompt is the trailing arguments (put "--" before a prompt
      that starts with "-"), --prompt-file <file>, or stdin.
      Options:
        --profile <name>   settings preset for a NEW thread (default: <thread>)
        --model <id>       override model        (new thread only)
        --effort <level>   override effort       (new thread only)
        --sandbox <mode>   read-only | workspace-write | danger-full-access
                           (new thread; on resume only read-only, to tighten)
        --new              discard the registered thread, start fresh
        --cd <dir>         project dir (default: current dir)
        --timeout <sec>    hard timeout (default depends on effort)
        --progress         print live progress lines before the reply
        --prompt-file <f>  read the prompt from a file
  steer <thread> [--cd <dir>] [--run <id>] [--] [text...]
      Add input to the turn that is running now (text, --prompt-file or stdin).
  interrupt <thread> [--cd <dir>] [--run <id>]
                                      stop the running turn
  status <thread> [--cd <dir>]        state of the thread's current/last run
  result <thread> [--cd <dir>] [--run <id>]
                                      reply of the last finished run
  watch  <thread> [--wait <sec>] [--max <sec>] [--cd <dir>]
                                      follow the live progress of the current
                                      run (or print the last run's progress)
  list   [--cd <dir>]                 threads registered for this project
  show   <thread> [--cd <dir>]        one thread's record
  reset  <thread> | --all [--cd <dir>]
                                      forget threads (Codex history is kept)
  profiles                            show effective profiles
  relay-init <profile> | relay-next <state> | relay <state> <msg> | relay-result <state>
                                      used by the relay subagents
  help | version

EXIT CODES
  0 ok | 2 usage | 3 busy | 4 failed | 5 timeout | 6 empty reply
  7 interrupted | 8 no running turn (steer/interrupt)
""" % VERSION


class CctError(Exception):
    def __init__(self, code, msg):
        Exception.__init__(self, msg)
        self.code = code
        self.msg = msg


def die(code, msg):
    raise CctError(code, msg)


def now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def valid_name(name, what="thread name"):
    if not NAME_RE.match(name or ""):
        die(USAGE, "invalid %s: '%s' (use A-Z a-z 0-9 . _ -, at most 64)" % (what, name))
    return name


def parse_seconds(value, flag, allow_zero=False):
    if not SECONDS_RE.match(value or ""):
        die(USAGE, "%s must be a whole number of seconds" % flag)
    n = int(value, 10)
    if n == 0 and not allow_zero:
        die(USAGE, "%s must be greater than 0" % flag)
    return n


def abs_dir(path):
    if not os.path.isdir(path):
        die(USAGE, "not a directory: %s" % path)
    return os.path.realpath(path)


def project_root(directory):
    try:
        r = subprocess.run(["git", "-C", directory, "rev-parse", "--show-toplevel"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
        root = r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        root = ""
    return os.path.realpath(root or directory)


def project_key(root):
    return hashlib.sha1(root.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def ensure_dir(path, mode=0o700):
    try:
        os.makedirs(path, mode=mode, exist_ok=True)
    except OSError as e:
        die(FAILED, "cannot create %s: %s" % (path, e))
    return path


def write_atomic(path, text):
    d = os.path.dirname(path)
    ensure_dir(d)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path, obj):
    write_atomic(path, json.dumps(obj, ensure_ascii=False, indent=1) + "\n")


def read_json(path):
    """Return the parsed JSON object, or None if the file does not exist."""
    try:
        with open(path, encoding="utf-8") as f:
            data = f.read()
    except FileNotFoundError:
        return None
    return json.loads(data)


def read_text(path, limit=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read() if limit is None else f.read(limit)
    except FileNotFoundError:
        return ""


def tail_text(path, n=3):
    lines = [l for l in read_text(path).splitlines() if l.strip()]
    return " | ".join(lines[-n:])


def short(text, n):
    text = re.sub(r"[\r\n\t]+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[:n] + "…"


# ---------------------------------------------------------------- locks

class Lock(object):
    def __init__(self, path):
        ensure_dir(os.path.dirname(path))
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)

    def acquire(self, exclusive=True, wait=0.0):
        op = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(self.fd, op)
                return True
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def release(self):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


# ---------------------------------------------------------------- project / registry

class Project(object):
    def __init__(self, directory):
        self.dir = abs_dir(directory)
        self.root = project_root(self.dir)
        self.key = project_key(self.root)
        self.registry = os.path.join(STATE_DIR, "projects", self.key + ".json")
        self.locks = os.path.join(STATE_DIR, "locks")

    def gate(self):
        return Lock(os.path.join(self.locks, self.key + ".gate.lock"))

    def thread_lock(self, thread):
        return Lock(os.path.join(self.locks, "%s-%s.lock" % (self.key, thread)))

    def registry_lock(self):
        lk = Lock(os.path.join(self.locks, self.key + ".registry.lock"))
        if not lk.acquire(True, 15):
            die(BUSY, "registry lock busy (another cct process is stuck?)")
        return lk

    def pointer(self, thread):
        return os.path.join(STATE_DIR, "runs", "current", "%s-%s" % (self.key, thread))

    def load(self):
        """Missing file -> empty registry. Invalid content -> failure, never a silent reset."""
        try:
            reg = read_json(self.registry)
        except (ValueError, OSError):
            die(FAILED, "registry is corrupt: %s (inspect it, or run: reset --all)" % self.registry)
        if reg is None:
            return {"root": self.root, "threads": {}}
        ok = isinstance(reg, dict) and reg.get("root") == self.root and isinstance(reg.get("threads"), dict)
        if ok:
            for rec in reg["threads"].values():
                if not (isinstance(rec, dict)
                        and all(isinstance(rec.get(k), str) and rec.get(k) for k in
                                ("thread_id", "model", "effort", "sandbox", "cwd"))
                        and isinstance(rec.get("turns"), int) and not isinstance(rec.get("turns"), bool)
                        and rec["turns"] >= 0):
                    ok = False
                    break
        if not ok:
            die(FAILED, "registry is corrupt: %s (inspect it, or run: reset --all)" % self.registry)
        return reg

    def update_thread(self, thread, fields, count_turn=False, fresh=False):
        lk = self.registry_lock()
        try:
            reg = self.load()
            now = now_utc()
            if not fresh and thread not in reg["threads"]:
                return
            rec = {} if fresh else dict(reg["threads"][thread])
            rec.setdefault("created", now)
            rec.setdefault("turns", 0)
            rec.update(fields)
            rec["updated"] = now
            if count_turn:
                rec["turns"] += 1
            reg["threads"][thread] = rec
            write_json(self.registry, reg)
        finally:
            lk.close()


# ---------------------------------------------------------------- profiles

def deep_merge(a, b):
    out = dict(a)
    for k, v in b.items():
        out[k] = deep_merge(out[k], v) if isinstance(out.get(k), dict) and isinstance(v, dict) else v
    return out


def load_profiles():
    merged = {}
    for path in (DEFAULT_PROFILES, USER_PROFILES):
        try:
            data = read_json(path)
        except (ValueError, OSError):
            die(USAGE, "invalid profiles JSON: %s" % path)
        if data is None:
            continue
        if not isinstance(data, dict):
            die(USAGE, "invalid profiles JSON (expected an object): %s" % path)
        merged = deep_merge(merged, data)
    return merged


def stop_grace():
    """Seconds a turn gets to stop after an interrupt before it is killed."""
    v = os.environ.get("CCT_STOP_GRACE", "30")
    return int(v, 10) if SECONDS_RE.match(v) else 30


def default_timeout(effort):
    return {"none": 300, "minimal": 300, "low": 300, "medium": 600, "high": 1200,
            "xhigh": 2400, "max": 2400}.get(effort, 3600)


# ---------------------------------------------------------------- runs

HAVE_PROC = os.path.isdir("/proc/self")


def proc_start_time(pid):
    """Kernel start time of a live process. Returns '' where /proc is not
    available (identity unknown) and None if the process is gone or a zombie."""
    if not HAVE_PROC:
        return ""
    try:
        with open("/proc/%d/stat" % pid) as f:
            stat = f.read()
        fields = stat[stat.rindex(")") + 2:].split()
        return None if fields[0] in ("Z", "X") else fields[19]
    except FileNotFoundError:
        return None
    except (OSError, ValueError, IndexError):
        return ""


def pid_alive(pid, start):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    st = proc_start_time(pid)
    if st is None:
        return False
    return not start or not st or st == start


def run_dir(run_id):
    return os.path.join(STATE_DIR, "runs", run_id)


def load_run(run_id):
    if not RUN_ID_RE.match(run_id or ""):
        return None
    try:
        rec = read_json(os.path.join(run_dir(run_id), "run.json"))
    except (ValueError, OSError):
        return None
    return rec if isinstance(rec, dict) and rec.get("run_id") == run_id else None


def run_alive(rec):
    return bool(rec) and rec.get("state") != "done" and pid_alive(rec.get("pid"), rec.get("pid_start"))


def current_run(project, thread):
    run_id = read_text(project.pointer(thread)).strip()
    return load_run(run_id) if run_id else None


def prune_runs(max_age_days=14):
    base = os.path.join(STATE_DIR, "runs")
    cutoff = time.time() - max_age_days * 86400
    try:
        names = os.listdir(base)
    except OSError:
        return
    for name in names:
        if not RUN_ID_RE.match(name):
            continue
        path = os.path.join(base, name)
        try:
            if os.path.getmtime(path) < cutoff and not run_alive(load_run(name)):
                for f in os.listdir(path):
                    os.unlink(os.path.join(path, f))
                os.rmdir(path)
        except OSError:
            pass


def sock_dir():
    d = os.environ.get("CCT_SOCK_DIR") or os.path.join(tempfile.gettempdir(), "cct-%d" % os.getuid())
    ensure_dir(d)
    st = os.lstat(d)
    if st.st_uid != os.getuid() or (st.st_mode & 0o077):
        try:
            os.chmod(d, 0o700)
            st = os.lstat(d)
        except OSError:
            pass
        if st.st_uid != os.getuid() or (st.st_mode & 0o077):
            die(FAILED, "socket directory %s is not private to this user" % d)
    return d


# ---------------------------------------------------------------- progress

class Progress(object):
    def __init__(self, path, echo):
        self.f = open(path, "a", encoding="utf-8", buffering=1)
        self.echo = echo

    def __call__(self, line):
        line = "%s %s" % (time.strftime("%H:%M:%S"), line)
        try:
            self.f.write(line + "\n")
        except (OSError, ValueError):
            pass
        if self.echo:
            try:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
            except (OSError, ValueError):
                pass

    def close(self):
        try:
            self.f.close()
        except OSError:
            pass


def strip_shell(cmd):
    if isinstance(cmd, list):
        cmd = " ".join(str(c) for c in cmd)
    m = re.match(r"^/bin/(?:ba)?sh -l?c (['\"])(.*)\1$", cmd or "", re.S)
    return m.group(2) if m else (cmd or "")


def reasoning_heading(texts):
    joined = "\n".join(t for t in texts if isinstance(t, str))
    heads = re.findall(r"\*\*([^*]+)\*\*", joined)
    if heads:
        return " / ".join(heads[:2])
    first = joined.strip().splitlines()
    return first[0] if first else ""


# ---------------------------------------------------------------- control socket

class ControlServer(object):
    """Unix stream socket: one JSON request line in, one JSON reply line out."""

    def __init__(self, path, inbox):
        self.path = path
        self.inbox = inbox
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            self.sock.bind(path)
        finally:
            os.umask(old)
        self.sock.listen(8)
        self.closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.closed:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._read, args=(conn,), daemon=True).start()

    def _read(self, conn):
        try:
            conn.settimeout(10)
            buf = b""
            while b"\n" not in buf and len(buf) <= MAX_CTL_BYTES:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            if len(buf) > MAX_CTL_BYTES:
                raise ValueError("request too large")
            req = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError("request must be an object")
        except Exception as e:  # malformed client: answer and drop
            reply(conn, {"ok": False, "outcome": "rejected", "error": "bad request: %s" % e})
            return
        self.inbox.put(("ctl", (req, conn)))

    def close(self):
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


def reply(conn, obj):
    try:
        conn.settimeout(10)
        conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def control_request(rec, op, text=None, wait=60):
    """Send one command to a live run. Returns the runner's reply dict.

    outcome: delivered / interrupt_sent / ... on success; not_running when the
    run has certainly ended; unknown when the result cannot be known."""
    req = {"op": op, "request_id": uuid.uuid4().hex, "run_id": rec["run_id"]}
    if text is not None:
        req["text"] = text
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(wait)
    try:
        s.connect(rec["socket"])
    except OSError as e:
        s.close()
        if not run_alive(load_run(rec["run_id"])):
            return {"ok": False, "outcome": "not_running", "error": "run %s has ended" % rec["run_id"]}
        return {"ok": False, "outcome": "unknown", "error": "cannot reach run %s: %s" % (rec["run_id"], e)}
    try:
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        if not buf:
            return {"ok": False, "outcome": "unknown", "error": "runner closed the connection without answering"}
        return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    except (OSError, ValueError) as e:
        return {"ok": False, "outcome": "unknown", "error": "no answer from run %s (%s); the command may or may not have been applied" % (rec["run_id"], e)}
    finally:
        s.close()


# ---------------------------------------------------------------- engines

try:
    _LIBC = ctypes.CDLL(None, use_errno=True) if sys.platform.startswith("linux") else None
except OSError:
    _LIBC = None


def _child_setup():
    # Runs between fork and exec. cmd_send spawns the child before it starts
    # any thread, and this only makes one libc call (loaded beforehand).
    if _LIBC is not None:
        _LIBC.prctl(1, signal.SIGTERM, 0, 0, 0)  # PR_SET_PDEATHSIG


class Child(object):
    """A codex process in its own session/process group.

    stdout lines go to the inbox; stdin is written only by a dedicated writer
    thread, so a child that stops reading cannot block the runner's loop."""

    def __init__(self, argv, cwd, inbox, stderr_path, stdin_data=None):
        self.inbox = inbox
        self.err = open(stderr_path, "ab")
        try:
            self.proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=self.err, start_new_session=True, preexec_fn=_child_setup,
                                         close_fds=True)
        except OSError as e:
            self.err.close()
            die(FAILED, "cannot start %s: %s" % (argv[0], e))
        self.start_time = proc_start_time(self.proc.pid) or ""
        self.outq = queue.Queue()
        self.writer_dead = False
        threading.Thread(target=self._writer, daemon=True).start()
        threading.Thread(target=self._pump, daemon=True).start()
        if stdin_data is not None:
            self.outq.put(stdin_data)
            self.outq.put(None)

    def _writer(self):
        try:
            while True:
                data = self.outq.get()
                if data is None:
                    break
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
        except (OSError, ValueError):
            pass
        finally:
            self.writer_dead = True
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass

    def _pump(self):
        for line in self.proc.stdout:
            self.inbox.put(("line", line))
        self.inbox.put(("eof", None))

    def write(self, obj):
        if self.writer_dead:
            raise OSError("app-server stdin is closed")
        self.outq.put((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    def signal_group(self, sig):
        try:
            os.killpg(self.proc.pid, sig)
        except OSError:
            pass

    def stop(self, grace=5.0):
        """EOF first (app-server exits on it), then TERM, then KILL the group."""
        self.outq.put(None)
        for sig, wait in ((None, grace), (signal.SIGTERM, 3.0), (signal.SIGKILL, 3.0)):
            if sig is not None:
                self.signal_group(sig)
            try:
                self.proc.wait(wait)
                break
            except subprocess.TimeoutExpired:
                continue
        self.signal_group(signal.SIGKILL)   # stragglers left in the group
        try:
            self.err.close()
        except OSError:
            pass


def reap_previous(project, thread):
    """A runner killed with SIGKILL releases the thread lock at once, while its
    worker may take a moment to exit. Never let two workers share a thread."""
    prev = current_run(project, thread)
    pid, start = (prev or {}).get("worker_pid"), (prev or {}).get("worker_start")
    if not pid_alive(pid, start):
        return
    wait = int(os.environ.get("CCT_REAP_WAIT", "10"))
    end = time.monotonic() + wait
    while time.monotonic() < end:
        time.sleep(0.2)
        if not pid_alive(pid, start):
            return
    if not start:   # cannot prove the pid is still ours: never signal it
        die(BUSY, "the previous worker of thread '%s' (pid %s) is still running; retry later" % (thread, pid))
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except OSError:
            pass
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if not pid_alive(pid, start):
                return
            time.sleep(0.2)
    die(BUSY, "the previous worker of thread '%s' (pid %s) does not exit" % (thread, pid))


class Rejected(Exception):
    pass


# Replies to server->client requests. A non-interactive runner never grants
# anything: no approvals, no extra permissions, no user answers, no tools.
def server_request_reply(method, params):
    if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        return {"decision": "decline"}, "declined %s" % method
    if method == "item/permissions/requestApproval":
        return {"permissions": {}, "scope": "turn"}, "granted no extra permissions"
    if method == "item/tool/requestUserInput":
        qs = (params or {}).get("questions") or []
        answers = {q["id"]: {"answers": []} for q in qs if isinstance(q, dict) and isinstance(q.get("id"), str)}
        return {"answers": answers}, "answered %d question(s) with no input (non-interactive)" % len(answers)
    if method == "mcpServer/elicitation/request":
        return {"action": "decline", "content": None}, "declined MCP elicitation"
    if method == "item/tool/call":
        return ({"success": False, "contentItems": [{"type": "inputText", "text": "Dynamic tools are not supported by this runner."}]},
                "refused dynamic tool call")
    if method in ("applyPatchApproval", "execCommandApproval"):
        return {"decision": "abort"}, "aborted legacy %s" % method
    return None, "unsupported server request %s" % method


TERMINAL = ("completed", "interrupted", "failed")


class AppServerRun(object):
    """Drives one turn through a private `codex app-server`."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.inbox = ctx.inbox
        self.child = None
        self.next_id = 0
        self.pending = {}          # request id -> (method, callback or None)
        self.responses = {}        # request id -> message, for synchronous calls
        self.thread_id = None
        self.turn_id = None
        self.turn_done = None      # final Turn object
        self.early_done = {}       # turn id -> Turn that completed before turn/start answered
        self.messages = {}         # agentMessage item id -> [order, phase, text]
        self.deltas = {}           # item id -> [order, text] for messages never completed
        self.order = 0
        self.usage_log = []        # (turn id, tokenUsage) in arrival order
        self.retries = 0
        self.last_error = ""
        self.queued_steers = []    # (text, conn) received before the turn id was known
        self.failed_steers = []    # (conn, error, time): answered once the turn's fate is known
        self.interrupt_sent = False
        self.violation = None
        self.eof = False

    def spawn(self):
        self.child = Child([CODEX, "app-server"], self.ctx.cwd, self.inbox, self.ctx.stderr_path)
        return self.child

    # -- transport
    def request(self, method, params, callback=None):
        self.next_id += 1
        rid = self.next_id
        self.pending[rid] = (method, callback)
        self.child.write({"id": rid, "method": method, "params": params})
        return rid

    def call(self, method, params, timeout):
        c = self.ctx
        rid = self.request(method, params)
        own_deadline = time.monotonic() + timeout
        while rid not in self.responses:
            if self.eof:
                die(FAILED, "codex app-server exited during %s: %s" % (method, tail_text(c.stderr_path)))
            if c.stop_reason is None and time.monotonic() >= c.deadline:
                c.stop_reason = "timeout"
            if c.stop_reason:
                die(TIMEOUT if c.stop_reason == "timeout" else INTERRUPTED,
                    "stopped during %s (%s)" % (method, c.stop_reason))
            if time.monotonic() >= own_deadline:
                die(FAILED, "codex app-server did not answer %s within %ds" % (method, timeout))
            self.step(0.25)
        msg = self.responses.pop(rid)
        if "error" in msg:
            err = msg.get("error") or {}
            raise Rejected("%s failed: %s" % (method, err.get("message") or err))
        return msg.get("result") or {}

    def step(self, timeout):
        try:
            kind, data = self.inbox.get(timeout=timeout)
        except queue.Empty:
            return
        if kind == "line":
            self.ctx.events.write(data)
            try:
                msg = json.loads(data.decode("utf-8"))
            except ValueError:
                self.ctx.progress("⚠ unparseable line from app-server")
                return
            if isinstance(msg, dict):
                self.dispatch(msg)
        elif kind == "eof":
            self.eof = True
        elif kind == "ctl":
            self.control(*data)
        elif kind == "signal":
            self.ctx.stop_reason = self.ctx.stop_reason or "signal"

    def dispatch(self, msg):
        method = msg.get("method")
        if method is None and "id" in msg:              # response to our request
            entry = self.pending.pop(msg["id"], None)
            if entry is None:
                return
            if entry[1] is not None:
                entry[1](msg)
            else:
                self.responses[msg["id"]] = msg
        elif method is not None and "id" in msg:        # server -> client request
            result, note = server_request_reply(method, msg.get("params"))
            answer = {"id": msg["id"], "result": result} if result is not None else \
                {"id": msg["id"], "error": {"code": -32601, "message": note}}
            try:
                self.child.write(answer)
            except OSError:
                pass
            self.ctx.progress("⚠ %s" % note)
        elif method is not None:
            self.notify(method, msg.get("params") or {})

    # -- notifications
    def next_order(self):
        self.order += 1
        return self.order

    def notify(self, method, p):
        out = self.ctx.progress
        tid = p.get("threadId")
        if tid is not None and self.thread_id is not None and tid != self.thread_id:
            return
        if method == "thread/tokenUsage/updated":
            self.usage_log.append((p.get("turnId"), p.get("tokenUsage") or {}))
            return
        if method == "turn/completed":
            turn = p.get("turn") or {}
            if self.turn_id is None:
                self.early_done[turn.get("id")] = turn
            elif turn.get("id") == self.turn_id:
                self.set_done(turn)
            return
        if method == "thread/settings/updated":
            self.check_live_settings(p.get("threadSettings") or {})
            return
        turn = p.get("turnId")
        if turn is not None and self.turn_id is not None and turn != self.turn_id:
            return
        if method == "turn/started":
            out("● turn started")
        elif method == "item/started":
            item = p.get("item") or {}
            if item.get("type") == "commandExecution":
                out("$ %s" % short(strip_shell(item.get("command")), 160))
        elif method == "item/completed":
            self.item_completed(p.get("item") or {})
        elif method == "item/agentMessage/delta":
            iid = p.get("itemId")
            if isinstance(iid, str) and isinstance(p.get("delta"), str):
                self.deltas.setdefault(iid, [self.next_order(), ""])[1] += p["delta"]
        elif method == "error":
            err = p.get("error") or {}
            text = short(err.get("message") or "error", 200)
            if p.get("willRetry"):
                self.retries += 1
                out("⚠ %s (retrying)" % text)
            else:
                self.last_error = err.get("message") or "error"
                out("⚠ %s" % text)
        elif method == "thread/compacted":
            out("… context compacted")
        elif method == "model/rerouted":
            out("⚠ model rerouted: %s" % short(json.dumps(p, ensure_ascii=False), 200))

    def add_message(self, item):
        iid = item.get("id") or "anon-%d" % self.next_order()
        text = item.get("text") if isinstance(item.get("text"), str) else ""
        if iid in self.messages:
            entry = self.messages[iid]
            entry[1] = item.get("phase") or entry[1]
            if text:
                entry[2] = text
        else:
            self.messages[iid] = [self.next_order(), item.get("phase"), text]
        return text

    def item_completed(self, item):
        out = self.ctx.progress
        t = item.get("type")
        if t == "commandExecution":
            code = item.get("exitCode")
            if isinstance(code, int) and code != 0:
                out("  ↳ exit %d" % code)
        elif t == "reasoning":
            head = reasoning_heading(item.get("summary") or [])
            if head:
                out("💭 %s" % short(head, 160))
        elif t == "fileChange":
            parts = []
            for ch in item.get("changes") or []:
                kind = ((ch.get("kind") or {}).get("type") or "edit") if isinstance(ch, dict) else "edit"
                parts.append("%s %s" % (kind, ch.get("path") if isinstance(ch, dict) else "?"))
            out("✎ %s%s" % (short(", ".join(parts), 200), "" if item.get("status") in (None, "completed") else " (%s)" % item.get("status")))
        elif t == "mcpToolCall":
            out("⚙ %s.%s" % (item.get("server") or "mcp", item.get("tool") or "?"))
        elif t == "webSearch":
            out("🔎 %s" % short(item.get("query") or "", 120))
        elif t == "agentMessage":
            text = self.add_message(item)
            if item.get("phase") != "final_answer":
                out("💬 %s" % short(text, 240))
        elif t == "contextCompaction":
            out("… context compacted")

    def set_done(self, turn):
        self.turn_done = turn
        for item in turn.get("items") or []:
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                self.add_message(item)
        for conn, err, _ in self.failed_steers:
            reply(conn, {"ok": False, "outcome": "not_running", "error": "the turn ended first (%s)" % err})
        self.failed_steers = []

    def check_live_settings(self, ts):
        c = self.ctx
        problems = []
        if ts.get("model") not in (None, c.model):
            problems.append("model %s" % ts.get("model"))
        sb = (ts.get("sandboxPolicy") or {}).get("type")
        if sb is not None and sb != SANDBOX_TYPES[c.sandbox]:
            problems.append("sandbox %s" % sb)
        if ts.get("approvalPolicy") not in (None, "never"):
            problems.append("approval policy %s" % ts.get("approvalPolicy"))
        if ts.get("approvalsReviewer") not in (None, "user"):
            problems.append("approvals reviewer %s" % ts.get("approvalsReviewer"))
        if problems and self.violation is None:
            self.violation = "codex changed the thread settings mid-run (%s); stopped" % ", ".join(problems)
            c.progress("⚠ " + self.violation)
            c.stop_reason = c.stop_reason or "violation"
        if self.turn_id is not None and ts.get("effort") not in (None, c.effort):
            c.progress("⚠ codex reports effort %s (requested %s)" % (ts.get("effort"), c.effort))

    # -- control commands from steer/interrupt/status clients
    def control(self, req, conn):
        c = self.ctx
        op = req.get("op")
        if req.get("run_id") != c.run_id:
            return reply(conn, {"ok": False, "outcome": "rejected", "error": "this socket serves run %s" % c.run_id})
        if op == "status":
            return reply(conn, {"ok": True, "outcome": "status", "state": c.state,
                                "thread_id": self.thread_id, "turn_id": self.turn_id})
        if op not in ("steer", "interrupt"):
            return reply(conn, {"ok": False, "outcome": "rejected", "error": "unknown op %r" % op})
        if self.turn_done is not None or c.stop_reason:
            return reply(conn, {"ok": False, "outcome": "not_running", "error": "the turn has ended or is stopping"})
        if op == "steer":
            text = req.get("text")
            if not isinstance(text, str) or not text.strip():
                return reply(conn, {"ok": False, "outcome": "rejected", "error": "empty steer text"})
            if self.turn_id is None:
                self.queued_steers.append((text, conn))
                return None
            return self.send_steer(text, conn)
        c.user_interrupt = True
        c.stop_reason = "interrupt"
        if self.turn_id is None:
            c.progress("■ interrupt requested before the turn started")
            return reply(conn, {"ok": True, "outcome": "interrupted"})
        return self.send_interrupt(conn)

    def send_steer(self, text, conn):
        self.ctx.progress("↪ steer: %s" % short(text, 200))

        def done(msg):
            if "error" not in msg:
                return reply(conn, {"ok": True, "outcome": "delivered", "turn_id": (msg.get("result") or {}).get("turnId")})
            err = (msg.get("error") or {}).get("message") or "rejected"
            if self.turn_done is not None:
                return reply(conn, {"ok": False, "outcome": "not_running", "error": "the turn ended first (%s)" % err})
            # The rejection may be a turn that is just ending: decide once its
            # fate is known (set_done), or report it as rejected after a moment.
            self.failed_steers.append((conn, err, time.monotonic()))
        try:
            self.request("turn/steer", {"threadId": self.thread_id, "expectedTurnId": self.turn_id,
                                        "input": [{"type": "text", "text": text}]}, done)
        except OSError as e:
            reply(conn, {"ok": False, "outcome": "unknown", "error": "app-server pipe closed: %s" % e})

    def expire_failed_steers(self):
        now = time.monotonic()
        keep = []
        for conn, err, when in self.failed_steers:
            if now - when >= 3:
                self.ctx.progress("⚠ steer rejected: %s" % short(err, 200))
                reply(conn, {"ok": False, "outcome": "rejected", "error": err})
            else:
                keep.append((conn, err, when))
        self.failed_steers = keep

    def send_interrupt(self, conn=None):
        if self.interrupt_sent:
            if conn is not None:
                reply(conn, {"ok": True, "outcome": "interrupt_pending"})
            return
        self.interrupt_sent = True
        self.ctx.progress("■ interrupt requested")

        def done(msg):
            if conn is None:
                return
            if "error" in msg:
                reply(conn, {"ok": False, "outcome": "rejected", "error": (msg.get("error") or {}).get("message")})
            else:
                reply(conn, {"ok": True, "outcome": "interrupt_sent"})
        try:
            self.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id}, done)
        except OSError as e:
            if conn is not None:
                reply(conn, {"ok": False, "outcome": "unknown", "error": str(e)})

    # -- the run
    def thread_params(self):
        c = self.ctx
        return {"model": c.model, "cwd": c.cwd, "sandbox": c.sandbox,
                "approvalPolicy": "never", "approvalsReviewer": "user"}

    def check_settings(self, res, expect_id):
        """Fail closed: the server must confirm exactly the settings we asked for."""
        c = self.ctx
        got_id = (res.get("thread") or {}).get("id")
        if not isinstance(got_id, str) or not UUID_RE.match(got_id):
            die(FAILED, "codex did not report a valid thread id")
        if expect_id and got_id.lower() != expect_id.lower():
            die(FAILED, "codex resumed a different thread (%s, expected %s)" % (got_id, expect_id))
        problems = []
        if res.get("model") != c.model:
            problems.append("model %r instead of %s" % (res.get("model"), c.model))
        sb = (res.get("sandbox") or {}).get("type")
        if sb != SANDBOX_TYPES[c.sandbox]:
            problems.append("sandbox %r instead of %s" % (sb, c.sandbox))
        if res.get("approvalPolicy") != "never":
            problems.append("approval policy %r instead of never" % (res.get("approvalPolicy"),))
        if res.get("approvalsReviewer") != "user":
            problems.append("approvals reviewer %r instead of user" % (res.get("approvalsReviewer"),))
        cwd = res.get("cwd")
        if not isinstance(cwd, str) or os.path.realpath(cwd) != os.path.realpath(c.cwd):
            problems.append("cwd %r instead of %s" % (cwd, c.cwd))
        if problems:
            die(FAILED, "codex applied different settings (%s); no turn was started" % "; ".join(problems))
        return got_id

    def run(self):
        c = self.ctx
        try:
            self.call("initialize", {"clientInfo": {"name": "cc-codex-threads", "title": "cc-codex-threads",
                                                    "version": VERSION}}, 60)
            self.child.write({"method": "initialized", "params": {}})
            if c.mode == "resume":
                params = dict(self.thread_params(), threadId=c.thread_id, excludeTurns=True)
                res = self.call("thread/resume", params, 120)
                self.thread_id = self.check_settings(res, c.thread_id)
            else:
                res = self.call("thread/start", dict(self.thread_params(), ephemeral=False), 120)
                self.thread_id = self.check_settings(res, None)
                # Register before the first turn, so an interrupted or failed
                # first turn can still be continued on the same thread.
                c.registered(self.thread_id)
            c.progress("● %s %s (%s/%s, %s)" % ("resumed" if c.mode == "resume" else "session", self.thread_id,
                                              c.model, c.effort, c.sandbox))
            res = self.call("turn/start", {"threadId": self.thread_id, "effort": c.effort, "model": c.model,
                                           "input": [{"type": "text", "text": c.prompt}]}, 120)
            turn = res.get("turn") or {}
            self.turn_id = turn.get("id")
            if not isinstance(self.turn_id, str) or not self.turn_id:
                die(FAILED, "codex did not report a turn id")
            c.started_turn(self.thread_id, self.turn_id)
            for item in turn.get("items") or []:
                if isinstance(item, dict) and item.get("type") == "agentMessage":
                    self.add_message(item)
            if self.turn_id in self.early_done:
                self.set_done(self.early_done.pop(self.turn_id))
            elif turn.get("status") in TERMINAL:
                self.set_done(turn)
            queued, self.queued_steers = self.queued_steers, []
            for text, conn in queued:
                if self.turn_done is not None or c.stop_reason:
                    reply(conn, {"ok": False, "outcome": "not_running", "error": "the turn has ended or is stopping"})
                else:
                    self.send_steer(text, conn)
            return self.wait_turn()
        except Rejected as e:
            die(FAILED, str(e))
        finally:
            for text, conn in self.queued_steers:
                reply(conn, {"ok": False, "outcome": "not_running", "error": "the turn never started"})
            for conn, err, _ in self.failed_steers:
                reply(conn, {"ok": False, "outcome": "not_running", "error": "the turn ended first (%s)" % err})
            self.queued_steers, self.failed_steers = [], []

    def wait_turn(self):
        c = self.ctx
        stop_deadline = None
        while self.turn_done is None:
            if self.eof:
                die(FAILED, "codex app-server exited before the turn completed: %s" % tail_text(c.stderr_path))
            now = time.monotonic()
            if c.stop_reason is None and now >= c.deadline:
                c.stop_reason = "timeout"
                c.progress("⏱ timed out after %ds; interrupting" % c.timeout)
            if c.stop_reason and stop_deadline is None:
                self.send_interrupt()
                stop_deadline = now + stop_grace()
            if stop_deadline is not None and now >= stop_deadline:
                c.progress("■ the turn did not stop within %ds; killing codex" % stop_grace())
                break
            self.expire_failed_steers()
            self.step(0.25)
        return self.finish()

    def final_text(self):
        msgs = sorted(self.messages.values())
        finals = [m for m in msgs if m[1] == "final_answer"]
        if finals:
            return finals[-1][2]
        if msgs:
            return msgs[-1][2]
        streamed = sorted(v for k, v in self.deltas.items() if k not in self.messages)
        return streamed[-1][1] if streamed else ""

    def turn_usage(self):
        mine = [u for t, u in self.usage_log if t == self.turn_id]
        if not mine:
            return "in=? cached=? out=? reasoning=? ctx=?"
        base = None
        for t, u in self.usage_log:
            if t == self.turn_id:
                break
            base = u
        total = mine[-1].get("total") or {}
        btotal = (base or {}).get("total") or ({} if self.ctx.mode == "new" else None)

        def tokens(key):
            v = total.get(key)
            if btotal is None or not isinstance(v, int):
                return "?"
            b = btotal.get(key, 0)
            return v - b if isinstance(b, int) else "?"
        last = mine[-1].get("last") or {}
        return "in=%s cached=%s out=%s reasoning=%s ctx=%s/%s" % (
            tokens("inputTokens"), tokens("cachedInputTokens"), tokens("outputTokens"),
            tokens("reasoningOutputTokens"), last.get("inputTokens", "?"), mine[-1].get("modelContextWindow", "?"))

    def finish(self):
        c = self.ctx
        turn = self.turn_done or {}
        status = turn.get("status") if self.turn_done is not None else "unfinished"
        text = self.final_text()
        c.usage, c.retries = self.turn_usage(), self.retries
        if self.violation:
            c.progress("✗ stopped: settings changed")
            return FAILED, text, self.violation
        if c.stop_reason == "timeout":
            c.progress("✗ timed out")
            return TIMEOUT, text, "timed out after %ds. Split the task or raise --timeout" % c.timeout
        if c.stop_reason in ("signal", "interrupt"):
            c.progress("■ interrupted")
            return INTERRUPTED, text, "turn interrupted (%s)" % ("by request" if c.user_interrupt else "by signal")
        if status == "completed":
            c.progress("✓ done")
            if not text.strip():
                return EMPTY, text, "codex finished but returned an empty reply"
            return OK, text, ""
        if status == "failed":
            err = (turn.get("error") or {}).get("message") or self.last_error or "turn failed"
            c.progress("✗ failed: %s" % short(err, 200))
            return FAILED, text, "codex turn failed: %s" % err
        if status == "interrupted":
            c.progress("■ interrupted by codex")
            return INTERRUPTED, text, "turn interrupted by codex"
        return FAILED, text, "turn ended with unexpected status %r" % status


class ExecRun(object):
    """Legacy engine: `codex exec --json`. No steering; interrupt stops the process."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.inbox = ctx.inbox
        self.child = None
        self.thread_id = None
        self.completed = False
        self.failed = ""
        self.usage = ""
        self.retries = 0
        self.eof = False
        self.last = os.path.join(ctx.dir, "last-message.txt")

    def spawn(self):
        c = self.ctx
        common = ["--json", "--skip-git-repo-check", "-m", c.model, "-c", 'model_reasoning_effort="%s"' % c.effort,
                  "-c", 'approval_policy="never"', "-o", self.last]
        if c.mode == "resume":
            argv = [CODEX, "exec", "resume", c.thread_id] + common + ["-c", 'sandbox_mode="%s"' % c.sandbox, "-"]
        else:
            argv = [CODEX, "exec"] + common + ["-s", c.sandbox, "-"]
        self.child = Child(argv, c.cwd, self.inbox, c.stderr_path, stdin_data=c.prompt.encode("utf-8"))
        return self.child

    def run(self):
        c = self.ctx
        child = self.child
        c.started_turn(c.thread_id, None)
        stop_at = hard_end = None
        while not self.eof:
            now = time.monotonic()
            if c.stop_reason is None and now >= c.deadline:
                c.stop_reason = "timeout"
            if c.stop_reason and stop_at is None:
                c.progress("■ stopping (%s)" % c.stop_reason)
                child.signal_group(signal.SIGINT)
                stop_at = now + stop_grace()
            if stop_at is not None and hard_end is None and now >= stop_at:
                child.signal_group(signal.SIGKILL)
                hard_end = now + 5
            if hard_end is not None and now >= hard_end and child.proc.poll() is not None:
                break   # a detached descendant may still hold stdout open
            try:
                kind, data = self.inbox.get(timeout=0.25)
            except queue.Empty:
                continue
            if kind == "line":
                c.events.write(data)
                try:
                    self.event(json.loads(data.decode("utf-8")))
                except ValueError:
                    pass
            elif kind == "eof":
                self.eof = True
            elif kind == "signal":
                c.stop_reason = c.stop_reason or "signal"
            elif kind == "ctl":
                req, conn = data
                if req.get("run_id") != c.run_id:
                    reply(conn, {"ok": False, "outcome": "rejected", "error": "this socket serves run %s" % c.run_id})
                elif req.get("op") == "status":
                    reply(conn, {"ok": True, "outcome": "status", "state": c.state, "thread_id": self.thread_id})
                elif req.get("op") == "interrupt":
                    if c.stop_reason:
                        reply(conn, {"ok": False, "outcome": "not_running", "error": "the run is already stopping"})
                    else:
                        c.user_interrupt = True
                        c.stop_reason = "interrupt"
                        reply(conn, {"ok": True, "outcome": "interrupt_sent"})
                else:
                    reply(conn, {"ok": False, "outcome": "rejected", "error": "the exec engine cannot steer a running turn"})
        try:
            rc = child.proc.wait(10)
        except subprocess.TimeoutExpired:
            child.signal_group(signal.SIGKILL)
            rc = child.proc.wait()
        c.usage, c.retries = self.usage, self.retries
        text = read_text(self.last)
        if c.stop_reason == "timeout":
            return TIMEOUT, text, "timed out after %ds. Split the task or raise --timeout" % c.timeout
        if c.stop_reason:
            return INTERRUPTED, text, "turn interrupted (%s)" % ("by request" if c.user_interrupt else "by signal")
        if rc != 0 or self.failed or not self.completed:
            why = self.failed or ("no turn.completed event: %s" % tail_text(c.stderr_path))
            if rc in (-signal.SIGKILL, 137):
                why = "killed by SIGKILL (out of memory or external kill). " + why
            return FAILED, text, "codex failed (exit %s): %s" % (rc, why)
        if not self.thread_id or not UUID_RE.match(self.thread_id):
            return FAILED, text, "codex did not report a valid session id"
        if c.mode == "resume" and self.thread_id.lower() != c.thread_id.lower():
            return FAILED, text, "codex resumed a different session (%s, expected %s)" % (self.thread_id, c.thread_id)
        if c.mode == "new":
            c.registered(self.thread_id)
        if not text.strip():
            return EMPTY, text, "codex finished but returned an empty reply"
        return OK, text, ""

    def event(self, ev):
        out = self.ctx.progress
        t = ev.get("type")
        if t == "thread.started":
            self.thread_id = ev.get("thread_id")
            out("● session %s" % self.thread_id)
        elif t == "error":
            self.retries += 1
            out("⚠ %s" % short(ev.get("message"), 160))
        elif t == "turn.completed":
            self.completed = True
            u = ev.get("usage") or {}
            self.usage = "in=%s cached=%s out=%s reasoning=%s" % (u.get("input_tokens"), u.get("cached_input_tokens"),
                                                                 u.get("output_tokens"), u.get("reasoning_output_tokens"))
            out("✓ done")
        elif t == "turn.failed":
            self.failed = (ev.get("error") or {}).get("message") or "turn failed"
            out("✗ failed: %s" % short(self.failed, 200))
        elif t == "item.started" and (ev.get("item") or {}).get("type") == "command_execution":
            out("$ %s" % short(strip_shell(ev["item"].get("command")), 160))
        elif t == "item.completed":
            i = ev.get("item") or {}
            it = i.get("type")
            if it == "command_execution" and (i.get("exit_code") or 0) != 0:
                out("  ↳ exit %s" % i.get("exit_code"))
            elif it == "reasoning":
                head = reasoning_heading([i.get("text") or ""])
                if head:
                    out("💭 %s" % short(head, 160))
            elif it == "file_change":
                out("✎ %s" % short(", ".join("%s %s" % (ch.get("kind", "edit"), ch.get("path")) for ch in i.get("changes") or []), 200))
            elif it == "mcp_tool_call":
                out("⚙ %s.%s" % (i.get("server") or "mcp", i.get("tool") or "?"))
            elif it == "web_search":
                out("🔎 %s" % short(i.get("query"), 120))
            elif it == "agent_message":
                out("💬 %s" % short(i.get("text"), 240))


# ---------------------------------------------------------------- send

class RunContext(object):
    pass


def stdin_timeout():
    limit = os.environ.get("CCT_STDIN_TIMEOUT", "60")
    if not SECONDS_RE.match(limit) or int(limit, 10) == 0:
        die(USAGE, "CCT_STDIN_TIMEOUT must be a positive number of seconds")
    return int(limit, 10)


def read_prompt(words, prompt_file):
    if prompt_file is not None:
        if words:
            die(USAGE, "send: give the prompt either as arguments or with --prompt-file, not both")
        try:
            with open(prompt_file, "rb") as f:
                data = f.read()
        except OSError as e:
            die(USAGE, "cannot read prompt file: %s" % e)
    elif words:
        return " ".join(words)
    else:
        if sys.stdin is None or sys.stdin.isatty():
            die(USAGE, "no prompt given (pass it as arguments, --prompt-file or stdin)")
        limit = stdin_timeout()
        deadline = time.monotonic() + limit
        fd = sys.stdin.fileno()
        chunks = []
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                die(TIMEOUT, "reading the prompt from stdin timed out after %ss" % limit)
            r, _, _ = select.select([fd], [], [], left)
            if not r:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        die(USAGE, "the prompt is not valid UTF-8")


def parse_send_args(args):
    o = {"thread": None, "profile": None, "model": None, "effort": None, "sandbox": None, "new": False,
         "cd": os.getcwd(), "timeout": None, "progress": False, "prompt_file": None, "words": []}
    valued = {"--profile": "profile", "--model": "model", "--effort": "effort", "--sandbox": "sandbox",
              "--cd": "cd", "--timeout": "timeout", "--prompt-file": "prompt_file"}
    i = 0
    while i < len(args):
        a = args[i]
        if a in valued:
            if i + 1 >= len(args) or not args[i + 1]:
                die(USAGE, "%s requires a non-empty value" % a)
            o[valued[a]] = args[i + 1]
            i += 2
            continue
        if a == "--new":
            o["new"] = True
        elif a == "--progress":
            o["progress"] = True
        elif a == "--":
            o["words"].extend(args[i + 1:])
            break
        elif a == "-":
            if o["words"]:
                o["words"].append(a)       # a later "-" is prompt text
        elif a.startswith("-") and not o["words"]:
            die(USAGE, "unknown option: %s (put '--' before a prompt that starts with '-')" % a)
        elif o["thread"] is None:
            o["thread"] = a
        else:
            o["words"].append(a)
        i += 1
    if not o["thread"]:
        die(USAGE, "send: missing <thread>")
    valid_name(o["thread"])
    if o["profile"]:
        valid_name(o["profile"], "profile name")
    if o["timeout"] is not None:
        o["timeout"] = parse_seconds(o["timeout"], "--timeout")
    return o


def resolve_settings(project, o):
    """Decide new vs resume and the effective model/effort/sandbox/cwd."""
    reg = project.load()
    rec = reg["threads"].get(o["thread"])
    s = {"mode": "new", "thread_id": None, "cwd": project.dir}
    if rec and not o["new"]:
        s.update(mode="resume", thread_id=rec["thread_id"], model=rec["model"], effort=rec["effort"],
                 sandbox=rec["sandbox"], cwd=rec["cwd"])
        if not UUID_RE.match(rec["thread_id"]):
            die(FAILED, "registered thread id for '%s' is invalid; use --new" % o["thread"])
        if not (rec["cwd"].startswith("/") and os.path.isdir(rec["cwd"])):
            die(FAILED, "working directory of '%s' no longer exists (%s); use --new" % (o["thread"], rec["cwd"]))
        if o["sandbox"] and o["sandbox"] != rec["sandbox"]:
            if o["sandbox"] != "read-only":
                die(USAGE, "thread '%s' is %s; a resumed thread can only be tightened to read-only (use --new to change it)"
                    % (o["thread"], rec["sandbox"]))
            s["sandbox"] = "read-only"
            s["tightened"] = True
    else:
        profile = o["profile"] or o["thread"]
        profs = load_profiles()
        prof = profs.get(profile)
        if prof is None and not (o["model"] and o["effort"]):
            die(USAGE, "no profile named '%s'; pass --profile <name> or both --model and --effort (see: codex-thread.sh profiles)" % profile)
        prof = prof if prof is not None else {}
        if not isinstance(prof, dict) or any(prof.get(k) is not None and not isinstance(prof.get(k), str)
                                             for k in ("model", "effort", "sandbox")):
            die(USAGE, "profile '%s': model, effort and sandbox must be strings" % profile)
        s["model"] = o["model"] or prof.get("model")
        s["effort"] = o["effort"] or prof.get("effort")
        s["sandbox"] = o["sandbox"] or prof.get("sandbox") or "workspace-write"
        if not (s["model"] and s["effort"]):
            die(USAGE, "profile '%s' must define model and effort" % profile)
    if not re.match(r"^[A-Za-z0-9._:/-]+$", s["model"]):
        die(USAGE, "invalid model: %s" % s["model"])
    if s["effort"] not in EFFORTS:
        die(USAGE, "unsupported effort: %s" % s["effort"])
    if s["sandbox"] not in SANDBOXES:
        die(USAGE, "invalid sandbox: %s" % s["sandbox"])
    return s


def cmd_send(args, lock_wait=0.0, on_start=None):
    stdin_timeout()
    o = parse_send_args(args)
    prompt = read_prompt(o["words"], o["prompt_file"])
    if not prompt.strip():
        die(USAGE, "send: prompt is empty")
    project = Project(o["cd"])
    engine = os.environ.get("CCT_ENGINE", "app-server")
    if engine not in ("app-server", "exec"):
        die(USAGE, "CCT_ENGINE must be app-server or exec")
    ensure_dir(os.path.join(STATE_DIR, "runs", "current"))

    gate = project.gate()
    if not gate.acquire(False, 15):
        die(BUSY, "a reset is in progress for this project; retry shortly")
    tlock = project.thread_lock(o["thread"])
    if not tlock.acquire(True, lock_wait):
        die(BUSY, "thread '%s' is already running in this project; steer it, wait for it, or use another thread name" % o["thread"])
    s = resolve_settings(project, o)
    reap_previous(project, o["thread"])
    prune_runs()

    c = RunContext()
    c.run_id = uuid.uuid4().hex[:12]
    c.dir = ensure_dir(run_dir(c.run_id))
    c.thread, c.project = o["thread"], project
    c.mode, c.thread_id, c.model, c.effort, c.sandbox, c.cwd = (
        s["mode"], s["thread_id"], s["model"], s["effort"], s["sandbox"], s["cwd"])
    c.prompt = prompt
    c.timeout = o["timeout"] or default_timeout(c.effort)
    c.inbox = queue.Queue()
    c.stop_reason = None
    c.user_interrupt = False
    c.usage, c.retries = "", 0
    c.state = "starting"
    c.stderr_path = os.path.join(c.dir, "stderr.log")
    c.socket = os.path.join(sock_dir(), c.run_id + ".sock")
    c.deadline = time.monotonic() + c.timeout
    write_atomic(os.path.join(c.dir, "prompt.txt"), prompt)
    c.events = open(os.path.join(c.dir, "events.jsonl"), "ab", buffering=0)
    c.progress = Progress(os.path.join(c.dir, "progress.log"), o["progress"])
    rec = {"run_id": c.run_id, "thread": c.thread, "root": project.root, "key": project.key, "engine": engine,
           "mode": c.mode, "model": c.model, "effort": c.effort, "sandbox": c.sandbox, "cwd": c.cwd,
           "pid": os.getpid(), "pid_start": proc_start_time(os.getpid()) or "", "socket": c.socket,
           "state": "starting", "thread_id": c.thread_id, "turn_id": None, "started": now_utc(),
           "timeout": c.timeout}

    def save():
        write_json(os.path.join(c.dir, "run.json"), rec)

    def registered(tid):
        c.thread_id = rec["thread_id"] = tid
        project.update_thread(c.thread, {"thread_id": tid, "model": c.model, "effort": c.effort,
                                         "sandbox": c.sandbox, "cwd": c.cwd, "engine": engine}, fresh=True)
        save()

    def started_turn(tid, turn_id):
        c.state = rec["state"] = "running"
        rec["thread_id"], rec["turn_id"] = tid or rec["thread_id"], turn_id
        save()

    c.registered, c.started_turn = registered, started_turn

    def on_signal(signum, frame):
        c.inbox.put(("signal", signum))
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, on_signal)

    runner = AppServerRun(c) if engine == "app-server" else ExecRun(c)
    child = ctl = None
    code, text, err = FAILED, "", ""
    t0 = time.monotonic()
    try:
        c.progress("● run %s: thread=%s %s %s/%s %s engine=%s" % (c.run_id, c.thread, c.mode, c.model, c.effort,
                                                               c.sandbox, engine))
        if s.get("tightened"):
            c.progress("● sandbox tightened to read-only for this run")
        # Spawn first: no other thread may exist while the child is forked.
        child = runner.spawn()
        rec["worker_pid"], rec["worker_start"] = child.proc.pid, child.start_time
        ctl = ControlServer(c.socket, c.inbox)
        # Publish the run only once its control socket accepts commands.
        save()
        write_atomic(project.pointer(c.thread), c.run_id + "\n")
        if on_start:
            on_start(c.run_id)
        code, text, err = runner.run()
    except CctError as e:
        code, err = e.code, e.msg
        if code == FAILED and c.mode == "resume":
            err += ". If the thread is gone or corrupted, start over with: send %s --new ..." % c.thread
    except Exception as e:  # never leave a run half-recorded
        code, err = FAILED, "internal error: %s: %s" % (type(e).__name__, e)
    finally:
        if ctl:
            ctl.close()
        if child:
            child.stop()
        # Answer commands that arrived at the very end, so no client hangs.
        while True:
            try:
                kind, data = c.inbox.get_nowait()
            except queue.Empty:
                break
            if kind == "ctl":
                reply(data[1], {"ok": False, "outcome": "not_running", "error": "the turn has already ended"})

    elapsed = int(time.monotonic() - t0)
    turn_ran = rec.get("turn_id") is not None if engine == "app-server" else code in (OK, EMPTY)
    if turn_ran:
        try:
            project.update_thread(c.thread, {}, count_turn=True)
        except CctError:
            pass
    status_line = "[cct] thread=%s mode=%s id=%s model=%s effort=%s sandbox=%s elapsed=%ds %s retries=%d run=%s log=%s" % (
        c.thread, c.mode, c.thread_id or "-", c.model, c.effort, c.sandbox, elapsed, c.usage or "", c.retries,
        c.run_id, c.dir)
    write_atomic(os.path.join(c.dir, "reply.txt"), text)
    rec.update(state="done", exit_code=code, error=err, ended=now_utc(), elapsed=elapsed, status_line=status_line)
    save()
    c.progress.close()
    c.events.close()
    gate.close()
    tlock.close()
    if code == OK:
        out = text.rstrip("\n") + "\n---\n" + status_line + "\n"
        sys.stdout.write(("\n" if o["progress"] else "") + out)
        sys.stdout.flush()
        return OK
    if text.strip():
        sys.stdout.write(text.rstrip("\n") + "\n---\n")
    sys.stdout.write(status_line + "\n")
    sys.stdout.flush()
    raise CctError(code, "%s (thread=%s, run=%s, log=%s)" % (err, c.thread, c.run_id, c.dir))


# ---------------------------------------------------------------- other commands

def split_common(args, allowed):
    """Parse flags with values (allowed: {flag: key}) and positionals."""
    opts, pos, words = {}, [], []
    i = 0
    while i < len(args):
        a = args[i]
        if a in allowed:
            if i + 1 >= len(args) or not args[i + 1]:
                die(USAGE, "%s requires a value" % a)
            opts[allowed[a]] = args[i + 1]
            i += 2
            continue
        if a == "--":
            words.extend(args[i + 1:])
            break
        if a.startswith("-") and a != "-":
            die(USAGE, "unknown argument: %s" % a)
        pos.append(a)
        i += 1
    return opts, pos, words


def thread_and_project(args, cmd, extra=None):
    allowed = {"--cd": "cd"}
    allowed.update(extra or {})
    opts, pos, words = split_common(args, allowed)
    if not pos:
        die(USAGE, "%s: missing <thread>" % cmd)
    valid_name(pos[0])
    return pos[0], Project(opts.get("cd") or os.getcwd()), opts, pos[1:], words


def pick_run(project, thread, opts, need_live):
    rec = load_run(opts["run"]) if opts.get("run") else current_run(project, thread)
    if opts.get("run") and (not rec or rec.get("thread") != thread or rec.get("key") != project.key):
        die(USAGE, "run %s does not belong to thread '%s' in %s" % (opts["run"], thread, project.root))
    if need_live and not run_alive(rec):
        die(NOT_RUNNING, "thread '%s' has no running turn" % thread)
    return rec


def cmd_steer(args):
    thread, project, opts, pos, words = thread_and_project(args, "steer", {"--run": "run", "--prompt-file": "prompt_file"})
    text = read_prompt(pos + words, opts.get("prompt_file"))
    if not text.strip():
        die(USAGE, "steer: text is empty")
    rec = pick_run(project, thread, opts, True)
    return report_control(control_request(rec, "steer", text), rec)


def cmd_interrupt(args):
    thread, project, opts, pos, _ = thread_and_project(args, "interrupt", {"--run": "run"})
    if pos:
        die(USAGE, "interrupt: unexpected argument %s" % pos[0])
    rec = pick_run(project, thread, opts, True)
    return report_control(control_request(rec, "interrupt"), rec)


def report_control(res, rec):
    outcome = res.get("outcome")
    if res.get("ok"):
        print("[cct] %s run=%s thread=%s" % (outcome, rec["run_id"], rec["thread"]))
        return OK
    code = NOT_RUNNING if outcome == "not_running" else (FAILED if outcome == "unknown" else USAGE)
    die(code, "%s: %s (run=%s)" % (outcome, res.get("error"), rec["run_id"]))


def cmd_status(args):
    thread, project, opts, pos, _ = thread_and_project(args, "status", {"--run": "run"})
    rec = pick_run(project, thread, opts, False)
    if not rec:
        die(USAGE, "no run recorded for thread '%s' in %s" % (thread, project.root))
    alive = run_alive(rec)
    out = dict(rec)
    out["alive"] = alive
    if rec.get("state") != "done" and not alive:
        out["state"] = "lost"
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return OK


def print_result(rec):
    reply_text = read_text(os.path.join(run_dir(rec["run_id"]), "reply.txt"))
    code = rec.get("exit_code")
    if reply_text.strip():
        sys.stdout.write(reply_text.rstrip("\n") + "\n---\n")
    sys.stdout.write((rec.get("status_line") or "[cct] run=%s" % rec["run_id"]) + "\n")
    if code not in (OK, None):
        sys.stdout.write("[cct] ERROR: %s\n" % rec.get("error"))
    sys.stdout.flush()
    return code if isinstance(code, int) else FAILED


def cmd_result(args):
    thread, project, opts, pos, _ = thread_and_project(args, "result", {"--run": "run"})
    rec = pick_run(project, thread, opts, False)
    if not rec:
        die(USAGE, "no run recorded for thread '%s' in %s" % (thread, project.root))
    if run_alive(rec):
        die(BUSY, "run %s is still running (watch it, or steer/interrupt it)" % rec["run_id"])
    if rec.get("state") != "done":
        die(FAILED, "run %s ended without recording a result (runner was killed)" % rec["run_id"])
    return print_result(rec)


def cmd_watch(args):
    thread, project, opts, pos, _ = thread_and_project(args, "watch", {"--wait": "wait", "--max": "max", "--run": "run"})
    wait = parse_seconds(opts.get("wait", "15"), "--wait", allow_zero=True)
    limit = parse_seconds(opts.get("max", "21600"), "--max")
    deadline = time.monotonic() + wait
    rec = None
    while True:
        rec = pick_run(project, thread, opts, False)
        if run_alive(rec) or time.monotonic() >= deadline:
            break
        time.sleep(0.3)
    if not rec:
        die(USAGE, "no run recorded for thread '%s' in %s" % (thread, project.root))
    path = os.path.join(run_dir(rec["run_id"]), "progress.log")
    print("[cct] watching run=%s thread=%s %s %s/%s" % (rec["run_id"], thread, rec.get("mode"), rec.get("model"), rec.get("effort")))
    sys.stdout.flush()
    end = time.monotonic() + limit
    pos_ = 0
    while True:
        live = run_alive(load_run(rec["run_id"]))
        try:
            with open(path, "rb") as f:
                f.seek(pos_)
                data = f.read()
        except FileNotFoundError:
            data = b""
        if data:
            cut = data.rfind(b"\n") + 1 if live else len(data)
            if cut:
                sys.stdout.write(data[:cut].decode("utf-8", "replace"))
                sys.stdout.flush()
                pos_ += cut
        if not live:
            break
        if time.monotonic() >= end:
            print("[cct] watch limit reached; the run is still going")
            return OK
        time.sleep(0.5)
    print("[cct] run ended")
    return OK


def cmd_list(args):
    opts, pos, _ = split_common(args, {"--cd": "cd"})
    if pos:
        die(USAGE, "unknown argument: %s" % pos[0])
    project = Project(opts.get("cd") or os.getcwd())
    reg = project.load()
    print("project: %s" % project.root)
    if not reg["threads"]:
        print("  (no threads)")
    for name, r in sorted(reg["threads"].items()):
        live = run_alive(current_run(project, name))
        print("  %s\t%s/%s\t%s\tturns=%d\tupdated=%s\tid=%s%s" % (name, r["model"], r["effort"], r["sandbox"], r["turns"],
                                                              r.get("updated", "?"), r["thread_id"], "\tRUNNING" if live else ""))
    return OK


def cmd_show(args):
    thread, project, _, pos, _ = thread_and_project(args, "show")
    rec = project.load()["threads"].get(thread)
    if rec is None:
        die(USAGE, "no thread '%s' in %s" % (thread, project.root))
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    return OK


def cmd_reset(args):
    target, cd, i = None, os.getcwd(), 0
    while i < len(args):
        a = args[i]
        if a == "--cd":
            if i + 1 >= len(args) or not args[i + 1]:
                die(USAGE, "--cd requires a directory")
            cd, i = args[i + 1], i + 2
            continue
        if target is not None or (a.startswith("-") and a != "--all"):
            die(USAGE, "reset: unexpected argument %s" % a)
        target, i = a, i + 1
    if target is None:
        die(USAGE, "reset: missing <thread> or --all")
    if target != "--all":
        valid_name(target)
    opts = {"cd": cd}
    project = Project(opts.get("cd") or os.getcwd())
    gate = project.gate()
    if not gate.acquire(True, 0):
        die(BUSY, "a thread is running in this project; reset after it finishes")
    lk = project.registry_lock()
    try:
        if target == "--all":
            reg = {"root": project.root, "threads": {}}   # also recovers a corrupt registry
        else:
            reg = project.load()
            reg["threads"].pop(target, None)
        write_json(project.registry, reg)
    finally:
        lk.close()
        gate.close()
    print("[cct] reset %s in %s" % (target, project.root))
    return OK


# ---------------------------------------------------------------- relay support

RELAY_DIR = os.path.join(STATE_DIR, "relay")
DIRECTIVE_RE = re.compile(r"^@cct(\s.*)?$")
STATE_RE = re.compile(r"^([0-9a-f]{12})\.json$")


def relay_state_path(state_path):
    state_path = os.path.realpath(state_path)
    m = STATE_RE.match(os.path.basename(state_path))
    if os.path.dirname(state_path) != os.path.realpath(RELAY_DIR) or not m:
        die(USAGE, "not a relay state file: %s" % state_path)
    if not os.path.isfile(state_path):
        die(USAGE, "relay state file does not exist: %s (run relay-init)" % state_path)
    return state_path, m.group(1)


def parse_directive(line):
    """`@cct thread=<name> new read-only cd=<dir>` or `@cct {json}`; also `@cct interrupt|status`.

    Tokens are split with shell-style quoting (cd="/a b"), never evaluated."""
    body = line[4:].strip()
    d = {}
    if body.startswith("{"):
        try:
            obj = json.loads(body)
        except ValueError as e:
            die(USAGE, "bad @cct JSON directive: %s" % e)
        if not isinstance(obj, dict):
            die(USAGE, "bad @cct JSON directive")
        for k, v in obj.items():
            if k in ("thread", "cd") and isinstance(v, str):
                d[k] = v
            elif k in ("new", "read_only", "interrupt", "status") and isinstance(v, bool):
                d[k] = v
            else:
                die(USAGE, "bad @cct directive field: %s" % k)
        return d
    try:
        tokens = shlex.split(body)
    except ValueError as e:
        die(USAGE, "bad @cct directive quoting: %s" % e)
    for tok in tokens:
        if tok.startswith("thread="):
            d["thread"] = tok[7:]
        elif tok.startswith("cd="):
            d["cd"] = tok[3:]
        elif tok == "new":
            d["new"] = True
        elif tok == "read-only":
            d["read_only"] = True
        elif tok in ("interrupt", "stop"):
            d["interrupt"] = True
        elif tok == "status":
            d["status"] = True
        else:
            die(USAGE, "unknown @cct directive token: %s" % tok)
    return d


def cmd_relay_init(args):
    opts, pos, _ = split_common(args, {"--cd": "cd"})
    if len(pos) != 1:
        die(USAGE, "relay-init <profile> [--cd <dir>]")
    profile = valid_name(pos[0], "profile name")
    if profile not in load_profiles():
        die(USAGE, "no profile named '%s'" % profile)
    project = Project(opts.get("cd") or os.getcwd())
    ensure_dir(RELAY_DIR)
    state = os.path.join(RELAY_DIR, uuid.uuid4().hex[:12] + ".json")
    write_json(state, {"profile": profile, "thread": profile, "cd": project.dir, "pending": [], "created": now_utc()})
    print("STATE=%s" % state)
    return OK


def cmd_relay_next(args):
    """Print a fresh, never-used message path for this relay session."""
    if len(args) != 1:
        die(USAGE, "relay-next <state-file>")
    _, sid = relay_state_path(args[0])
    print("MSG=%s" % os.path.join(os.path.realpath(RELAY_DIR), "%s-%s.msg" % (sid, uuid.uuid4().hex[:12])))
    return OK


def relay_lock(state):
    lk = Lock(state + ".lock")
    if not lk.acquire(True, 30):
        die(BUSY, "relay state is locked by another relay command")
    return lk


def relay_record(state, entry):
    lk = relay_lock(state)
    try:
        cur = read_json(state)
        cur["pending"].append(entry)
        write_json(state, cur)
    finally:
        lk.close()


def relay_note(state, text, code=OK):
    relay_record(state, {"type": "note", "text": text})
    print(text)
    return code


def cmd_relay(args):
    """Deliver one message file: start a turn, or steer/interrupt the running one.

    Every outcome is queued in the state file, so `relay-result` reports it
    exactly once."""
    if len(args) != 2:
        die(USAGE, "relay <state-file> <message-file>")
    state, sid = relay_state_path(args[0])
    msg_path = os.path.realpath(args[1])
    if os.path.dirname(msg_path) != os.path.realpath(RELAY_DIR) or \
            not re.match(r"^%s-[0-9a-f]{12}\.msg$" % sid, os.path.basename(msg_path)):
        die(USAGE, "not a message file of this relay session: %s (get one with relay-next)" % msg_path)
    try:
        with open(msg_path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return relay_note(state, "[cct] ERROR: message file %s does not exist (write it first)" % msg_path, USAGE)
    os.unlink(msg_path)
    started = []
    try:
        return relay_deliver(state, raw, started)
    except CctError as e:
        if started:
            raise
        return relay_note(state, "[cct] ERROR: %s" % e.msg, e.code)


def relay_deliver(state, raw, started):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        die(USAGE, "message is not valid UTF-8")
    first, _, rest = text.partition("\n")
    d = {}
    if DIRECTIVE_RE.match(first.strip()):
        d = parse_directive(first.strip())
        text = rest
    lk = relay_lock(state)
    try:
        st = read_json(state)
        if "thread" in d:
            st["thread"] = valid_name(d["thread"])
        if "cd" in d:
            if not d["cd"].startswith("/"):
                die(USAGE, "cd= must be an absolute path")
            st["cd"] = abs_dir(d["cd"])
        write_json(state, st)
    finally:
        lk.close()
    thread, cd, profile = st["thread"], st["cd"], st["profile"]
    project = Project(cd)
    rec = current_run(project, thread)
    live = run_alive(rec)

    if d.get("status"):
        return relay_note(state, "[cct] status thread=%s: %s" % (thread, "running (run %s)" % rec["run_id"] if live else "idle"))
    if d.get("interrupt"):
        if not live:
            return relay_note(state, "[cct] thread=%s has no running turn; nothing to interrupt" % thread)
        res = control_request(rec, "interrupt")
        if res.get("ok"):
            return relay_note(state, "[cct] interrupt sent to run %s (thread %s); its result follows when it stops" % (rec["run_id"], thread))
        return relay_note(state, "[cct] ERROR: interrupt failed: %s" % res.get("error"), FAILED)
    if not text.strip():
        die(USAGE, "message is empty")
    if live and (d.get("new") or d.get("read_only")):
        die(BUSY, "thread '%s' is running; settings cannot change mid-turn. Send '@cct interrupt' first, "
                  "then resend this message" % thread)
    if live:
        res = control_request(rec, "steer", text)
        if res.get("ok"):
            return relay_note(state, "[cct] delivered to the running turn as steering input (run %s, thread %s); "
                                     "the reply follows when that run ends" % (rec["run_id"], thread))
        if res.get("outcome") != "not_running":
            return relay_note(state, "[cct] ERROR: steering failed and was not resent: %s" % res.get("error"), FAILED)
        print("[cct] the running turn ended before the message arrived; starting a new turn with it")

    fd, pfile = tempfile.mkstemp(prefix="prompt-", dir=RELAY_DIR)
    with os.fdopen(fd, "wb") as f:
        f.write(text.encode("utf-8"))
    sargs = [thread, "--profile", profile, "--cd", cd, "--progress", "--prompt-file", pfile]
    if d.get("new"):
        sargs.append("--new")
    if d.get("read_only"):
        sargs += ["--sandbox", "read-only"]

    def on_start(run_id):
        started.append(run_id)
        relay_record(state, {"type": "run", "run_id": run_id})
    try:
        code = cmd_send(sargs, lock_wait=60, on_start=on_start)
    except CctError as e:
        if not started:
            raise
        code = e.code
        sys.stdout.write("[cct] ERROR: %s\n" % e.msg)
    finally:
        try:
            os.unlink(pfile)
        except OSError:
            pass
    print("[cct] RUN FINISHED (exit %d)" % code)
    return code


def cmd_relay_result(args):
    if len(args) != 1:
        die(USAGE, "relay-result <state-file>")
    state, _ = relay_state_path(args[0])
    lk = relay_lock(state)
    try:
        st = read_json(state)
        keep, shown = [], 0
        for entry in st.get("pending", []):
            if entry.get("type") == "note":
                print(entry["text"])
                shown += 1
                continue
            rec = load_run(entry.get("run_id"))
            if rec and run_alive(rec):
                keep.append(entry)
                print("[cct] run %s on thread %s is still running" % (rec["run_id"], rec["thread"]))
                continue
            if not rec:
                print("[cct] ERROR: run %s has no record" % entry.get("run_id"))
            elif rec.get("state") != "done":
                print("[cct] ERROR: run %s ended without a result (runner was killed)" % rec["run_id"])
            else:
                print_result(rec)
            shown += 1
        st["pending"] = keep
        write_json(state, st)
    finally:
        lk.close()
    if not shown and not keep:
        print("[cct] nothing new to report")
    return OK


def cmd_profiles(args):
    print(json.dumps(load_profiles(), ensure_ascii=False, indent=2))
    return OK


COMMANDS = {
    "send": cmd_send, "steer": cmd_steer, "interrupt": cmd_interrupt, "status": cmd_status,
    "result": cmd_result, "watch": cmd_watch, "list": cmd_list, "show": cmd_show, "reset": cmd_reset,
    "profiles": cmd_profiles, "relay-init": cmd_relay_init, "relay-next": cmd_relay_next, "relay": cmd_relay,
    "relay-result": cmd_relay_result,
}


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    cmd = argv[0] if argv else "help"
    if cmd in ("help", "-h", "--help"):
        sys.stdout.write(USAGE_TEXT)
        return OK
    if cmd in ("version", "--version"):
        print("cc-codex-threads %s" % VERSION)
        return OK
    fn = COMMANDS.get(cmd)
    if fn is None:
        sys.stderr.write(USAGE_TEXT)
        return USAGE
    try:
        return fn(argv[1:])
    except CctError as e:
        try:
            sys.stdout.flush()
        except OSError:
            pass
        sys.stderr.write("[cct] ERROR: %s\n" % e.msg)
        return e.code
    except KeyboardInterrupt:
        sys.stderr.write("[cct] ERROR: interrupted\n")
        return INTERRUPTED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
