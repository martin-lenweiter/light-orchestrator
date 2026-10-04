import json
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import lo_v1 as lo

LO = [sys.executable, str(Path(__file__).with_name("lo_v1.py"))]


def run_lo(*args, check=True):
    p = subprocess.run(LO + [str(a) for a in args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AssertionError(p.stderr)
    try:
        return p.returncode, json.loads(p.stdout or p.stderr)
    except json.JSONDecodeError:  # argparse usage errors are plain text
        return p.returncode, {"error": p.stderr}


def state_of(run_dir):
    return lo.Run(run_dir).load()


class LoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / "run"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        path = Path(self.tmp.name) / name
        path.write_text(json.dumps(data))
        return path

    def to_executing(self, n=2, **init):
        flags = []
        for k, v in init.items():
            flags += [f"--{k.replace('_', '-')}", v]
        run_lo("init", self.run_dir, *flags)
        run_lo("phase", self.run_dir, "planning")
        tasks = [{"id": f"t{i}", "goal": f"goal {i}", "done_when": "x", "checkpoint": True} for i in range(1, n + 1)]
        tasks[-1]["acceptance"] = True
        tasks[-1]["depends_on"] = [x["id"] for x in tasks[:-1]]
        run_lo("tasks", "set", self.run_dir, self.write("tasks.json", tasks))
        run_lo("phase", self.run_dir, "awaiting-approval")
        run_lo("approve", self.run_dir)

    def test_graph_layers_tasks_and_marks_phase(self):
        self.to_executing(n=3)
        run_lo("claim", self.run_dir, "--owner", "w1", "--id", "t1")
        p = subprocess.run(LO + ["graph", str(self.run_dir)], capture_output=True, text=True, check=True)
        lines = p.stdout.splitlines()
        self.assertIn("[executing]", p.stdout)
        self.assertTrue(any(l.startswith("L0  [>] t1") and "w1" in l for l in lines))
        self.assertTrue(any(l.startswith("    [ ] t2") for l in lines))
        self.assertTrue(any(l.startswith("L1  [.] t3") and "<- t1, t2" in l for l in lines))

    def complete(self, tid):
        task = state_of(self.run_dir)["tasks"][tid]
        out = Path("out") / tid / task["token"] / "result.md"
        (self.run_dir / out).parent.mkdir(parents=True, exist_ok=True)
        (self.run_dir / out).write_text("result")
        return run_lo("done", self.run_dir, tid, "--token", task["token"], "--output", str(out))

    def execute_all(self):
        while True:
            _, r = run_lo("claim", self.run_dir, "--owner", "w")
            if not r["claimed"]:
                break
            self.complete(r["claimed"])
        run_lo("phase", self.run_dir, "verifying")

    def findings(self, *keys):
        return self.write("f.json", [{"key": k, "severity": "blocking", "text": k} for k in keys])

    def test_gate1_requires_approval(self):
        run_lo("init", self.run_dir)
        run_lo("phase", self.run_dir, "awaiting-answers")
        run_lo("phase", self.run_dir, "planning")
        code, err = run_lo("phase", self.run_dir, "awaiting-approval", check=False)
        self.assertEqual(code, 2)  # no tasks yet
        run_lo("tasks", "set", self.run_dir, self.write("t.json", [{"id": "a", "goal": "g", "done_when": "d", "acceptance": True}]))
        run_lo("phase", self.run_dir, "awaiting-approval")
        code, _ = run_lo("phase", self.run_dir, "executing", check=False)
        self.assertEqual(code, 2)  # must use approve
        code, _ = run_lo("claim", self.run_dir, "--owner", "w", check=False)
        self.assertEqual(code, 2)

    def test_happy_path(self):
        self.to_executing()
        self.execute_all()
        run_lo("verdict", self.run_dir, "t1", "pass")
        _, r = run_lo("finish", self.run_dir)
        self.assertEqual(r["ready"], ["t2"])  # acceptance task runs last
        self.execute_all()
        run_lo("verdict", self.run_dir, "t2", "pass")
        _, r = run_lo("finish", self.run_dir)
        self.assertEqual(r["phase"], "done")

    def test_plan_requires_one_acceptance_task_covering_all(self):
        run_lo("init", self.run_dir)
        run_lo("phase", self.run_dir, "planning")
        none = [{"id": "a", "goal": "g", "done_when": "d"}]
        code, _ = run_lo("tasks", "set", self.run_dir, self.write("t.json", none), check=False)
        self.assertEqual(code, 2)
        partial = [{"id": "a", "goal": "g", "done_when": "d"}, {"id": "b", "goal": "g", "done_when": "d"},
                   {"id": "z", "goal": "g", "done_when": "d", "acceptance": True, "depends_on": ["a"]}]
        code, err = run_lo("tasks", "set", self.run_dir, self.write("t.json", partial), check=False)
        self.assertEqual(code, 2)
        self.assertIn("b", err["error"])

    def test_failed_acceptance_means_partial(self):
        self.to_executing(n=1, max_repairs=0)
        self.execute_all()
        run_lo("verdict", self.run_dir, "t1", "fail", "--findings", self.findings("a"))
        _, r = run_lo("finish", self.run_dir)
        self.assertEqual(r["phase"], "partial")

    def test_repair_then_pass(self):
        self.to_executing(n=1)
        self.execute_all()
        _, r = run_lo("verdict", self.run_dir, "t1", "fail", "--findings", self.findings("a"))
        self.assertEqual(r["status"], "todo")
        _, r = run_lo("finish", self.run_dir)
        self.assertEqual(r["phase"], "executing")
        _, c = run_lo("claim", self.run_dir, "--owner", "w")
        self.assertEqual(c["last_findings"][0]["key"], "a")
        self.complete("t1")
        run_lo("phase", self.run_dir, "verifying")
        run_lo("verdict", self.run_dir, "t1", "pass")
        _, r = run_lo("finish", self.run_dir)
        self.assertEqual(r["phase"], "done")

    def test_stall_on_repeated_finding(self):
        self.to_executing(n=1)
        self.execute_all()
        run_lo("verdict", self.run_dir, "t1", "fail", "--findings", self.findings("a"))
        run_lo("finish", self.run_dir)
        self.execute_all()
        _, r = run_lo("verdict", self.run_dir, "t1", "fail", "--findings", self.findings("a", "b"))
        self.assertEqual(r["status"], "needs-human")
        self.assertIn("stalled", r["stop_reason"])
        _, r = run_lo("finish", self.run_dir)
        self.assertEqual(r["phase"], "partial")

    def test_repair_cap(self):
        self.to_executing(n=1, max_repairs=1)
        self.execute_all()
        run_lo("verdict", self.run_dir, "t1", "fail", "--findings", self.findings("a"))
        run_lo("finish", self.run_dir)
        self.execute_all()
        _, r = run_lo("verdict", self.run_dir, "t1", "fail", "--findings", self.findings("b"))
        self.assertEqual(r["status"], "needs-human")
        self.assertIn("repair cap", r["stop_reason"])

    def test_verifier_cannot_pass_with_blocking_or_fail_without(self):
        self.to_executing(n=1)
        self.execute_all()
        code, _ = run_lo("verdict", self.run_dir, "t1", "pass", "--findings", self.findings("a"), check=False)
        self.assertEqual(code, 2)
        notes = self.write("n.json", [{"key": "n", "severity": "note", "text": "minor"}])
        code, _ = run_lo("verdict", self.run_dir, "t1", "fail", "--findings", notes, check=False)
        self.assertEqual(code, 2)

    def test_expired_worker_requires_recovery_without_quality_attempt(self):
        self.to_executing(n=2)
        run_lo("claim", self.run_dir, "--owner", "w1", "--lease", "1")
        time.sleep(1.2)
        _, r = run_lo("resume", self.run_dir)
        self.assertEqual(r["released"], ["t1"])
        _, s = run_lo("status", self.run_dir)
        self.assertEqual(s["tasks"][0]["status"], "needs-human")
        self.assertEqual(s["tasks"][0]["attempts"], 0)

    def test_snapshot_loss_and_torn_ledger_line(self):
        self.to_executing(n=2)
        run_lo("claim", self.run_dir, "--owner", "w")
        (self.run_dir / "state.json").unlink()
        with open(self.run_dir / "ledger.jsonl", "a") as fh:
            fh.write('{"type": "done", "id"')  # crash mid-append
        code, s = run_lo("status", self.run_dir, check=False)
        self.assertEqual(code, 2)
        self.assertIn("ledger", s["error"].lower())

    def test_parallel_claims_never_duplicate(self):
        self.to_executing(n=9)  # t9 is the acceptance task and waits
        with ThreadPoolExecutor(8) as ex:
            results = list(ex.map(lambda i: run_lo("claim", self.run_dir, "--owner", f"w{i}")[1]["claimed"], range(8)))
        self.assertEqual(sorted(results), sorted(f"t{i}" for i in range(1, 9)))

    def test_plan_frozen_after_approval(self):
        self.to_executing(n=1)
        code, _ = run_lo("tasks", "set", self.run_dir, self.write("t.json", [{"id": "z", "goal": "g", "done_when": "d"}]), check=False)
        self.assertEqual(code, 2)

    def finish_all(self):
        while True:
            self.execute_all()
            r = self.pass_all_done()
            if r["phase"] != "executing":
                return r

    def test_amend_reopens_finished_run_and_keeps_unaffected_verdicts(self):
        self.to_executing(n=3)
        self.assertEqual(self.finish_all()["phase"], "done")
        change = [{"id": "t4", "goal": "soft cut", "done_when": "y"},
                  {"id": "t3", "depends_on": ["t1", "t2", "t4"], "done_when": "includes soft cut"}]
        code, _ = run_lo("amend", self.run_dir, self.write("a.json", change[:1]), "--note", "n", check=False)
        self.assertEqual(code, 2)  # acceptance must cover the new task
        _, r = run_lo("amend", self.run_dir, self.write("a.json", change), "--note", "Martin: add soft cut")
        self.assertEqual((r["phase"], r["added"], r["reopened"]), ("executing", ["t4"], ["t3", "t4"]))
        tasks = state_of(self.run_dir)["tasks"]
        self.assertEqual([tasks[t]["status"] for t in ("t1", "t2", "t3", "t4")],
                         ["verified", "verified", "todo", "todo"])
        self.assertEqual((tasks["t3"]["goal"], tasks["t3"]["done_when"]), ("goal 3", "includes soft cut"))
        self.assertEqual(self.finish_all()["phase"], "done")
        _, s = run_lo("status", self.run_dir)
        self.assertEqual(s["amendments"][0]["note"], "Martin: add soft cut")
        self.assertEqual(s["counts"]["verified"], 4)

    def test_amend_revision_restarts_task_and_dependents(self):
        self.staged()
        self.execute_all()
        run_lo("verdict", self.run_dir, "r1", "fail", "--findings", self.findings("a"))
        run_lo("verdict", self.run_dir, "r2", "pass")
        run_lo("finish", self.run_dir)
        self.execute_all()
        self.pass_all_done()
        self.execute_all()
        self.pass_all_done()
        _, r = run_lo("amend", self.run_dir, self.write("a.json", [{"id": "r1", "goal": "new scope"}]), "--note", "n")
        self.assertEqual(r["reopened"], ["r1", "p1", "c1"])
        tasks = state_of(self.run_dir)["tasks"]
        self.assertEqual((tasks["r1"]["attempts"], len(tasks["r1"]["history"])), (0, 1))
        self.assertEqual(tasks["r2"]["status"], "verified")
        self.assertEqual(self.finish_all()["phase"], "done")

    def test_amend_refuses_to_reset_running_work(self):
        self.to_executing(n=3)
        run_lo("claim", self.run_dir, "--owner", "w", "--id", "t1")
        code, err = run_lo("amend", self.run_dir, self.write("a.json", [{"id": "t1", "goal": "g"}]), "--note", "n", check=False)
        self.assertIn("t1", err["error"])
        run_lo("amend", self.run_dir, self.write("a.json", [{"id": "t2", "goal": "g"}]), "--note", "n")
        self.complete("t1")  # unaffected worker keeps its claim
        dup = [{"id": "t2", "goal": "g"}, {"id": "t2", "goal": "h"}]
        code, _ = run_lo("amend", self.run_dir, self.write("a.json", dup), "--note", "n", check=False)
        self.assertEqual(code, 2)

    def test_amend_drops_task_and_requires_dependents_to_be_revised(self):
        self.to_executing(n=3)
        self.assertEqual(self.finish_all()["phase"], "done")
        drop = self.write("a.json", [{"id": "t2", "drop": True}])
        code, err = run_lo("amend", self.run_dir, drop, "--note", "n", check=False)
        self.assertIn("t2", err["error"])  # acceptance still depends on t2
        for bad in ([{"id": "t3", "drop": True}], [{"id": "zz", "drop": True}]):
            code, _ = run_lo("amend", self.run_dir, self.write("a.json", bad), "--note", "n", check=False)
            self.assertEqual(code, 2)  # acceptance cannot vanish; unknown ids rejected
        change = [{"id": "t2", "drop": True}, {"id": "t3", "depends_on": ["t1"]}]
        _, r = run_lo("amend", self.run_dir, self.write("a.json", change), "--by", "martin", "--note", "Martin: skip Attio")
        self.assertEqual((r["dropped"], r["reopened"]), (["t2"], ["t3"]))
        self.assertNotIn("t2", state_of(self.run_dir)["tasks"])
        self.assertEqual(self.finish_all()["phase"], "done")
        report = (self.run_dir / "report.md").read_text()
        self.assertIn("Martin: skip Attio", report)
        self.assertIn("Dropped: t2", report)
        self.assertIn("t2", (self.run_dir / "ledger.jsonl").read_text())

    def test_amend_refuses_to_drop_running_task(self):
        self.to_executing(n=3)
        run_lo("claim", self.run_dir, "--owner", "w", "--id", "t2")
        change = [{"id": "t2", "drop": True}, {"id": "t3", "depends_on": ["t1"]}]
        code, err = run_lo("amend", self.run_dir, self.write("a.json", change), "--note", "n", check=False)
        self.assertIn("t2", err["error"])

    def test_amend_requires_approved_plan(self):
        run_lo("init", self.run_dir)
        run_lo("phase", self.run_dir, "planning")
        code, _ = run_lo("amend", self.run_dir, self.write("a.json", [{"id": "a", "goal": "g", "done_when": "d", "acceptance": True}]), "--note", "n", check=False)
        self.assertEqual(code, 2)

    def staged(self, max_repairs=2):
        run_lo("init", self.run_dir, "--max-repairs", max_repairs)
        run_lo("phase", self.run_dir, "planning")
        tasks = [{"id": "r1", "goal": "g", "done_when": "x", "checkpoint": True},
                 {"id": "r2", "goal": "g", "done_when": "x", "checkpoint": True},
                 {"id": "p1", "goal": "g", "done_when": "x", "depends_on": ["r1", "r2"], "checkpoint": True},
                 {"id": "c1", "goal": "g", "done_when": "x", "depends_on": ["p1"], "acceptance": True}]
        run_lo("tasks", "set", self.run_dir, self.write("tasks.json", tasks))
        run_lo("phase", self.run_dir, "awaiting-approval")
        run_lo("approve", self.run_dir)

    def pass_all_done(self):
        _, s = run_lo("status", self.run_dir)
        for t in s["tasks"]:
            if t["status"] == "done":
                run_lo("verdict", self.run_dir, t["id"], "pass")
        return run_lo("finish", self.run_dir)[1]

    def test_dependencies_gate_claims_and_stages_do_not_use_rounds(self):
        self.staged()
        self.execute_all()  # only r1, r2 are claimable
        _, s = run_lo("status", self.run_dir)
        self.assertEqual([t["status"] for t in s["tasks"]], ["done", "done", "todo", "todo"])
        r = self.pass_all_done()
        self.assertEqual(r["ready"], ["p1"])
        self.execute_all()
        r = self.pass_all_done()
        self.assertEqual(r["ready"], ["c1"])
        self.execute_all()
        r = self.pass_all_done()
        self.assertEqual(r["phase"], "done")
        _, s = run_lo("status", self.run_dir)
        self.assertEqual(s["round"], 1)

    def test_dependency_cycle_rejected(self):
        run_lo("init", self.run_dir)
        run_lo("phase", self.run_dir, "planning")
        tasks = [{"id": "a", "goal": "g", "done_when": "x", "depends_on": ["b"]},
                 {"id": "b", "goal": "g", "done_when": "x", "depends_on": ["a"]}]
        code, _ = run_lo("tasks", "set", self.run_dir, self.write("t.json", tasks), check=False)
        self.assertEqual(code, 2)

    def test_resource_capacity_and_parallel_limit(self):
        run_lo("init", self.run_dir, "--resource", "web-search=2", "--resource", "clay=1", "--max-parallel", "3")
        run_lo("phase", self.run_dir, "planning")
        tasks = [{"id": f"r{i}", "goal": "g", "done_when": "x", "uses": ["web-search"], "model": "fast"} for i in range(3)]
        tasks += [{"id": "p1", "goal": "g", "done_when": "x", "uses": ["clay"]},
                  {"id": "p2", "goal": "g", "done_when": "x", "uses": ["clay"]},
                  {"id": "z", "goal": "g", "done_when": "x", "model": "strong", "acceptance": True,
                   "depends_on": ["r0", "r1", "r2", "p1", "p2"]}]
        run_lo("tasks", "set", self.run_dir, self.write("t.json", tasks))
        run_lo("phase", self.run_dir, "awaiting-approval")
        run_lo("approve", self.run_dir)
        claimed = [run_lo("claim", self.run_dir, "--owner", "w")[1] for _ in range(4)]
        self.assertEqual([c["claimed"] for c in claimed], ["r0", "r1", "p1", None])
        self.assertEqual(claimed[0]["model"], "fast")
        self.assertIn("max_parallel", claimed[3]["waiting_on_capacity"]["r2"])
        self.complete("r0")
        _, c = run_lo("claim", self.run_dir, "--owner", "w")
        self.assertEqual(c["claimed"], "r2")  # web-search slot freed; p2 still waits for clay
        self.complete("r1")
        _, c = run_lo("claim", self.run_dir, "--owner", "w")
        self.assertEqual(c["claimed"], None)
        self.assertIn("clay", c["waiting_on_capacity"]["p2"])

    def test_invalid_model_rejected(self):
        run_lo("init", self.run_dir)
        run_lo("phase", self.run_dir, "planning")
        tasks = [{"id": "a", "goal": "g", "done_when": "d", "model": [], "acceptance": True}]
        code, _ = run_lo("tasks", "set", self.run_dir, self.write("t.json", tasks), check=False)
        self.assertEqual(code, 2)

    def test_resolve_needs_human_then_done(self):
        self.to_executing(n=1, max_repairs=0)
        self.execute_all()
        run_lo("verdict", self.run_dir, "t1", "fail", "--findings", self.findings("a"))
        _, r = run_lo("finish", self.run_dir)
        self.assertEqual(r["phase"], "partial")
        code, _ = run_lo("resolve", self.run_dir, "t1", check=False)
        self.assertEqual(code, 2)  # --note required
        _, r = run_lo("resolve", self.run_dir, "t1", "--note", "fixed by hand", "--by", "martin")
        self.assertEqual(r["phase"], "verifying")
        run_lo("verdict", self.run_dir, "t1", "pass")
        _, r = run_lo("finish", self.run_dir)
        self.assertEqual(r["phase"], "done")

    def global_plan(self, checkpoint=False, max_repairs=2):
        run_lo("init", self.run_dir, "--max-repairs", max_repairs)
        run_lo("phase", self.run_dir, "planning")
        tasks = [{"id": "source", "goal": "research", "done_when": "supported", "checkpoint": checkpoint},
                 {"id": "result", "goal": "integrate", "done_when": "goal met", "acceptance": True,
                  "depends_on": ["source"]}]
        run_lo("tasks", "set", self.run_dir, self.write("tasks.json", tasks))
        run_lo("phase", self.run_dir, "awaiting-approval")
        run_lo("approve", self.run_dir)

    def test_global_default_executes_chain_before_verification(self):
        self.global_plan()
        self.execute_all()
        self.assertEqual([t["status"] for t in state_of(self.run_dir)["tasks"].values()], ["done", "done"])
        result = self.pass_all_done()
        self.assertEqual(result["phase"], "done")
        report = self.run_dir / "report.md"
        self.assertIn("source", report.read_text())
        report.unlink()
        run_lo("resume", self.run_dir)
        self.assertTrue(report.exists())

    def test_checkpoint_waits_for_independent_verification(self):
        self.global_plan(checkpoint=True)
        self.execute_all()
        self.assertEqual([t["status"] for t in state_of(self.run_dir)["tasks"].values()], ["done", "todo"])
        run_lo("verdict", self.run_dir, "source", "pass")
        self.assertEqual(run_lo("finish", self.run_dir)[1]["phase"], "executing")
        self.execute_all()
        self.assertEqual(self.pass_all_done()["phase"], "done")

    def test_human_checkpoint_waits_for_user_verdict(self):
        self.global_plan(checkpoint="human")
        self.execute_all()
        self.assertIn("human: review source", run_lo("status", self.run_dir)[1]["next"])
        run_lo("verdict", self.run_dir, "source", "fail", "--note", "make it simpler")
        task = state_of(self.run_dir)["tasks"]["source"]
        self.assertEqual((task["status"], task["last_findings"][0]["text"]), ("todo", "make it simpler"))
        self.assertEqual(run_lo("finish", self.run_dir)[1]["phase"], "executing")
        self.execute_all()
        self.assertEqual(state_of(self.run_dir)["tasks"]["result"]["status"], "todo")
        run_lo("verdict", self.run_dir, "source", "pass", "--note", "looks good")
        self.assertEqual(run_lo("finish", self.run_dir)[1]["phase"], "executing")
        self.execute_all()
        self.assertEqual(self.pass_all_done()["phase"], "done")

    def test_checkpoint_value_is_validated(self):
        run_lo("init", self.run_dir)
        run_lo("phase", self.run_dir, "planning")
        tasks = [{"id": "a", "goal": "g", "done_when": "d", "checkpoint": "maybe", "acceptance": True}]
        code = run_lo("tasks", "set", self.run_dir, self.write("tasks.json", tasks), check=False)[0]
        self.assertNotEqual(code, 0)

    def test_repair_invalidates_already_verified_consumer(self):
        self.global_plan()
        self.execute_all()
        run_lo("verdict", self.run_dir, "result", "pass")
        run_lo("verdict", self.run_dir, "source", "fail", "--findings", self.findings("incorrect"))
        self.assertEqual(state_of(self.run_dir)["tasks"]["result"]["status"], "todo")
        run_lo("finish", self.run_dir)
        self.execute_all()
        self.assertEqual(self.pass_all_done()["phase"], "done")

    def test_blocked_dependency_ends_partial_without_loop(self):
        self.global_plan(checkpoint=True, max_repairs=0)
        self.execute_all()
        run_lo("verdict", self.run_dir, "source", "fail", "--findings", self.findings("missing"))
        self.assertEqual(run_lo("finish", self.run_dir)[1]["phase"], "partial")
        self.assertNotEqual(state_of(self.run_dir)["tasks"]["result"]["status"], "verified")

    def test_stale_claim_cannot_complete_new_attempt(self):
        self.to_executing(n=1)
        first = run_lo("claim", self.run_dir, "--owner", "old")[1]
        run_lo("resume", self.run_dir, "--force")
        second = run_lo("claim", self.run_dir, "--owner", "new")[1]
        self.assertNotEqual(first["token"], second["token"])
        out = self.run_dir / first["output_dir"] / "result.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("old")
        code, _ = run_lo("done", self.run_dir, "t1", "--token", first["token"], "--output", out, check=False)
        self.assertEqual(code, 2)
        code, _ = run_lo("done", self.run_dir, "t1", "--token", second["token"], "--output", out, check=False)
        self.assertEqual(code, 2)
        self.complete("t1")

    def test_valid_final_record_without_newline_remains_appendable(self):
        self.to_executing(n=1)
        ledger = self.run_dir / "ledger.jsonl"
        ledger.write_text(ledger.read_text().rstrip("\n"))
        run_lo("claim", self.run_dir, "--owner", "worker")
        self.assertEqual(run_lo("status", self.run_dir)[1]["counts"]["running"], 1)

    def test_middle_ledger_corruption_preserves_history_and_fails(self):
        self.to_executing(n=1)
        ledger = self.run_dir / "ledger.jsonl"
        lines = ledger.read_text().splitlines(keepends=True)
        lines.insert(1, "broken json\n")
        corrupt = "".join(lines)
        ledger.write_text(corrupt)
        code, _ = run_lo("claim", self.run_dir, "--owner", "worker", check=False)
        self.assertEqual(code, 2)
        self.assertEqual(ledger.read_text(), corrupt)

    def test_block_and_explicit_retry_recover_dependents(self):
        self.global_plan()
        first = run_lo("claim", self.run_dir, "--owner", "worker")[1]
        run_lo("block", self.run_dir, "source", "--token", first["token"], "--reason", "missing local input")
        run_lo("phase", self.run_dir, "verifying")
        self.assertEqual(run_lo("finish", self.run_dir)[1]["phase"], "partial")
        run_lo("resolve", self.run_dir, "source", "--retry", "--note", "input restored, old worker stopped")
        self.execute_all()
        self.assertEqual(self.pass_all_done()["phase"], "done")


if __name__ == "__main__":
    unittest.main()
