"""Self-contained ON-DEVICE agent for this folder's model.

Everything runs locally: inference via the local Ollama server on
127.0.0.1:11434 (weights in C:\\Users\\Lab User\\SAIL\\ollama), files and
memory stay in this folder. Nothing is sent to any cloud service.

Usage:
    run.ps1 "Summarize my Wednesday meetings and message Jordan"
    run.ps1            <- interactive prompt

Real-computer mode (off by default) gives the agent the same kind of access
Claude Code / Codex have - read, write, move, delete and search real files,
optionally run shell commands - scoped to one folder:

    run.ps1 --root C:\\Users\\Lab User\\Desktop\\sandbox "Tidy up these notes"
    run.ps1 --root . --shell "What changed in this project today?"

Without --root the agent only sees the simulated office world, as before.

Real accounts (off by default) connect Gmail / Outlook / Teams through their
MCP servers, so the agent works your actual mail and calendar instead of the
simulated office. Reads are real; sends are not — see --mcp-live.

    run.ps1 --mcp gmail,ms365 "Draft a reply to the budget thread"
    run.ps1 --mcp-help                  <- what each server needs before it works

Flags:
    --root PATH     working root; every path the agent touches must be inside it.
                    Repeat it for several folders: --root work --root personal
    --shell         also allow run_command (PowerShell), still confirmed
    --yolo          skip confirmation prompts for overwrite/delete/move/shell
    --max-calls N   LLM call budget (default 20 simulated, 40 with --root/--mcp)
    --mcp LIST      comma-separated MCP servers from mcp/servers.json
                    (gmail, gcal, ms365, teams, ms365-work)
    --mcp-live      allow send/reply tools. Default is draft: the model composes,
                    a human sends. Every write is still confirmed.
    --mcp-read-only drop every world-changing MCP tool
    --mcp-list      start the named servers, print the tools they expose, exit
    --mcp-help      print setup steps for every known server, exit
    --route-tools   show only the tools the task needs; the model asks for more
                    with request_tools (harness/toolrouter.py)

State persists between runs:
    workspace/state.json   inbox, calendar, sent mail, messages, reminders
    workspace/files/       real .pptx / .xlsx the agent creates
    memory/memory.jsonl    long-term memory (learning)
"""
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, PROJECT)

from harness import agent as agent_mod  # noqa: E402
from harness import fs_tools  # noqa: E402
from harness import mcp_bridge  # noqa: E402
from harness import mcp_config  # noqa: E402
from harness import calendars  # noqa: E402
from harness import mailbox  # noqa: E402
from harness import tools as tools_mod  # noqa: E402
from harness import profiles  # noqa: E402
from harness.tuner import run_metrics  # noqa: E402
from harness.agent import run_harness  # noqa: E402
from harness.llm import LLM, OLLAMA_URL  # noqa: E402
from harness.memory import MemoryStore  # noqa: E402
from harness.model_router import ModelRouter, adapters_note, default_roles  # noqa: E402
from harness.world import World  # noqa: E402

REAL_RULES = """

You also have tools that act on the REAL computer, inside {root}
- Look before you write: call list_dir or read_file first, so you change the
  file that actually exists instead of one you assumed.
- Never delete or overwrite anything the task did not ask you to change.
- The user is asked to confirm deletes, overwrites and shell commands. If one
  is declined, do not retry it - choose another approach."""


