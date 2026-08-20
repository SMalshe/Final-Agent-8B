"""Real-filesystem tools — opt-in, NOT part of the benchmark.

The benchmark registry in tools.py is deliberately untouched by this module:
nothing here is imported by bench/, so raw-vs-harness stays comparable with
runs already on disk. The per-model agents in agents/ call enable() at
startup, which injects these tools into the shared TOOLS dict *in that
process only*.

Scoping model (same shape as Claude Code / Codex):
    root        every path is resolved against it and must stay inside it
    deny-list   system dirs and this project's live experiment are never
                writable, even when root is a drive root
    confirm     destructive calls (overwrite/delete/move/shell) go through a
                callback the runner wires to a y/n prompt; --yolo bypasses it

A 1B model that scores 0.3 on "put the right number in a spreadsheet" will
eventually issue a wrong delete_path. Choose root accordingly.
"""
import os
import shutil
import subprocess
import sys

from .tools import TOOLS
from .world import ToolError

MAX_READ_BYTES = 200_000
MAX_OUTPUT_CHARS = 4_000
MAX_LIST_ENTRIES = 300
COMMAND_TIMEOUT = 60

def _eg(*parts):
    """A path example in this platform's separator. The model copies these
    verbatim, and a Windows example on POSIX makes one file literally named
    "notes\\todo.txt" instead of todo.txt inside notes/."""
    return os.path.join(*parts)


_ROOT = None      # primary root: where a new relative path lands, and the shell cwd
_ROOTS = []       # every root, primary first
_LABELS = {}      # label -> root. With more than one root the model sees and
                  # writes paths as "<label>/rest", so "which folder" is part of
                  # the path rather than something it has to remember.
_ALLOW_SHELL = False
_CONFIRM = None          # callable(action:str, detail:str) -> bool

def _default_deny():
    """Locations never writable, whatever the root is.

    This list was Windows absolute paths only - C:\\Windows, Program Files, and
    the lab machine's own SAIL folder. On macOS and Linux not one of them
    exists, so the guard matched nothing and a root of "/" had no protected
    locations at all. The system paths now follow the platform, and the two
    entries that are about THIS project rather than the OS are derived from
    __file__, so they hold on every machine instead of only the one the paths
    were typed on.
    """
    if os.name == "nt":
        drive = os.environ.get("SystemDrive", "C:") + os.sep
        paths = [os.environ.get("SystemRoot", r"C:\Windows"),
                 os.path.join(drive, "Program Files"),
                 os.path.join(drive, "Program Files (x86)")]
    else:
        paths = ["/System", "/Library", "/usr", "/bin", "/sbin", "/etc", "/var",
                 "/opt", "/boot"]
    # The agent's own engine and the interpreter running it. An agent that can
    # rewrite harness/ can rewrite the rules it is being held to.
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths += [os.path.dirname(os.path.abspath(__file__)),   # harness/
              os.path.join(project, "webui"),
              os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))]
    return [p for p in paths if p]


_DENY_WRITE = _default_deny()


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def _within(path, root):
    path, root = _norm(path), _norm(root)
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


def _root_of(path):
    """The root that contains path, or None."""
    return next((r for r in _ROOTS if _within(path, r)), None)


def _label_of(root):
    return next((l for l, r in _LABELS.items() if _norm(r) == _norm(root)), "")


def _label_for(root, taken):
    base = (os.path.basename(root.rstrip("\\/"))
            or root.strip("\\/").replace(":", "") or "root")
    label, n = base, 2
    while label in taken:
        label, n = f"{base}{n}", n + 1
    return label


def _roots_listing():
    return "\n".join(f"{l}/  ({r})" for l, r in _LABELS.items())


