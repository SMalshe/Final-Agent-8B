"""Self-tuning: run logs in, one bounded profile change out.

The harness already writes down everything that went wrong in a run - broken
JSON, rejected calls, a verifier that sent a finished task back, a budget that
ran out with the task half done. Until now nothing read any of it back, so the
profile table in profiles.py moved only when a human noticed a pattern and
edited it (the 8B's budget went 14 -> 20 -> 50 that way, each step with a
written rationale). This module automates exactly that edit, and nothing more.

Shape, and why each part is shaped that way:

  mine      Aggregate the last N runs of ONE model into a failure signature.
            Runs are only comparable if the model is - a 1B's broken JSON and a
            32B's broken JSON call for opposite fixes.

  propose   Map the signature to at most ONE knob step, drawn from a fixed
            operator table. Not free-form self-editing: the agent may move a
            number inside a bound, it may not rewrite its own loop. The
            published self-improving harnesses land in the same place - scoped
            repair operators with allowed and forbidden targets - because an
            unconstrained proposer regresses in ways nothing catches.

  apply     Write the knob into config.json's "harness" block. That block is
            already a supported override (profiles.for_model filters it against
            the dataclass), so a tuned agent is still a plain config file a
            human can read, diff and revert. profiles.py itself is never
            touched: DEFAULT is what bench/ resolves, and moving it would
            silently invalidate every raw-vs-harness number on disk.

  judge     The next invocation scores the runs that happened AFTER the change
            against the baseline recorded when it was made. Worse beyond a
            tolerance and the knob goes back and the operator is retired for
            this model, so the loop cannot re-propose a change it has already
            disproved. Better or level, and the change is kept.

That last stage is the whole difference between tuning and thrashing. A
proposer with no regression gate will happily walk a profile downhill one
plausible step at a time.

The proposer is deterministic Python over counters - no model call. Self-
improvement built on a meta-model that rewrites scaffolding costs more
inference than the agent it is improving, which is the wrong trade on a laptop
that is already spending tens of seconds a step.

    python -m harness.tuner --agent agents/8b            # report only
    python -m harness.tuner --agent agents/8b --apply    # judge, then step
"""
import argparse
import glob
import json
import os
import time
from collections import namedtuple

from harness import profiles


# Knobs this module may move, and the range each may move inside. A knob absent
# from this table cannot be tuned - that is the "forbidden target" list, and it
# is why plan/loop_break (booleans that restructure the loop) and num_ctx (a
# model-load property, not a tuning dial) are missing.
BOUNDS = {
    "max_calls": (10, 60),
    "num_predict": (300, 1200),
    "verify_rounds": (0, 3),
    "plan_max_steps": (0, 8),
    "repeat_limit": (1, 4),
    "memory_k": (1, 6),
}

MIN_RUNS = 6        # runs needed before any proposal is trusted
MIN_JUDGE = 5       # runs needed AFTER a change before it is judged
WINDOW = 20         # most recent runs per model that count as "now"
TOLERANCE = 0.02    # score drop tolerated before a change is reverted

Proposal = namedtuple("Proposal", "knob old new operator why evidence")


# ------------------------------------------------------------------ record ---

def run_metrics(ep, llm, budget):
    """The per-run numbers a later run can learn from.

    Defined here rather than in either runner because this module is what reads
    them back: the schema belongs next to its consumer, and both the CLI runner
    and the web UI runner then write the identical shape.
    """
    return {"calls": llm.calls, "budget": budget,
            "parse_failures": ep.parse_failures,
            "invalid_calls": ep.invalid_calls,
            "tool_errors": ep.tool_errors,
            "unrequested": bool(ep.unrequested),
            "output_tokens": llm.output_tokens,
            "wall": round(llm.wall, 1)}


# -------------------------------------------------------------------- mine ---

