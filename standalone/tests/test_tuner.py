"""Tests for the self-tuning loop.

    cd standalone && python3 -m tests.test_tuner

The loop's whole safety argument is the regression gate and the operator
table, so that is what these cover: a change that makes things worse must come
back out, and an operator that has been disproved must not be re-proposed.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import tuner  # noqa: E402


MODEL = "llama3.1:8b"


def write_run(log_dir, n, finished=True, calls=8, budget=20, parse=0, invalid=0,
              verify_rejections=0, observations_after=True, metrics=True):
    transcript = [{"kind": "task", "content": "t"}]
    for _ in range(verify_rejections):
        transcript.append({"kind": "verify", "content": json.dumps(
            {"complete": False, "missing": "the deck"})})
        if observations_after:
            transcript.append({"kind": "observation", "content": "ok"})
    payload = {"task": "t", "root": None, "model": MODEL, "via": "test",
               "transcript": transcript, "finished": finished, "summary": None}
    if metrics:
        payload["metrics"] = {"calls": calls, "budget": budget,
                              "parse_failures": parse, "invalid_calls": invalid,
                              "tool_errors": 0, "unrequested": False,
                              "output_tokens": 100, "wall": 1.0}
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, f"run_{n:03d}.json"), "w") as f:
        json.dump(payload, f)


def make_agent(tmp, n_runs=0, **run_kw):
    with open(os.path.join(tmp, "config.json"), "w") as f:
        json.dump({"name": "t", "model": MODEL}, f)
    for i in range(1, n_runs + 1):
        write_run(os.path.join(tmp, "logs"), i, **run_kw)
    return tmp


# -------------------------------------------------------------------- mine ---

class TestMining(unittest.TestCase):
    def test_runs_without_metrics_are_skipped_not_zeroed(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_run(tmp, 1, metrics=False)
            write_run(tmp, 2)
            runs = tuner.load_runs(tmp)
            self.assertEqual([r["name"] for r in runs], ["run_002.json"])

    def test_a_half_written_metrics_block_is_skipped_not_crashed_on(self):
        """Regression: load_runs only checked for "calls", so a crashed write
        got admitted and signature() then died on the first missing counter."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "run_001.json"), "w") as f:
                json.dump({"task": "t", "model": MODEL, "finished": True,
                           "transcript": [], "metrics": {"calls": 9}}, f)
            write_run(tmp, 2)
            runs = tuner.load_runs(tmp, model=MODEL)
            self.assertEqual([r["name"] for r in runs], ["run_002.json"])
            tuner.signature(runs)     # must not raise

    def test_other_models_are_not_pooled(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_run(tmp, 1)
            path = os.path.join(tmp, "run_001.json")
            data = json.load(open(path))
            data["model"] = "llama3.2:1b"
            json.dump(data, open(path, "w"))
            write_run(tmp, 2)
            self.assertEqual(len(tuner.load_runs(tmp, model=MODEL)), 1)

    def test_wasted_verification_needs_no_observation_after(self):
        productive = [{"kind": "verify", "content": '{"complete": false}'},
                      {"kind": "observation", "content": "ok"}]
        barren = [{"kind": "verify", "content": '{"complete": false}'},
                  {"kind": "done", "content": "ok"}]
        self.assertEqual(tuner._verify_signals(productive), (1, 0))
        self.assertEqual(tuner._verify_signals(barren), (1, 1))

    def test_runs_are_ordered_numerically_not_lexicographically(self):
        """Regression: filename comparison puts run_1000 BEFORE run_999, which
        would freeze "runs since the change" once an agent passed 999 runs."""
        self.assertGreater(tuner._index("run_1000.json"), tuner._index("run_999.json"))
        self.assertEqual(tuner._index(""), 0)
        with tempfile.TemporaryDirectory() as tmp:
            for n in (998, 999, 1000, 1001):
                write_run(tmp, n)
            self.assertEqual([r["name"] for r in tuner.load_runs(tmp)],
                             ["run_998.json", "run_999.json",
                              "run_1000.json", "run_1001.json"])

    def test_score_prefers_completion_then_efficiency(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_run(tmp, 1, finished=True, calls=4)
            cheap = tuner.score(tuner.load_runs(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            write_run(tmp, 1, finished=True, calls=19)
            dear = tuner.score(tuner.load_runs(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            write_run(tmp, 1, finished=False, calls=4)
            failed = tuner.score(tuner.load_runs(tmp))
        self.assertGreater(cheap, dear)
        self.assertGreater(dear, failed)


# ----------------------------------------------------------------- propose ---

class TestProposals(unittest.TestCase):
    knobs = {"max_calls": 20, "num_predict": 700, "verify_rounds": 2,
             "plan_max_steps": 5, "repeat_limit": 3, "memory_k": 3, "plan": True}

    def _sig(self, **over):
        sig = {"runs": 10, "completion": 1.0, "exhaustion": 0.0,
               "parse_per_call": 0.0, "invalid_per_call": 0.0,
               "wasted_verify": 0.0, "budget_used": 0.4}
        sig.update(over)
        return sig

    def test_healthy_window_proposes_nothing(self):
        self.assertIsNone(tuner.propose(self._sig(), self.knobs))

    def test_too_few_runs_proposes_nothing(self):
        self.assertIsNone(tuner.propose(self._sig(runs=2, exhaustion=1.0), self.knobs))

    def test_exhaustion_raises_the_budget(self):
        p = tuner.propose(self._sig(exhaustion=0.5), self.knobs)
        self.assertEqual((p.knob, p.old, p.new), ("max_calls", 20, 26))

    def test_parse_failures_shorten_replies(self):
        p = tuner.propose(self._sig(parse_per_call=0.3), self.knobs)
        self.assertEqual((p.knob, p.new), ("num_predict", 600))

    def test_wasted_verification_drops_a_round(self):
        p = tuner.propose(self._sig(wasted_verify=0.5), self.knobs)
        self.assertEqual((p.knob, p.new), ("verify_rounds", 1))

    def test_one_proposal_at_a_time_highest_priority_first(self):
        p = tuner.propose(self._sig(exhaustion=0.5, parse_per_call=0.3), self.knobs)
        self.assertEqual(p.knob, "max_calls")

    def test_a_knob_at_its_bound_does_not_step(self):
        knobs = dict(self.knobs, max_calls=tuner.BOUNDS["max_calls"][1])
        self.assertIsNone(tuner.propose(self._sig(exhaustion=1.0), knobs))

    def test_a_step_down_to_zero_is_still_a_proposal(self):
        """Regression: the operators returned `new and Proposal(...)`, so a step
        to 0 - dropping the verifier, which is what the 1B profile does on
        purpose - evaluated falsy and vanished silently."""
        knobs = dict(self.knobs, verify_rounds=1)
        p = tuner.propose(self._sig(wasted_verify=0.9), knobs)
        self.assertIsNotNone(p)
        self.assertEqual((p.knob, p.old, p.new), ("verify_rounds", 1, 0))

    def test_a_retired_operator_is_not_reproposed(self):
        sig = self._sig(exhaustion=0.5)
        self.assertIsNone(tuner.propose(sig, self.knobs, retired={"budget_exhaustion"}))


# ------------------------------------------------------------------- write ---

class TestWrites(unittest.TestCase):
    def test_knob_lands_in_the_harness_block_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(make_agent(tmp), "config.json")
            cfg = tuner.write_knob(path, "max_calls", 26)
            self.assertEqual(cfg["harness"], {"max_calls": 26})
            self.assertEqual(cfg["model"], MODEL)
            self.assertEqual(json.load(open(path))["harness"]["max_calls"], 26)

    def test_untunable_knob_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(make_agent(tmp), "config.json")
            with self.assertRaises(ValueError):
                tuner.write_knob(path, "num_ctx", 99999)
            with self.assertRaises(ValueError):
                tuner.write_knob(path, "loop_break", False)

    def test_effective_knobs_apply_the_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(make_agent(tmp), "config.json")
            tuner.write_knob(path, "max_calls", 33)
            model, knobs = tuner.effective_knobs(path)
            self.assertEqual(model, MODEL)
            self.assertEqual(knobs["max_calls"], 33)


# -------------------------------------------------------------------- loop ---

class TestLoop(unittest.TestCase):
    def test_apply_writes_the_knob_and_records_a_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_agent(tmp, n_runs=8, finished=False, calls=20, budget=20)
            # The step is relative to the model's REAL profile, not the budget
            # the simulated history was recorded under.
            base = tuner.effective_knobs(os.path.join(tmp, "config.json"))[1]["max_calls"]
            report = tuner.tune(tmp, apply=True)
            self.assertTrue(report["applied"])
            self.assertEqual(report["proposal"]["knob"], "max_calls")
            self.assertEqual(json.load(open(os.path.join(tmp, "config.json")))
                             ["harness"]["max_calls"], base + 6)
            ledger = tuner.Ledger(os.path.join(tmp, "tuning.jsonl"))
            self.assertEqual(ledger.entries[0]["status"], "applied")

    def test_judgement_waits_for_enough_new_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_agent(tmp, n_runs=8, finished=False, calls=20, budget=20)
            tuner.tune(tmp, apply=True)
            write_run(os.path.join(tmp, "logs"), 9, finished=True, calls=10, budget=26)
            report = tuner.tune(tmp, apply=True)
            self.assertEqual(report["judged"]["verdict"], "pending")
            self.assertIsNone(report["proposal"])   # nothing new while one is in flight

    def test_a_change_that_helps_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_agent(tmp, n_runs=8, finished=False, calls=20, budget=20)
            tuner.tune(tmp, apply=True)
            base = tuner.effective_knobs(os.path.join(tmp, "config.json"))[1]["max_calls"]
            for i in range(9, 15):
                write_run(os.path.join(tmp, "logs"), i, finished=True, calls=12, budget=26)
            report = tuner.tune(tmp, apply=True)
            self.assertEqual(report["judged"]["verdict"], "keep")
            self.assertEqual(json.load(open(os.path.join(tmp, "config.json")))
                             ["harness"]["max_calls"], base)

    def test_evidence_resets_at_a_change_so_a_knob_does_not_run_away(self):
        """The pre-change failures must stop counting once the fix is in.

        Otherwise the operator re-fires on the same stale window every
        invocation and walks the knob to its bound.
        """
        with tempfile.TemporaryDirectory() as tmp:
            make_agent(tmp, n_runs=8, finished=False, calls=20, budget=20)
            tuner.tune(tmp, apply=True)
            after = tuner.effective_knobs(os.path.join(tmp, "config.json"))[1]["max_calls"]
            for i in range(9, 15):   # healthy under the new budget
                write_run(os.path.join(tmp, "logs"), i, finished=True, calls=12, budget=after)
            tuner.tune(tmp, apply=True)
            tuner.tune(tmp, apply=True)
            self.assertEqual(tuner.effective_knobs(os.path.join(tmp, "config.json"))
                             [1]["max_calls"], after)

    def test_a_change_that_hurts_is_reverted_and_retired(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_agent(tmp)
            # Half the baseline window finishes cheaply and half dies at the
            # ceiling: enough exhaustion to trigger the operator, and enough
            # completion that a later window can measurably fall below it.
            for i in range(1, 5):
                write_run(os.path.join(tmp, "logs"), i, finished=True, calls=8, budget=20)
            for i in range(5, 9):
                write_run(os.path.join(tmp, "logs"), i, finished=False, calls=20, budget=20)
            tuner.tune(tmp, apply=True)
            for i in range(9, 15):    # every run worse after the change
                write_run(os.path.join(tmp, "logs"), i, finished=False,
                          calls=26, budget=26)
            report = tuner.tune(tmp, apply=True)
            self.assertEqual(report["judged"]["verdict"], "revert")
            cfg = json.load(open(os.path.join(tmp, "config.json")))
            base = tuner.profiles.for_model(MODEL, None).max_calls
            self.assertEqual(cfg["harness"]["max_calls"], base)   # put back
            ledger = tuner.Ledger(os.path.join(tmp, "tuning.jsonl"))
            self.assertEqual(ledger.retired(MODEL), {"budget_exhaustion"})
            # and it is not proposed again
            self.assertIsNone(tuner.tune(tmp, apply=True)["proposal"])

    def test_a_revert_restarts_the_evidence_window(self):
        """Regression: after a revert the boundary stayed where the change was
        applied, so runs recorded under the knob that had just been taken back
        out still counted as evidence for the restored config."""
        with tempfile.TemporaryDirectory() as tmp:
            make_agent(tmp)
            logs = os.path.join(tmp, "logs")
            for i in range(1, 5):
                write_run(logs, i, finished=True, calls=8, budget=20)
            for i in range(5, 9):
                write_run(logs, i, finished=False, calls=20, budget=20)
            tuner.tune(tmp, apply=True)
            for i in range(9, 15):
                write_run(logs, i, finished=False, calls=26, budget=26)
            self.assertEqual(tuner.tune(tmp, apply=True)["judged"]["verdict"], "revert")
            ledger = tuner.Ledger(os.path.join(tmp, "tuning.jsonl"))
            self.assertEqual(ledger.last_change_run(MODEL), "run_014.json")
            # nothing recorded under the reverted knob is evidence any more
            self.assertEqual(tuner.tune(tmp)["signature"]["runs"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