def _resolve(rel, write=False):
    """Resolve a model-supplied path against the root and enforce the scope."""
    if not isinstance(rel, str) or not rel.strip():
        raise ToolError(f'path is required, e.g. "{_eg("notes", "todo.txt")}"')
    raw = os.path.expandvars(os.path.expanduser(rel.strip().strip('"')))
    if os.path.isabs(raw):
        path = os.path.abspath(raw)
        root = _root_of(path)
    else:
        head, _, rest = raw.replace("\\", "/").partition("/")
        if len(_ROOTS) > 1 and head in _LABELS:
            # "work/notes/todo.txt" - the model named the root itself
            root = _LABELS[head]
            path = os.path.abspath(os.path.join(root, rest))
        else:
            # A bare relative path: prefer the root where it already exists, so
            # "report.xlsx" finds the one that is actually there. Falling back
            # to the primary root is what makes a NEW file land somewhere
            # predictable.
            tries = [(r, os.path.abspath(os.path.join(r, raw))) for r in _ROOTS]
            root, path = next((t for t in tries if os.path.exists(t[1])), tries[0])
    if root is None or not _within(path, root):
        where = (f"the allowed roots:\n{_roots_listing()}" if len(_ROOTS) > 1
                 else f"the allowed root {_ROOT}")
        raise ToolError(f"path is outside {where}; stay inside")
    if write:
        for denied in _DENY_WRITE:
            if _within(path, denied):
                raise ToolError(f"{path} is in a protected location and cannot be modified")
    return path


def _ask(action, detail):
    if _CONFIRM is None:
        return True
    if not _CONFIRM(action, detail):
        raise ToolError(f"the user declined the {action}. Do not retry it; choose another approach.")
    return True


def _rel(path):
    """How a path is shown to the model. With several roots it carries its
    label, so what comes back is what can be passed straight back in."""
    if len(_ROOTS) > 1:
        root = _root_of(path)
        if root:
            rel = os.path.relpath(path, root)
            label = _label_of(root)
            return label + "/" if rel == "." else f"{label}/{rel}"
    try:
        return os.path.relpath(path, _ROOT)
    except ValueError:
        return path


def _clip(text, limit=MAX_OUTPUT_CHARS):
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"


# ------------------------------------------------------------------ tools ---

def _list_dir(a):
    asked = str(a.get("path", ".") or ".").strip()
    if len(_ROOTS) > 1 and asked in (".", "", "/", "\\"):
        # There is no single "here" with several roots, so listing nothing in
        # particular lists the roots.
        return "working folders:\n" + _roots_listing()
    path = _resolve(a.get("path", "."))
    if not os.path.isdir(path):
        raise ToolError(f"{_rel(path)} is not a directory")
    out = []
    for name in sorted(os.listdir(path))[:MAX_LIST_ENTRIES]:
        full = os.path.join(path, name)
        if os.path.isdir(full):
            out.append(f"{name}/")
        else:
            try:
                out.append(f"{name} ({os.path.getsize(full)} bytes)")
            except OSError:
                out.append(name)
    if not out:
        return f"{_rel(path)} is empty"
    return f"{_rel(path)} contains:\n" + "\n".join(out)


def _read_file(a):
    path = _resolve(a.get("path"))
    if not os.path.isfile(path):
        raise ToolError(f"{_rel(path)} does not exist or is not a file")
    size = os.path.getsize(path)
    if size > MAX_READ_BYTES:
        raise ToolError(f"{_rel(path)} is {size} bytes, too large to read (limit {MAX_READ_BYTES})")
    with open(path, "rb") as f:
        blob = f.read()
    if b"\x00" in blob[:2000]:
        raise ToolError(f"{_rel(path)} looks like a binary file; it cannot be read as text")
    return _clip(blob.decode("utf-8", errors="replace"))