def parse_flags(argv):
    opts = {"root": [], "shell": False, "yolo": False, "max_calls": None,
            "tiers": False, "small": None, "deep": None, "office": False,
            "route_tools": False,
            "mcp": [], "mcp_mode": None, "mcp_list": False, "mcp_help": False}
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--mcp" and i + 1 < len(argv):
            opts["mcp"] = [s.strip() for s in argv[i + 1].split(",") if s.strip()]
            i += 2
        elif a == "--mcp-live":
            opts["mcp_mode"] = "live"
            i += 1
        elif a == "--mcp-read-only":
            opts["mcp_mode"] = "read_only"
            i += 1
        elif a == "--mcp-list":
            opts["mcp_list"] = True
            i += 1
        elif a == "--mcp-help":
            opts["mcp_help"] = True
            i += 1
        elif a == "--root" and i + 1 < len(argv):
            # Repeatable: --root work --root personal. Each stays its own
            # sandbox; the model addresses them by folder name.
            opts["root"].append(argv[i + 1])
            i += 2
        elif a == "--shell":
            opts["shell"] = True
            i += 1
        elif a == "--yolo":
            opts["yolo"] = True
            i += 1
        elif a == "--max-calls" and i + 1 < len(argv):
            opts["max_calls"] = int(argv[i + 1])
            i += 2
        elif a == "--tiers":
            opts["tiers"] = True
            i += 1
        elif a == "--small" and i + 1 < len(argv):
            opts["small"] = argv[i + 1]
            opts["tiers"] = True
            i += 2
        elif a == "--deep" and i + 1 < len(argv):
            opts["deep"] = argv[i + 1]
            opts["tiers"] = True
            i += 2
        elif a == "--with-office":
            opts["office"] = True
            i += 1
        elif a == "--route-tools":
            opts["route_tools"] = True
            i += 1
        else:
            rest.append(a)
            i += 1
    return opts, " ".join(rest).strip()


def build_llm(cfg, opts):
    """A plain single-model LLM by default; a tiered ModelRouter when --tiers
    (or a config 'router' block) is set. The router's default lineup keeps ONE
    model resident (driver/router/verifier share the base); a heavier 'deep'
    tier is load-on-demand and evicted after use."""
    use_router = opts["tiers"] or bool(cfg.get("router"))
    if not use_router:
        return LLM(cfg["model"], num_ctx=cfg.get("num_ctx", 8192)), None
    rcfg = cfg.get("router", {})
    roles = rcfg.get("roles") or default_roles(
        base=rcfg.get("base", cfg["model"]),
        small=opts["small"] or rcfg.get("small"),
        deep=opts["deep"] or rcfg.get("deep", "qwen2.5:14b"))
    log_path = os.path.join(HERE, "logs", "model_calls.jsonl")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    router = ModelRouter(roles=roles, num_ctx=cfg.get("num_ctx", 8192), log_path=log_path)
    return router, router


