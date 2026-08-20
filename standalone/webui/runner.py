"""Run one agent folder and narrate it as a JSONL event stream on stdout.

Same engine, same config, same state files as `agents/<size>/run_agent.py` — it
just installs the harness's observation hooks so a watcher can see every step
as it happens, and it takes its confirmations over stdin instead of a terminal
prompt. The web server spawns one of these per run; nothing here is imported by
the benchmark.

    python -m webui.runner --agent 8b --task "Book a free hour Thursday"

Events (one JSON object per line):
    banner   agent/model/budget/toolset, once at the start
    llm_start / token / llm_end     a model call, streamed as it is written
    note     a transcript entry: plan, model, observation, feedback, repair,
             verify, done, system
    tool     an executed call, with the arguments as actually run
    world    a snapshot of the agent's folder (inbox, calendar, files, memory)
    confirm  a destructive action awaiting a y/n answer on stdin
    end      finished/summary/usage, once
    error    the run died
"""
import argparse
import datetime
import json
import os
import re
import signal
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT)

from harness import agent as agent_mod  # noqa: E402
from harness import chat  # noqa: E402
from harness import fs_tools  # noqa: E402
from harness import llm as llm_mod  # noqa: E402
from harness import mcp_bridge  # noqa: E402
from harness import mcp_config  # noqa: E402
from harness import narrate  # noqa: E402
from harness import profiles  # noqa: E402
from harness.tuner import run_metrics  # noqa: E402
from harness import tools as tools_mod  # noqa: E402
from harness.agent import run_harness  # noqa: E402
from harness.llm import LLM, OLLAMA_URL  # noqa: E402
from harness.memory import MemoryStore  # noqa: E402
from harness.model_router import ModelRouter, adapters_note, default_roles  # noqa: E402
from harness.world import World  # noqa: E402

AGENTS_DIR = os.path.join(PROJECT, "agents")

REAL_RULES = """

You also have tools that act on the REAL computer, inside the working root
{root}. Paths are relative to that root.
- Look before you write: call list_dir or read_file first, so you change the
  file that actually exists instead of one you assumed.
- Never delete or overwrite anything the task did not ask you to change.
- The user is asked to confirm deletes, overwrites and shell commands. If one
  is declined, do not retry it - choose another approach.
- create_presentation and create_spreadsheet write .pptx and .xlsx into this
  agent's own files folder, NOT into the working root. To put text in the
  working root, use write_file."""

MAX_TREE_ENTRIES = 400


def emit(event, **fields):
    line = json.dumps({"t": event, "ts": round(time.time(), 3), **fields},
                      ensure_ascii=False, default=str)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


# ------------------------------------------------------------- the folder ----

def list_files(files_dir):
    out = []
    if not os.path.isdir(files_dir):
        return out
    for name in sorted(os.listdir(files_dir)):
        path = os.path.join(files_dir, name)
        if os.path.isfile(path):
            st = os.stat(path)
            out.append({"name": name, "size": st.st_size, "mtime": st.st_mtime})
    return out


def list_tree(root):
    """A shallow listing of the real working root, for the folder panel."""
    out = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath[len(root):].count(os.sep)
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d != "__pycache__")
        if depth >= 3:
            dirnames[:] = []
        for name in dirnames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            out.append({"name": rel, "dir": True})
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            out.append({"name": os.path.relpath(full, root), "size": size})
        if len(out) >= MAX_TREE_ENTRIES:
            return out[:MAX_TREE_ENTRIES]
    return out



def next_run_index(log_dir):
    """One past the highest run_NNN already there.

    This counted files, not runs. Two things broke it. With --tiers,
    model_calls.jsonl takes a slot and the numbering jumps; and deleting any
    transcript frees an index the next run then silently writes over, so a run
    could destroy an earlier one with no error. Max-of-existing never reuses a
    number, whatever else is in the folder.
    """
    used = [int(m.group(1)) for m in
            (re.match(r"run_(\d+)\.json$", f) for f in os.listdir(log_dir)) if m]
    return max(used, default=0) + 1


CALL_CEILING = 200
CALL_FLOOR = 2
REAL_WORK_FLOOR = 40


def call_budget(profile, override=None, extended=False):
    """LLM calls one run may spend, before the loop stops it.

    A ceiling, not a target: an agent that calls done in four calls costs four,
    so headroom is cheap and a tight number mostly buys premature cut-offs.

    `override` is the user's own number, from the preferences menu or --max-calls,
    and it wins outright - clamped, because this is the trust boundary. A number
    input's min/max is a hint to a browser, not a constraint on the HTTP API.

    `extended` means real files or real accounts are in play. That work costs
    more calls than the simulated office, since the agent has to look before it
    writes and every listing is a whole call. It raises a tight profile to a
    floor rather than scaling it, so a model already given room keeps its own
    number instead of being doubled into a runaway.
    """
    if override:
        return max(CALL_FLOOR, min(int(override), CALL_CEILING))
    return max(profile.max_calls, REAL_WORK_FLOOR) if extended else profile.max_calls