def _write_file(a):
    path = _resolve(a.get("path"), write=True)
    content = a.get("content")
    if content is None:
        raise ToolError("missing required parameter 'content'")
    content = content if isinstance(content, str) else str(content)
    if os.path.exists(path):
        _ask("overwrite", f"{path} ({os.path.getsize(path)} bytes will be replaced)")
    os.makedirs(os.path.dirname(path) or _ROOT, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return f"wrote {len(content)} characters to {_rel(path)}"


def _append_file(a):
    path = _resolve(a.get("path"), write=True)
    text = a.get("text")
    if text is None:
        raise ToolError("missing required parameter 'text'")
    os.makedirs(os.path.dirname(path) or _ROOT, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(str(text) + "\n")
    return f"appended 1 line to {_rel(path)}"


def _delete_path(a):
    path = _resolve(a.get("path"), write=True)
    if not os.path.exists(path):
        raise ToolError(f"{_rel(path)} does not exist")
    if os.path.isdir(path):
        n = sum(len(files) for _, _, files in os.walk(path))
        _ask("delete", f"{path} (directory containing {n} files)")
        shutil.rmtree(path)
        return f"deleted directory {_rel(path)} and {n} files"
    _ask("delete", f"{path} ({os.path.getsize(path)} bytes)")
    os.remove(path)
    return f"deleted {_rel(path)}"


def _move_path(a):
    src = _resolve(a.get("path"), write=True)
    dst = _resolve(a.get("to"), write=True)
    if not os.path.exists(src):
        raise ToolError(f"{_rel(src)} does not exist")
    if os.path.exists(dst):
        raise ToolError(f"{_rel(dst)} already exists; delete it first or choose another name")
    _ask("move", f"{src} -> {dst}")
    os.makedirs(os.path.dirname(dst) or _ROOT, exist_ok=True)
    shutil.move(src, dst)
    return f"moved {_rel(src)} to {_rel(dst)}"


def _search_files(a):
    query = a.get("query")
    if not query:
        raise ToolError("missing required parameter 'query'")
    query = str(query).lower()
    root = _resolve(a.get("path", "."))
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if query in name.lower():
                hits.append(f"{_rel(full)} (filename match)")
            elif os.path.splitext(name)[1].lower() in (
                    ".txt", ".md", ".py", ".json", ".csv", ".ps1", ".log", ".ini", ".yml", ".yaml"):
                try:
                    if os.path.getsize(full) > MAX_READ_BYTES:
                        continue
                    with open(full, encoding="utf-8", errors="ignore") as f:
                        for i, line in enumerate(f, 1):
                            if query in line.lower():
                                hits.append(f"{_rel(full)}:{i}: {line.strip()[:120]}")
                                break
                except OSError:
                    continue
            if len(hits) >= 40:
                return "found (showing first 40):\n" + "\n".join(hits)
    return ("found:\n" + "\n".join(hits)) if hits else f"no matches for {query!r} under {_rel(root)}"


def _shell_argv(cmd):
    """How to hand one command line to this machine's shell.

    It was hardcoded to powershell.exe, so shell mode raised FileNotFoundError
    on every macOS and Linux box - the flag existed, the tool was advertised in
    the prompt, and the first call died. SHELL is honoured when it is set so a
    command behaves the way it does in the user's own terminal.
    """
    if os.name == "nt":
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd]
    return [os.environ.get("SHELL") or "/bin/sh", "-c", cmd]


SHELL_NAME = "PowerShell" if os.name == "nt" else "shell"


def _run_command(a):
    if not _ALLOW_SHELL:
        raise ToolError("shell access is disabled for this agent; use the file tools instead")
    cmd = a.get("command")
    if not cmd:
        raise ToolError("missing required parameter 'command'")
    _ask("shell command", str(cmd))
    try:
        proc = subprocess.run(
            _shell_argv(str(cmd)),
            cwd=_ROOT, capture_output=True, text=True, timeout=COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ToolError(f"command timed out after {COMMAND_TIMEOUT}s")
    out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    return _clip(f"exit code {proc.returncode}\n{out.strip() or '(no output)'}")


_FS_TOOLS = {
    "list_dir": {
        "desc": "List the files and folders in a directory on the real computer.",
        "params": {"path": ("string, folder path relative to the working root; omit for the root itself", False)},
        "example": {"tool": "list_dir", "args": {"path": "."}},
        "run": lambda w, m, a: _list_dir(a),
    },
    "read_file": {
        "desc": "Read the text contents of a real file on this computer.",
        "params": {"path": ("string, path to the file", True)},
        "example": {"tool": "read_file", "args": {"path": _eg("notes", "todo.txt")}},
        "run": lambda w, m, a: _read_file(a),
    },
    "write_file": {
        "desc": "Create a real file, or replace its entire contents. Writes the exact text given.",
        "params": {"path": ("string, path to the file", True),
                   "content": ("string, the full text to write", True)},
        "example": {"tool": "write_file", "args": {"path": _eg("notes", "summary.txt"),
                                                   "content": "Three meetings on Wednesday."}},
        "run": lambda w, m, a: _write_file(a),
    },
    "append_file": {
        "desc": "Add one line to the end of a real file, creating it if needed.",
        "params": {"path": ("string, path to the file", True),
                   "text": ("string, the line to add", True)},
        "example": {"tool": "append_file", "args": {"path": _eg("notes", "log.txt"), "text": "Called Dana."}},
        "run": lambda w, m, a: _append_file(a),
    },
    "delete_path": {
        "desc": "Delete a real file or folder. This cannot be undone, so be certain first.",
        "params": {"path": ("string, path to delete", True)},
        "example": {"tool": "delete_path", "args": {"path": _eg("notes", "draft.txt")}},
        "run": lambda w, m, a: _delete_path(a),
    },
    "move_path": {
        "desc": "Move or rename a real file or folder.",
        "params": {"path": ("string, what to move", True),
                   "to": ("string, the new path", True)},
        "example": {"tool": "move_path", "args": {"path": "a.txt", "to": _eg("archive", "a.txt")}},
        "run": lambda w, m, a: _move_path(a),
    },
    "search_files": {
        "desc": "Search filenames and text files for a word or phrase, under a folder.",
        "params": {"query": ("string, the word or phrase to look for", True),
                   "path": ("string, folder to search in; omit for the whole root", False)},
        "example": {"tool": "search_files", "args": {"query": "invoice"}},
        "run": lambda w, m, a: _search_files(a),
    },
    "run_command": {
        "desc": f"Run one {SHELL_NAME} command on this computer and read its output.",
        "params": {"command": ("string, the command line to run", True)},
        "example": {"tool": "run_command", "args": {"command": "git status --short"}},
        "run": lambda w, m, a: _run_command(a),
    },
}

# Tools that change the world — the harness loop uses this to decide when a
# repeated identical call may be suppressed.
WRITE_TOOLS = {"write_file", "append_file", "delete_path", "move_path", "run_command"}

# Kept in files-only mode alongside the file tools; everything else (the
# simulated inbox/calendar/messages/office suite) is dropped.
_KEEP_ALWAYS = {"think", "save_memory", "recall_memories", "done"}


def injected():
    """The file tools currently in the registry — which is flag-dependent, since
    enable() leaves run_command out unless --shell was given. mcp_bridge needs
    this to avoid dropping them when both real files and real accounts are on."""
    return {name for name in _FS_TOOLS if name in TOOLS}


def restrict_to_files():
    """Drop the simulated-office tools so a real-folder agent isn't distracted
    by a fake inbox/calendar (a known attractor for small models). Leaves the
    file tools plus think / memory / done. Process-local; the benchmark, in its
    own process, is unaffected."""
    keep = set(_FS_TOOLS) | _KEEP_ALWAYS
    for name in list(TOOLS):
        if name not in keep:
            TOOLS.pop(name, None)


def enable(root, allow_shell=False, confirm=None, shell_only=False, deny=None):
    """Inject the real-filesystem tools into the shared registry, scoped to root.

    `root` may be one path or several. With several, each gets a label from its
    folder name and the model addresses files as "<label>/rest" - the roots stay
    separate sandboxes, they are not merged into one tree.

    `deny` adds locations that stay read-only even inside the root, on top of
    the platform defaults.

    Call once at process start, before run_harness(). The benchmark never calls
    this, so bench/ keeps the original 14-tool registry.
    """
    global _ROOT, _ROOTS, _LABELS, _ALLOW_SHELL, _CONFIRM, _DENY_WRITE
    given = root if isinstance(root, (list, tuple, set)) else [root]
    roots = []
    for r in given:
        r = os.path.abspath(os.path.expandvars(os.path.expanduser(str(r))))
        if not os.path.isdir(r):
            raise ToolError(f"working root {r} does not exist")
        if not any(_norm(r) == _norm(x) for x in roots):
            roots.append(r)
    if not roots:
        raise ToolError("at least one working root is required")
    _ROOTS = roots
    _ROOT = roots[0]
    _LABELS = {}
    for r in roots:
        _LABELS[_label_for(r, _LABELS)] = r
    _ALLOW_SHELL, _CONFIRM = allow_shell, confirm
    _DENY_WRITE = _default_deny() + [os.path.abspath(os.path.expanduser(d))
                                     for d in (deny or [])]
    for name, spec in _FS_TOOLS.items():
        want = not (shell_only and name != "run_command") and not (
            name == "run_command" and not allow_shell)
        if want:
            TOOLS[name] = spec
        else:
            TOOLS.pop(name, None)  # keep the registry matching the flags on re-enable
    return _ROOT if len(_ROOTS) == 1 else list(_ROOTS)
