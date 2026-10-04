import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LO = [sys.executable, str(Path(__file__).with_name("lo.py"))]


def sh(cwd, *cmd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def plan(tasks, prose="Approach."):
    return f"# Plan\n\n{prose}\n\n```json lo-tasks\n{json.dumps(tasks, indent=2)}\n```\n"


class LoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        sh(self.root, "git", "init", "-q")
        sh(self.root, "git", "config", "user.email", "t@example.com")
        sh(self.root, "git", "config", "user.name", "t")
        (self.root / "src").mkdir()
        (self.root / "src" / "a.py").write_text("a = 1\n")
        (self.root / "docs.md").write_text("docs\n")
        self.commit("start")
        self.run_dir = self.root / ".lo" / "job"

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, msg):
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-qm", msg)

    def assertRow(self, text, tid, status):
        self.assertRegex(text, rf"(?m)^  {tid} +{re.escape(status)}")

    def lo(self, *args, code=0):
        p = subprocess.run(LO + [str(a) for a in args], capture_output=True, text=True)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        return p.stdout + p.stderr

    def tasks(self, commands=("true",)):
        return [
            {"id": "build", "goal": "Build it", "repos": {".": ["src"]},
             "verify": {"commands": list(commands), "review": "diff review", "live": "n/a: library"}},
            {"id": "accept", "goal": "Whole result", "depends_on": ["build"], "acceptance": True,
             "verify": {"human": "the user approves the result"}},
        ]

    def start(self, tasks=None):
        self.lo("init", self.run_dir)
        (self.run_dir / "plan.md").write_text(plan(tasks or self.tasks()))
        self.lo("approve", self.run_dir, "--quote", "go")

    def evidence(self, tid, name, text="looked at it"):
        folder = self.run_dir / "reviews" / tid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text)

    # ------------------------------------------------------------ planning

    def test_init_writes_plan_and_hides_run_from_git(self):
        out = json.loads(self.lo("init", self.run_dir))
        self.assertTrue((self.run_dir / "plan.md").exists())
        self.assertTrue((self.run_dir / "decisions.md").exists())
        self.assertIn(".lo/", Path(out["git_excluded"]).read_text())
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.root,
                                capture_output=True, text=True).stdout
        self.assertEqual(status, "")

    def test_check_lints_the_plan_before_approval(self):
        self.lo("init", self.run_dir)
        bad = self.tasks()
        del bad[0]["verify"]["review"]
        (self.run_dir / "plan.md").write_text(plan(bad))
        out = self.lo("check", self.run_dir, code=1)
        self.assertIn("verify needs review", out)
        (self.run_dir / "plan.md").write_text(plan(self.tasks()))
        self.assertIn("2 tasks parse and validate", self.lo("check", self.run_dir, code=1))

    def test_invalid_plans_are_refused(self):
        self.lo("init", self.run_dir)
        cases = {
            "without a reason": lambda t: t[0]["verify"].update(live="n/a"),
            "unknown keys": lambda t: t[0].update(checkpoint=True),
            "exactly one task": lambda t: t[0].update(acceptance=True),
            "cycle": lambda t: t[0].update(depends_on=["accept"]),
            "unknown dependency": lambda t: t[1].update(depends_on=["nope"]),
        }
        for message, mutate in cases.items():
            tasks = self.tasks()
            mutate(tasks)
            (self.run_dir / "plan.md").write_text(plan(tasks))
            self.assertIn(message, self.lo("approve", self.run_dir, "--quote", "go", code=2))

    def test_approve_needs_the_users_words(self):
        self.lo("init", self.run_dir)
        (self.run_dir / "plan.md").write_text(plan(self.tasks()))
        self.lo("approve", self.run_dir, "--quote", " ", code=2)
        self.lo("approve", self.run_dir, "--quote", "yes, build it")
        self.assertIn('"yes, build it"', self.lo("check", self.run_dir, code=1))
        self.assertIn("unchanged", self.lo("approve", self.run_dir, "--quote", "again", code=2))

    # ------------------------------------------------------------ verdicts

    def test_pass_needs_evidence_for_each_planned_kind(self):
        self.start()
        self.assertIn("no evidence for 'review'", self.lo("verdict", self.run_dir, "build", "pass", code=2))
        self.evidence("build", "review.md")
        out = json.loads(self.lo("verdict", self.run_dir, "build", "pass"))
        self.assertEqual(out["commands"], [{"run": "true", "exit": 0}])
        self.assertRow(self.lo("check", self.run_dir, code=1), "build", "pass @")

    def test_empty_evidence_does_not_count(self):
        self.start()
        self.evidence("build", "review.md", "")
        self.lo("verdict", self.run_dir, "build", "pass", code=2)

    def test_lo_runs_the_commands_and_refuses_on_failure(self):
        self.start(self.tasks(commands=["echo ok", "exit 3"]))
        self.evidence("build", "review.md")
        out = self.lo("verdict", self.run_dir, "build", "pass", code=2)
        self.assertIn("command failed (3): exit 3", out)
        log = (self.run_dir / "reviews" / "build" / "commands.log").read_text()
        self.assertIn("ok", log)
        self.assertIn("[exit 3]", log)
        self.assertRow(self.lo("check", self.run_dir, code=1), "build", "no verdict")

    def test_commands_run_in_the_task_repo(self):
        self.start(self.tasks(commands=["test -f src/a.py"]))
        self.evidence("build", "review.md")
        self.lo("verdict", self.run_dir, "build", "pass")

    def test_uncommitted_task_files_refuse_a_pass(self):
        self.start()
        self.evidence("build", "review.md")
        (self.root / "src" / "a.py").write_text("a = 2\n")
        self.assertIn("uncommitted", self.lo("verdict", self.run_dir, "build", "pass", code=2))

    def test_a_human_check_needs_the_users_words(self):
        self.start()
        self.evidence("build", "review.md")
        self.lo("verdict", self.run_dir, "build", "pass")
        self.assertIn("--quote", self.lo("verdict", self.run_dir, "accept", "pass", code=2))
        self.lo("verdict", self.run_dir, "accept", "pass", "--quote", "looks right")
        self.assertIn("Result: DONE", self.lo("check", self.run_dir))

    def test_acceptance_waits_for_every_other_task(self):
        self.start()
        out = self.lo("verdict", self.run_dir, "accept", "pass", "--quote", "ok", code=2)
        self.assertIn("open: build", out)

    def test_fail_needs_a_finding(self):
        self.start()
        self.lo("verdict", self.run_dir, "build", "fail", code=2)
        out = json.loads(self.lo("verdict", self.run_dir, "build", "fail", "--finding", "total", "off by one"))
        self.assertEqual(out["status"], "fail")

    def test_the_same_finding_twice_goes_to_the_user(self):
        self.start()
        self.lo("verdict", self.run_dir, "build", "fail", "--finding", "total", "off by one")
        out = json.loads(self.lo("verdict", self.run_dir, "build", "fail", "--finding", "total", "still off"))
        self.assertEqual(out["status"], "needs-you")
        self.assertIn("same finding twice: total", out["reason"])

    def test_three_failed_verdicts_go_to_the_user(self):
        self.start()
        for key in ("a", "b"):
            self.lo("verdict", self.run_dir, "build", "fail", "--finding", key, "x")
        out = json.loads(self.lo("verdict", self.run_dir, "build", "fail", "--finding", "c", "x"))
        self.assertEqual(out["status"], "needs-you")

    # ------------------------------------------------------------ staleness

    def test_a_change_to_task_files_makes_the_pass_stale(self):
        self.start()
        self.evidence("build", "review.md")
        self.lo("verdict", self.run_dir, "build", "pass")
        (self.root / "docs.md").write_text("other\n")
        self.commit("outside the task's paths")
        self.assertRow(self.lo("check", self.run_dir, code=1), "build", "pass")
        (self.root / "src" / "a.py").write_text("a = 2\n")
        self.assertIn("uncommitted: src/a.py", self.lo("check", self.run_dir, code=1))
        self.commit("inside")
        out = self.lo("check", self.run_dir, code=1)
        self.assertRow(out, "build", "stale")
        self.assertIn("src/a.py changed since", out)

    def test_a_stale_acceptance_means_not_done(self):
        self.start()
        self.evidence("build", "review.md")
        self.lo("verdict", self.run_dir, "build", "pass")
        self.lo("verdict", self.run_dir, "accept", "pass", "--quote", "ok")
        self.lo("check", self.run_dir)
        (self.root / "src" / "a.py").write_text("a = 3\n")
        self.commit("late change")
        self.assertIn("Result: NOT DONE", self.lo("check", self.run_dir, code=1))

    # ------------------------------------------------------------ amendments

    def test_an_unapproved_plan_edit_blocks_verdicts(self):
        self.start()
        self.evidence("build", "review.md")
        text = (self.run_dir / "plan.md").read_text()
        (self.run_dir / "plan.md").write_text(text.replace("Approach.", "Approach, edited."))
        out = self.lo("check", self.run_dir, code=1)
        self.assertIn("CHANGED since approval", out)
        self.assertIn("none (prose only)", out)
        self.assertIn("+Approach, edited.", self.lo("check", self.run_dir, "--diff", code=1))
        self.assertIn("changed since approval", self.lo("verdict", self.run_dir, "build", "pass", code=2))

    def test_reapproval_clears_only_changed_tasks_and_acceptance(self):
        tasks = self.tasks() + [{"id": "docs", "goal": "Docs", "verify": {"read": "docs say X"}}]
        tasks[1]["depends_on"] = ["build", "docs"]
        self.start(tasks)
        self.evidence("build", "review.md")
        self.evidence("docs", "read.md")
        self.lo("verdict", self.run_dir, "build", "pass")
        self.lo("verdict", self.run_dir, "docs", "pass")
        self.lo("verdict", self.run_dir, "accept", "pass", "--quote", "ok")
        tasks[2]["goal"] = "Docs, longer"
        (self.run_dir / "plan.md").write_text(plan(tasks))
        self.assertIn("tasks changed: docs", self.lo("check", self.run_dir, code=1))
        out = json.loads(self.lo("approve", self.run_dir, "--quote", "ok, longer docs"))
        self.assertEqual(out["reverify"], ["docs"])
        check = self.lo("check", self.run_dir, code=1)
        self.assertRow(check, "build", "pass")
        self.assertRow(check, "docs", "no verdict")
        self.assertRow(check, "accept", "no verdict")

    def test_restoring_an_old_task_definition_does_not_revive_its_verdict(self):
        self.start()
        self.evidence("build", "review.md")
        self.lo("verdict", self.run_dir, "build", "pass")
        original = (self.run_dir / "plan.md").read_text()
        (self.run_dir / "plan.md").write_text(original.replace("Build it", "Build it twice"))
        self.lo("approve", self.run_dir, "--quote", "b")
        (self.run_dir / "plan.md").write_text(original)
        self.lo("approve", self.run_dir, "--quote", "back to a")
        self.assertRow(self.lo("check", self.run_dir, code=1), "build", "no verdict")

    def test_diff_keeps_the_check_exit_status(self):
        self.start()
        text = (self.run_dir / "plan.md").read_text()
        (self.run_dir / "plan.md").write_text(text + "\nMore.\n")
        out = self.lo("check", self.run_dir, "--diff", code=1)
        self.assertIn("+More.", out)
        self.assertIn("Result: NOT DONE", out)

    def test_new_files_count_even_when_git_hides_untracked_files(self):
        sh(self.root, "git", "config", "status.showUntrackedFiles", "no")
        self.start()
        self.evidence("build", "review.md")
        (self.root / "src" / "new.py").write_text("b = 1\n")
        self.assertIn("uncommitted", self.lo("verdict", self.run_dir, "build", "pass", code=2))

    def test_a_blank_quote_is_not_the_users_verdict(self):
        self.start()
        self.evidence("build", "review.md")
        self.lo("verdict", self.run_dir, "build", "pass")
        self.lo("verdict", self.run_dir, "accept", "pass", "--quote", " ", code=2)

    def test_a_verdict_recorded_during_the_commands_wins(self):
        marker = self.root / "go"
        self.start(self.tasks(commands=[f"while [ ! -f {marker} ]; do sleep 0.1; done"]))
        self.evidence("build", "review.md")
        slow = subprocess.Popen(LO + ["verdict", str(self.run_dir), "build", "pass"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        while not (self.run_dir / "reviews" / "build" / "commands.log").exists():
            pass
        self.lo("verdict", self.run_dir, "build", "fail", "--finding", "bug", "found by B")
        marker.write_text("")
        _, err = slow.communicate(timeout=30)
        self.assertEqual(slow.returncode, 2)
        self.assertIn("another verdict", err)


    def test_a_run_from_lo_1_is_refused_plainly(self):
        old = self.root / "old"
        old.mkdir()
        (old / "ledger.jsonl").write_text(json.dumps({"ts": 1, "type": "init", "title": "old"}) + "\n")
        (old / "plan.md").write_text("# old\n")
        self.assertIn("created by lo 1", self.lo("check", old, code=2))

if __name__ == "__main__":
    unittest.main()
