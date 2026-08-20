"""Real mail when it is connected, and an honest refusal to pretend when it is not.

Mail is the one part of the simulated office that reads exactly like the real
thing. `send_email` appended a row to workspace/state.json and the feed said
"Sent it to sharvinmalshe@gmail.com" - true about the tool, wrong about the
world, and four of those went by before anyone noticed nothing had left the
machine. Then the agent watched a fixture inbox for a reply that could not come.

So mail now has a mode, decided per run:

    real         a mail MCP server has credentials on this machine. Its tools
                 are the mail tools, and the simulated inbox is dropped so
                 there is no second, fake path to reach for.

    simulated    no credentials yet. The fixtures still work - the demo tasks
                 and the office scenario depend on them - but every result and
                 every line about it says so, in those words, every time.

Detection is a file check, not a login: credentials on disk mean the server can
authenticate, and starting each server to ask would cost seconds per run. A
server that has keys but fails anyway reports its own error, which is the right
place for that failure to appear.

Connecting is a person's job - an OAuth consent screen is not something a local
agent should be clicking through on your behalf - so this only ever reports what
is there and points at the steps.
"""
import os

# The simulated office's mail tools. Calendar and documents are not in here:
# a fixture meeting is obviously a fixture, an email is not.
SIM_MAIL_TOOLS = ("list_emails", "read_email", "send_email")

# server id -> a file that exists only once its one-time login has been done.
CREDENTIALS = {
    "gmail": ("~/.gmail-mcp/credentials.json",),
    "gcal": ("~/.gmail-mcp/credentials.json", "~/.config/google-calendar-mcp/tokens.json"),
    "ms365": ("~/.ms-365-mcp-server/token-cache.json",),
    "ms365-personal": ("~/.ms-365-mcp-server/token-cache.json",),
    "ms365-work": ("~/.ms-365-mcp-server/token-cache.json",),
}

MAIL_SERVERS = ("gmail", "ms365", "ms365-personal", "ms365-work")


def _exists(path):
    return os.path.exists(os.path.expanduser(path))


def connected(servers=None):
    """Mail servers whose one-time login has already been done."""
    return [s for s in (servers or MAIL_SERVERS)
            if any(_exists(p) for p in CREDENTIALS.get(s, ()))]


def is_real(chosen=None):
    """Whether this run's mail is real: a connected server was chosen, or one
    is available to choose."""
    return bool(connected(chosen) if chosen else connected())


def drop_simulated(tools):
    """Remove the fixture mail tools from a registry, in place.

    Called when real mail is on. Leaving both in is how a model ends up sending
    a real reply and then reading a fixture inbox to check it.
    """
    for name in SIM_MAIL_TOOLS:
        tools.pop(name, None)


def mark_simulated(tools):
    """Make every simulated mail result say that it is simulated.

    Wrapping the result rather than the description on purpose: a caveat in a
    tool's docs is read once, at the top of a long prompt, and a model that
    has been reading real calendars all run will not carry it. A marker in the
    observation is in front of it at the moment it matters, and it lands in the
    transcript, so the person reading back sees it too.
    """
    for name in SIM_MAIL_TOOLS:
        spec = tools.get(name)
        if not spec or spec.get("_simulated_marked"):
            continue
        inner = spec["run"]
        spec["run"] = _marked(inner)
        spec["_simulated_marked"] = True


def _marked(inner):
    def run(world, memory, args):
        result = inner(world, memory, args)
        note = ("[SIMULATED MAILBOX - this is the practice office, not a real "
                "account. Nothing here was really sent or received.] ")
        return note + (result if isinstance(result, str) else str(result))
    return run


def setup_hint():
    return ("Email is not connected yet, so mail is simulated. Connect a real "
            "account once (Gmail or Outlook) and it stops being a practice run: "
            "python3 -m webui.server, then Run options, or --mcp-help for the steps.")
