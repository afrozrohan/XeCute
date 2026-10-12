import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

import streamlit as st

PROJECT_DIR = Path(__file__).resolve().parent
WORKSPACE = PROJECT_DIR / "workspace"
LOG_FILE = PROJECT_DIR / "logs" / "runs.jsonl"

DEFAULT_MAX_RUNTIME = 300
MAX_LOG_LINES = 5000
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
TEXT_PREVIEW_SUFFIXES = {".txt", ".md", ".csv", ".json", ".log"}

DEFAULT_TASK = (
    "list latest emails from the last 2 days" 
)
EXAMPLES = {

    "Invoice email -> summary saved as a file": (
        "Find the latest invoice email, download its PDF attachment, read it, "
        "and save a short summary to invoice_summary.txt in the workspace."
    ),
    "List workspace files": (
        "List the files in the workspace and briefly describe each one."
    ),
    "Show open windows": (
        "List my visible desktop windows and say which ones look like "
        "text editors."
    ),
}
STATUS_ICONS = {"completed": "✅", "failed": "❌", "needs_review": "⚠️"}


def backend_command() -> list[str]:
    """Use `uv run` only for uv projects; otherwise the current interpreter."""
    if shutil.which("uv") and (PROJECT_DIR / "pyproject.toml").exists():
        return ["uv", "run", "python", "-u", "main.py"]
    return [sys.executable, "-u", "main.py"]


class AgentRun:
    """One execution of main.py, observed from background threads."""

    def __init__(self, task: str, max_runtime: int, extra_env: dict, drop_env: list):
        self.id = uuid.uuid4().hex[:8]
        self.task = " ".join(task.split())
        self.max_runtime = max_runtime
        self.extra_env = extra_env
        self.drop_env = drop_env

        self.lines: list[str] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        self.process = None
        self.started = time.monotonic()
        self.ended = None
        self.returncode = None
        self.timed_out = False
        self.stopped = False
        self.start_error = None
        self.record = None
        self._log_offset = 0

    # lifecycle
    def start(self) -> None:
        self.started = time.monotonic()
        try:
            self._log_offset = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
        except OSError:
            self._log_offset = 0

        env = os.environ.copy()
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
        env.update(self.extra_env)
        for name in self.drop_env:
            env.pop(name, None)

        # New process group / session so the whole tree can be killed.
        group_args = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )

        try:
            self.process = subprocess.Popen(
                backend_command(),
                cwd=PROJECT_DIR,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                **group_args,
            )
        except OSError as error:
            self.start_error = f"{type(error).__name__}: {error}"
            self.ended = time.monotonic()
            return

        try:
            self.process.stdin.write(self.task + "\n")
            self.process.stdin.flush()
            self.process.stdin.close()
        except OSError:
            pass  # Process already died; the output will show why.

        self._reader = threading.Thread(target=self._read_output, daemon=True)
        self._reader.start()
        threading.Thread(target=self._supervise, daemon=True).start()

    def stop(self) -> None:
        self._stop_event.set()

    @property
    def running(self) -> bool:
        return self.ended is None

    @property
    def elapsed(self) -> float:
        return (self.ended or time.monotonic()) - self.started

    def text(self) -> str:
        with self._lock:
            return "".join(self.lines)

    # -- background threads ------------------------------------------------
    def _read_output(self) -> None:
        try:
            for line in self.process.stdout:
                with self._lock:
                    self.lines.append(line)
                    if len(self.lines) > MAX_LOG_LINES:
                        del self.lines[: len(self.lines) - MAX_LOG_LINES]
        except (ValueError, OSError):
            pass

    def _kill_tree(self) -> None:
        proc = self.process
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                )
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass

    def _supervise(self) -> None:
        deadline = self.started + self.max_runtime
        while self.process.poll() is None:
            if self._stop_event.is_set():
                self.stopped = True
                self._kill_tree()
                break
            if time.monotonic() > deadline:
                self.timed_out = True
                self._kill_tree()
                break
            time.sleep(0.25)

        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self._reader.join(timeout=3)

        self.returncode = self.process.returncode
        self.record = self._load_record()
        self.ended = time.monotonic()  # Set last: marks the run as finished.

    def _load_record(self):
        """Read the structured record main.py appended during this run."""
        try:
            if not LOG_FILE.exists():
                return None
            with LOG_FILE.open("rb") as file:
                file.seek(self._log_offset)
                data = file.read().decode("utf-8", errors="replace")
            lines = [ln for ln in data.splitlines() if ln.strip()]
            return json.loads(lines[-1]) if lines else None
        except (OSError, json.JSONDecodeError):
            return None


