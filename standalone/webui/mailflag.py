"""New mail, noticed by the app instead of watched for by the agent.

The agent is now told plainly that waiting is not a step: nothing arrives while
it works, so checking for a reply burns calls to re-read an inbox that cannot
have changed (harness/agent.py, WAIT_GUARD). That leaves a real question
unanswered - who tells you when something DOES arrive?

Not the model. Watching a mailbox is a comparison between two lists, which is
the cheapest thing a computer does and one of the most expensive things to ask
an 8B to do on a loop. This compares them.

What counts as new is "an id this agent has not shown you yet", kept in
seen_mail.json next to the world it describes. First look at a fresh workspace
marks everything seen rather than announcing the demo fixtures as breaking news.
"""
import json
import os

SEEN_FILE = "seen_mail.json"


def _paths(agent_dir):
    workspace = os.path.join(agent_dir, "workspace")
    return os.path.join(workspace, "state.json"), os.path.join(workspace, SEEN_FILE)


def _inbox(state_path):
    try:
        with open(state_path, encoding="utf-8") as f:
            return json.load(f).get("emails") or []
    except (OSError, ValueError):
        return []


def _seen(seen_path):
    try:
        with open(seen_path, encoding="utf-8") as f:
            return set(json.load(f).get("ids") or [])
    except (OSError, ValueError):
        return None          # None = never looked, distinct from "seen nothing"


def _write_seen(seen_path, ids):
    os.makedirs(os.path.dirname(seen_path), exist_ok=True)
    with open(seen_path, "w", encoding="utf-8") as f:
        json.dump({"ids": sorted(ids)}, f, indent=1)


def check(agent_dir):
    """What has arrived since this agent last showed you the inbox."""
    state_path, seen_path = _paths(agent_dir)
    inbox = _inbox(state_path)
    ids = {str(e.get("id")) for e in inbox if e.get("id")}
    seen = _seen(seen_path)
    if seen is None:
        # A workspace opened for the first time is not a pile of new mail.
        _write_seen(seen_path, ids)
        return {"count": 0, "new": []}
    fresh = [e for e in inbox if str(e.get("id")) in ids - seen]
    fresh.sort(key=lambda e: str(e.get("date") or ""), reverse=True)
    return {"count": len(fresh),
            "new": [{"id": e.get("id"), "from": e.get("from"),
                     "subject": e.get("subject"), "date": e.get("date")}
                    for e in fresh[:5]]}


def mark_seen(agent_dir):
    """Stop flagging what is currently there."""
    state_path, seen_path = _paths(agent_dir)
    ids = {str(e.get("id")) for e in _inbox(state_path) if e.get("id")}
    _write_seen(seen_path, ids)
    return {"count": 0, "new": []}