def world_snapshot(world, mem, root=None):
    snap = {
        "emails": world.emails,
        "sent": world.sent_emails,
        "events": sorted(world.events, key=lambda e: (e["date"], e["start"])),
        "messages": world.messages,
        "reminders": world.reminders,
        "files": list_files(world.files_dir),
        "memory": mem.all(),
    }
    if root:
        snap["tree"] = list_tree(root)
    return snap


# ------------------------------------------------------------ confirmation ----

class Confirmer:
    """Ask the browser instead of the terminal. Emits a confirm event and
    blocks until the server writes the answer to stdin.

    A decline is remembered. Every caller's ToolError already tells the model
    not to retry a declined call, but nothing enforced it: observed live
    against a real MCP server, a declined draft was re-attempted three times,
    which put the SAME dialog in front of the person three times after they had
    said no, and spent 22 of the run's calls doing it. Re-asking a question
    already answered is the bug; the model rewording its arguments slightly
    each time is why signature-based dedupe in the loop cannot catch it.

    Keyed on the action and the tool, not the exact wording, for that reason.
    Lives here rather than in fs_tools or mcp_bridge because both take this one
    callback, so one guard covers every module that asks."""

    def __init__(self):
        self.n = 0
        self.declined = set()
        # Filled in after mcp_bridge.enable(), so a confirmation can say whether
        # it touches a real account. The dialog was identical for overwriting a
        # scratch file and for sending mail from a live mailbox.
        self.real_servers = set()
        self.mode = None

    def __call__(self, action, detail):
        # "gmail: draft_mail {...}" -> "gmail: draft_mail". The arguments move
        # between attempts; the decision was about the action.
        key = (action, str(detail).split("{")[0].strip())
        if key in self.declined:
            emit("note", kind="feedback",
                 content=f"auto-declined: the user already refused this {action}")
            return False
        # mcp_bridge always formats its detail as "<server-id>: <tool> {args}",
        # so the prefix identifies a real account without changing the callback
        # signature that fs_tools and the terminal runner also use.
        head = str(detail).split(":", 1)[0].strip()
        real = head if head in self.real_servers else None
        self.n += 1
        cid = self.n
        emit("confirm", id=cid, action=action, detail=detail,
             real=real, mode=self.mode if real else None)
        while True:
            line = sys.stdin.readline()
            if not line:  # server went away: nobody can answer, so nothing may proceed
                self.declined.add(key)
                return False
            try:
                ans = json.loads(line)
            except ValueError:
                continue
            if ans.get("id") == cid:
                allow = bool(ans.get("allow"))
                if not allow:
                    self.declined.add(key)
                return allow


# -------------------------------------------------------------------- run ----

def build_llm(cfg, args, log_dir):
    use_router = args.tiers or bool(cfg.get("router"))
    if not use_router:
        return LLM(cfg["model"], num_ctx=cfg.get("num_ctx", 8192)), None
    rcfg = cfg.get("router", {})
    roles = rcfg.get("roles") or default_roles(
        base=rcfg.get("base", cfg["model"]),
        small=args.small or rcfg.get("small"),
        deep=args.deep or rcfg.get("deep", "qwen2.5:14b"))
    os.makedirs(log_dir, exist_ok=True)
    router = ModelRouter(roles=roles, num_ctx=cfg.get("num_ctx", 8192),
                         log_path=os.path.join(log_dir, "model_calls.jsonl"))
    return router, router


def _on_terminate(signum, frame):
    """Stop in the UI sends SIGTERM, and Python's default reaction is to die
    without unwinding - so the harness's finally never ran and a STOPPED run
    lost its world writes exactly the way a crashed one used to. Raising
    SystemExit turns the signal into ordinary unwinding: the snapshot in
    run_harness's finally executes, the hooks are cleared, and the process
    still exits nonzero. 143 is the conventional code for death by SIGTERM."""
    raise SystemExit(143)