@st.cache_resource
def run_registry() -> dict:
    """Process-wide registry so two browser tabs can't drive the desktop at once."""
    return {"active": None}


# --------------------------------------------------------------------------
# Output parsing
# --------------------------------------------------------------------------

def parse_output(text: str) -> dict:
    info = {
        "tools": [],
        "phase": "Starting agent...",
        "final": None,
        "status": None,
        "retries": 0,
        "attempts": [],
        "error": None,
    }

    for match in re.finditer(
        r"\[DEBUG\] Agent response: tool_calls = \[(.*?)\]", text
    ):
        info["tools"].extend(re.findall(r"['\"]([^'\"]+)['\"]", match.group(1)))

    for line in reversed(text.splitlines()):
        if "[DEBUG] Entered recover_node" in line:
            info["phase"] = "Recovering from a failed attempt"
            break
        if "[DEBUG] Entered verify_node" in line or "Verification verdict" in line:
            info["phase"] = "Verifying the result"
            break
        match = re.search(r"\[DEBUG\] Agent response: tool_calls = \[(.*?)\]", line)
        if match:
            names = re.findall(r"['\"]([^'\"]+)['\"]", match.group(1))
            info["phase"] = (
                f"Ran {', '.join(names)}; thinking about the next step"
                if names
                else "Writing the final answer"
            )
            break

    final = re.search(
        r"--- Agent response ---\n(.*?)\n\s*--- Task state ---", text, re.DOTALL
    )
    if final:
        info["final"] = final.group(1).strip() or None

    statuses = re.findall(r"^Status: (\w+)\s*$", text, re.MULTILINE)
    if statuses:
        info["status"] = statuses[-1]

    retries = re.findall(r"^Retry count: (\d+)", text, re.MULTILINE)
    if retries:
        info["retries"] = int(retries[-1])

    info["attempts"] = [
        {"attempt": int(n), "status": s, "reason": r.strip()}
        for n, s, r in re.findall(
            r"^Attempt (\d+): (\w+) - (.*)$", text, re.MULTILINE
        )
    ]

    error = re.search(r"^Task (?:failed|stopped): (.*)$", text, re.MULTILINE)
    if error:
        info["error"] = error.group(1).strip()
    elif "Traceback (most recent call last)" in text:
        info["error"] = "The backend raised an unhandled exception (see log)."

    return info


def describe_outcome(run: AgentRun, info: dict):
    """Return (streamlit level, title, detail) for a finished run."""
    last_reason = info["attempts"][-1]["reason"] if info["attempts"] else ""

    if run.start_error:
        return "error", "Could not start the backend", run.start_error
    if run.stopped:
        return "warning", "Stopped by you", "The agent was terminated early."
    if run.timed_out:
        return (
            "error",
            "Timed out",
            f"The run exceeded {run.max_runtime} seconds and was terminated.",
        )
    if run.returncode not in (0, None):
        return (
            "error",
            f"Backend crashed (exit code {run.returncode})",
            info["error"] or "See the execution log below.",
        )
    if info["error"] and info["status"] != "completed":
        return "error", "The agent run failed", info["error"]
    if info["status"] == "completed":
        return "success", "Completed and verified", last_reason
    if info["status"] == "failed":
        return "error", "Task failed verification", last_reason
    if info["status"] == "needs_review":
        return "warning", "Finished, but needs your review", last_reason
    return "warning", "Finished", "No verification status was reported."


