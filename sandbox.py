"""Guarded execution of student code (Python, C, C++, Java, JavaScript) with live output and interactive stdin.

Guardrails (all configurable via env, see LIMITS below):
- Server-wide cap on concurrent runs, with a short queue; extra runs get a "server busy" message.
- One run per session at a time (starting a new run stops the old one) and a per-session rate limit.
- Per-process limits: CPU seconds, wall-clock time, idle time, memory, file size, open files.
- Output cap: the program is stopped once it prints too much.
- Each run gets a private temp directory that is deleted afterwards; filenames are sanitized.
- Python runs behind an audit hook that blocks subprocesses, network access, ctypes and file
  access outside the run directory. JavaScript runs under Node's permission model: it may only
  read its own directory, and cannot write files or start processes, workers or native addons. When the process runs as root (the runner container), each
  program additionally runs as an unprivileged SANDBOX_UID with a process-count limit.

This module is used directly by main.py in local development, and by runner.py (a separate
container without secrets or internet access) in production.
"""

import asyncio
import codecs
import collections
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import time
import urllib.parse
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple

try:
    import resource  # POSIX only
except ImportError:  # pragma: no cover
    resource = None  # type: ignore[assignment]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


LIMITS = {
    "max_concurrent": _env_int("CODE_MAX_CONCURRENT", 40),     # runs at once (keep max_concurrent × memory_mb under the runner's RAM)
    "queue_seconds": _env_int("CODE_QUEUE_SECONDS", 20),       # max wait for a free slot
    "runs_per_minute": _env_int("CODE_RUNS_PER_MINUTE", 20),   # per session
    "cpu_seconds": _env_int("CODE_CPU_SECONDS", 10),
    "wall_seconds": _env_int("CODE_WALL_SECONDS", 120),        # interactive run, includes waiting for input
    "batch_wall_seconds": _env_int("CODE_BATCH_WALL_SECONDS", 15),
    "idle_seconds": _env_int("CODE_IDLE_SECONDS", 60),         # no output and no input
    "compile_seconds": _env_int("CODE_COMPILE_SECONDS", 20),
    "memory_mb": _env_int("CODE_MEMORY_MB", 256),
    "compile_memory_mb": _env_int("CODE_COMPILE_MEMORY_MB", 1024),
    "file_size_mb": _env_int("CODE_FILE_SIZE_MB", 2),
    "output_bytes": _env_int("CODE_OUTPUT_BYTES", 100_000),
    "stdin_bytes": _env_int("CODE_STDIN_BYTES", 64_000),
    "max_files": 20,
    "max_source_bytes": 400_000,
    "nproc": _env_int("CODE_NPROC", 512),                      # total processes for SANDBOX_UID
}
SANDBOX_UID = _env_int("SANDBOX_UID", 0)
SANDBOX_GID = _env_int("SANDBOX_GID", SANDBOX_UID)

LANG_EXT = {
    "python": (".py",),
    "c": (".c", ".h"),
    "cpp": (".cpp", ".cc", ".cxx", ".hpp", ".h"),
    "java": (".java",),
    "javascript": (".js",),
}
# Like the JVM, V8 reserves far more address space than it uses, so Node gets a heap cap instead of
# RLIMIT_AS. Node 20-22 spell the permission flag --experimental-permission.
NODE_BIN = os.getenv("NODE_BIN") or shutil.which("node") or "node"
# The JVM reserves far more address space than it uses, so Java gets a heap cap instead of RLIMIT_AS.
_JAVA_OPTS = ["-XX:+UseSerialGC", "-XX:TieredStopAtLevel=1", "-XX:CICompilerCount=1", "-Xss8m",
              "-XX:MaxMetaspaceSize=128m", "-Dfile.encoding=UTF-8", "-Dstdout.encoding=UTF-8"]
