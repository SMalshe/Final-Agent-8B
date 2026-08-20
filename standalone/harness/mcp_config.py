"""Resolve named MCP servers from the registry into configs mcp_bridge can launch.

mcp_bridge.py already knows how to speak to an MCP server; it just had no way to
be told *which* servers to start. This module is that missing half:

    names_to_servers(["gmail", "ms365"])  ->  [{...}, {...}]  for mcp_bridge.enable()

The registry itself is data, not code — mcp/servers.json — so adding a provider
never means touching the harness. An agent folder can override any field of any
server through a "mcp" block in its config.json:

    "mcp": {
      "enable": ["gmail", "ms365"],
      "mode": "draft",
      "servers": {
        "gmail": {"allow": ["search_emails", "read_email", "draft_email"]}
      }
    }

Three things this does that a plain json.load would not:

  - Command resolution. mcp_bridge does subprocess.Popen([command, ...]) with no
    shell. On Windows "npx" is npx.cmd and that Popen raises FileNotFoundError,
    which is exactly the box this repo targets. shutil.which() finds the real
    executable via PATHEXT, so the same registry entry works on both platforms.
  - Expansion. ~ and ${VARS} in env values and cwd, so credential paths in the
    registry are not machine-specific.
  - A tool-count guard. An 8B at num_ctx 8192 has the whole tool list in its
    system prompt; a server that injects 300 tools does not fail loudly, it just
    quietly makes the agent stupid. count_warnings() flags that before the run.
"""
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STANDALONE = os.path.dirname(HERE)
REGISTRY_PATH = os.path.join(STANDALONE, "mcp", "servers.json")

# Substituted into registry paths before ${ENV} expansion, so an entry can point
# at a server that ships in this repo without hard-coding anyone's checkout.
_VARS = {"${STANDALONE}": STANDALONE}

# Above this many injected tools, an 8B's system prompt is mostly tool spec and
# the model starts picking tools at random. Tune with the registry's allow list
# or the server's own --preset flag.
TOOL_BUDGET_WARN = 25

# Keys mcp_bridge.enable() understands. Everything else in a registry entry is
# documentation and is stripped before launch.
_BRIDGE_KEYS = {"id", "command", "args", "env", "cwd", "prefix", "allow", "drop",
                "read_tools", "write_tools", "mode"}


class ConfigError(Exception):
    pass


def load_registry(path=None):
    with open(path or REGISTRY_PATH, encoding="utf-8-sig") as f:
        reg = json.load(f)
    return {k: v for k, v in reg.items() if not k.startswith("_")}


def available(path=None):
    """[(name, summary)] for --mcp-help and the webui's server picker."""
    reg = load_registry(path)
    return [(name, cfg.get("summary", "")) for name, cfg in sorted(reg.items())]


def _expand(value):
    if isinstance(value, str):
        for token, target in _VARS.items():
            value = value.replace(token, target)
        return os.path.expanduser(os.path.expandvars(value))
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _resolve_command(command, server_id):
    """Absolute path to the executable, so Popen works without a shell.

    On Windows this is what turns 'npx' into '...\\npx.cmd'; without it every
    npx-based server dies with FileNotFoundError on the Snapdragon box.

    A Python server always runs under the interpreter the harness is running
    under, never a stray system python — that is what keeps a first-party server
    in mcp/servers/ inside the same virtualenv as everything else."""
    if command in ("python", "python3"):
        return sys.executable
    found = shutil.which(command)
    if found:
        return found
    raise ConfigError(
        f"MCP server {server_id!r} needs {command!r}, which is not on PATH.\n"
        f"    npx-based servers need Node.js installed: https://nodejs.org")


def _merge(base, override):
    out = dict(base)
    out.update(override or {})
    return out


def names_to_servers(names, agent_cfg=None, mode=None, registry_path=None,
                     allow_all=False):
    """Resolve server names into configs ready for mcp_bridge.enable().

    names       list of registry keys, e.g. ["gmail", "ms365"]
    agent_cfg   the agent's config.json "mcp" block, for per-agent overrides
    mode        "draft" | "live" | "read_only", applied to servers that do not
                pin their own mode

    Raises ConfigError on an unknown name or a missing executable — both are
    worth failing loudly at startup rather than half-way through a run.
    """
    reg = load_registry(registry_path)
    overrides = (agent_cfg or {}).get("servers", {})
    out = []
    for name in names:
        if name not in reg:
            raise ConfigError(
                f"unknown MCP server {name!r}. Known: {', '.join(sorted(reg))}")
        cfg = _merge(reg[name], overrides.get(name))
        if allow_all:
            # Register everything the server exposes and let the router decide
            # what the model SEES (harness/toolrouter.py). The allow list was a
            # standing guess at which ten of Outlook's sixty-nine tools matter;
            # per task it can be a better guess, and request_tools covers a miss.
            # `drop` and draft mode stay - those are safety, not curation.
            cfg.pop("allow", None)
        cfg = {k: v for k, v in cfg.items() if k in _BRIDGE_KEYS}
        cfg["id"] = cfg.get("id", name)
        cfg["command"] = _resolve_command(_expand(cfg["command"]), name)
        for key in ("args", "env", "cwd"):
            if key in cfg:
                cfg[key] = _expand(cfg[key])
        if mode and "mode" not in cfg:
            cfg["mode"] = mode
        out.append(cfg)
    return out


def setup_notes(name, registry_path=None):
    """The human setup steps for one server — printed by --mcp-help."""
    reg = load_registry(registry_path)
    if name not in reg:
        raise ConfigError(f"unknown MCP server {name!r}")
    cfg = reg[name]
    lines = [f"{name} — {cfg.get('summary', '')}"]
    for step in cfg.get("setup", []):
        lines.append(f"    {step}")
    for note in cfg.get("notes", []):
        lines.append(f"    ! {note}")
    if cfg.get("docs"):
        lines.append(f"    docs: {cfg['docs']}")
    return "\n".join(lines)


def count_warnings(summary, budget=TOOL_BUDGET_WARN):
    """Warn when a server injected more tools than a small model can hold.

    summary is what mcp_bridge.enable() returns."""
    warnings = []
    total = sum(len(s["tools"]) for s in summary)
    for s in summary:
        if len(s["tools"]) > budget:
            warnings.append(
                f"{s['id']} injected {len(s['tools'])} tools (> {budget}). A small "
                f"model reads all of them in its system prompt — narrow it with the "
                f"server's own preset flag or an 'allow' list in mcp/servers.json.")
    if total > budget and not warnings:
        warnings.append(
            f"{total} MCP tools injected in total (> {budget}); expect the model to "
            f"spend calls choosing between them.")
    return warnings
