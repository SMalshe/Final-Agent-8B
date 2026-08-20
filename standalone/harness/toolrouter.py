"""Which tools the model sees, and how it asks for the ones it doesn't.

The context window is the real budget. An 8B holds 8192 tokens and every tool
in the registry spends some of them on a description and a worked example -
connecting Outlook exposes 69 tools, which is why mcp/servers.json carries
hand-written allow lists cutting that to ten. Those lists are static: they are
the same ten whether the task is "what's on Thursday" or "tidy this folder".

This routes per task instead, and it is deliberately built so that being wrong
is cheap:

  select()        Score every tool against the task text and expose the ones
                  that match, plus the core four that every task needs. Pure
                  keyword overlap, no model call - the same cheap mechanism
                  memory.py retrieves facts with.

  abstain         If nothing scores, expose EVERYTHING. A router that guesses
                  badly and hides the one tool the task needed is worse than no
                  router at all, so silence means step aside.

  request_tools   The model describes what it wants to do and gets the matching
                  tools added mid-run. This is the discovery path: it does not
                  know the name to ask for.

  name recovery   A call naming a real tool that simply is not exposed yet gets
                  the tool exposed and executed, not rejected. The model has
                  already told us what it needs by naming it; spending a round
                  trip to make it ask politely would be the scaffolding wasting
                  the budget it exists to protect.

Off unless a runner turns it on, so bench/ and the benchmark prompt are
untouched - the same rule EXTRA_RULES and SIM_TODAY follow.
"""
import re

from .tools import TOOLS

# Needed by every task regardless of what it says, so they are never routed
# away: thinking, finishing, and the memory pair that makes runs cumulative.
CORE = ("think", "done", "save_memory", "recall_memories")

# Task words that should pull in tools whose own text uses different words.
# "book me an hour" names no tool; it means the calendar.
SYNONYMS = {
    "meeting": "event calendar", "meetings": "event calendar",
    "schedule": "event calendar", "scheduled": "event calendar",
    "book": "event calendar add", "booking": "event calendar add",
    "free": "event calendar", "busy": "event calendar",
    "cancel": "cancel event", "reschedule": "update event",
    "invite": "event calendar", "appointment": "event calendar",
    "hour": "event calendar", "morning": "event calendar",
    "afternoon": "event calendar", "thursday": "event calendar",
    "inbox": "email", "mail": "email", "email": "email",
    "reply": "email send", "forward": "email send", "draft": "email send",
    "wrote": "email read", "sender": "email read", "unread": "email",
    "deck": "presentation slides", "slides": "presentation",
    "slide": "presentation", "presentation": "presentation",
    "spreadsheet": "spreadsheet", "excel": "spreadsheet",
    "sheet": "spreadsheet", "workbook": "spreadsheet", "csv": "spreadsheet",
    "numbers": "spreadsheet read", "totals": "spreadsheet",
    "remind": "reminder", "reminder": "reminder", "deadline": "reminder",
    "message": "message send", "ping": "message send", "tell": "message send",
    "file": "file read write directory", "files": "file directory",
    "folder": "directory", "folders": "directory",
    "document": "file read", "documents": "file read",
    "rename": "move file", "delete": "delete file", "tidy": "file move directory",
    "search": "search find", "find": "search find",
    "remember": "memory save", "recall": "memory",
}

# Past this many tools, "show everything" stops being a safe fallback.
ABSTAIN_CAP = 20

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "to", "of", "and", "or", "for", "with", "my", "me",
         "i", "is", "are", "in", "on", "at", "it", "that", "this", "be", "do",
         "please", "can", "you", "then", "any", "all", "from", "into", "up"}

# Exposed set for the current run, and whether it changed since the loop last
# rebuilt the prompt from it. Module state for the same reason the rest of the
# harness uses it: one process runs one agent.
_EXPOSED = set()
_DIRTY = False


def _tokens(text):
    return {w for w in _WORD.findall(str(text).lower()) if w not in _STOP}


def _expand(tokens):
    out = set(tokens)
    for t in tokens:
        if t in SYNONYMS:
            out |= _tokens(SYNONYMS[t])
    return out


def _shared(a, b):
    """Tokens in common, treating one as a prefix of the other from four
    characters up - the same rule memory.py retrieves facts with. Without it
    "event" misses list_events and "slide" misses a tool about slides, which
    is most of what a plain set intersection gets wrong here."""
    n = 0
    for x in a:
        for y in b:
            if x == y or (len(x) >= 4 and len(y) >= 4
                          and (x.startswith(y) or y.startswith(x))):
                n += 1
                break
    return n