# In a real terminal a Node program exits once readline closes; with our open stdin pipe it would wait
# for EOF until the idle limit. Unref stdin when a readline interface on it closes. Only readline is
# patched: touching process.stdin up front would break fs.readFileSync(0) (EAGAIN). A data: URL so
# the permission model needs no extra read access.
_NODE_PRELOAD = "data:text/javascript," + urllib.parse.quote(
    "import r from 'node:readline';import rp from 'node:readline/promises';"
    "import {syncBuiltinESMExports} from 'node:module';"
    "for(const m of [r,rp]){const c=m.createInterface;m.createInterface=function(...a){"
    "const i=c.apply(this,a);const s=i.input;"
    "if(s===process.stdin)i.on('close',()=>{if(s.unref)s.unref();});return i;};}"
    "syncBuiltinESMExports();"
)
_JAVA_MAIN_RE = re.compile(r"public\s+static\s+(?:final\s+)?void\s+main\s*\(")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")

Emit = Callable[[Dict[str, Any]], Awaitable[None]]


class RunRejected(Exception):
    """The run was refused before starting (busy, rate limited, invalid input)."""


# ─── Python sandbox bootstrap (runs inside the student process) ──────────────

_PY_BOOT = r'''
import os, sys

_WORK = os.path.realpath(sys.argv[1])
_ENTRY = os.path.join(_WORK, sys.argv[2])
_READ_ROOTS = tuple({os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix,
                    sys.base_exec_prefix, "/usr/share/zoneinfo", "/dev/null", "/dev/urandom")})
_BLOCKED = ("os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty",
            "subprocess.", "pty.", "socket.", "ctypes.", "os.kill", "os.killpg",
            "signal.pthread_kill", "os.setuid", "os.setgid", "resource.setrlimit", "_posixsubprocess.")
_PATH_EVENTS = {"os.remove", "os.rmdir", "os.mkdir", "os.rename", "os.replace", "os.truncate",
                "os.chmod", "os.chown", "os.link", "os.symlink", "os.utime", "shutil.rmtree",
                "shutil.copyfile", "shutil.move", "os.chdir"}

def _inside(path, roots):
    try:
        p = os.path.realpath(os.fsdecode(path) if not isinstance(path, int) else "")
    except Exception:
        return False
    return any(p == r or p.startswith(r + os.sep) for r in roots)

def _deny(what):
    raise PermissionError(what + " is not allowed in this environment")

def _hook(event, args):
    if event.startswith(_BLOCKED):
        _deny(event.split(".")[0] if event.startswith(("socket.", "subprocess.", "ctypes.")) else event)
    if event == "open":
        path, mode, flags = (list(args) + [None, None, None])[:3]
        if isinstance(path, int):
            return
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        if not (_inside(path, (_WORK,)) or (not writing and _inside(path, _READ_ROOTS))):
            _deny("Access to " + os.fsdecode(path))
    elif event in ("os.listdir", "os.scandir"):
        if args and args[0] is not None and not _inside(args[0], (_WORK,) + _READ_ROOTS):
            _deny("Listing " + os.fsdecode(args[0]))
    elif event in _PATH_EVENTS:
        for a in args[:2]:
            if isinstance(a, (str, bytes, os.PathLike)) and not _inside(a, (_WORK,)):
                _deny(event + " outside your project folder")

sys.addaudithook(_hook)
sys.path.insert(0, _WORK)

import runpy, traceback
try:
    runpy.run_path(_ENTRY, run_name="__main__")
except SystemExit:
    raise
except BaseException as exc:
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename.startswith(_WORK)]
    for f in frames:
        f.filename = os.path.relpath(f.filename, _WORK)
    sys.stderr.write("Traceback (most recent call last):\n" if frames else "")
    sys.stderr.write("".join(traceback.format_list(frames)))
    sys.stderr.write("".join(traceback.format_exception_only(type(exc), exc)))
    sys.exit(1)
'''

# GCC's <bits/stdc++.h>, which many students include, for compilers without it (clang on macOS).
# Passed with -idirafter, so a compiler that has the real header uses that one.
_STDCXX_SHIM = "\n".join("#include <%s>" % h for h in (
    "cassert cctype cerrno cfloat climits clocale cmath csetjmp csignal cstdarg cstddef cstdint cstdio "
    "cstdlib cstring ctime cwchar cwctype algorithm array atomic bitset chrono complex condition_variable "
    "deque exception forward_list fstream functional initializer_list iomanip ios iosfwd iostream istream "
    "iterator limits list locale map memory mutex new numeric optional ostream queue random ratio regex "
    "set sstream stack stdexcept streambuf string string_view system_error thread tuple type_traits "
    "typeindex typeinfo unordered_map unordered_set utility valarray variant vector"
).split()) + "\n"