def fmt_seconds(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


# --------------------------------------------------------------------------
# UI pieces
# --------------------------------------------------------------------------

def render_run() -> None:
    """Live view of the current run (re-executed every second while running)."""
    run = st.session_state.get("run")
    if run is None:
        return

    # When the run ends, do one full rerun so buttons/history refresh.
    if not run.running and st.session_state.get("_finalized_run") != run.id:
        st.session_state["_finalized_run"] = run.id
        st.rerun()

    text = run.text()
    info = parse_output(text)

    if run.running:
        st.info(f"⏳ {info['phase']} · {fmt_seconds(run.elapsed)} elapsed")
    else:
        level, title, detail = describe_outcome(run, info)
        getattr(st, level)(f"**{title}**" + (f"\n\n{detail}" if detail else ""))

    col1, col2, col3 = st.columns(3)
    col1.metric("Elapsed", fmt_seconds(run.elapsed))
    col2.metric("Tool calls", len(info["tools"]))
    col3.metric("Retries", info["retries"])

    if info["final"] and not run.running:
        st.subheader("Result")
        with st.container(border=True):
            st.markdown(info["final"])

    record = run.record or {}
    steps = record.get("tool_calls") or [
        {"name": name, "args": {}} for name in info["tools"]
    ]
    with st.expander("Steps taken", expanded=run.running):
        if not steps:
            st.caption("No tool calls yet.")
        for index, step in enumerate(steps, 1):
            args = json.dumps(step.get("args") or {}, ensure_ascii=False)
            args = args.replace("`", "'")
            args = args if len(args) <= 140 else args[:140] + "..."
            suffix = f" - `{args}`" if args != "{}" else ""
            st.markdown(f"{index}. **{step.get('name', '?')}**{suffix}")

    if info["attempts"]:
        with st.expander("Verification attempts"):
            for attempt in info["attempts"]:
                icon = STATUS_ICONS.get(attempt["status"], "•")
                st.markdown(
                    f"{icon} **Attempt {attempt['attempt']}: "
                    f"{attempt['status']}** - {attempt['reason']}"
                )

    with st.expander("Execution log", expanded=False):
        tail = "".join(text.splitlines(keepends=True)[-300:])
        st.code(tail or "(no output yet)", language="text")
        if not run.running:
            st.download_button(
                "Download full log",
                data=text,
                file_name=f"run_{run.id}.log",
                mime="text/plain",
            )


def load_history(limit: int = 30) -> list[dict]:
    if not LOG_FILE.exists():
        return []
    records = []
    with LOG_FILE.open(encoding="utf-8", errors="replace") as file:
        for line in deque(file, maxlen=limit):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records[::-1]


def local_time(timestamp: str) -> str:
    try:
        return datetime.fromisoformat(timestamp).astimezone().strftime("%d %b %H:%M")
    except (ValueError, TypeError):
        return timestamp or "?"


def render_history() -> None:
    records = load_history()
    if not records:
        st.caption("No runs recorded yet.")
        return

    rows = [
        {
            "When": local_time(rec.get("timestamp", "")),
            "Status": f"{STATUS_ICONS.get(rec.get('status'), '•')} {rec.get('status', '?')}",
            "Duration": fmt_seconds(rec.get("duration_seconds", 0)),
            "Tool calls": len(rec.get("tool_calls", [])),
            "Task": (rec.get("task") or "")[:100],
        }
        for rec in records
    ]
    st.dataframe(rows, hide_index=True)

    choice = st.selectbox(
        "Inspect a run",
        range(len(records)),
        format_func=lambda i: f"{rows[i]['When']} - {rows[i]['Task'][:60]}",
    )
    st.json(records[choice], expanded=False)


def render_workspace() -> None:
    if not WORKSPACE.exists():
        st.caption("The workspace folder does not exist yet.")
        return

    files = sorted(
        (p for p in WORKSPACE.rglob("*") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:50]

    if not files:
        st.caption("The workspace is empty.")
        return

    st.dataframe(
        [
            {
                "File": p.relative_to(WORKSPACE).as_posix(),
                "Size (KB)": round(p.stat().st_size / 1024, 1),
                "Modified": datetime.fromtimestamp(p.stat().st_mtime).strftime(
                    "%d %b %H:%M"
                ),
            }
            for p in files
        ],
        hide_index=True,
    )

    selected = st.selectbox(
        "Open a file",
        files,
        format_func=lambda p: p.relative_to(WORKSPACE).as_posix(),
    )

    if selected.stat().st_size > MAX_DOWNLOAD_BYTES:
        st.caption("File is too large to download from here.")
        return

    data = selected.read_bytes()
    if selected.suffix.lower() in TEXT_PREVIEW_SUFFIXES:
        st.code(data[:5000].decode("utf-8", errors="replace"), language="text")
    st.download_button("Download", data=data, file_name=selected.name)


def render_sidebar() -> dict:
    with st.sidebar:
        st.header("Settings")
        max_runtime = st.slider(
            "Max runtime (seconds)", 60, 900, DEFAULT_MAX_RUNTIME, step=30
        )
        model = st.text_input(
            "Model override (optional)",
            placeholder="uses OPENROUTER_MODEL from .env",
            help="Free routed models are often unreliable at tool calling.",
        ).strip()
        allow_risky = st.checkbox(
            "Allow risky desktop actions",
            value=False,
            help="Lets the agent click controls such as Close, Send, Delete "
            "and Submit. Leave off unless you are watching the run.",
        )
        if allow_risky:
            st.warning("The agent may now close apps or send/submit things.")

        with st.expander("Setup check"):
            checks = {
                ".env file": (PROJECT_DIR / ".env").exists(),
                "Gmail credentials.json": (PROJECT_DIR / "credentials.json").exists(),
                "Gmail token.json (after first login)": (
                    PROJECT_DIR / "token.json"
                ).exists(),
                "workspace folder": WORKSPACE.exists(),
                "main.py": (PROJECT_DIR / "main.py").exists(),
            }
            for name, ok in checks.items():
                st.write(("✅ " if ok else "⚠️ ") + name)
            st.caption("Backend: `" + " ".join(backend_command()) + "`")

    extra_env, drop_env = {}, []
    if model:
        extra_env["OPENROUTER_MODEL"] = model
    if allow_risky:
        extra_env["ALLOW_RISKY_DESKTOP_ACTIONS"] = "1"
    else:
        drop_env.append("ALLOW_RISKY_DESKTOP_ACTIONS")

    return {"max_runtime": max_runtime, "extra_env": extra_env, "drop_env": drop_env}


def load_example() -> None:
    choice = st.session_state.get("example")
    if choice in EXAMPLES:
        st.session_state["task_text"] = EXAMPLES[choice]


def main() -> None:
    st.set_page_config(page_title="AI Task Worker", page_icon="🤖", layout="wide")

    if not hasattr(st, "fragment"):
        st.error("This app needs Streamlit 1.37 or newer: `pip install -U streamlit`.")
        st.stop()

    st.title("🤖 AI Task Worker")
    st.caption("Give your autonomous agent a task and watch it work.")

    settings = render_sidebar()
    st.session_state.setdefault("task_text", DEFAULT_TASK)

    run = st.session_state.get("run")
    running = run is not None and run.running

    tab_run, tab_history, tab_files = st.tabs(["Run", "History", "Workspace"])

    with tab_run:
        st.selectbox(
            "Load an example",
            ["Choose an example..."] + list(EXAMPLES),
            key="example",
            on_change=load_example,
            disabled=running,
        )
        task = st.text_area(
            "What should the AI worker do?",
            key="task_text",
            height=130,
            disabled=running,
            placeholder="Describe the task you want the agent to perform...",
        )

        col_run, col_stop, _ = st.columns([1, 1, 5])
        run_clicked = col_run.button("▶ Run task", type="primary", disabled=running)
        stop_clicked = col_stop.button("■ Stop", disabled=not running)

        if stop_clicked and running:
            run.stop()

        if run_clicked:
            registry = run_registry()
            active = registry["active"]
            if not task.strip():
                st.warning("Please enter a task.")
            elif active is not None and active.running:
                st.warning(
                    "Another agent run is already in progress (possibly in "
                    "another browser tab). Wait for it to finish."
                )
            else:
                new_run = AgentRun(
                    task,
                    settings["max_runtime"],
                    settings["extra_env"],
                    settings["drop_env"],
                )
                registry["active"] = new_run
                new_run.start()
                st.session_state["run"] = new_run
                st.rerun()

        run = st.session_state.get("run")
        if run is not None:
            st.divider()
            st.fragment(render_run, run_every=1 if run.running else None)()

    with tab_history:
        render_history()

    with tab_files:
        render_workspace()

    st.divider()
    st.caption("Local prototype · LangGraph backend · logs in logs/runs.jsonl")


if __name__ == "__main__":
    main()