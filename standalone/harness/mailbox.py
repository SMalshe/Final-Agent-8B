"""The rest of the mailbox: what you already sent, and what is waiting to go.

The registry could read the inbox and send mail, and nothing in between. Two
things fell through that gap.

**Sent.** The world has always recorded sent mail and no tool could read it
back, so "did I already reply to Dana?" had to be answered from memory, and a
second run had no way to see what the first one sent. The record existed the
whole time; it just had no door.

**Drafts.** There was no such folder at all, which made the simulated office
disagree with the real one: connect Outlook and the agent's default mode is to
compose a draft and leave sending to a person (mcp_bridge's draft mode), but
practise the same task against fixtures and its only option was to send for
real. Now both worlds work the same way.

Note what is deliberately missing: there is no send_draft. Composing is the
agent's half and sending is yours, which is the whole point of a draft. An
agent that can write a draft and then send it has just sent mail with extra
steps.

Injected on request rather than declared in tools.py, the same shape fs_tools
and mcp_bridge use, so bench/ keeps the registry it was graded against.
"""
from .tools import TOOLS

# Changes the world, so loop-breaking has to know: an identical save_draft
# against an unchanged world is a duplicate, not a retry.
WRITE_TOOLS = {"save_draft"}

SPECS = {
    "list_sent": {
        # Named against list_emails on purpose. Asked "who have I emailed
        # recently?" an 8B reached for the INBOX and then reported that Dana
        # had emailed them - the right folder is the one distinction the
        # description has to make, so it makes it first and in those words.
        "desc": "Your SENT mail - messages you wrote to other people. Use this "
                "for anything about what you have sent, replied to or already "
                "told someone. (list_emails is the opposite: mail you "
                "received.) Gives recipient, subject and the start of the body.",
        "params": {},
        "example": {"tool": "list_sent", "args": {}},
        "run": lambda w, m, a: w.list_sent(),
    },
    "list_drafts": {
        "desc": "Your DRAFTS folder - mail you have written but not sent, "
                "waiting for the person to look at. Gives id, recipient and subject.",
        "params": {},
        "example": {"tool": "list_drafts", "args": {}},
        "run": lambda w, m, a: w.list_drafts(),
    },
    "read_draft": {
        "desc": "Read one draft in full by its id.",
        "params": {"id": ("string, a draft id like 'd1'", True)},
        "example": {"tool": "read_draft", "args": {"id": "d1"}},
        "run": lambda w, m, a: w.read_draft(a["id"]),
    },
    "save_draft": {
        "desc": "Write an email and leave it in drafts instead of sending it. "
                "Use this when the person should read it before it goes out.",
        "params": {"to": ("string, recipient address", True),
                   "subject": ("string", True),
                   "body": ("string", True)},
        "example": {"tool": "save_draft",
                    "args": {"to": "dana@corp.com", "subject": "Re: numbers",
                             "body": "Thanks - I'll have these by Friday."}},
        "run": lambda w, m, a: w.save_draft(a["to"], a.get("subject", ""),
                                            a.get("body", "")),
    },
}


# Where they belong in the docs. The registry is a list the model reads top to
# bottom, and appending put these AFTER done - so asked "who have I emailed
# recently?" the plan reached for list_emails, never saw a sent folder past the
# finish tool, and answered from the inbox. Twice. Neighbours matter: they sit
# with the mail tools now.
AFTER = "send_email"


def enable():
    """Add the sent and drafts tools to the registry, in this process only."""
    ordered, placed = {}, False
    for name, spec in list(TOOLS.items()):
        if name in SPECS:
            continue                      # re-enabling: drop the old copies
        ordered[name] = spec
        if name == AFTER:
            ordered.update(SPECS)
            placed = True
    if not placed:
        ordered.update(SPECS)             # no send_email here (real-file mode)
    TOOLS.clear()
    TOOLS.update(ordered)
    return sorted(SPECS)


def disable():
    for name in SPECS:
        TOOLS.pop(name, None)
