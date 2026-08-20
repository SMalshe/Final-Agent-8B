---
tags: [architecture, tools, context]
cssclasses: [topic-core]
---

# Tool Routing

[toolrouter.py](../standalone/harness/toolrouter.py) — the model sees the tools
the task needs, and can ask for the rest.

> The context window is the budget. Every tool in the registry spends some of it
> on a description and a worked example, and [[Real Accounts|connecting Outlook]]
> exposes 69 tools against an 8k context. `mcp/servers.json` already cuts that
> to ten by hand — but it is the same ten whether the task is "what's on
> Thursday" or "tidy this folder".

## The three ways a hidden tool comes back

Routing is only safe because being wrong is cheap. A router that guesses badly
and hides the one tool the task needed would be worse than no router, so there
are three recoveries and one abstention:

| path | when | cost |
|---|---|---|
| **abstain** | nothing scores against the task | free — the whole registry is shown |
| **the plan** | [[Agent Loop#Plan\|the plan]] names a tool | free — exposed before the first act |
| **name recovery** | the model calls a real tool it cannot see | free — exposed and executed |
| **request_tools** | the model doesn't know the name | 1 call |

Name recovery is the one worth arguing for. A model that calls
`create_presentation` while holding only calendar tools has already said what it
needs; spending a round trip making it ask politely would be the scaffolding
wasting the budget it exists to protect. So the call runs, and the tool joins
the visible set.

`request_tools` is the discovery path, for when the model cannot name what it
wants: it describes the job ("build a slide deck") and the matching tools are
added. **The system message is rebuilt when the set changes** — docs the model
cannot read are not available to it, whatever the registry says.

## Selection

Keyword overlap between the task and each tool's name and description, name
matches counting double, with prefix matching from four characters up so
"event" reaches `list_events` — the same rule [[Persistent State|memory.py]]
retrieves facts with. A synonym table covers the words a task uses for tools it
never names: "book me an hour" is the calendar.

Then a relevance cutoff rather than a flat top-N: keep what scores near the
best, because against a 16-tool registry a top-12 keeps almost everything, which
is not routing.

    "find a free hour on Thursday and book it"
        -> add_event, list_events, update_event, cancel_event
    "build a three slide deck about the budget"
        -> create_presentation

**Routing narrows the docs, never the registry.** Every tool stays callable,
which is exactly what makes all three recoveries possible.

Off unless a runner turns it on (`--route-tools`), so `bench/` and the graded
prompt are untouched — the same rule `EXTRA_RULES` and `SIM_TODAY` follow.

## What routing made affordable

Both of these add tools, which is why they arrive with the router rather than
before it.

### Several working folders

[[Real-Computer Mode|fs_tools]] `enable()` now takes one path or several.
`--root work --root personal` gives each root a label from its folder name, and
the model addresses files as `work/report.xlsx`. The roots stay **separate
sandboxes** — they are not merged into one tree, and neither is a way out of the
other. A bare filename prefers the root where it already exists, so
`report.xlsx` finds the one that is really there; a *new* file lands in the
primary root, so creation stays predictable. Listing nothing in particular lists
the roots, because with several there is no single "here".

### Every calendar at once

[calendars.py](../standalone/harness/calendars.py) injects one extra tool,
`list_events_all`, **only when more than one calendar is registered** — a second
tool that duplicates an existing one is a way for a small model to get the call
wrong, not a convenience.

It calls each calendar with only the arguments that calendar declares, because
they disagree: the simulated office takes `date`, Microsoft Graph takes a start
and end datetime. A source that cannot be satisfied, or that fails, says so
inline and the others still answer — half an answer plus a named gap beats an
error. It only reads: creating an event means choosing a calendar, and guessing
which one is the kind of silent decision the harness questions everywhere else.

## Status

Tested: 12 routing tests, 10 multi-root, 9 calendar merge, and the existing 113
still pass with routing off, so an unrouted run builds the same context as
before.

Not tested against reality: the calendar merge runs against fakes shaped like
the real servers (different argument names, one that fails), because two live
accounts are not available here. The name patterns that decide what counts as a
calendar listing are the part to check against `--mcp-list` output before
trusting it on a real account.

## Related

- [[Agent Loop]] — where the routed prompt is built and rebuilt
- [[Real Accounts]] — the 69 tools that motivated this · [[Real-Computer Mode]]
- [[Self-Tuning]] · [[Harness Profiles]] · [[Tools]]