def _verify_signals(transcript):
    """(rejections, wasted) from a transcript.

    A rejection is the verifier answering complete:false. It is *wasted* when no
    observation follows it - the verifier spent a call sending the model back,
    the model did no further real work, and the run ended anyway. That is the
    false-negative pattern the 3B profile already documents; this counts it.
    """
    rejections = wasted = 0
    for i, note in enumerate(transcript):
        if note.get("kind") != "verify":
            continue
        try:
            verdict = json.loads(note.get("content") or "{}")
        except (ValueError, TypeError):
            continue
        if verdict.get("complete", True):
            continue
        rejections += 1
        if not any(n.get("kind") == "observation" for n in transcript[i + 1:]):
            wasted += 1
    return rejections, wasted


def load_runs(log_dir, model=None):
    """Runs carrying a metrics block, oldest first.

    Logs written before this module existed have no metrics and are skipped
    rather than guessed at: a missing counter is not a zero.
    """
    runs = []
    for path in sorted(glob.glob(os.path.join(log_dir, "run_*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError):
            continue
        metrics = data.get("metrics")
        if not isinstance(metrics, dict) or "calls" not in metrics:
            continue
        if model and data.get("model") and data["model"] != model:
            continue
        transcript = data.get("transcript") or []
        rejections, wasted = _verify_signals(transcript)
        runs.append({
            "path": path,
            "name": os.path.basename(path),
            "model": data.get("model"),
            "finished": bool(data.get("finished")),
            "metrics": metrics,
            "profile": data.get("profile") or {},
            "verify_rejections": rejections,
            "wasted_verifications": wasted,
        })
    return runs


def signature(runs):
    """Rates, not raw counts, so a 6-run window and a 20-run window compare."""
    n = len(runs) or 1
    calls = sum(r["metrics"]["calls"] for r in runs) or 1
    exhausted = sum(1 for r in runs
                    if not r["finished"]
                    and r["metrics"]["calls"] >= r["metrics"]["budget"])
    return {
        "runs": len(runs),
        "completion": sum(1 for r in runs if r["finished"]) / n,
        "exhaustion": exhausted / n,
        "parse_per_call": sum(r["metrics"]["parse_failures"] for r in runs) / calls,
        "invalid_per_call": sum(r["metrics"]["invalid_calls"] for r in runs) / calls,
        "wasted_verify": sum(r["wasted_verifications"] for r in runs) / n,
        "budget_used": sum(r["metrics"]["calls"] / (r["metrics"]["budget"] or 1)
                           for r in runs) / n,
    }


def score(runs):
    """Completion, with a small efficiency tiebreak.

    Completion dominates by design - finishing the task is the point. Budget
    burn is worth at most 0.05, enough to separate two windows that both
    complete everything but one of which flails to get there.
    """
    if not runs:
        return None
    sig = signature(runs)
    return round(sig["completion"] - 0.05 * sig["budget_used"], 4)


# ----------------------------------------------------------------- propose ---

def _step(knob, current, delta):
    """A bounded step, or None if the knob is already at the wall."""
    low, high = BOUNDS[knob]
    new = min(high, max(low, current + delta))
    return None if new == current else new


def _op_budget(sig, knobs):
    if sig["exhaustion"] < 0.34:
        return None
    new = _step("max_calls", knobs["max_calls"], +6)
    return new and Proposal(
        "max_calls", knobs["max_calls"], new, "budget_exhaustion",
        "runs are hitting the call ceiling with the task unfinished, so the "
        "budget is cutting off work rather than braking a loop",
        f"{sig['exhaustion']:.0%} of runs exhausted the budget without finishing")


def _op_json(sig, knobs):
    if sig["parse_per_call"] < 0.15:
        return None
    new = _step("num_predict", knobs["num_predict"], -100)
    return new and Proposal(
        "num_predict", knobs["num_predict"], new, "json_fragility",
        "long replies are where small-model JSON breaks; a shorter cap keeps "
        "the object closed",
        f"{sig['parse_per_call']:.2f} parse failures per model call")


def _op_verify(sig, knobs):
    if sig["wasted_verify"] < 0.34:
        return None
    new = _step("verify_rounds", knobs["verify_rounds"], -1)
    return new and Proposal(
        "verify_rounds", knobs["verify_rounds"], new, "wasted_verification",
        "the verifier is rejecting done and nothing happens afterwards, which "
        "is the false-negative failure, not a caught gap",
        f"{sig['wasted_verify']:.2f} wasted verifier rejections per run")


def _op_plan(sig, knobs):
    if sig["invalid_per_call"] < 0.2 or not knobs.get("plan", True):
        return None
    new = _step("plan_max_steps", knobs["plan_max_steps"], +1)
    return new and Proposal(
        "plan_max_steps", knobs["plan_max_steps"], new, "invalid_calls",
        "calls are being rejected before execution; a longer tool-grounded "
        "plan puts more of the right tool names in front of the model",
        f"{sig['invalid_per_call']:.2f} invalid calls per model call")


# Ordered: the first operator whose trigger fires wins, so a run window that
# looks bad in several ways gets the most load-bearing fix first and the rest
# are re-measured against it. One knob per step is what makes the regression
# gate able to attribute a change at all.
OPERATORS = [_op_budget, _op_json, _op_verify, _op_plan]


def propose(sig, knobs, retired=()):
    """At most one proposal, or None when the window looks healthy."""
    if sig["runs"] < MIN_RUNS:
        return None
    for op in OPERATORS:
        prop = op(sig, knobs)
        if prop and prop.operator not in retired:
            return prop
    return None


# ------------------------------------------------------------------ ledger ---

class Ledger:
    """Every change this module ever made, and how it turned out.

    Kept as JSONL next to config.json so it diffs like the rest of the repo.
    Its real job is memory: an operator that has been tried and reverted is
    retired for this model, so the loop cannot spend the next twenty runs
    re-proposing a change it already disproved.
    """

    def __init__(self, path):
        self.path = path
        self.entries = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            self.entries.append(json.loads(line))
                        except ValueError:
                            pass

    def append(self, entry):
        self.entries.append(entry)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _rewrite(self):
        with open(self.path, "w", encoding="utf-8") as f:
            for e in self.entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def last_change_run(self, model):
        """The run this model's most recent change was measured from.

        Evidence resets at every change. Without this the window still holds
        runs recorded under the OLD knob, so an operator keeps re-firing on
        failures its own fix already addressed and walks the knob to its bound
        one plausible step at a time.
        """
        for e in reversed(self.entries):
            if e.get("model") == model:
                return e.get("after_run") or ""
        return ""

    def outstanding(self, model):
        for e in reversed(self.entries):
            if e.get("model") == model and e.get("status") == "applied":
                return e
        return None

    def settle(self, entry, status, after_score):
        entry["status"] = status
        entry["after_score"] = after_score
        entry["settled"] = time.strftime("%Y-%m-%d %H:%M")
        self._rewrite()

    def retired(self, model):
        return {e["operator"] for e in self.entries
                if e.get("model") == model and e.get("status") == "reverted"}


# ------------------------------------------------------------------- apply ---

def write_knob(config_path, knob, value):
    """Set one knob in config.json's "harness" block.

    The only write this module performs. profiles.py is not a target: DEFAULT
    is the benchmark's baseline, and bench/ resolves it directly.
    """
    if knob not in BOUNDS:
        raise ValueError(f"{knob} is not a tunable knob")
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("harness", {})[knob] = value
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return cfg


def effective_knobs(config_path):
    """The profile the next run will actually use: the model's profile with the
    config's harness overrides already applied."""
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    profile = profiles.for_model(cfg["model"], cfg.get("harness"))
    return cfg["model"], profile.to_dict()


# -------------------------------------------------------------------- loop ---

def tune(agent_dir, apply=False):
    """One turn of the loop: judge what is outstanding, then propose one step.

    Safe to call as often as you like - with nothing new to judge and nothing
    worth changing it reports and returns. Calling it after every session is
    what makes this continuous rather than a one-off audit.
    """
    config_path = os.path.join(agent_dir, "config.json")
    model, knobs = effective_knobs(config_path)
    runs = load_runs(os.path.join(agent_dir, "logs"), model=model)
    ledger = Ledger(os.path.join(agent_dir, "tuning.jsonl"))
    report = {"model": model, "runs": len(runs), "judged": None, "proposal": None,
              "applied": False, "signature": signature(runs[-WINDOW:])}

    # 1. Judge the outstanding change before proposing another, so at most one
    #    change is ever in flight and the score difference is attributable.
    entry = ledger.outstanding(model)
    if entry:
        after = [r for r in runs if r["name"] > entry["after_run"]]
        if len(after) < MIN_JUDGE:
            report["judged"] = {"verdict": "pending",
                                "have": len(after), "need": MIN_JUDGE}
            return report
        now = score(after)
        regressed = now is not None and now < entry["baseline"] - TOLERANCE
        if regressed and apply:
            write_knob(config_path, entry["knob"], entry["old"])
            ledger.settle(entry, "reverted", now)
            model, knobs = effective_knobs(config_path)
        elif not regressed and apply:
            ledger.settle(entry, "kept", now)
        report["judged"] = {"verdict": "revert" if regressed else "keep",
                            "knob": entry["knob"], "baseline": entry["baseline"],
                            "after": now, "operator": entry["operator"]}
        if regressed:
            return report   # one move per invocation; re-measure before stepping again

    # 2. Propose from runs recorded since the last change only.
    since = ledger.last_change_run(model)
    window = [r for r in runs if r["name"] > since][-WINDOW:]
    report["signature"] = signature(window)
    prop = propose(signature(window), knobs, retired=ledger.retired(model))
    if not prop:
        return report
    report["proposal"] = prop._asdict()
    if apply:
        write_knob(config_path, prop.knob, prop.new)
        ledger.append({"ts": time.strftime("%Y-%m-%d %H:%M"), "model": model,
                       "knob": prop.knob, "old": prop.old, "new": prop.new,
                       "operator": prop.operator, "why": prop.why,
                       "evidence": prop.evidence, "status": "applied",
                       "baseline": score(window),
                       "after_run": window[-1]["name"]})
        report["applied"] = True
    return report


def format_report(report):
    out = [f"model: {report['model']}   runs with metrics: {report['runs']}"]
    sig = report["signature"]
    if sig["runs"]:
        out.append(f"  completion {sig['completion']:.0%} | budget used "
                   f"{sig['budget_used']:.0%} | exhausted {sig['exhaustion']:.0%} | "
                   f"parse/call {sig['parse_per_call']:.2f} | "
                   f"invalid/call {sig['invalid_per_call']:.2f} | "
                   f"wasted verify/run {sig['wasted_verify']:.2f}")
    judged = report["judged"]
    if judged and judged["verdict"] == "pending":
        out.append(f"  outstanding change: {judged['have']}/{judged['need']} runs "
                   f"before it can be judged")
    elif judged:
        out.append(f"  judged {judged['knob']} ({judged['operator']}): "
                   f"{judged['baseline']} -> {judged['after']}  => {judged['verdict']}")
    prop = report["proposal"]
    if prop:
        out.append(f"  proposal: {prop['knob']} {prop['old']} -> {prop['new']}  "
                   f"[{prop['operator']}]")
        out.append(f"    why: {prop['why']}")
        out.append(f"    evidence: {prop['evidence']}")
        out.append("    applied" if report["applied"] else "    not applied (pass --apply)")
    elif not judged:
        out.append("  nothing to change: no operator triggered on this window")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Tune an agent's profile from its run logs")
    ap.add_argument("--agent", default=".", help="agent folder (holds config.json and logs/)")
    ap.add_argument("--apply", action="store_true", help="write the change, not just report it")
    args = ap.parse_args()
    print(format_report(tune(os.path.abspath(args.agent), apply=args.apply)))


if __name__ == "__main__":
    main()
