"""Conversation threads — what turns a one-shot agent into something you talk to.

The harness runs one task and stops. Nothing carries from one run to the next
except the world and long-term memory, so asking "and what about Thursday?"
after a run lands on an agent with no idea what Thursday refers to.

This stores the turns. A thread is a list of {role, text} plus the run id that
produced each assistant turn, so the transcript of *how* an answer was reached
stays available without living in the conversation itself.

    threads(folder)              -> [{id, title, updated, n}]  newest first
    create(folder, first_task)   -> id
    messages(folder, tid)        -> [{role, text, ts, run}]
    append(folder, tid, role, text, run=None)
    delete(folder, tid)
    prompt_block(msgs, k)        -> text injected into the system prompt

Stored as one JSON file per agent at <folder>/chat/threads.json. One file
because this is a single-user local app: the whole history of a laptop's worth
of conversations is smaller than one of the .pptx files in workspace/, and one
file means no half-written thread if the process dies mid-append.

NOT in workspace/ on purpose — a factory reset clears the simulated inbox and
the files, and losing the conversation with them would be surprising.
"""
import json
import os
import time
import uuid

# Enough turns for a follow-up to make sense, few enough that the block cannot
# crowd out the tool docs in an 8k context. Older turns fall off the top.
DEFAULT_TURNS = 6
TITLE_CHARS = 60


def _path(folder):
    return os.path.join(folder, "chat", "threads.json")


def _load(folder):
    try:
        with open(_path(folder), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"threads": []}
    return data if isinstance(data, dict) and "threads" in data else {"threads": []}


def _save(folder, data):
    p = _path(folder)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False, default=str)
    os.replace(tmp, p)          # atomic: a crash leaves the old file, not half of one


def _find(data, tid):
    for t in data["threads"]:
        if t["id"] == tid:
            return t
    return None


def threads(folder):
    """Sidebar rows, newest first. Deliberately without message bodies — the
    list is rendered on every poll and the bodies can be large."""
    out = []
    for t in _load(folder)["threads"]:
        out.append({"id": t["id"], "title": t.get("title") or "New chat",
                    "updated": t.get("updated", 0), "n": len(t.get("messages", []))})
    return sorted(out, key=lambda t: t["updated"], reverse=True)


def create(folder, first_task=""):
    data = _load(folder)
    tid = uuid.uuid4().hex[:12]
    title = (first_task or "").strip().replace("\n", " ")
    if len(title) > TITLE_CHARS:
        title = title[:TITLE_CHARS - 1] + "…"
    data["threads"].append({"id": tid, "title": title or "New chat",
                            "created": time.time(), "updated": time.time(),
                            "messages": []})
    _save(folder, data)
    return tid


def messages(folder, tid):
    t = _find(_load(folder), tid)
    return list(t.get("messages", [])) if t else []


def append(folder, tid, role, text, run=None):
    data = _load(folder)
    t = _find(data, tid)
    if not t:
        return False
    t["messages"].append({"role": role, "text": text, "ts": time.time(), "run": run})
    t["updated"] = time.time()
    # A thread created before its first task was known gets its name from it.
    if role == "user" and t.get("title") in (None, "", "New chat"):
        title = text.strip().replace("\n", " ")
        t["title"] = title[:TITLE_CHARS - 1] + "…" if len(title) > TITLE_CHARS else title
    _save(folder, data)
    return True


def delete(folder, tid):
    data = _load(folder)
    before = len(data["threads"])
    data["threads"] = [t for t in data["threads"] if t["id"] != tid]
    _save(folder, data)
    return len(data["threads"]) != before


def prompt_block(msgs, k=DEFAULT_TURNS):
    """The earlier turns, as text for the system prompt.

    Deliberately NOT replayed as real chat messages. The harness contract is
    "reply with exactly one JSON object", and a run of prior assistant prose in
    the message list is the strongest possible cue for a small model to reply
    with prose too. As a block it reads as context; as messages it reads as a
    pattern to continue.
    """
    msgs = [m for m in msgs if m.get("text")][-k:]
    if not msgs:
        return ""
    lines = []
    for m in msgs:
        who = "User" if m["role"] == "user" else "You"
        text = " ".join(str(m["text"]).split())
        if len(text) > 300:
            text = text[:299] + "…"
        lines.append(f"{who}: {text}")
    return ("\n\nEARLIER IN THIS CONVERSATION (for context; the task below is what "
            "you must do now):\n" + "\n".join(lines))


def reply_rules():
    """Prompt text for a run that is a conversation rather than an errand.

    Two things it has to establish. First, that not every message is a job:
    the loop's contract is one tool call per reply, so without a way to simply
    talk, "morning, what can you do?" becomes a hunt through the inbox for an
    answer that was never in there. say() is that way.

    Second, the voice. The summary done() carries is the only part of a run the
    person reads - server.py stores it as the assistant's turn - but the tool
    doc asks for a "short summary" and shows one written like a log line, so
    that is what came back: "Recalled user's favourite colour as teal from
    memory." Third person, and the answer buried inside an account of finding
    it.

    Kept out of the shared prompt deliberately. bench/ grades runs whose prompt
    must not move, and the CLI is a task tool where a terse line is right. Only
    the chat surface appends this.

    It stays abstract for the reason agent.py's RULES do: concrete example
    content in an instruction is an attractor a small model copies verbatim,
    and a specimen answer inside an instruction about answering is the worst
    possible place for one. Shape, not sentences.
    """
    return """

YOU ARE IN A CONVERSATION:
- say is how you talk to the person. It sends them a message and nothing else.
- Not every message needs a tool. A greeting, something you were just told,
  something you already know, or a question back at them: say it, then call
  done. Do not go looking through the inbox or the calendar for an answer that
  is not in them.
- Use the other tools only when the request really does need the inbox, the
  calendar, a file or a document. Then use them without asking permission.
- You do not need to narrate the steps. The person can see what you are doing.

HOW TO WRITE WHAT YOU SAY, in say and in the summary you pass to done:
- Say "you" and "I". Never "the user".
- If they asked a question, the first sentence IS the answer. Do not describe
  looking it up, recalling it, or checking it - just answer.
- One or two plain sentences. No preamble, no sign-off.
- If you already answered with say, the summary can be a few words: they have
  read your message already."""


SAY = {
    "desc": "Send the person a message right now. Use it to answer, ask, or "
            "explain when the request needs nothing but words.",
    "params": {"text": ("string, what you want to say, in your own words", True)},
    "example": {"tool": "say", "args": {"text": "<what you want to tell them>"}},
    # The message IS the effect: the UI renders it from the call, and the
    # observation only has to confirm it left. Echoing the text back would put
    # a second copy of it in the context for no gain.
    "run": lambda world, memory, args: "sent",
}


def enable_say():
    """Add say to the registry. Chat surfaces only - the same opt-in shape
    fs_tools and mcp_bridge use, so bench/ keeps its own tool list."""
    from .tools import TOOLS
    TOOLS["say"] = SAY


def disable_say():
    from .tools import TOOLS
    TOOLS.pop("say", None)


def said(events):
    """Everything the agent said during a run, in order.

    server.py stores this as the assistant's turn when there is any, because a
    run that already spoke should not have a summary of itself pasted
    underneath it.
    """
    out = []
    for e in events:
        if e.get("t") == "tool" and e.get("name") == "say":
            text = str((e.get("args") or {}).get("text") or "").strip()
            if text:
                out.append(text)
    return out
