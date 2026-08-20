"""One question, every calendar.

Connecting Google Calendar and Outlook at once already works - mcp_bridge
starts both and qualifies any colliding tool name with its server id - but the
model is then holding two unrelated tools and no notion that they describe the
same week. "What do I have on Thursday" has to become two calls plus a merge
the model performs in its head, and a small model that forgets the second call
answers confidently from half the week. That is the failure this removes.

So when more than one calendar source is registered, one more tool appears:

    list_events_all   call every calendar, label each answer by its source

Each source is called with only the arguments it declares, because they do not
agree on argument names - the simulated office takes `date`, Microsoft Graph
takes a start and end datetime. A source that cannot be satisfied from the
arguments given, or that fails, reports that inline instead of taking the whole
call down with it: half an answer plus a named gap beats an error, and the
model can still call that one source directly.

The merged tool only reads. Creating an event means choosing a calendar, and
guessing which one on the model's behalf is exactly the kind of silent decision
the harness questions everywhere else.
"""
import re

from .tools import TOOLS
from .world import ToolError

# Tools that list what is on a calendar. Deliberately loose: it has to match
# names invented by servers this repo does not control (list-calendar-events,
# get-calendar-view, list_events).
_LISTS_EVENTS = re.compile(
    r"(?:^|_|-)(?:list|get|search|find|view)[_-]?.*(?:event|calendar)"
    r"|calendar[_-]?view", re.I)

# ... but not the ones that change it, and not a single-event lookup.
_NOT_A_LISTING = re.compile(
    r"create|add|update|delete|cancel|remove|accept|decline|dismiss|snooze"
    r"|reminder|free[_-]?busy", re.I)

MERGED = "list_events_all"


def sources(registry=None):
    """Every calendar-listing tool currently registered, by name."""
    reg = TOOLS if registry is None else registry
    return [n for n in reg
            if n != MERGED and _LISTS_EVENTS.search(n) and not _NOT_A_LISTING.search(n)]


def _args_for(spec, args):
    """The arguments this source accepts, or None if it needs one we lack."""
    params = spec.get("params") or {}
    out = {k: v for k, v in args.items() if k in params}
    missing = [p for p, (_, req) in params.items() if req and p not in out]
    return None if missing else out


def _run_all(world, memory, args):
    found = sources()
    if not found:
        raise ToolError("no calendar is connected")
    args = {k: v for k, v in (args or {}).items() if v not in (None, "")}
    blocks = []
    for name in found:
        spec = TOOLS[name]
        taken = _args_for(spec, args)
        if taken is None:
            need = ", ".join(p for p, (_, req) in spec["params"].items() if req)
            blocks.append(f"[{name}] not asked: it needs {need}. Call it directly.")
            continue
        try:
            result = spec["run"](world, memory, taken)
        except ToolError as e:
            blocks.append(f"[{name}] ERROR: {e}")
        except Exception as e:                      # a server that misbehaves
            blocks.append(f"[{name}] ERROR: {type(e).__name__}: {e}")
        else:
            blocks.append(f"[{name}]\n{result}")
    return "\n\n".join(blocks)


SPEC = {
    "desc": "List events from EVERY connected calendar at once and label each "
            "answer with the calendar it came from. Use this to answer "
            "'what do I have on <day>' when more than one calendar is connected.",
    "params": {"date": ("string YYYY-MM-DD, the day to look at", False),
               "start_date": ("string YYYY-MM-DD, optional range start", False),
               "end_date": ("string YYYY-MM-DD, optional range end", False)},
    "example": {"tool": MERGED, "args": {"date": "2026-07-23"}},
    "run": _run_all,
}


def enable(min_sources=2):
    """Inject the merged tool if there is more than one calendar to merge.

    With a single calendar it stays out of the registry: another tool that does
    what an existing tool already does is a way for a small model to get the
    call wrong, not a convenience.
    """
    found = sources()
    if len(found) >= min_sources:
        TOOLS[MERGED] = SPEC
    else:
        TOOLS.pop(MERGED, None)
    return found


def disable():
    TOOLS.pop(MERGED, None)