def score(task_tokens, name, spec):
    """How well one tool answers this task. Name matches count double - a task
    that says "spreadsheet" wants read_spreadsheet more than it wants a tool
    that merely mentions spreadsheets in its description."""
    name_tokens = _tokens(name.replace("_", " ").replace("-", " "))
    desc_tokens = _tokens(spec.get("desc", ""))
    return 2 * _shared(task_tokens, name_tokens) + _shared(task_tokens, desc_tokens)


def select(task, names=None, limit=12):
    """The tools a task should start with. Never empty, never silently lossy."""
    names = list(names if names is not None else TOOLS)
    core = [n for n in names if n in CORE or n == "request_tools"]
    task_tokens = _expand(_tokens(task))
    scored = [(score(task_tokens, n, TOOLS.get(n, {})), n)
              for n in names if n not in core]
    hits = sorted([(s, n) for s, n in scored if s > 0], reverse=True)
    if not hits:
        # Abstain: a bad guess is worse than no guess, so show everything...
        # unless everything is a real Outlook account, where "everything" is
        # sixty-nine tools against an 8k context and showing them all is not
        # caution, it is the failure this module exists to prevent. Past the
        # cap, abstaining means admitting it: the core tools plus request_tools,
        # and the model asks for what it needs by describing it.
        if len(names) <= ABSTAIN_CAP:
            return set(names)
        return set(core)
    # Keep what is close to the best match, not merely better than nothing. One
    # tool scoring 7 next to one scoring 1 means the 1 is noise, and a flat
    # top-N would seat it anyway whenever the registry is small.
    cutoff = max(2, hits[0][0] / 3.0)
    keep = [n for s, n in hits if s >= cutoff][:limit]
    return set(core) | set(keep)


# ------------------------------------------------------------------- state ---

def begin(task, limit=12):
    """Start a run: choose the opening set and register request_tools."""
    global _EXPOSED, _DIRTY
    TOOLS["request_tools"] = REQUEST_SPEC
    _EXPOSED = select(task, limit=limit)
    _EXPOSED.add("request_tools")
    _DIRTY = False
    return set(_EXPOSED)


def end():
    """Put the registry back. request_tools is scaffolding, not a world tool."""
    global _EXPOSED, _DIRTY
    TOOLS.pop("request_tools", None)
    _EXPOSED, _DIRTY = set(), False


def exposed():
    return set(_EXPOSED)


def is_routing():
    return bool(_EXPOSED)


def expose(names):
    """Add tools mid-run. Returns the ones that were actually new."""
    global _DIRTY
    added = {n for n in names if n in TOOLS and n not in _EXPOSED}
    if added:
        _EXPOSED.update(added)
        _DIRTY = True
    return added


def take_dirty():
    """True once after the exposed set changes, so the loop rebuilds the prompt."""
    global _DIRTY
    was, _DIRTY = _DIRTY, False
    return was


def find(need, limit=6):
    """Tools matching a description, best first. Backs request_tools."""
    task_tokens = _expand(_tokens(need))
    scored = [(score(task_tokens, n, s), n) for n, s in TOOLS.items()
              if n != "request_tools"]
    return [n for s, n in sorted([x for x in scored if x[0] > 0], reverse=True)[:limit]]


def _run_request(world, memory, args):
    need = str(args.get("need") or "").strip()
    if not need:
        return "Describe what you are trying to do, e.g. {\"need\": \"read a spreadsheet\"}"
    matches = find(need)
    if not matches:
        return (f"No tool matches {need!r}. Tools that exist: "
                + ", ".join(sorted(n for n in TOOLS if n != "request_tools")))
    added = expose(matches)
    from .tools import tool_docs
    return ("These tools are now available to you:\n"
            + tool_docs(with_examples=True, names=matches)
            + ("" if added else "\n(they were already available)"))


REQUEST_SPEC = {
    "desc": "Ask for a tool you cannot see. Describe what you are trying to do "
            "and the matching tools are added to your list, then call one.",
    "params": {"need": ("string, what you are trying to do, "
                        "e.g. 'read a spreadsheet'", True)},
    "example": {"tool": "request_tools", "args": {"need": "create a calendar event"}},
    "run": _run_request,
}