# Makes C stdout unbuffered so prompts like printf("Name: ") appear before scanf waits.
_C_INIT = "#include <stdio.h>\n__attribute__((constructor)) static void _sandbox_unbuffer(void) " \
          "{ setvbuf(stdout, NULL, _IONBF, 0); }\n"


# ─── Admission control ───────────────────────────────────────────────────────

_slots = asyncio.Semaphore(LIMITS["max_concurrent"])
_active: Dict[str, "Run"] = {}
_recent: Dict[str, Deque[float]] = collections.defaultdict(collections.deque)
stats = {"started": 0, "rejected_busy": 0, "rejected_rate": 0, "running": 0}


def _check_rate(key: str) -> None:
    now = time.monotonic()
    q = _recent[key]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= LIMITS["runs_per_minute"]:
        stats["rejected_rate"] += 1
        raise RunRejected(
            f"You're running code too often (max {LIMITS['runs_per_minute']} runs per minute). "
            "Please wait a few seconds and try again."
        )
    q.append(now)
    if len(_recent) > 5000:  # forget idle sessions
        for k in [k for k, v in _recent.items() if not v or now - v[-1] > 60]:
            del _recent[k]


def clean_files(language: str, files: List[Dict[str, str]], entry: Optional[str]) -> Tuple[List[Dict[str, str]], str]:
    if language not in LANG_EXT:
        raise RunRejected(f"Running {language} on the server is not supported.")
    if not files:
        raise RunRejected("No code to run.")
    if len(files) > LIMITS["max_files"]:
        raise RunRejected(f"Too many files (max {LIMITS['max_files']}).")
    out, total = [], 0
    for f in files:
        name = os.path.basename(str(f.get("filename") or ""))
        if not _FILENAME_RE.match(name):
            raise RunRejected(f"Invalid file name: {name or '(empty)'}")
        content = str(f.get("content") or "")
        total += len(content.encode("utf-8", "replace"))
        out.append({"filename": name, "content": content})
    if total > LIMITS["max_source_bytes"]:
        raise RunRejected("Your code is too large to run.")
    names = [f["filename"] for f in out]
    if entry not in names:
        entry = next((n for n in names if n.endswith(LANG_EXT[language][0])), names[0])
    if language == "python" and not entry.endswith(".py"):
        raise RunRejected("The file to run must be a .py file.")
    if language == "javascript" and not entry.endswith(".js"):
        raise RunRejected("The file to run must be a .js file.")
    if language == "java":
        java = [f for f in out if f["filename"].endswith(".java")]
        if not entry.endswith(".java") or not _JAVA_MAIN_RE.search(next(f["content"] for f in out if f["filename"] == entry)):
            entry = next((f["filename"] for f in java if _JAVA_MAIN_RE.search(f["content"])), entry)
    return out, entry


# ─── A single run ────────────────────────────────────────────────────────────

def _preexec(memory_mb: int, cpu_seconds: int) -> Callable[[], None]:
    def apply() -> None:
        os.umask(0o077)
        try:  # under memory pressure, Linux kills student programs before the runner itself
            with open("/proc/self/oom_score_adj", "w") as fh:
                fh.write("1000")
        except OSError:
            pass
        if resource is not None:
            limits = [
                (resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1)),
                (resource.RLIMIT_FSIZE, (LIMITS["file_size_mb"] << 20,) * 2),
                (resource.RLIMIT_NOFILE, (256, 256)),
                (resource.RLIMIT_CORE, (0, 0)),
            ]
            if memory_mb > 0:  # 0 = the program caps its own memory (the JVM's -Xmx)
                limits.append((getattr(resource, "RLIMIT_AS", None), (memory_mb << 20,) * 2))
            if SANDBOX_UID and os.geteuid() == 0:
                limits.append((resource.RLIMIT_NPROC, (LIMITS["nproc"],) * 2))
            for which, value in limits:
                if which is None:
                    continue
                try:
                    resource.setrlimit(which, value)
                except (ValueError, OSError):
                    pass  # e.g. RLIMIT_AS is not enforceable on macOS
        if SANDBOX_UID and os.geteuid() == 0:
            os.setgroups([])
            os.setgid(SANDBOX_GID)
            os.setuid(SANDBOX_UID)
    return apply


