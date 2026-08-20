"""Two agent loops over the SAME tools and the SAME LLM-call budget.

raw     - what you get wiring a model to tools naively: tool list in the
          prompt, strict JSON parsing, errors fed back verbatim, no other help.

harness - the scaffolding under test:
          1. few-shot example per tool in the docs
          2. grammar-constrained decoding (Ollama format=json)
          3. lenient JSON extraction + repair feedback
          4. deterministic call repair (rename near-miss params, drop unknowns,
             lift top-level args) before rejecting anything
          5. schema validation with corrective, example-bearing feedback
          6. date/time argument normalization ("2pm" -> "14:00", "tomorrow" ->
             resolved against the simulated clock)
          7. a tool-grounded plan step (JSON list of tool names, not free prose)
          8. loop-breaking: an identical call may repeat up to a per-profile
             budget while the world is unchanged; past that it is not
             re-executed, the duplicated exchanges are removed from context
             (they act as attractors for small models) and the task is restated.
             Any successful write resets the budget.
          9. a verifier pass before accepting done()
         10. auto-injection of relevant long-term memories

Both loops stop after MAX_CALLS total LLM invocations, so the harness pays
for its plan/verify/repair calls out of the same budget.
"""
import datetime
import difflib
import json
import re

from .profiles import DEFAULT as _DEFAULT_PROFILE
from . import toolrouter
from .tools import TOOLS, execute, tool_docs, validate_call
from .world import SIM_TODAY, SIM_TODAY_HUMAN

MAX_CALLS = 14
OBS_LIMIT = 2000  # observation truncation, same in both conditions

# The roles this loop actually asks the LLM for. A tiered lineup may define more
# (model_router ships a "deep" tier); anything not named here is configuration
# the loop never reaches, and a banner should say so rather than list it as one
# of the run's models.
ROLES = ("driver", "router", "verifier")

# Per-model harness tuning (plan/verify/loop-break/output length/...). DEFAULT
# reproduces the benchmark harness exactly; the on-device agents swap in a
# profile chosen for their model via set_profile(). The benchmark never sets it,
# so run_raw and the graded run_harness stay byte-identical to earlier runs.
PROFILE = _DEFAULT_PROFILE


def set_profile(profile):
    global PROFILE
    PROFILE = profile or _DEFAULT_PROFILE

# Abstract on purpose: concrete example content in an instruction becomes an
# attractor that 1B models copy verbatim. Real examples live per-tool in docs.
SHAPE = '{"thought": "<why>", "tool": "<tool_name>", "args": { ... }}'


# ---------------------------------------------------------------- parsing ----

def strip_fences(text):
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    return m.group(1).strip() if m else text.strip()


def parse_strict(text):
    """Raw condition: fence-strip + json.loads, nothing else."""
    try:
        obj = json.loads(strip_fences(text))
        if isinstance(obj, dict):
            return obj, None
        return None, "response was not a JSON object"
    except Exception as e:
        return None, f"response was not valid JSON ({e})"


def parse_lenient(text):
    """Harness condition: also brace-match the first object and repair
    trailing commas."""
    obj, err = parse_strict(text)
    if obj is not None:
        return obj, None
    text = strip_fences(text)
    start = text.find("{")
    if start == -1:
        return None, "no JSON object found in response"
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                cand = text[start:i + 1]
                for fix in (cand, re.sub(r",\s*([}\]])", r"\1", cand)):
                    try:
                        obj = json.loads(fix)
                        if isinstance(obj, dict):
                            return obj, None
                    except Exception:
                        pass
                return None, "found a {...} block but it is not valid JSON"
    return None, "unbalanced braces in response"


# ---------------------------------------------------- date/time normalizing ----

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}


def normalize_date(value, today=None):
    # bound at call time, not import time, so a runner can point the harness at
    # the real clock by setting agent.SIM_TODAY (the benchmark leaves it alone)
    today = today or SIM_TODAY
    if not isinstance(value, str):
        return value
    s = value.strip().lower()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    if s == "today":
        return today.isoformat()
    if s == "tomorrow":
        return (today + datetime.timedelta(days=1)).isoformat()
    m = re.match(r"^(?:next\s+)?([a-z]+day)$", s)
    if m and m.group(1) in _WEEKDAYS:
        delta = (_WEEKDAYS.index(m.group(1)) - today.weekday()) % 7 or 7
        return (today + datetime.timedelta(days=delta)).isoformat()
    m = re.match(r"^([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?$", s)
    if m:
        for name, num in _MONTHS.items():
            if name.startswith(m.group(1)):
                year = int(m.group(3)) if m.group(3) else today.year
                return f"{year:04d}-{num:02d}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$", s)
    if m:
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        return f"{year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return value