def main():
    signal.signal(signal.SIGTERM, _on_terminate)
    p = argparse.ArgumentParser()
    p.add_argument("--agent", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--root", default=None)
    p.add_argument("--shell", action="store_true")
    p.add_argument("--yolo", action="store_true")
    p.add_argument("--max-calls", type=int, default=None)
    p.add_argument("--tiers", action="store_true")
    p.add_argument("--small", default=None)
    p.add_argument("--deep", default=None)
    p.add_argument("--with-office", action="store_true")
    p.add_argument("--thread", default=None,
                   help="conversation thread id; earlier turns enter the prompt")
    p.add_argument("--mcp", default=None,
                   help="comma-separated MCP servers from mcp/servers.json")
    p.add_argument("--mcp-mode", default=None,
                   choices=["draft", "live", "read_only"],
                   help="draft (default) composes but never transmits")
    p.add_argument("--model", default=None,
                   help="drive this agent folder with a different installed tag "
                        "than its config.json names (demo convenience)")
    args = p.parse_args()

    folder = os.path.join(AGENTS_DIR, args.agent)
    with open(os.path.join(folder, "config.json"), encoding="utf-8-sig") as f:
        cfg = json.load(f)
    if args.model:
        # Applied before the profile lookup on purpose: the harness tuning must
        # follow the model actually doing the work, not the one config.json asks
        # for. State paths are unaffected — the folder still owns them.
        cfg["model"] = args.model
    assert "127.0.0.1" in OLLAMA_URL or "localhost" in OLLAMA_URL, "refusing non-local endpoint"

    # This model's custom harness. The profile owns the tuning knobs; a config
    # "harness" block can patch individual fields.
    profile = profiles.for_model(cfg["model"], cfg.get("harness"))
    agent_mod.set_profile(profile)
    cfg["num_ctx"] = cfg.get("num_ctx") or profile.num_ctx

    root = args.root or cfg.get("root")
    # ONE Confirmer for both tool sources: it numbers the prompts it sends to the
    # browser, and two instances would both start at 1, so an answer to a file
    # prompt could resolve an unrelated mail prompt.
    confirmer = None if args.yolo else Confirmer()
    if root:
        root = fs_tools.enable(root,
                               allow_shell=args.shell or bool(cfg.get("allow_shell")),
                               confirm=confirmer)
        if not args.with_office:
            fs_tools.restrict_to_files()
        agent_mod.EXTRA_RULES = REAL_RULES.format(root=root)
        agent_mod.EXTRA_WRITE_TOOLS = fs_tools.WRITE_TOOLS
        today = datetime.date.today()
        agent_mod.SIM_TODAY = today
        agent_mod.SIM_TODAY_HUMAN = today.strftime("%A, %B %d, %Y")

    mcp_cfg = cfg.get("mcp") or {}
    names = ([s.strip() for s in args.mcp.split(",") if s.strip()] if args.mcp
             else mcp_cfg.get("enable") or [])
    mcp_mode = args.mcp_mode or mcp_cfg.get("mode") or "draft"
    mcp_summary = None
    if names:
        servers = mcp_config.names_to_servers(names, mcp_cfg, mode=mcp_mode)
        mcp_summary = mcp_bridge.enable(servers, confirm=confirmer, mode=mcp_mode)
        if confirmer is not None:
            confirmer.real_servers = {x["id"] for x in mcp_summary}
            confirmer.mode = mcp_mode
        mcp_bridge.restrict_to_mcp(keep_office_docs=not root,
                                   keep_extra=fs_tools.injected() if root else ())
        agent_mod.EXTRA_RULES += mcp_bridge.mail_rules(mcp_mode)
        agent_mod.EXTRA_WRITE_TOOLS = (set(agent_mod.EXTRA_WRITE_TOOLS)
                                       | mcp_bridge.WRITE_TOOLS)
        today = datetime.date.today()
        agent_mod.SIM_TODAY = today
        agent_mod.SIM_TODAY_HUMAN = today.strftime("%A, %B %d, %Y")
    # The web UI is a conversation, so the reply the person reads should sound
    # like one. Appended, never assigned: --root and --mcp may already have put
    # their rules in.
    agent_mod.EXTRA_RULES += chat.reply_rules()
    chat.enable_say()      # the tool those rules are about
    agent_mod.MAX_CALLS = call_budget(profile,
                                      override=args.max_calls or cfg.get("max_calls"),
                                      extended=bool(root or names))

    world = World(os.path.join(folder, "workspace"), persistent=True)
    mem = MemoryStore(os.path.join(folder, "memory", "memory.jsonl"))
    # Earlier turns of this conversation, if this run belongs to one. The server
    # writes the turns; the runner only reads them, so a run started from the
    # CLI with no --thread behaves exactly as it always did.
    history = chat.prompt_block(chat.messages(folder, args.thread)) if args.thread else ""
    log_dir = os.path.join(folder, "logs")
    llm, router = build_llm(cfg, args, log_dir)

    tiers = None
    if router:
        # Split what the loop actually calls from what is merely configured. The
        # banner listed every role in the lineup, so a run reported qwen2.5:14b
        # as one of its models when nothing in run_harness ever asks for the
        # "deep" role - and the tag did not have to be installed to be named.
        used = {r: sp["model"] for r, sp in router.roles.items() if r in agent_mod.ROLES}
        idle = {r: sp["model"] for r, sp in router.roles.items() if r not in agent_mod.ROLES}
        tiers = {"roles": used, "unused_roles": idle,
                 "resident": sorted({m for r, m in used.items()
                                     if not router.roles[r].get("on_demand")}),
                 "note": adapters_note()}
    emit("banner", agent=args.agent, name=cfg["name"], model=cfg["model"],
         note=cfg.get("note", ""), budget=agent_mod.MAX_CALLS, task=args.task,
         endpoint=OLLAMA_URL, root=root, shell=bool(args.shell), yolo=bool(args.yolo),
         toolset=("real accounts" if names and not root
                  else "files + real accounts" if names and root
                  else "files only" if root and not args.with_office
                  else "files + office world" if root else "office world"),
         tiers=tiers, today=agent_mod.SIM_TODAY_HUMAN,
         tools=sorted(tools_mod.TOOLS), profile=profile.to_dict(),
         mcp=({"mode": mcp_mode, "servers": mcp_summary,
               "warnings": mcp_config.count_warnings(mcp_summary)}
              if mcp_summary else None))
    emit("world", **world_snapshot(world, mem, root))

    # ---- hooks: narrate the run without changing it ----
    state = {"call": 0}

    def on_stream(event, payload):
        if event == "start":
            state["call"] += 1
            emit("llm_start", call=state["call"], budget=agent_mod.MAX_CALLS,
                 role=payload.get("role") or "driver", model=payload.get("model"))
        elif event == "token":
            emit("token", text=payload.get("text", ""))
        else:
            emit("llm_end", role=payload.get("role") or "driver",
                 ms=payload.get("ms", 0), output_tokens=payload.get("output_tokens", 0))

    def on_note(kind, content):
        emit("note", kind=kind, content=content)

    def on_tool(name, args_, ok, obs):
        # `line` is the step said in a sentence (harness/narrate.py). It rides
        # along with the tool event so the phrasing has one source rather than
        # a copy of the table in JavaScript, and so a tool an MCP server
        # invented is described by the same rule as a built-in one.
        line = (narrate.about(name, args_, done=True) if ok
                else narrate.failed(name, args_, obs))
        emit("tool", name=name, args=args_, ok=ok, result=obs, line=line)
        emit("world", **world_snapshot(world, mem, root))

    llm_mod.STREAM_HOOK = on_stream
    agent_mod.EVENT_HOOK = on_note
    tools_mod.TOOL_HOOK = on_tool

    try:
        ep = run_harness(llm, world, mem, args.task, history=history)
    except Exception as e:
        emit("error", message=f"{type(e).__name__}: {e}", trace=traceback.format_exc())
        raise SystemExit(1)
    finally:
        llm_mod.STREAM_HOOK = agent_mod.EVENT_HOOK = tools_mod.TOOL_HOOK = None

    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"run_{next_run_index(log_dir):03d}.json")
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump({"task": args.task, "root": root, "agent": args.agent,
                   "model": cfg["model"], "via": "webui",
                   "mcp": {"servers": names, "mode": mcp_mode} if names else None,
                   "transcript": ep.transcript, "finished": ep.finished,
                   "summary": ep.done_summary,
                   # The same numbers the "end" event below streams to the
                   # browser, persisted. They are what harness/tuner.py reads:
                   # a run that only leaves a transcript behind can be watched
                   # but not learned from.
                   "profile": dict(profile.to_dict(), max_calls=agent_mod.MAX_CALLS),
                   "metrics": run_metrics(ep, llm, agent_mod.MAX_CALLS)},
                  f, indent=1, ensure_ascii=False)

    emit("world", **world_snapshot(world, mem, root))
    emit("end", finished=ep.finished, summary=ep.done_summary,
         unrequested=ep.unrequested or None,
         calls=llm.calls, budget=agent_mod.MAX_CALLS,
         output_tokens=llm.output_tokens, prompt_tokens=llm.prompt_tokens,
         wall=round(llm.wall, 1), parse_failures=ep.parse_failures,
         invalid_calls=ep.invalid_calls, tool_errors=ep.tool_errors,
         actions=[a for a in world.actions if a["tool"] != "think"],
         usage_by_role=router.usage_by_role() if router else None,
         log=os.path.relpath(log_path, PROJECT))


if __name__ == "__main__":
    main()