# How long a running program must be silent (and, on Linux, idle) before the terminal shows that it is
# waiting for input.
INPUT_WAIT_SECONDS = 0.4


def _cpu_ticks(pid: int) -> Optional[int]:
    """User + system CPU time of the process (all its threads) in clock ticks, or None without /proc."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
        return int(fields[11]) + int(fields[12])
    except (OSError, IndexError, ValueError):
        return None


class Run:
    def __init__(self, key: str, emit: Emit, stdin: Optional["asyncio.Queue[Optional[str]]"]):
        self.key = key
        self.emit = emit
        self.stdin = stdin
        self.interactive = False
        self.wall = 0
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.reason = ""          # why we stopped it: timeout | idle | output_limit | cpu | memory | stopped
        self.out_bytes = 0
        self.in_bytes = 0
        self.last_activity = time.monotonic()
        self.last_output = self.last_input = time.monotonic()
        self.stdin_open = False
        self.input_wait = False   # last "input_wait" state sent to the client
        self.stopped = asyncio.Event()

    def kill(self, reason: str) -> None:
        if not self.reason:
            self.reason = reason
        self.stopped.set()
        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass

    async def _pump(self, stream: asyncio.StreamReader, name: str) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            self.last_activity = self.last_output = time.monotonic()
            room = LIMITS["output_bytes"] - self.out_bytes
            self.out_bytes += len(chunk)
            if room <= 0:
                self.kill("output_limit")
                break
            text = decoder.decode(chunk[:room])
            if text:
                await self.emit({"type": "output", "stream": name, "data": text})
            if len(chunk) > room:
                self.kill("output_limit")
                break

    async def _feed(self) -> None:
        assert self.proc and self.proc.stdin
        if self.stdin is None or not self.interactive:
            self.proc.stdin.close()
            return
        self.stdin_open = True
        while True:
            line = await self.stdin.get()
            if line is None:  # EOF (Ctrl+D) or client gone
                self.stdin_open = False
                await self._set_input_wait(False)
                self.proc.stdin.close()
                return
            self.in_bytes += len(line)
            self.last_input = time.monotonic()
            await self._set_input_wait(False)  # the program has input to work on
            if self.in_bytes > LIMITS["stdin_bytes"]:
                self.kill("input_limit")
                return
            self.last_activity = time.monotonic()
            try:
                self.proc.stdin.write(line.encode("utf-8", "replace"))
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                return

    async def _set_input_wait(self, waiting: bool) -> None:
        if waiting != self.input_wait:
            self.input_wait = waiting
            await self.emit({"type": "input_wait", "waiting": waiting})

    async def _input_watch(self) -> None:
        """Tells the client when the program is most likely waiting for input: stdin is open, it has
        printed nothing and got no input for INPUT_WAIT_SECONDS, and (where /proc shows it) used no
        CPU in that time."""
        cpu_prev, cpu_since = None, time.monotonic()
        while self.proc and self.proc.returncode is None:
            await asyncio.sleep(0.2)
            now = time.monotonic()
            cpu = _cpu_ticks(self.proc.pid)
            if cpu != cpu_prev:
                cpu_prev, cpu_since = cpu, now
            quiet = now - max(self.last_output, self.last_input) >= INPUT_WAIT_SECONDS
            idle = cpu is None or now - cpu_since >= INPUT_WAIT_SECONDS
            await self._set_input_wait(self.stdin_open and quiet and idle)

    async def _watchdog(self, started: float) -> None:
        while self.proc and self.proc.returncode is None:
            await asyncio.sleep(0.5)
            now = time.monotonic()
            if now - started > self.wall:
                self.kill("timeout")
            elif self.interactive and now - self.last_activity > LIMITS["idle_seconds"]:
                self.kill("idle")

    async def execute(
        self, argv: List[str], cwd: str, *, memory_mb: int, cpu: int, wall: int, interactive: bool
    ) -> Dict[str, Any]:
        self.wall, self.interactive = wall, interactive
        started = self.last_activity = time.monotonic()
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": cwd, "PYTHONIOENCODING": "utf-8"},
                start_new_session=True, preexec_fn=_preexec(memory_mb, cpu),
            )
        except OSError as e:  # missing toolchain, or a noexec /tmp in the runner container
            logging.getLogger(__name__).error("Could not start %s: %s", argv[0], e)
            self.reason = "start_failed"
            return {"exit_code": None, "duration_ms": 0}
        assert self.proc.stdout and self.proc.stderr
        tasks = [
            asyncio.create_task(self._pump(self.proc.stdout, "stdout")),
            asyncio.create_task(self._pump(self.proc.stderr, "stderr")),
        ]
        helpers = [asyncio.create_task(self._feed()), asyncio.create_task(self._watchdog(started))]
        if interactive:
            helpers.append(asyncio.create_task(self._input_watch()))
        try:
            await asyncio.gather(*tasks)
            await self.proc.wait()
        finally:
            for t in helpers:
                t.cancel()
            if self.proc.returncode is None:
                self.kill(self.reason or "stopped")
                await self.proc.wait()
        code = self.proc.returncode
        if not self.reason and code is not None and code < 0:
            sig = -code
            self.reason = {signal.SIGXCPU: "cpu", signal.SIGKILL: "cpu", signal.SIGSEGV: "crash",
                           signal.SIGXFSZ: "file_size"}.get(sig, "signal")
        return {"exit_code": code, "duration_ms": int((time.monotonic() - started) * 1000)}


_REASON_TEXT = {
    "timeout": "Stopped: the program ran longer than {wall} seconds.",
    "idle": "Stopped: no output or input for {idle} seconds.",
    "output_limit": "Stopped: the program printed too much output (limit {out_kb} KB).",
    "input_limit": "Stopped: too much input was sent to the program.",
    "cpu": "Stopped: the program used more than {cpu} seconds of CPU time (infinite loop?).",
    "file_size": "Stopped: the program tried to write a file larger than {fs} MB.",
    "crash": "The program crashed (segmentation fault).",
    "stopped": "Stopped.",
    "replaced": "Stopped because a new run was started.",
    "start_failed": "The server could not start the program. Please tell the study team.",
}


def reason_text(reason: str, wall: int) -> str:
    return _REASON_TEXT.get(reason, "").format(
        wall=wall, idle=LIMITS["idle_seconds"], out_kb=LIMITS["output_bytes"] // 1000,
        cpu=LIMITS["cpu_seconds"], fs=LIMITS["file_size_mb"],
    )


async def run_code(
    *,
    key: str,
    language: str,
    files: List[Dict[str, str]],
    entry: Optional[str],
    emit: Emit,
    stdin: Optional["asyncio.Queue[Optional[str]]"] = None,
    compile_only: bool = False,
    batch: bool = False,
) -> Dict[str, Any]:
    """Run a program, streaming events through `emit`. `stdin=None` means non-interactive (EOF).
    `compile_only` (C, C++, Java) stops after compiling, like running `gcc` in a terminal.

    Raises RunRejected before starting if the request is invalid or the server is busy.
    """
    language = (language or "").lower()
    files, entry = clean_files(language, files, entry)
    _check_rate(key)
    wall = LIMITS["batch_wall_seconds"] if batch or stdin is None else LIMITS["wall_seconds"]

    run = Run(key, emit, stdin)
    previous = _active.get(key)
    if previous:
        previous.kill("replaced")
    _active[key] = run

    if _slots.locked():
        await emit({"type": "status", "phase": "queued"})
    try:
        await asyncio.wait_for(_slots.acquire(), timeout=LIMITS["queue_seconds"])
    except asyncio.TimeoutError:
        stats["rejected_busy"] += 1
        if _active.get(key) is run:
            del _active[key]
        raise RunRejected("The server is busy running other students' code. Please try again in a moment.")

    stats["started"] += 1
    stats["running"] += 1
    root = tempfile.mkdtemp(prefix="run_")
    work = os.path.join(root, "work")
    result: Dict[str, Any] = {"exit_code": None, "duration_ms": 0}
    try:
        if run.stopped.is_set():  # replaced while queued
            return {"exit_code": None, "duration_ms": 0, "reason": run.reason, "message": reason_text(run.reason, wall)}
        os.mkdir(work)
        for f in files:
            with open(os.path.join(work, f["filename"]), "w", encoding="utf-8") as fh:
                fh.write(f["content"])
        if SANDBOX_UID and os.geteuid() == 0:
            for d, _dirs, fnames in os.walk(root):
                os.chown(d, SANDBOX_UID, SANDBOX_GID)
                for n in fnames:
                    os.chown(os.path.join(d, n), SANDBOX_UID, SANDBOX_GID)
            os.chmod(root, 0o700)

        if language == "python":
            boot = os.path.join(root, "boot.py")
            with open(boot, "w", encoding="utf-8") as fh:
                fh.write(_PY_BOOT)
            if SANDBOX_UID and os.geteuid() == 0:
                os.chown(boot, SANDBOX_UID, SANDBOX_GID)
            argv = [sys.executable, "-I", "-u", "-X", "utf8", boot, work, entry]
            await emit({"type": "status", "phase": "running"})
            result = await run.execute(argv, work, memory_mb=LIMITS["memory_mb"], cpu=LIMITS["cpu_seconds"],
                                       wall=wall, interactive=stdin is not None)
        elif language == "javascript":
            argv = [NODE_BIN, "--no-warnings", "--experimental-permission", f"--allow-fs-read={os.path.realpath(work)}",
                    f"--max-old-space-size={LIMITS['memory_mb']}", "--stack-size=4000", "--import", _NODE_PRELOAD, entry]
            await emit({"type": "status", "phase": "running"})
            result = await run.execute(argv, work, memory_mb=0, cpu=LIMITS["cpu_seconds"],
                                       wall=wall, interactive=stdin is not None)
        elif language == "java":
            await emit({"type": "status", "phase": "compiling"})
            sources = [f["filename"] for f in files if f["filename"].endswith(".java")]
            mb = LIMITS["compile_memory_mb"]
            cres = await run.execute(
                ["javac", f"-J-Xmx{mb // 2}m", "-J-XX:+UseSerialGC", "-J-XX:TieredStopAtLevel=1",
                 "-encoding", "UTF-8", "-Xlint:none", "-d", "../classes", *sources],
                work, memory_mb=0, cpu=LIMITS["compile_seconds"], wall=LIMITS["compile_seconds"], interactive=False,
            )
            main_class = entry[:-5]
            if run.reason or cres["exit_code"] != 0:
                result = {**cres, "compile_error": True}
            elif compile_only:
                result = {**cres, "compiled": True}
            else:
                await emit({"type": "status", "phase": "running"})
                result = await run.execute(
                    ["java", f"-Xmx{LIMITS['memory_mb']}m", *_JAVA_OPTS, "-cp", "../classes", main_class],
                    work, memory_mb=0, cpu=LIMITS["cpu_seconds"], wall=wall, interactive=stdin is not None,
                )
            result["compile_ms"] = cres["duration_ms"]
        else:  # c / cpp
            await emit({"type": "status", "phase": "compiling"})
            init = "sandbox_init.cpp" if language == "cpp" else "sandbox_init.c"
            with open(os.path.join(root, init), "w") as fh:
                fh.write(_C_INIT)
            exe = os.path.join(root, "program")
            if language == "cpp":
                sources = [f["filename"] for f in files if f["filename"].endswith((".cpp", ".cc", ".cxx"))]
                os.makedirs(os.path.join(root, "include", "bits"))
                with open(os.path.join(root, "include", "bits", "stdc++.h"), "w") as fh:
                    fh.write(_STDCXX_SHIM)
                compiler = ["g++", "-std=gnu++17", "-idirafter", "../include"]
            else:
                sources = [f["filename"] for f in files if f["filename"].endswith(".c")]
                compiler = ["gcc", "-std=gnu11"]
            # Relative paths so compiler errors read "main.c:3:5: error: ...".
            cres = await run.execute(
                [*compiler, "-O0", "-Wall", "-o", "../program", *sources, "../" + init, "-lm"],
                work, memory_mb=LIMITS["compile_memory_mb"], cpu=LIMITS["compile_seconds"],
                wall=LIMITS["compile_seconds"], interactive=False,
            )
            if run.reason or cres["exit_code"] != 0 or not os.path.exists(exe):
                result = {**cres, "compile_error": True}
            elif compile_only:
                result = {**cres, "compiled": True}
            else:
                await emit({"type": "status", "phase": "running"})
                result = await run.execute([exe], work, memory_mb=LIMITS["memory_mb"], cpu=LIMITS["cpu_seconds"],
                                           wall=wall, interactive=stdin is not None)
            result["compile_ms"] = cres["duration_ms"]
    finally:
        stats["running"] -= 1
        _slots.release()
        if _active.get(key) is run:
            del _active[key]
        shutil.rmtree(root, ignore_errors=True)

    result["reason"] = run.reason
    result["message"] = reason_text(run.reason, wall) if run.reason else ""
    return result


def stop(key: str) -> None:
    run = _active.get(key)
    if run:
        run.kill("stopped")


async def run_batch(
    *, key: str, language: str, files: List[Dict[str, str]], entry: Optional[str], stdin: Optional[str] = None
) -> Dict[str, Any]:
    """Non-interactive run that collects output (used by the agent's run_code tool and /execute).
    `stdin` text, if given, is fed to the program followed by end-of-input."""
    out: List[str] = []
    err: List[str] = []

    async def collect(ev: Dict[str, Any]) -> None:
        if ev["type"] == "output":
            (out if ev["stream"] == "stdout" else err).append(ev["data"])

    started = time.monotonic()
    try:
        queue: Optional["asyncio.Queue[Optional[str]]"] = None
        if stdin:
            queue = asyncio.Queue()
            queue.put_nowait(stdin[: LIMITS["stdin_bytes"]])
            queue.put_nowait(None)
        res = await run_code(key=key, language=language, files=files, entry=entry, emit=collect, stdin=queue, batch=True)
    except RunRejected as e:
        return {"success": False, "output": "", "error": str(e), "execution_time": 0.0}
    error = "".join(err)
    if res.get("message"):
        error = (error + "\n" + res["message"]).strip()
    return {
        "success": res.get("exit_code") == 0 and not res.get("reason"),
        "output": "".join(out),
        "error": error,
        "execution_time": time.monotonic() - started,
    }


async def serve_websocket(ws: Any, start: Optional[Dict[str, Any]] = None) -> None:
    """Drive one interactive run over an accepted Starlette WebSocket (see runner.py for the protocol)."""
    from starlette.websockets import WebSocketDisconnect
    if start is None:
        try:
            start = await asyncio.wait_for(ws.receive_json(), timeout=10)
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
            await ws.close()
            return
    key = str(start.get("key") or "")
    stdin: "asyncio.Queue[Optional[str]]" = asyncio.Queue()
    closed = False

    async def emit(ev: Dict[str, Any]) -> None:
        if not closed:
            try:
                await ws.send_json(ev)
            except Exception:
                pass

    async def read_client() -> None:
        nonlocal closed
        try:
            while True:
                msg = await ws.receive_json()
                t = msg.get("type")
                if t == "stdin":
                    await stdin.put(str(msg.get("data", ""))[:4000])
                elif t == "eof":
                    await stdin.put(None)
                elif t == "stop":
                    stop(key)
        except (WebSocketDisconnect, RuntimeError, ValueError):
            closed = True
            stop(key)
            await stdin.put(None)

    reader = asyncio.create_task(read_client())
    try:
        result = await run_code(
            key=key, language=str(start.get("language") or ""), files=list(start.get("files") or []),
            entry=start.get("entry"), emit=emit, stdin=stdin, compile_only=bool(start.get("compile_only")),
        )
        await emit({"type": "exit", **result})
    except RunRejected as e:
        await emit({"type": "rejected", "message": str(e)})
    except Exception as e:  # never leave the client hanging
        logging.getLogger(__name__).exception("run failed")
        await emit({"type": "rejected", "message": f"Could not run the program: {e}"})
    finally:
        reader.cancel()
        if not closed:
            try:
                await ws.close()
            except Exception:
                pass