def normalize_time(value):
    if not isinstance(value, str):
        return value
    s = value.strip().lower().replace(".", "")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if not m:
        return value
    h = int(m.group(1))
    mins = m.group(2) or "00"
    ap = m.group(3)
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or int(mins) > 59:
        return value
    return f"{h:02d}:{mins}"


def normalize_args(name, args):
    if not isinstance(args, dict):
        return args
    out = dict(args)
    for key in out:
        if key == "date":
            out[key] = normalize_date(out[key])
        elif key in ("start_time", "end_time", "time"):
            out[key] = normalize_time(out[key])
    return out


def task_dates(task_text, today=None):
    """Every date expression the task itself names, resolved to ISO dates.

    Reuses the same normalizer the harness applies to arguments, so the two
    always agree on what "next tuesday" means. Matches the expression kinds
    normalize_date can resolve: weekday names (with optional "next", plural s),
    today/tomorrow, "July 23", "7/23", and literal YYYY-MM-DD. A bare month
    with no day number is not a date and does not match.
    """
    today = today or SIM_TODAY
    text = str(task_text).lower()
    found = set()
    patterns = [
        r"\b(?:next\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b",
        r"\btoday\b|\btomorrow\b",
        r"\b(?:january|february|march|april|may|june|july|august|september|october"
        r"|november|december)\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?\b",
        r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",
        r"\b\d{4}-\d{2}-\d{2}\b",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text):
            expr = m.group(0).rstrip("s") if re.match(patterns[0], m.group(0)) else m.group(0)
            resolved = normalize_date(expr, today)
            if isinstance(resolved, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", resolved):
                found.add(resolved)
    return found


def _describe(iso, today):
    d = datetime.date.fromisoformat(iso)
    return f"{iso} (a {_WEEKDAYS[d.weekday()].capitalize()})"


def task_date_mismatch(task_text, args, today=None):
    """The task names exactly one date and the call carries a different one.

    normalize_date fixes "wednesday" but returns immediately when the model has
    already written a well-formed YYYY-MM-DD - so a model that does the
    arithmetic itself and gets it wrong sails straight through. Observed live:
    the task said Wednesday, the 8B sent 2026-07-27 (a Monday), every tool
    answered honestly for the wrong day, and the agent told a colleague their
    Wednesday was clear.

    This began as a weekday-only check; it now covers every date expression the
    normalizer understands, because the failure is the same whatever form the
    task used: "tomorrow", "July 23" and "7/23" can all be mis-resolved by the
    model with no tool ever noticing.

    Deliberately conservative in the same way as before: only when the task
    names exactly ONE distinct date, so "move my Wednesday meeting to Friday"
    is left alone. The harness never rewrites the date - it says what is wrong
    and what the right one is, the same way it handles a bad parameter.
    """
    today = today or SIM_TODAY
    value = (args or {}).get("date")
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", value.strip()):
        return None
    named = task_dates(task_text, today)
    if len(named) != 1:
        return None
    want = next(iter(named))
    got = value.strip()
    try:
        datetime.date.fromisoformat(got)
    except ValueError:
        return None
    if got == want:
        return None
    return (f"The call uses {_describe(got, today)}, but the task means "
            f"{_describe(want, today)}. Use {want} unless a tool result says otherwise.")


def repair_args(name, args):
    """Deterministic near-miss repair: rename close-match parameter names to
    the missing required ones, then drop unknown parameters. Returns
    (fixed_args, [notes])."""
    spec = TOOLS.get(name)
    if not spec or not isinstance(args, dict):
        return args, []
    valid = spec["params"]
    out = dict(args)
    notes = []
    unknown = [k for k in out if k not in valid]
    missing = [p for p, (_, req) in valid.items() if req and out.get(p) in (None, "")]
    for miss in missing:
        cand = difflib.get_close_matches(miss, unknown, n=1, cutoff=0.5)
        if not cand:
            cand = [u for u in unknown if u in miss or miss in u]
        if cand:
            out[miss] = out.pop(cand[0])
            unknown.remove(cand[0])
            notes.append(f"renamed '{cand[0]}' -> '{miss}'")
    for u in unknown:
        out.pop(u)
        notes.append(f"dropped unknown parameter '{u}'")
    return out, notes


# ------------------------------------------------------------- transcripts ----

# Optional observation hook (the web UI sets it): called as hook(kind, content)
# for every transcript note, so a watcher sees each step as it happens. None
# everywhere else, including the benchmark, so the loops are unaffected.
EVENT_HOOK = None


class Episode:
    def __init__(self):
        self.transcript = []   # readable log of everything
        self.parse_failures = 0
        self.invalid_calls = 0
        self.tool_errors = 0
        self.done_summary = None
        self.finished = False
        self.unrequested = ""   # the verifier's report of side effects the task never asked for

    def note(self, kind, content):
        self.transcript.append({"kind": kind, "content": content})
        if EVENT_HOOK:
            EVENT_HOOK(kind, content)


def _obs(text):
    text = str(text)
    return text if len(text) <= OBS_LIMIT else text[:OBS_LIMIT] + " ...[truncated]"


# ------------------------------------------------------------------- RAW ----

RAW_SYSTEM = """You are an assistant that completes office tasks using tools. \
Today is {today}.

Available tools:
{docs}

Respond with a single JSON object of the form {{"tool": "<tool name>", "args": {{...}}}}. \
Call the done tool when the task is finished."""


def run_raw(llm, world, mem, task_text):
    ep = Episode()
    system = RAW_SYSTEM.format(today=SIM_TODAY_HUMAN, docs=tool_docs(with_examples=False))
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": task_text}]
    ep.note("system", system)
    ep.note("task", task_text)

    try:
        _raw_loop(llm, world, mem, ep, messages)
    finally:
        # Snapshot no matter how the loop ended. Without this, a crash mid-run
        # (ollama dying, a network error) lost every world mutation: the UI had
        # already told the user "send_message - written", but state.json was
        # only written on clean exit, so after a restart the sent message had
        # never happened.
        world.snapshot()
    return ep


def _raw_loop(llm, world, mem, ep, messages):
    while llm.calls < MAX_CALLS:
        reply = llm.chat(messages, force_json=False)
        messages.append({"role": "assistant", "content": reply})
        ep.note("model", reply)
        obj, err = parse_strict(reply)
        if obj is None:
            ep.parse_failures += 1
            fb = f"ERROR: {err}. Respond with a single JSON object: {{\"tool\": ..., \"args\": {{...}}}}"
            messages.append({"role": "user", "content": fb})
            ep.note("feedback", fb)
            continue
        name = obj.get("tool") or obj.get("name") or ""
        args = obj.get("args") if isinstance(obj.get("args"), dict) else {}
        if name == "done":
            ep.done_summary = str(args.get("summary", ""))
            ep.finished = True
            ep.note("done", ep.done_summary)
            break
        ok, obs = execute(name, args, world, mem)
        if not ok:
            ep.tool_errors += 1
        obs = _obs(obs)
        messages.append({"role": "user", "content": f"OBSERVATION: {obs}"})
        ep.note("observation", obs)


# --------------------------------------------------------------- HARNESS ----

HARNESS_SYSTEM = """You are a careful office assistant agent. Today is {today}.
You interact with the world ONLY by calling tools, one call per reply.

RESPONSE FORMAT - every reply must be exactly one JSON object:
{shape}

Rules:
- ONE tool call per reply. No text outside the JSON object.
- Only do what the task requires - nothing extra.
- Look before you act: read the relevant emails or calendar before writing anything
  that depends on them. This applies even when you already believe you know the answer.
- Dates must be YYYY-MM-DD. Times must be 24-hour HH:MM.
- If a tool returns an ERROR, fix the arguments and try again.
- When every part of the task is complete, call done with a short summary.

TOOLS:
{docs}{memory_block}{extra_rules}"""

# Appended to the harness system prompt. Empty for the benchmark, so the graded
# prompt stays byte-identical to earlier runs; the on-device agents set it.
EXTRA_RULES = ""

# Extra world-changing tool names, for the loop-breaking check. Empty for the
# benchmark; the on-device agents add the real-filesystem writers.
EXTRA_WRITE_TOOLS = set()

# Show the model only the tools a task looks like it needs (toolrouter.py),
# instead of the whole registry. Off for the benchmark, so the graded prompt
# keeps every tool exactly as before.
ROUTE_TOOLS = False

PLAN_PROMPT = ('Which tools will you need to call to complete this task, in order? '
               'Reply with one JSON object: {"steps": [{"tool": "<tool_name>", "what": "<5 words>"}, ...]}. '
               'Most tasks need only 1-4 calls. Do not include tools the task does not need.')


FILENAME_RE = re.compile(r"\b[\w.\-]+\.xlsx\b")

# The tools that change something. Module level because the verifier needs the
# same answer the loop uses when it decides what counts as a side effect.
BASE_WRITE_TOOLS = frozenset({"send_email", "add_event", "update_event", "cancel_event",
                              "send_message", "set_reminder", "create_presentation",
                              "create_spreadsheet", "save_memory"})

REPLAN_PROMPT = (
    'TASK: {task}\n\nYour plan was written before you had read anything:\n{plan}\n\n'
    'You have now read something, and it may require work the plan does not '
    'name. Rewrite the plan for what is ACTUALLY left to do, as tool names. '
    'Respond with one JSON object: {{"steps": [{{"tool": "<tool name>", "what": '
    '"<why>"}}]}} - and nothing the task does not need.')


def planned_tools(plan_text):
    """The tool names out of a rendered plan, in order. plan_step writes each
    step as "N. tool - what", so this reads back what it wrote."""
    return [m.group(1) for m in re.finditer(r"^\d+\.\s+(\S+)", plan_text or "", re.M)
            if m.group(1) in TOOLS]


def plan_step(llm, messages, ep):
    """Ask for a tool-grounded plan; return it as short text (or ''). Invalid
    tool names are dropped - free prose never enters the context."""
    reply = llm.chat(messages, force_json=True, num_predict=250, role="router")
    obj, _ = parse_lenient(reply)
    steps = []
    if isinstance(obj, dict) and isinstance(obj.get("steps"), list):
        for s in obj["steps"][:PROFILE.plan_max_steps]:
            if isinstance(s, dict) and s.get("tool") in TOOLS:
                what = str(s.get("what", ""))[:60]
                steps.append(f"{len(steps) + 1}. {s['tool']} - {what}")
    plan = "\n".join(steps)
    ep.note("plan", plan or f"(unusable plan reply: {reply[:200]})")
    return plan


def run_harness(llm, world, mem, task_text, history=""):
    """history: prior conversation turns as a text block (harness/chat.py
    prompt_block). Empty by default, so a run with no conversation behind it
    builds byte-identical context to before — the same opt-in shape as
    EXTRA_RULES."""
    ep = Episode()
    memories = mem.search(task_text, k=PROFILE.memory_k)  # only matches, no recency fallback
    memory_block = ""
    if memories:
        # "apply them when relevant" read as instruction, and a memory outranked
        # looking. Observed live: a run had saved "Wednesday has 0 meetings", so
        # the agent messaged a colleague that their Wednesday was clear without
        # opening the calendar, which held three meetings. A memory is a hint
        # from an earlier run, not a reading of the world as it is now.
        memory_block = ("\n\nNOTES FROM EARLIER TASKS (hints, not facts - they may be out "
                        "of date. If one describes the inbox, the calendar or a file, "
                        "check the real thing before you rely on it):\n"
                        + "\n".join(f"- {f}" for f in memories))
    # Routing narrows the docs, never the registry: every tool stays callable,
    # so the plan, request_tools and a model that simply names a tool can all
    # pull one back into view mid-run. build_system() re-renders the prompt when
    # that happens, because docs the model cannot read are not available to it.
    routing = ROUTE_TOOLS
    if routing:
        toolrouter.begin(task_text)

    def build_system():
        return HARNESS_SYSTEM.format(
            today=SIM_TODAY_HUMAN, shape=SHAPE,
            docs=tool_docs(with_examples=True,
                           names=sorted(toolrouter.exposed()) if routing else None),
            memory_block=memory_block,
            extra_rules=EXTRA_RULES + (history or ""))

    system = build_system()
    messages = [{"role": "system", "content": system}]
    ep.note("system", system)
    ep.note("task", task_text)

    # Planning is opt-out per profile: a small model that can't follow a plan
    # should not spend a call producing one.
    plan = ""
    if PROFILE.plan:
        messages.append({"role": "user", "content": f"TASK: {task_text}\n\n{PLAN_PROMPT}"})
        plan = plan_step(llm, messages, ep)
        messages.pop()  # the plan request leaves the context; the plan re-enters as guidance
    act = f"TASK: {task_text}\n\n"
    if plan:
        act += f"Suggested tool sequence (adapt if the results demand it):\n{plan}\n\n"
    act += f"Make the first tool call now. Reply with exactly one JSON object: {SHAPE}"
    messages.append({"role": "user", "content": act})

    verify_rounds = 0
    seen_calls = {}      # signature -> (world_version at last exec, times run there)
    world_version = 0    # bumped on successful writes; a call's repeat budget is
                         # only spent while the world is unchanged, and resets
                         # the moment anything writes
    write_tools = set(BASE_WRITE_TOOLS)
    write_tools |= EXTRA_WRITE_TOOLS  # empty for the benchmark; fs_tools adds its own

    # The plan was rendered for the model to read and then never looked at
    # again. It is the only statement of intent the run has, so it is worth one
    # check: if the model planned to look something up and then writes a
    # document before looking at anything, say so once.
    #
    # Observed live, and it is the worst failure this app can produce: asked to
    # build a spreadsheet of July receipts, an 8B skipped to create_spreadsheet
    # and invented 100/200/300/400/500 for receipts that are really $230.00,
    # $87.50 and $412.30. It saved the invented total to long-term memory as a
    # fact, and the run reported success - every check downstream can see that a
    # file was written and none can see that its numbers were made up.
    # Only a read the plan itself put BEFORE its first write counts. Taking the
    # first non-write in the plan was wrong: a plan of think -> create_spreadsheet
    # -> read_spreadsheet reads back the file it is about to create, and the
    # nudge sent the agent to open a spreadsheet that did not exist yet. If the
    # plan never proposed looking at anything first, there is nothing to hold
    # the model to and the loop says nothing.
    planned = planned_tools(plan)
    if routing and toolrouter.expose(planned):
        messages[0]["content"] = build_system()
        toolrouter.take_dirty()
    first_write_at = next((i for i, t in enumerate(planned) if t in write_tools), None)
    first_read_planned = None
    if first_write_at is not None:
        first_read_planned = next((t for t in planned[:first_write_at]
                                   if t not in ("think", "done") and t not in write_tools),
                                  None)
    looked = False
    nudged_to_look = False
    # The other half of holding the model to its own plan. Observed live: asked
    # only to "list my emails", an 8B read one, then SENT an email, added a
    # calendar event and messaged a third party - four side effects for a
    # read-only request, and every surface reported success. Harmless against
    # the simulation; real mail the moment a live account is wired. A write the
    # plan never named is questioned once, then allowed if the model insists -
    # the same contract as the read-before-write nudge: the harness questions,
    # it never forbids. Only armed when a plan exists, and save_memory is
    # exempt (remembering is never a side effect on another person).
    planned_set = set(planned)
    questioned_writes = set()
    replanned = False
    # Files the run has been TOLD about (a filename inside something it read)
    # versus files it has actually opened. Writing a document while the task's
    # own data sits unopened in a file on disk is the same failure as writing
    # before reading anything, one step further in: observed live, an email said
    # "the export is in q3_raw.xlsx", the agent never opened it, and invented
    # Sales/Profit rows with formulas summing empty cells.
    mentioned_files = set()
    opened_files = set()
    questioned_files = set()
    last_reply = None
    think_streak = 0

    def give_feedback(fb, reply):
        """Append corrective feedback; a verbatim-repeated bad reply gets its
        older copy deleted from context (repetition is an attractor)."""
        nonlocal last_reply
        if reply == last_reply and len(messages) >= 3 \
                and messages[-3]["role"] == "assistant" and messages[-3]["content"] == reply:
            del messages[-3:-1]
            fb = "You repeated the same invalid reply. It is still invalid. " + fb
        messages.append({"role": "user", "content": fb})
        ep.note("feedback", fb)
        last_reply = reply

    # try/finally, not a call at the end: a crash mid-run (ollama dying, a
    # network error) used to lose every world mutation, because the snapshot
    # only ran on clean exit. The UI had already told the user a write
    # succeeded; after a restart it had never happened.
    try:
        while llm.calls < MAX_CALLS:
            reply = llm.chat(messages, force_json=True, num_predict=PROFILE.num_predict,
                             role="driver")
            messages.append({"role": "assistant", "content": reply})
            ep.note("model", reply)
            obj, err = parse_lenient(reply)
            if obj is None:
                ep.parse_failures += 1
                give_feedback(f"FORMAT ERROR: {err}. Reply with exactly one JSON object: {SHAPE}", reply)
                continue
            name = str(obj.get("tool") or obj.get("name") or "").strip()
            args = obj.get("args") if isinstance(obj.get("args"), dict) else {}
            if not args:
                # repair: models sometimes put args at the top level next to "tool"
                args = {k: v for k, v in obj.items() if k not in ("tool", "name", "thought", "args")}

            if name == "done":
                if verify_rounds < PROFILE.verify_rounds and llm.calls < MAX_CALLS:
                    verify_rounds += 1
                    verdict = _verify(llm, world, task_text)
                    ep.note("verify", json.dumps(verdict, ensure_ascii=False))
                    # Carried on the episode, not acted on: the writes already
                    # happened, and auto-undoing them would be a bigger side
                    # effect than the one being reported. The runner surfaces it
                    # so the person who asked can judge.
                    extra = str(verdict.get("unrequested") or "").strip()
                    if extra and extra.lower() not in ("none", "n/a", "nothing"):
                        ep.unrequested = extra
                    if not verdict.get("complete", True):
                        give_feedback("VERIFIER: the task is NOT finished yet. Missing: "
                                      f"{verdict.get('missing', 'unknown')}. Continue with the next tool call.",
                                      reply)
                        continue
                ep.done_summary = str(args.get("summary", ""))
                ep.finished = True
                ep.note("done", ep.done_summary)
                break

            args, fixes = repair_args(name, args)
            if fixes:
                ep.note("repair", "; ".join(fixes))
            args = normalize_args(name, args)

            # Name recovery: a real tool the model named but cannot see is
            # exposed and run, not rejected. Naming it IS the request, and a
            # round trip spent making it ask politely would be the scaffolding
            # wasting the budget it exists to protect.
            if routing and name in TOOLS and name not in toolrouter.exposed():
                toolrouter.expose([name])
                ep.note("repair", f"exposed {name}: named but not in view")
            problems = validate_call(name, args)
            # Writes only. The guard exists to stop a write landing on the wrong
            # day; a READ with a mismatched date is the model looking around,
            # and the result comes back as evidence either way. Checked on
            # every call, it hounded a run whose task merely said "never on
            # Fridays": four corrections for four innocent list_events probes,
            # 14 calls for a task that needs four.
            wrong_day = (task_date_mismatch(task_text, args)
                         if not problems and name in write_tools else None)
            if wrong_day:
                ep.invalid_calls += 1
                give_feedback("WRONG DATE: " + wrong_day + " Reply with one corrected JSON object.",
                              reply)
                continue
            if problems:
                ep.invalid_calls += 1
                hint = ""
                if name in TOOLS:
                    hint = " Correct shape: " + json.dumps(TOOLS[name]["example"], ensure_ascii=False)
                else:
                    close = difflib.get_close_matches(name, TOOLS.keys(), n=1)
                    if close:
                        hint = (f" Did you mean '{close[0]}'? Correct shape: "
                                + json.dumps(TOOLS[close[0]]["example"], ensure_ascii=False))
                give_feedback("INVALID CALL: " + "; ".join(problems) + "." + hint
                              + " Reply with one corrected JSON object.", reply)
                continue
            last_reply = reply

            if (planned_set and name in write_tools and name != "save_memory"
                    and name not in planned_set and name not in questioned_writes):
                # The plan is written before the agent has read anything, so on
                # any task whose requirements live in the data ("read this email
                # and do what it asks") it CANNOT name the work. Holding the
                # model to it then punishes the model for discovering the job.
                # Observed live: plan was list/read/send, the email asked for a
                # spreadsheet, the model correctly called create_spreadsheet,
                # this guard questioned it, and the model gave up and sent an
                # email claiming it had built the sheet. Nothing was built and
                # every surface reported success.
                #
                # So: a plan made before discovery is a hypothesis. Once a read
                # has landed, spend one call revising it instead. Only once - a
                # model that could re-plan on every surprise could rewrite its
                # way to anything, which is the guard this replaces.
                if looked and not replanned:
                    replanned = True
                    messages.append({"role": "user", "content": REPLAN_PROMPT.format(
                        task=task_text, plan=plan or "(none)")})
                    plan = plan_step(llm, messages, ep) or plan
                    messages.pop()
                    planned = planned_tools(plan)
                    if routing and toolrouter.expose(planned):
                        messages[0]["content"] = build_system()
                        toolrouter.take_dirty()
                    planned_set = set(planned)
                if name not in planned_set:
                    questioned_writes.add(name)
                    give_feedback(
                        f"Your plan for this task never included {name}, and the task is: "
                        f"\"{task_text}\". If {name} is genuinely what the task needs, "
                        f"call it again and it will run. If it is not, continue with the "
                        f"plan or call done.",
                        reply)
                    continue

            # Same contract, one step further in than the nudge below: there,
            # nothing has been read at all; here, something was read and it
            # named a file that exists and is still unopened.
            unread = sorted(mentioned_files & set(world.file_names()) - opened_files
                            - questioned_files)
            if unread and name in ("create_spreadsheet", "create_presentation"):
                questioned_files.update(unread)
                give_feedback(
                    f"What you read names {unread[0]}, which exists here, and you "
                    f"have not opened {unread[0]} yet - so {name} would be writing "
                    f"from memory rather than from the task's own data. Call "
                    f"read_spreadsheet on it first. If you genuinely do not need it, "
                    f"call {name} again and it will run.",
                    reply)
                continue

            # One nudge, never a block: if it insists, the next identical call runs.
            if (first_read_planned and not looked and not nudged_to_look
                    and name in write_tools and name != "save_memory"):
                nudged_to_look = True
                give_feedback(
                    f"You planned to call {first_read_planned} first and have not read "
                    f"anything yet, so {name} would be writing from memory rather than "
                    f"from the task's own data. Call {first_read_planned} first. If you "
                    f"genuinely do not need it, call {name} again and it will run.",
                    reply)
                continue

            sig = json.dumps({"t": name, "a": args}, sort_keys=True, default=str)
            # A call may repeat up to its budget while the world is unchanged; any
            # successful write moves world_version and hands out a fresh budget,
            # because the same call can now legitimately return something new.
            last_version, repeats, last_ok = seen_calls.get(sig, (None, 0, True))
            if last_version != world_version:
                repeats = 0
            limit = PROFILE.repeat_limit_write if name in write_tools else PROFILE.repeat_limit
            if not last_ok:
                # The repeat budget exists so a model can look at something twice -
                # read the email, think, read it again. That reasoning only holds
                # for a call that WORKED. An identical call that errored against an
                # unchanged world will produce the identical error, so a budget of
                # three buys three copies of the same failure. Observed live: an 8B
                # spent three of its twenty calls on read_email("c3"), a calendar id
                # it had mistaken for an email id.
                limit = 1
            if PROFILE.loop_break and name != "think" and repeats >= limit:
                # Budget spent against an unchanged world: re-running it cannot
                # return anything new. If this is a verbatim repeat of the previous
                # exchange, delete the older copy (repetition in context is an
                # attractor for small models).
                if len(messages) >= 3 and messages[-3]["role"] == "assistant" \
                        and messages[-3]["content"] == reply:
                    del messages[-3:-1]
                if not last_ok:
                    fb = (f"{name} with exactly those arguments already failed, and nothing has "
                          f"changed since, so it will fail the same way. Its error is above - fix "
                          f"the arguments or use a different tool. The task is: \"{task_text}\"")
                elif limit == 1:
                    # byte-identical to the phrasing the benchmark runs on
                    fb = (f"You already called {name} with exactly those arguments; its result is above "
                          f"and has not changed. Do the NEXT step of the task: \"{task_text}\" "
                          f"If everything is complete, call done.")
                else:
                    fb = (f"You have called {name} with exactly those arguments {repeats} times now; "
                          f"its result is above and has not changed. Do the NEXT step of the task: "
                          f"\"{task_text}\" If everything is complete, call done.")
                messages.append({"role": "user", "content": fb})
                ep.note("feedback", fb)
                continue
            think_streak = think_streak + 1 if name == "think" else 0

            ok, obs = execute(name, args, world, mem)
            if routing and toolrouter.take_dirty():
                messages[0]["content"] = build_system()
            if ok and name not in write_tools and name != "think":
                looked = True
                # A filename the run was told about, from the result rather than
                # from the model's own words: a model that writes "I'll check
                # data.xlsx" has not been told anything.
                mentioned_files.update(FILENAME_RE.findall(str(obs)))
            if ok and name == "read_spreadsheet":
                opened_files.add(str(args.get("filename", "")))
            if ok and name in write_tools:
                world_version += 1
            # recorded against the world version AFTER any bump, so an identical
            # write stacked on its own result still counts as a repeat
            seen_calls[sig] = (world_version, repeats + 1, ok)
            if not ok:
                ep.tool_errors += 1
            obs = _obs(obs)
            if think_streak >= PROFILE.think_streak_cap:
                obs += " NOTE: stop thinking and take a concrete action now."
            messages.append({"role": "user", "content": f"OBSERVATION: {obs}"})
            ep.note("observation", obs)
    finally:
        world.snapshot()
    if routing:
        toolrouter.end()
    return ep


VERIFY_SYSTEM = ("You are a task-completion verifier. Today is {today}.\n"
                 "Judge ONLY the requirements stated in the task. You are not "
                 "reviewing how the tools were called, how they could have been "
                 "called better, or what would be nice to add: if every "
                 "requirement the task states has a matching successful action, "
                 "the task is complete. When in doubt, answer complete: true.\n"
                 "If the task DELEGATES its requirements to something the "
                 "assistant read (\"do what the email asks\", \"produce what she "
                 "wants\"), then the requirements are whatever that read's result "
                 "asked for, and actions that satisfy them were requested.\n"
                 "Separately, report any action that CHANGED something (sent, "
                 "created, updated, cancelled) that the task never asked for. "
                 "Reading and thinking are never unrequested. This report does "
                 "not make the task incomplete.")


def _verify(llm, world, task_text):
    acts = [a for a in world.actions if a["tool"] != "think"]
    lines = []
    for a in acts:
        status = "ok" if a["ok"] else "FAILED"
        # The result, not just the signature. Given only "create_spreadsheet(...)
        # -> ok" a verifier cannot see that the file it asked for already
        # exists, and it answers complete:false with an invented requirement.
        # Observed: it sent an 8B back to redo a finished task and the rerun
        # wrote a SECOND spreadsheet, so one task left the user two files.
        # A write's result echoes arguments the model already chose, so 200
        # chars is plenty. A READ's result is the only place new information
        # enters the run, and on an indirect task ("do what the email asks")
        # it is where the requirements themselves live. Measured: a 248-char
        # email was clipped four words before "turn the same numbers into a
        # short deck", so the verifier passed runs that never built the deck
        # and reported the spreadsheet it DID build as unrequested.
        cap = 200 if a["tool"] in (BASE_WRITE_TOOLS | EXTRA_WRITE_TOOLS) else 800
        result = str(a.get("result", ""))[:cap].replace("\n", " ")
        lines.append(f"- {a['tool']}({json.dumps(a['args'], ensure_ascii=False, default=str)[:200]}) "
                     f"-> {status}: {result}")
    prompt = (f"TASK GIVEN TO AN ASSISTANT:\n{task_text}\n\n"
              f"ACTIONS THE ASSISTANT TOOK, WITH THEIR RESULTS:\n" + "\n".join(lines or ["(none)"])
              + "\n\nTake each requirement the task states, in turn, and find the action "
                "that satisfies it. Report as missing only a requirement with no such action. "
                'Respond with one JSON object: {"complete": true or false, "missing": "<the '
                'task requirements with no matching action, or an empty string>", '
                '"unrequested": "<TOOL NAMES ONLY, comma separated, of world-changing '
                'actions the task never asked for - or an empty string>"}')
    msgs = [{"role": "system", "content": VERIFY_SYSTEM.format(today=SIM_TODAY_HUMAN)},
            {"role": "user", "content": prompt}]
    # Failing open is the right call: a broken verifier must not trap the agent
    # in a loop it cannot exit. But an unmarked {"complete": true} is
    # indistinguishable from a genuine pass, so a systematically broken verifier
    # reads as a clean run in every transcript. The flag says which one it was.
    try:
        reply = llm.chat(msgs, force_json=True, num_predict=200, role="verifier")
        obj, _ = parse_lenient(reply)
        if isinstance(obj, dict) and isinstance(obj.get("complete"), bool):
            # Repair before rejection, the same as everywhere else in the loop.
            # The format instruction alone does not hold at 8B: it copies the
            # evidence line's shape and runs out of tokens mid-string, and
            # "send_email({" went to the UI verbatim. Keep the tool names this
            # run actually performed and drop whatever else came back.
            # Writes only. The system prompt already says reading is never
            # unrequested and the 8B reports it anyway - observed live,
            # "list_emails, read_spreadsheet" on a run that had looked before it
            # wrote, which is the behaviour the rest of the harness is built to
            # produce. Flagging it would teach precisely the wrong lesson.
            said = str(obj.get("unrequested") or "")
            writes = BASE_WRITE_TOOLS | EXTRA_WRITE_TOOLS
            obj["unrequested"] = ", ".join(sorted(
                {a["tool"] for a in acts if a["tool"] in writes
                 and re.search(rf"\b{re.escape(a['tool'])}\b", said)}))
            return obj
        return {"complete": True, "missing": "", "unverified": "verifier reply was not usable"}
    except Exception as exc:
        return {"complete": True, "missing": "",
                "unverified": f"verifier failed: {type(exc).__name__}"}
