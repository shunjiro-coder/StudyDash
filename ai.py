"""Wrapper around the `claude` CLI (subprocess).

Environment facts baked in (measured):
- `claude` is NOT on PATH; real binary at ~/.local/bin/claude. Resolve absolute.
- Nested `claude -p` works even under CLAUDECODE=1 (keychain auth passes).
- Reading an image path works when cwd is the project root and --add-dir is
  passed for uploads/. If unreadable it fails SILENTLY (no is_error) -> so we
  always run with cwd=BASE_DIR and add the uploads dir.
- --output-format json returns {result, is_error, subtype, total_cost_usd,...}.
  is_error:true is also returned on rate-limit, so treat it as a failure.
- Cost ~ $0.05/call; models are chosen per task via --model (settings.json).
"""

import json
import os
import shutil
import signal
import subprocess

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class ClaudeError(Exception):
    """Raised when the claude call fails (not found, timeout, non-JSON, is_error)."""

    def __init__(self, message, raw=None, cost=0.0):
        super().__init__(message)
        self.raw = raw
        self.cost = cost


IS_WINDOWS = os.name == "nt"


def resolve_claude():
    """Absolute path to the claude binary (settings override > PATH > ~/.local).

    On Windows the CLI installs as claude.cmd; shutil.which consults PATHEXT, so
    the plain name still resolves. _spawn_cmd handles actually executing it.
    """
    s = db.load_settings()
    override = s.get("claude_bin")
    if override:
        return os.path.expanduser(override)
    found = shutil.which("claude")
    if found:
        return found
    if IS_WINDOWS:
        return os.path.expanduser(r"~\AppData\Local\Programs\claude\claude.cmd")
    return os.path.expanduser("~/.local/bin/claude")


def _spawn_cmd(cmd):
    """Argv for Popen. A .cmd/.bat shim is not a PE image, so CreateProcess cannot
    run it directly — it has to go through the command processor."""
    if IS_WINDOWS and cmd and cmd[0].lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", *cmd]
    return cmd


def _kill_tree(proc):
    """Kill the timed-out claude and any grandchildren it spawned. os.killpg does
    not exist on Windows, so an AttributeError here would escape the caller's
    guard and crash the ingest worker instead of failing the call cleanly."""
    try:
        if IS_WINDOWS:
            proc.kill()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        pass


def check_claude():
    """Returns (ok, path). ok means the binary exists and is executable."""
    path = resolve_claude()
    ok = bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)
    return ok, path


def model_for(kind):
    """kind: 'image' or 'text' -> configured model alias (or None = default)."""
    s = db.load_settings()
    return s.get("model_image") if kind == "image" else s.get("model_text")


DEFAULT_TIMEOUT = 180
MAX_TIMEOUT = 900


def timeout_for(path=None):
    """How long to allow a claude call, scaled by how much document it has to read.

    A flat 180s silently killed a 646KB, 327-entry vocabulary PDF: transcribing a
    long word list simply takes longer than answering questions about a slide. The
    budget grows with file size and is capped so a runaway call still dies.
    Overridable via the `ai_timeout_sec` setting.
    """
    s = db.load_settings()
    override = s.get("ai_timeout_sec")
    if override:
        try:
            return max(30, min(MAX_TIMEOUT, int(override)))
        except (TypeError, ValueError):
            pass
    base = DEFAULT_TIMEOUT
    try:
        if path and os.path.exists(path):
            mb = os.path.getsize(path) / (1024.0 * 1024.0)
            base += int(mb * 240)      # ~4 extra minutes per MB of source
    except OSError:
        pass
    return max(DEFAULT_TIMEOUT, min(MAX_TIMEOUT, base))


def run_claude(prompt, model=None, add_dirs=None, allowed_tools="Read",
               timeout=None, cwd=None):
    """Run `claude -p` and return the parsed JSON dict.

    Raises ClaudeError on: binary missing, timeout (child + group killed),
    non-JSON output, or is_error:true (incl. rate limit).
    Returns dict with keys like 'result', 'total_cost_usd', 'is_error'.
    """
    if timeout is None:
        timeout = timeout_for()
    ok, claude = check_claude()
    if not ok:
        raise ClaudeError(f"claude CLI not found or not executable: {claude}")

    cmd = [claude, "-p", prompt, "--output-format", "json",
           "--allowedTools", allowed_tools]
    if model:
        cmd += ["--model", model]
    for d in (add_dirs or []):
        cmd += ["--add-dir", d]

    # start_new_session=True -> child is a process-group leader so we can kill
    # the whole tree (claude may spawn grandchildren) on timeout.
    try:
        proc = subprocess.Popen(
            _spawn_cmd(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=cwd or BASE_DIR,
            # POSIX: own process group so the whole tree can be killed. Windows
            # has no setsid; CREATE_NEW_PROCESS_GROUP is the nearest equivalent.
            start_new_session=not IS_WINDOWS,
            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
               if IS_WINDOWS else {}),
        )
    except OSError as e:
        # spawn itself failed (ENOEXEC, EACCES, too many procs, ...). Without
        # this wrap the material stays 'extracting' forever instead of failing.
        raise ClaudeError(f"failed to spawn claude: {e}")
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise ClaudeError(f"claude timed out after {timeout}s")

    if not out:
        raise ClaudeError(
            f"claude produced no output (exit {proc.returncode}): {(err or '')[:300]}")

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise ClaudeError(f"claude returned non-JSON: {out[:300]}", raw=out)

    cost = float(data.get("total_cost_usd") or 0.0)
    if data.get("is_error"):
        subtype = data.get("subtype") or "error"
        raise ClaudeError(
            f"claude reported is_error ({subtype})",
            raw=data.get("result") or out, cost=cost)
    return data


def result_text(data):
    """Extract the assistant's textual result from a run_claude() dict."""
    return (data.get("result") or "").strip()


def extract_json(text):
    """Pull the first JSON object out of a model response.

    Handles ```json fences and leading/trailing prose. Returns the parsed
    object, or raises ValueError if no valid JSON object is present.
    """
    if not text:
        raise ValueError("empty response")
    t = text.strip()
    # strip code fences if present
    if "```" in t:
        import re
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
        if m:
            t = m.group(1).strip()
    # try whole string first, then the outermost {...} span
    for candidate in (t, _outermost_braces(t)):
        if candidate is None:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ValueError("no valid JSON object found")


def _outermost_braces(t):
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return t[start:end + 1]


if __name__ == "__main__":
    ok, path = check_claude()
    print(f"claude ok={ok} path={path}")
