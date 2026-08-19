# Real accounts — Gmail, Outlook, Teams

The agent reaches live mail, calendars and chat by borrowing **MCP servers** that
other people already wrote and maintain. The harness does not implement the Gmail
API or Microsoft Graph, and it never will: providers change their APIs far faster
than this repo changes, and an on-device agent has no business owning an OAuth
implementation it can't keep patched.

    python3 agents/8b/run_agent.py --mcp gmail,ms365 "Draft a reply to the budget thread"

Everything here is **off by default**. Without `--mcp`, nothing in this folder
runs and the agent keeps talking to the simulated office as before.

## What it costs you

Model inference is still fully local — the loopback assertion in every runner
still holds, and no prompt or reply leaves the machine. **The tools are the
departure.** A `gmail_search_emails` call goes to Google; an `outlook_list-mail`
call goes to Microsoft. If "nothing leaves the machine" is the claim you are
making about a demo, do not pass `--mcp` during it.

These are also *third-party* servers. They hold the OAuth tokens, not us. That
is a deliberate trade for now — see [Phase 2](#phase-2--first-party-servers).

## Setup

    python3 agents/8b/run_agent.py --mcp-help

prints what each server needs. All of them need **Node.js** on PATH, because all
of them are `npx` packages.

| name | surface | server |
|---|---|---|
| `gmail` | Gmail read/search/draft | `@gongrzhe/server-gmail-autoauth-mcp` |
| `gcal` | Google Calendar | `@cocal/google-calendar-mcp` |
| `ms365` | Outlook mail + calendar | `@softeria/ms-365-mcp-server` |
| `teams` | Teams chat | same server, `--org-mode` |
| `ms365-work` | all three Microsoft surfaces in one process | same server |
| `selftest` | a fake mailbox, no credentials | `mcp/selftest_server.py` |

**Personal Microsoft accounts must use `ms365-personal`, not `ms365`.** As of June
2026 Microsoft rejects tokens issued to personal accounts through the default
`common` authority, so that entry pins `MS365_MCP_TENANT_ID=consumers`. Get it
wrong and the device page bounces to `localhost/callback` with a missing
`response_type` error and no token is ever cached. The tell: `common` sends you
to `login.microsoft.com/device`, `consumers` sends you to `www.microsoft.com/link`.
The authority must match between login and runtime.

**`--verify-login` lies.** It reports `Silent token acquisition failed` against a
working personal-account connection. Verified: that same connection served a real
`list-mail-folders` call and a full agent run. Test with `--mcp-list` plus an
actual call, never with `--verify-login`.

**Log in once, by hand, before the agent ever runs.** Both providers cache a
token; the agent then reuses it and never sees a login prompt. The registry
deliberately drops the `login`/`logout` tools so a model cannot trigger a
device-code flow in the middle of a run and hang waiting for a human.

Use `ms365-work` *instead of* `ms365` + `teams`, not alongside — the latter
starts two copies of the same server.

## Safety

Three guards, all on by default, all enforced by
[`harness/mcp_bridge.py`](../harness/mcp_bridge.py):

1. **Draft mode.** Any tool that transmits to a person — send, forward, reply —
   is not merely blocked, it is *never shown to the model*. The agent composes;
   you send. `--mcp-live` lifts this; `--mcp-read-only` drops every write.
2. **Confirmation.** Every world-changing call prints what it is about to do and
   waits for `y`. A decline raises a `ToolError` that tells the model not to
   retry it.
3. **Allow / drop lists** in `servers.json`, per server, overriding the bridge's
   name-based write classifier where it guesses wrong.

**Sending is possible, but never by accident.** `--mcp-live` (or Mode → live in
the Connections menu) adds `send-mail`, `reply-mail-message` and
`send-draft-message` for the Outlook servers. Each is still confirmed
per-call with the full payload shown, and `--yolo` is the only way to skip that
— do not combine `--yolo` with `--mcp-live` on an account you care about.

`send-draft-message` is the safest of the three: the agent composes, you read the
draft in Outlook, and only then does it send.

Three consequences worth knowing before you demo:

- **Teams is read-only in draft mode.** There is no such thing as a Teams draft,
  so every way to put a message in front of a person is a transmit tool and all
  of them get dropped. Reading chats works. Posting needs `--mcp-live`.
- **The transmit regex over-matches.** `create-reply-draft` and
  `create-forward-draft` only compose a draft, but they contain "reply" and
  "forward", so draft mode dropped them — the agent could write a fresh mail but
  not draft a *reply*, which is the more common request. Naming a tool in
  `write_tools` exempts it from the draft filter while keeping it confirmed;
  that is what the Outlook entries now do.
- **Guard 1 is a regex over tool names.** A server whose send tool is named
  something `_TRANSMIT_RE` doesn't match would be exposed in draft mode. Run
  `--mcp-list` against any server you add and read the classification yourself:

      python3 agents/8b/run_agent.py --mcp gmail --mcp-list

  That prints each tool as `read` or `write` exactly as the model will see it.
  It is the only way to know, and the registry's `write_tools` override exists
  because the classifier is already known to be wrong about `modify_*`.

## Verifying the wiring

`selftest` is a fake mailbox that speaks real MCP over real stdio and needs no
credentials, network or Node. Its tool names are chosen to hit every branch of
the classifier:

    python3 -m mcp.test_bridge

asserts all of it — that send/reply/login are dropped in draft mode, that a
decline is terminal, that `modify_mail` is only classified as a write because
of the registry override, that live mode does expose send, and that the
simulated office inbox is removed so the model can't confuse it with the real
one. If that passes on a fresh machine, the harness side is correct and any
remaining problem is credentials.

## Watch the tool count

An 8B at `num_ctx` 8192 carries every tool's name, description and parameters in
its system prompt. Measured against the real server: `--preset mail,calendar`
still returns **69 tools**. Injecting those doesn't fail loudly — it quietly
makes the agent stupid and burns its budget choosing between near-identical
options. The registry's `allow` list cuts that to **10**:

    $ python3 agents/8b/run_agent.py --mcp ms365 --mcp-list
    ms365  (draft)
         read  outlook_list-mail-messages
        write  outlook_create-draft-email
         read  outlook_get-mail-message
         ... 10 total

Narrow server-side with the server's own `--preset` first, then the registry's
`allow` list. `mcp_config.count_warnings()` warns past 25.

That same run is where the registry's `write_tools` entries come from.
`copy-mail-message`, `dismiss-calendar-event-reminder` and
`snooze-calendar-event-reminder` all change state but contain no verb the
classifier knows, so they were being exposed as unconfirmed reads;
`get-mailbox-settings` was the reverse, matching `set` inside `settings`. **Run
`--mcp-list` against any server you add and read the classification yourself** —
this is not hypothetical, it was wrong four times out of 69 on the first real
server tried.

## Phase 2 — first-party servers

The registry is data, and `mcp_config` resolves `python`/`python3` to the running
interpreter, so a server we write ourselves drops in beside the others with no
harness change:

    "gmail": {
      "command": "python3",
      "args": ["-m", "mcp.servers.gmail"],
      "cwd": "${STANDALONE}"
    }

Replace them in this order, worst-first:

1. **`gmail`** — the upstream repo was archived in March 2026. Unmaintained code
   holding a live Gmail token is the weakest link here.
2. **`gcal`** — same OAuth client as Gmail, so one server can cover both and
   halve the consent surface.
3. **`ms365` / `teams`** — last. It is actively maintained, and Graph plus the
   device-code flow is the most work to reimplement for the least gain.

`selftest_server.py` is ~120 lines and is a complete worked example of the
protocol the harness needs: `initialize`, `tools/list`, `tools/call`, text
content blocks, `isError`. A first-party server is that plus an HTTP client and
a token cache.

## Known gaps

- **stdio only.** `mcp_bridge` speaks stdio JSON-RPC. Google's *official* Gmail
  MCP server is remote HTTP (`https://gmailmcp.googleapis.com/mcp/v1`) and can't
  be used directly — it needs either an HTTP transport in the bridge or a
  stdio↔HTTP proxy in front of it. The community stdio servers above avoid the
  question entirely, which is why they're the default.
- **Teams tenant policy.** The Microsoft server ships a built-in public app
  registration. Tenants that block unapproved apps will reject it, and pointing
  `MS365_MCP_CLIENT_ID` at your own registration needs an admin to consent to
  the `Chat.*` delegated scopes. No amount of code here fixes that.
- **Windows.** `mcp_config` resolves commands through `shutil.which()` so `npx`
  finds `npx.cmd`, but the npx-based servers themselves have not been run on the
  Snapdragon X Elite box yet.
