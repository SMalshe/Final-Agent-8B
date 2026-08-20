---
tags: [architecture, tuning, plan]
cssclasses: [topic-core]
---

# Self-Tuning

[tuner.py](../standalone/harness/tuner.py) — the harness reading its own run
logs and moving one [[Harness Profiles|profile]] knob at a time.

> The profile table already changed over time; it just changed by hand. The 8B's
> budget went 14 → 20 → 50, each step because a human noticed a pattern and
> wrote down why. This automates that edit and nothing else.

## The loop

| stage | what happens | cost |
|---|---|---|
| mine | aggregate the last 20 runs **of one model** into a failure signature | free |
| propose | map the signature to at most one knob step from a fixed operator table | free |
| apply | write the knob into `config.json`'s `harness` block | free |
| judge | score the runs since the change against the baseline; keep or revert | free |

No model call anywhere in it. The proposer is arithmetic over counters the
[[Agent Loop|loop]] was already keeping.

## What it is allowed to touch

`BOUNDS` is the whole permission list: `max_calls`, `num_predict`,
`verify_rounds`, `plan_max_steps`, `repeat_limit`, `memory_k` — each with a
range it may move inside. Everything else is a forbidden target:

- **`plan` and `loop_break`** are booleans that restructure the loop, not dials.
- **`num_ctx`** is a model-load property.
- **profiles.py itself.** `DEFAULT` is what `bench/` resolves, so moving it
  would silently invalidate every [[Raw vs Harness]] number already on disk.
- **any source file.** [[Real-Computer Mode|fs_tools]] already keeps `harness\`
  unwritable; this module does not become the exception.

The write target is `config.json`, which `profiles.for_model(tag, override)`
already accepts and filters against the dataclass. A tuned agent is still a
config file a human can read, diff and revert.

## The operators

| trigger | step | reasoning |
|---|---|---|
| ≥34% of runs hit the ceiling unfinished | `max_calls` +6 | the budget is cutting off work, not braking a loop |
| ≥0.15 parse failures per call | `num_predict` −100 | long replies are where small-model JSON breaks |
| ≥0.34 wasted verifier rejections per run | `verify_rounds` −1 | rejecting `done` with nothing happening after is the false-negative failure |
| ≥0.2 invalid calls per call | `plan_max_steps` +1 | more of the right tool names in front of the model |

A *wasted* rejection is one with no `observation` after it: the verifier spent a
call sending the model back, nothing further happened, and the run ended anyway.
That is the failure the 3B profile's rationale already describes; the tuner
counts it rather than inferring it.

First trigger wins, one knob per invocation. One change at a time is what makes
the regression gate able to attribute anything.

## The two things that stop it thrashing

**The gate.** A change is scored against the baseline recorded when it was made,
over at least five runs since. Worse by more than 0.02 and the knob goes back
and the operator is **retired for that model** — the ledger is memory, so the
loop cannot spend the next twenty runs re-proposing a change it already
disproved.

**The window reset.** Evidence restarts at every change. Runs recorded under the
old knob stop counting, otherwise an operator keeps firing on failures its own
fix already addressed and walks the knob to its bound one plausible step at a
time. This was a real bug in the first draft, caught by a test.

Score is completion rate with a small efficiency tiebreak: finishing is the
point, and budget burn is worth at most 0.05 — enough to separate two windows
that both complete but one of which flails.

## Prior art

The shape is not invented here. Published self-improving harnesses converge on
the same two constraints, for the same reason:

- **[HarnessFix](https://arxiv.org/html/2606.06324v2)** mines failed
  trajectories, maps flaws to *scoped repair operators* with allowed and
  forbidden targets rather than free-form editing, and gates every patch on
  regression against a held-out set. 6.3–18.4% gains across four benchmarks.
- **[Self-Harness](https://bdtechtalks.substack.com/p/a-primer-on-self-improving-agent)**
  runs weakness-mining → proposal → strict regression testing, and rejects any
  edit that fixes the target case but breaks a passing one.
- **[GEPA](https://arxiv.org/abs/2507.19457)** shows reflective evolution over
  execution traces beating RL with far fewer rollouts — but it optimises prompt
  text with a model in the loop, which is the expensive half we skip.

The deliberate departure: those systems use a frontier model as the meta-agent
that rewrites scaffolding. On a laptop already spending tens of seconds a step,
paying more inference to improve inference is the wrong trade. Counters and a
fixed operator table buy most of it for nothing.

## Status

The loop is built and tested (20 tests in
[tests/test_tuner.py](../standalone/tests/test_tuner.py)); it has **not yet
tuned anything**, because the 13 run logs on disk predate the `metrics` block
and are skipped rather than guessed at. A missing counter is not a zero. It
starts learning from the next run.

The honest limit: this tunes against whatever tasks you happen to run, and
`workspace/state.json` seeds the same fixtures every time, so a long unattended
loop would tune to the fixture rather than to office work. The benchmark's
fresh-synthetic-office-per-attempt design is what would fix that — pointing the
tuner at [[Raw vs Harness|bench]] contexts instead of live runs is the next
step, and the reason `--apply` is opt-in rather than automatic today.

    python -m harness.tuner --agent agents/8b            # report only
    python -m harness.tuner --agent agents/8b --apply    # judge, then step

## Related

- [[Harness Profiles]] — the knobs this moves, and the hand-tuned table it started from
- [[Agent Loop]] — where the counters come from
- [[Raw vs Harness]] · [[Determinism]] · [[Architecture]]
