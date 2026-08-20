"""Every step, in the words a person would use for it.

The run feed says what happened in the harness's vocabulary: a tool name, its
first argument, and "completed". That is the right log for someone debugging
the loop and the wrong thing to hand someone who just asked about their
Thursday. "list_events  2026-07-22  completed" is not an answer, it is evidence
that an answer is being assembled.

So each call also gets a sentence. Two of them, actually - one for while it is
running and one for after - because "Checking your calendar" and "Checked your
calendar" carry the state without needing a spinner to explain it.

This is a lookup table, not a model call. Narrating a run by asking the model to
narrate it would double the cost of every step to produce text that is more
variable and no more accurate. The phrasing is fixed here, and where the table
has no entry - anything an MCP server invented - the tool's own name is
conjugated into a sentence rather than guessed at.

Nothing here is on the graded path: the benchmark reads results, not prose.
"""
import re

# tool -> (while it runs, once it has). {braces} pull from the call's arguments;
# a missing argument leaves the sentence still readable, so every template has
# to make sense with its slot empty.
LINES = {
    "list_emails":         ("Looking through your inbox", "Looked through your inbox"),
    "read_email":          ("Opening that email", "Read the email"),
    "send_email":          ("Writing to {to}", "Sent it to {to}"),
    "list_events":         ("Checking your calendar", "Checked your calendar"),
    "add_event":           ("Putting {title} on {date}", "Added {title} to {date}"),
    "update_event":        ("Moving that meeting", "Updated the meeting"),
    "cancel_event":        ("Cancelling that meeting", "Cancelled the meeting"),
    "send_message":        ("Messaging {to}", "Messaged {to}"),
    "set_reminder":        ("Setting a reminder", "Set the reminder"),
    "create_presentation": ("Building {filename}", "Made {filename}"),
    "create_spreadsheet":  ("Building {filename}", "Made {filename}"),
    "read_spreadsheet":    ("Opening {filename}", "Read {filename}"),
    "save_memory":         ("Making a note of that", "Noted that"),
    "recall_memories":     ("Thinking back", "Checked what I remember"),
    "list_events_all":     ("Checking every calendar", "Checked every calendar"),
    # Real files
    "list_dir":            ("Looking in {path}", "Looked in {path}"),
    "read_file":           ("Reading {path}", "Read {path}"),
    "write_file":          ("Writing {path}", "Wrote {path}"),
    "append_file":         ("Adding to {path}", "Added to {path}"),
    "delete_path":         ("Deleting {path}", "Deleted {path}"),
    "move_path":           ("Moving {src}", "Moved {src}"),
    "search_files":        ("Searching for {query}", "Searched the folder"),
    "run_command":         ("Running that command", "Ran the command"),
}

# Steps that are the harness talking to itself. A person watching a
# conversation does not need to be told the agent thought about it.
SILENT = {"think", "done", "request_tools"}

# First word of an invented tool name -> how to say it mid-action. Anything not
# listed keeps the plain name, which reads oddly but never lies about what ran.
VERBS = {"list": "Listing", "get": "Getting", "read": "Reading", "search": "Searching",
         "find": "Finding", "create": "Creating", "send": "Sending", "update": "Updating",
         "delete": "Deleting", "add": "Adding", "cancel": "Cancelling", "move": "Moving",
         "copy": "Copying", "draft": "Drafting", "reply": "Replying to", "view": "Viewing"}
PAST = {"Listing": "Listed", "Getting": "Got", "Reading": "Read", "Searching": "Searched",
        "Finding": "Found", "Creating": "Created", "Sending": "Sent", "Updating": "Updated",
        "Deleting": "Deleted", "Adding": "Added", "Cancelling": "Cancelled",
        "Moving": "Moved", "Copying": "Copied", "Drafting": "Drafted",
        "Replying to": "Replied to", "Viewing": "Viewed"}


def _fill(template, args):
    """Fill the slots, and drop any that have nothing to put in them."""
    out = template
    for slot in re.findall(r"\{(\w+)\}", template):
        value = str((args or {}).get(slot, "") or "").strip()
        if len(value) > 60:
            value = value[:57] + "..."
        out = out.replace("{" + slot + "}", value)
    out = re.sub(r"\s+", " ", out).strip(" ,")
    # An empty slot leaves its preposition stranded ("Putting Deep work on"),
    # which reads as a sentence cut off mid-thought rather than a shorter one.
    return re.sub(r"\s+(on|to|in|for|into|at|from|with)$", "", out)


def _from_name(name):
    """A sentence for a tool this table has never seen, out of its own name.

    MCP servers name their own tools, so this is the common case the moment a
    real mailbox is connected: get-calendar-view, create-reply-draft.
    """
    words = [w for w in re.split(r"[_\-\s]+", str(name)) if w]
    if not words:
        return ("Working on it", "Done")
    head, rest = words[0].lower(), " ".join(w.lower() for w in words[1:])
    verb = VERBS.get(head)
    if not verb:
        return (f"Running {' '.join(words).lower()}", f"Ran {' '.join(words).lower()}")
    return (f"{verb} {rest}".strip(), f"{PAST.get(verb, verb)} {rest}".strip())


def about(name, args=None, done=False):
    """One line for a call: what it is doing, or what it did.

    Returns None for the steps a person should not be shown at all.
    """
    if name in SILENT:
        return None
    doing, did = LINES.get(name) or _from_name(name)
    return _fill(did if done else doing, args)


def failed(name, args=None, reason=""):
    """The same call, when it did not work. Names what was being attempted, so
    the failure is about the errand rather than about a function."""
    line = about(name, args) or "That step"
    reason = " ".join(str(reason or "").split())[:120]
    return f"{line} - that didn't work" + (f": {reason}" if reason else "")