def confirm(action, detail):
    print(f"\n  the agent wants to {action}:\n    {detail}")
    try:
        return input("  allow? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def mcp_names(opts, cfg):
    """Servers to start: --mcp wins, otherwise a config.json "mcp".enable list."""
    return opts["mcp"] or (cfg.get("mcp") or {}).get("enable") or []


def mcp_mode(opts, cfg):
    return opts["mcp_mode"] or (cfg.get("mcp") or {}).get("mode") or "draft"


def start_mcp(names, mode, cfg, confirm_cb, root=None):
    """Launch the named servers and fold their tools into the harness registry.

    Returns the enable() summary. The simulated office tools are dropped: an
    agent holding both list_emails (fake inbox) and gmail_search_emails (real
    account) picks the wrong one, and the fake one always answers faster."""
    servers = mcp_config.names_to_servers(names, cfg.get("mcp"), mode=mode)
    summary = mcp_bridge.enable(servers, confirm=confirm_cb, mode=mode)
    mcp_bridge.restrict_to_mcp(keep_office_docs=not root,
                               keep_extra=fs_tools.injected() if root else ())
    return summary


def print_mcp_banner(summary, mode):
    label = {"draft": "DRAFT — reads real, composes drafts, cannot send",
             "live": "LIVE — send/reply exposed, every write confirmed",
             "read_only": "READ-ONLY — no world-changing tools"}[mode]
    print(f"  real accounts: {label}")
    for s in summary:
        writes = f", {len(s['writes'])} write" if s["writes"] else ", read-only"
        print(f"    {s['id']}: {len(s['tools'])} tools{writes}")
    for w in mcp_config.count_warnings(summary):
        print(f"    warning: {w}")


def main():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8-sig") as f:
        cfg = json.load(f)
    assert "127.0.0.1" in OLLAMA_URL or "localhost" in OLLAMA_URL, "refusing non-local endpoint"

    # This model's custom harness (see harness/profiles.py). The profile owns the
    # tuning knobs; a config "harness" block can patch individual fields.
    profile = profiles.for_model(cfg["model"], cfg.get("harness"))
    agent_mod.set_profile(profile)
    cfg["num_ctx"] = cfg.get("num_ctx") or profile.num_ctx

    opts, task = parse_flags(sys.argv[1:])
    root = opts["root"] or cfg.get("root") or []
    if isinstance(root, str):
        root = [root]

    if opts["mcp_help"]:
        for name, _ in mcp_config.available():
            print(mcp_config.setup_notes(name) + "\n")
        return
    if opts["mcp_list"]:
        # Ground truth for the allow lists in mcp/servers.json: what the server
        # ACTUALLY calls its tools, rather than what the registry guessed.
        names = mcp_names(opts, cfg)
        if not names:
            print("--mcp-list needs --mcp NAME. Known: "
                  + ", ".join(n for n, _ in mcp_config.available()))
            return
        for s in start_mcp(names, mcp_mode(opts, cfg), cfg, confirm):
            print(f"\n{s['id']}  ({s['mode']})")
            for t in s["tools"]:
                print(f"    {'write' if t in s['writes'] else ' read'}  {t}")
        return

    if not task:
        task = input("Task for the agent: ").strip()
    if not task:
        print("No task given.")
        return

    if root:
        root = fs_tools.enable(root,
                               allow_shell=opts["shell"] or bool(cfg.get("allow_shell")),
                               confirm=None if opts["yolo"] else confirm)
        if not opts["office"]:
            fs_tools.restrict_to_files()  # a real-folder agent shouldn't fiddle with a fake inbox
        if isinstance(root, list):
            where = ("these working folders. Address a file by the folder name "
                     "it is in, like \"work/report.xlsx\":\n"
                     + "\n".join(f"  {label}/  = {path}"
                                for label, path in fs_tools._LABELS.items()))
        else:
            where = f"the working root\n{root}. Paths are relative to that root."
        agent_mod.EXTRA_RULES = REAL_RULES.format(root=where)
        agent_mod.EXTRA_WRITE_TOOLS = fs_tools.WRITE_TOOLS
        # a real-file agent should reason about the real date, not the fixed
        # benchmark clock
        today = datetime.date.today()
        agent_mod.SIM_TODAY = today
        agent_mod.SIM_TODAY_HUMAN = today.strftime("%A, %B %d, %Y")

    names, mcp_summary = mcp_names(opts, cfg), None
    mcp_run_mode = mcp_mode(opts, cfg)   # not `mode`: the banner below reuses that
    if names:
        mcp_summary = start_mcp(names, mcp_run_mode, cfg,
                                None if opts["yolo"] else confirm, root=root)
        # Append, don't assign: --root may already have installed REAL_RULES, and
        # union the write sets for the same reason.
        agent_mod.EXTRA_RULES += mcp_bridge.mail_rules(mcp_run_mode)
        agent_mod.EXTRA_WRITE_TOOLS = (set(agent_mod.EXTRA_WRITE_TOOLS)
                                       | mcp_bridge.WRITE_TOOLS)
        # Real mail has real dates; the fixed benchmark clock would make the
        # model search the wrong week.
        today = datetime.date.today()
        agent_mod.SIM_TODAY = today
        agent_mod.SIM_TODAY_HUMAN = today.strftime("%A, %B %d, %Y")
    # Two calendars connected at once get one tool that asks both, so "what do
    # I have on Thursday" cannot be answered from half the week. No-ops unless
    # there really is more than one calendar registered.
    merged_calendars = calendars.enable()
    # Sending is the end of it: no waiting for replies, and no fresh look at
    # the inbox bought by having sent something.
    agent_mod.WAIT_GUARD = True
    agent_mod.EXTRA_RULES += agent_mod.WAIT_RULES
    # The sent folder and a drafts folder. Enabled after --root and --mcp have
    # had their say, so the union below keeps whatever they added.
    mailbox.enable()
    agent_mod.EXTRA_WRITE_TOOLS = (set(agent_mod.EXTRA_WRITE_TOOLS)
                                   | mailbox.WRITE_TOOLS)
    if calendars.MERGED in tools_mod.TOOLS:
        print(f"  calendars: {len(merged_calendars)} connected, "
              f"{calendars.MERGED} asks all of them")

    agent_mod.ROUTE_TOOLS = opts["route_tools"] or bool(cfg.get("route_tools"))
    agent_mod.MAX_CALLS = (opts["max_calls"] or cfg.get("max_calls")
                           or (40 if (root or names) else profile.max_calls))

    world = World(os.path.join(HERE, "workspace"), persistent=True)
    mem = MemoryStore(os.path.join(HERE, "memory", "memory.jsonl"))
    llm, router = build_llm(cfg, opts)

    print(f"[{cfg['name']}] fully on-device via {OLLAMA_URL}")
    steps = f"{profile.plan_max_steps}-step plan" if profile.plan else "no planning"
    verify = f"{profile.verify_rounds} verify round(s)" if profile.verify_rounds else "no verifier"
    print(f"  harness profile: {profile.label} — {steps}, {verify}, "
          f"loop-break {'on' if profile.loop_break else 'off'}, "
          f"out<={profile.num_predict} tok, ctx {cfg['num_ctx']}")
    print(f"    why: {profile.rationale}")
    if router:
        print(f"  model tiers: " + ", ".join(f"{r}={s['model']}" for r, s in router.roles.items()))
        print(f"  resident at once: {', '.join(router.resident_models())}  (others load on demand, evict after)")
        print(f"  {adapters_note()}")
    else:
        print(f"  model: {cfg['model']}")
    if root:
        mode = "read/write" + (" + shell" if opts["shell"] or cfg.get("allow_shell") else "")
        toolset = "files + office world" if opts["office"] else "files only (office tools dropped)"
        shown = ", ".join(root) if isinstance(root, list) else root
        print(f"  real files: {mode} inside {shown}"
              + ("   [--yolo: confirmations off]" if opts["yolo"] else ""))
        print(f"  toolset: {toolset}")
    if mcp_summary:
        print_mcp_banner(mcp_summary, mcp_run_mode)
    print(f"  budget: {agent_mod.MAX_CALLS} LLM calls")
    ep = run_harness(llm, world, mem, task)

    print("\n--- run finished ---")
    print(f"finished cleanly: {ep.finished}   llm calls: {llm.calls}   "
          f"tokens out: {llm.output_tokens}   wall: {llm.wall:.0f}s")
    if router:
        for role, u in router.usage_by_role().items():
            print(f"  tier {role:<8} {u['model']:<16} {u['calls']:>2} calls  "
                  f"{u['output_tokens']:>5} out-tok  {u['ms'] / 1000:>5.1f}s")
    if ep.done_summary:
        print(f"agent summary: {ep.done_summary}")
    acts = [a for a in world.actions if a["tool"] != "think"]
    if acts:
        print("actions taken:")
        for a in acts:
            print(f"  - {a['tool']}({json.dumps(a['args'], ensure_ascii=False, default=str)[:120]})"
                  f" -> {'ok' if a['ok'] else 'ERROR'}")
    print(f"files: {world.files_dir}")
    log_dir = os.path.join(HERE, "logs")
    os.makedirs(log_dir, exist_ok=True)
    n = len(os.listdir(log_dir)) + 1
    log_path = os.path.join(log_dir, f"run_{n:03d}.json")
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump({"task": task, "root": root, "model": cfg["model"], "via": "cli",
                   "transcript": ep.transcript,
                   "finished": ep.finished, "summary": ep.done_summary,
                   "mcp": ({"servers": names, "mode": mcp_run_mode}
                           if names else None),
                   # Same shape the web UI writes, so harness/tuner.py reads one
                   # log format however the run was started.
                   "profile": dict(profile.to_dict(), max_calls=agent_mod.MAX_CALLS),
                   "metrics": run_metrics(ep, llm, agent_mod.MAX_CALLS)}, f,
                  indent=1, ensure_ascii=False)
    print(f"transcript: {log_path}")


if __name__ == "__main__":
    main()
