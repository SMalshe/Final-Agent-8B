"""What has to be true before a run can work, and how to fix each thing.

Downloading this and having it work means clearing four hurdles: the Python
packages, the Ollama binary, the Ollama server, and a model on disk. Until now
each of those failed at a different moment and in a different language - a pip
traceback in a terminal, a connection error mid-run, an empty model list - and
none of them said what to do next.

So they are one list, checked in order, each with the action that fixes it:

    packages    -> install them (pip, this interpreter)
    ollama      -> a download link. Installing a system binary is the user's
                   call, not something a local web page should do behind them.
    server      -> start it
    model       -> pull it, with progress, because it is gigabytes

Two rules shape what belongs here. Anything this module can fix safely, it
offers to fix; anything that installs software system-wide it explains instead.
And a check that cannot be evaluated says so rather than guessing - "unknown"
is a state, distinct from "fine".

The whole module talks to nothing but loopback and pip.
"""
import json
import os
import shutil
import subprocess
import sys
import threading

try:
    import requests
except ImportError:                     # the very thing check 1 is about
    requests = None

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
DOWNLOAD_URL = "https://ollama.com/download"

# import name -> pip name. Split because they disagree often enough to matter.
REQUIRED = [("requests", "requests"), ("pptx", "python-pptx"), ("openpyxl", "openpyxl")]
OPTIONAL = [("webview", "pywebview")]

# State of a running pull, read by the setup screen while it polls.
PULL = {"tag": None, "status": "", "percent": 0, "done": False, "error": None}
_LOCK = threading.Lock()


def _missing(packages):
    out = []
    for module, pip_name in packages:
        try:
            __import__(module)
        except ImportError:
            out.append(pip_name)
    return out


def installed_tags():
    """{tag: size} from Ollama, or None when the server is unreachable.

    None and {} mean different things: not running, versus running with nothing
    pulled. Collapsing them is what makes "install Ollama" show up for someone
    who already has it.
    """
    if requests is None:
        return None
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        r.raise_for_status()
        return {m["name"]: m.get("size", 0) for m in r.json().get("models", [])}
    except Exception:
        return None


def tag_installed(tag, tags):
    """Ollama calls llama3.1:8b and llama3.1:latest different tags for the same
    weights, so a bare name matches its :latest and vice versa."""
    if not tags or not tag:
        return False
    if tag in tags:
        return True
    base = tag.split(":")[0]
    return any(t == base or t.startswith(base + ":") for t in tags)


def check(model=None):
    """The four hurdles, in the order they have to be cleared."""
    out = []

    missing = _missing(REQUIRED)
    out.append({
        "id": "packages", "title": "Python packages",
        "state": "ok" if not missing else "fail",
        "detail": ("requests, python-pptx and openpyxl are installed"
                   if not missing else "missing: " + ", ".join(missing)),
        "fix": None if not missing else "install_deps",
        "fix_label": "Install them",
    })

    have_binary = bool(shutil.which("ollama"))
    tags = installed_tags()
    # A reachable server proves it is installed however it got there, which
    # covers a Docker Ollama or one that simply is not on this PATH.
    installed = have_binary or tags is not None
    out.append({
        "id": "ollama", "title": "Ollama",
        "state": "ok" if installed else "fail",
        "detail": ("found" if have_binary else
                   "reachable, though not on this PATH" if tags is not None else
                   "not installed - this is what runs the model on your machine"),
        "fix": None if installed else "open_url",
        "fix_label": "Get Ollama",
        "url": None if installed else DOWNLOAD_URL,
    })

    out.append({
        "id": "server", "title": "Ollama is running",
        "state": "ok" if tags is not None else ("fail" if installed else "unknown"),
        "detail": (f"up, {len(tags)} model(s) installed" if tags is not None else
                   f"not responding on {OLLAMA_URL}" if installed else
                   "cannot tell until Ollama is installed"),
        "fix": "start_ollama" if (installed and tags is None) else None,
        "fix_label": "Start it",
    })

    want = model or ""
    have_model = tag_installed(want, tags)
    out.append({
        "id": "model", "title": f"The model ({want or 'none chosen'})",
        "state": ("ok" if have_model else "unknown" if tags is None else "fail"),
        "detail": (f"{want} is on this machine" if have_model else
                   "cannot tell until Ollama is running" if tags is None else
                   f"{want} is not downloaded yet - a few GB, once"),
        "fix": ("pull_model" if (tags is not None and not have_model and want)
                else None),
        "fix_label": "Download it",
        "tag": want,
    })

    optional = _missing(OPTIONAL)
    out.append({
        "id": "desktop", "title": "App window",
        "state": "ok" if not optional else "warn",
        "detail": ("pywebview is installed - Agent Lab opens in its own window"
                   if not optional else
                   "pywebview is missing, so this opens in a browser tab instead"),
        "fix": None if not optional else "install_optional",
        "fix_label": "Install it",
    })
    return out


def ready(checks):
    """Everything that would stop a run actually working. Warnings do not."""
    return not any(c["state"] in ("fail", "unknown") for c in checks
                   if c["id"] != "desktop")


# ------------------------------------------------------------------- fixes ---

def _pip(args):
    return subprocess.run([sys.executable, "-m", "pip", "install", *args],
                          capture_output=True, text=True, timeout=600)


def install_deps(optional=False):
    names = [p for _, p in (OPTIONAL if optional else REQUIRED)]
    r = _pip(names)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "pip failed").strip()[-400:])
    return f"installed {', '.join(names)}"


def start_ollama():
    """Start the server and leave it running after this process exits.

    Detached on purpose: someone who closes Agent Lab and reopens it should not
    have to start Ollama twice.
    """
    if not shutil.which("ollama"):
        raise RuntimeError("ollama is not installed")
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # DETACHED | NEW_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(["ollama", "serve"], **kwargs)
    return "starting Ollama"


def pull_model(tag):
    """Download a model in the background, reporting progress into PULL.

    Streamed rather than shelled out to `ollama pull` so the percentage is a
    real number the page can show. Gigabytes with a silent UI reads as a hang.
    """
    with _LOCK:
        if PULL["tag"] and not PULL["done"]:
            return f"already pulling {PULL['tag']}"
        PULL.update({"tag": tag, "status": "starting", "percent": 0,
                     "done": False, "error": None})
    threading.Thread(target=_pull_worker, args=(tag,), daemon=True).start()
    return f"pulling {tag}"


def _pull_worker(tag):
    try:
        r = requests.post(f"{OLLAMA_URL}/api/pull",
                          json={"model": tag, "stream": True},
                          stream=True, timeout=(10, 600))
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("error"):
                raise RuntimeError(msg["error"])
            total, done = msg.get("total") or 0, msg.get("completed") or 0
            with _LOCK:
                PULL["status"] = msg.get("status", "")
                if total:
                    PULL["percent"] = round(100.0 * done / total, 1)
        with _LOCK:
            PULL.update({"percent": 100, "done": True, "status": "done"})
    except Exception as e:
        with _LOCK:
            PULL.update({"done": True, "error": f"{type(e).__name__}: {e}"})


FIXES = {
    "install_deps": lambda p: install_deps(False),
    "install_optional": lambda p: install_deps(True),
    "start_ollama": lambda p: start_ollama(),
    "pull_model": lambda p: pull_model(p.get("tag")),
}


def apply_fix(action, params=None):
    if action not in FIXES:
        raise ValueError(f"unknown fix {action!r}")
    return FIXES[action](params or {})
