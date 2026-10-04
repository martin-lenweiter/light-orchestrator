#!/usr/bin/env python3
"""lo: approval and verification records for a multi-step agent job.

A run is a folder. The orchestrator owns plan.md and decisions.md. lo owns
ledger.jsonl and records only what an agent should not declare about its own
work: the user's approval of the plan, and verdicts with their evidence,
command results, and commits. `lo check` derives everything else.
"""

import argparse
import contextlib
import difflib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

LEDGER = "ledger.jsonl"
LOCK = ".lock"
PLAN = "plan.md"
DECISIONS = "decisions.md"
REVIEWS = "reviews"

MAX_FAILS = 3
TASKS_FENCE = re.compile(r"^```json lo-tasks[ \t]*\n(.*?)^```[ \t]*$", re.S | re.M)
TASK_KEYS = {"id", "goal", "verify", "depends_on", "repos", "acceptance", "model", "effort"}
CODE_KINDS = ("commands", "review", "live")
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
KIND = re.compile(r"^[a-z][a-z0-9-]*$")
NA = re.compile(r"^n/a:\s*\S")

PLAN_TEMPLATE = """# Plan: {name}

## Request

{brief}

## Approach

## Tasks

```json lo-tasks
[]
```
"""


class LoError(Exception):
    pass


# ---------------------------------------------------------------- run folder

class Run:
    def __init__(self, path):
        self.dir = Path(path).expanduser().resolve()
        self.ledger = self.dir / LEDGER
        self.plan = self.dir / PLAN

    @property
    def root(self):
        """Folder that relative repo paths resolve from: the one holding .lo."""
        return self.dir.parent.parent if self.dir.parent.name == ".lo" else self.dir.parent

    @contextlib.contextmanager
    def locked(self):
        if not self.ledger.exists():
            raise LoError(f"no run at {self.dir}; create it with lo init")
        with open(self.dir / LOCK, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def events(self):
        out = []
        for number, line in enumerate(self.ledger.read_text().splitlines(), 1):
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise LoError(f"corrupt ledger at line {number}: {exc.msg}") from exc
        if not out or out[0].get("version") != 2:
            raise LoError(f"{self.dir} was created by lo 1, which this lo cannot read (lo 1 is commit 055cddc)")
        return out

    def append(self, event):
        line = json.dumps({"ts": round(time.time(), 3), **event}, ensure_ascii=False) + "\n"
        with open(self.ledger, "a") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def plan_text(self):
        if not self.plan.exists():
            raise LoError(f"missing {self.plan}")
        return self.plan.read_text()


# ---------------------------------------------------------------- tasks

def parse_tasks(plan_text):
    blocks = TASKS_FENCE.findall(plan_text)
    if len(blocks) != 1:
        raise LoError("plan.md needs exactly one ```json lo-tasks block")
    try:
        tasks = json.loads(blocks[0])
    except json.JSONDecodeError as exc:
        raise LoError(f"lo-tasks block is not valid JSON: {exc}") from exc
    validate(tasks)
    return {t["id"]: t for t in tasks}


def validate(tasks):
    if not isinstance(tasks, list) or not tasks:
        raise LoError("lo-tasks must be a non-empty list of tasks")
    ids = set()
    for t in tasks:
        if not isinstance(t, dict) or not isinstance(t.get("id"), str) or not ID.match(t["id"]):
            raise LoError(f"task needs an id of letters, digits, - or _: {t}")
        tid = t["id"]
        if tid in ids:
            raise LoError(f"duplicate task id: {tid}")
        ids.add(tid)
        if extra := set(t) - TASK_KEYS:
            raise LoError(f"{tid}: unknown keys {sorted(extra)}")
        if not isinstance(t.get("goal"), str) or not t["goal"].strip():
            raise LoError(f"{tid}: goal is required")
        validate_verify(tid, t.get("verify"), "repos" in t)
        if "repos" in t:
            repos = t["repos"]
            if not isinstance(repos, dict) or not repos or not all(
                    isinstance(p, list) and all(isinstance(x, str) and x for x in p) for p in repos.values()):
                raise LoError(f"{tid}: repos maps a repository path to a list of paths ([] for all)")
    for t in tasks:
        for dep in t.get("depends_on", []):
            if dep not in ids:
                raise LoError(f"{t['id']}: unknown dependency {dep}")
    accept = [t["id"] for t in tasks if t.get("acceptance")]
    if len(accept) != 1:
        raise LoError(f"exactly one task needs \"acceptance\": true (found {len(accept)})")
    topo_order({t["id"]: t for t in tasks})


def validate_verify(tid, verify, has_repos):
    if not isinstance(verify, dict) or not verify:
        raise LoError(f"{tid}: verify lists at least one check kind")
    for kind, value in verify.items():
        if not KIND.match(kind):
            raise LoError(f"{tid}: check kind '{kind}' must be lowercase letters, digits or -")
        if isinstance(value, str) and value.startswith("n/a"):
            if not NA.match(value):
                raise LoError(f"{tid}: {kind} says n/a without a reason (write 'n/a: <reason>')")
        elif kind == "commands":
            if not isinstance(value, list) or not value or not all(isinstance(c, str) and c.strip() for c in value):
                raise LoError(f"{tid}: commands is a list of shell commands or 'n/a: <reason>'")
        elif not isinstance(value, str) or not value.strip():
            raise LoError(f"{tid}: {kind} needs a criterion or 'n/a: <reason>'")
    if has_repos:
        missing = [k for k in CODE_KINDS if k not in verify]
        if missing:
            raise LoError(f"{tid} changes code, so verify needs {', '.join(missing)} (or 'n/a: <reason>')")


def topo_order(tasks):
    order, state = [], {}

    def visit(tid):
        if state.get(tid) == 1:
            raise LoError(f"dependency cycle through {tid}")
        if state.get(tid) == 2:
            return
        state[tid] = 1
        for dep in tasks[tid].get("depends_on", []):
            visit(dep)
        state[tid] = 2
        order.append(tid)

    for tid in tasks:
        visit(tid)
    return order


def is_na(value):
    return isinstance(value, str) and value.startswith("n/a")


def task_hash(task):
    return hashlib.sha256(json.dumps(task, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- git

def git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return p.returncode, p.stdout.rstrip(), p.stderr.strip()


def resolve_repos(run, task):
    out = []
    for path, paths in task.get("repos", {}).items():
        repo = Path(path).expanduser()
        repo = (repo if repo.is_absolute() else run.root / repo).resolve()
        code, top, _ = git(repo, "rev-parse", "--show-toplevel")
        if code:
            raise LoError(f"{path} is not a git repository")
        out.append((path, Path(top), paths))
    return out


def dirty(repo, paths):
    code, out, err = git(repo, "status", "--porcelain", "--untracked-files=all", "--", *(paths or ["."]))
    if code:
        raise LoError(f"git status failed in {repo}: {err}")
    return out.splitlines()


def changed_since(repo, sha, paths):
    code, out, err = git(repo, "diff", "--name-only", sha, "HEAD", "--", *(paths or ["."]))
    if code:
        return [f"commit {sha[:7]} not found"]
    return out.splitlines()


# ---------------------------------------------------------------- derived state

def last_approval(events):
    for i in range(len(events) - 1, -1, -1):
        if events[i]["type"] == "approve":
            return i, events[i]
    return None, None


def defined_at(events, tid):
    """Index of the approval that last introduced or changed this task.

    The acceptance task resets at every approval, because any amendment
    changes the whole result it checks."""
    idx, approval = last_approval(events)
    if approval["tasks"][tid].get("acceptance"):
        return idx
    since, previous = idx, None
    for i, e in enumerate(events[:idx + 1]):
        if e["type"] == "approve":
            if e["tasks"].get(tid) != previous:
                since = i
            previous = e["tasks"].get(tid)
    return since


def task_status(run, events, tid):
    """Status of one task under the latest approval: open, pass, stale, fail, needs-you."""
    _, approval = last_approval(events)
    task = approval["tasks"][tid]
    since = defined_at(events, tid)
    mine = [e for e in events[since + 1:] if e["type"] == "verdict" and e["id"] == tid]
    if not mine:
        return {"status": "open"}
    last = mine[-1]
    if last["result"] == "pass":
        stale = []
        for path, sha in last.get("commits", {}).items():
            repo = next((r for p, r, _ in resolve_repos(run, task) if p == path), None)
            paths = task["repos"][path]
            stale += [f"{f} changed since {sha[:7]}" for f in changed_since(repo, sha, paths)]
            stale += [f"uncommitted: {line[3:]}" for line in dirty(repo, paths)]
        if stale:
            return {"status": "stale", "detail": stale}
        return {"status": "pass", "commits": last.get("commits", {})}
    fails = []
    for e in reversed(mine):
        if e["result"] == "pass":
            break
        fails.insert(0, e)
    keys = [{f["key"] for f in e["findings"]} for e in fails]
    repeated = sorted(keys[-1] & keys[-2]) if len(keys) > 1 else []
    status = {"status": "fail", "fails": len(fails), "findings": fails[-1]["findings"]}
    if repeated:
        status.update(status="needs-you", reason=f"same finding twice: {', '.join(repeated)}")
    elif len(fails) >= MAX_FAILS:
        status.update(status="needs-you", reason=f"{len(fails)} failed verdicts")
    return status


# ---------------------------------------------------------------- commands

def cmd_init(args):
    run = Run(args.run)
    if run.ledger.exists():
        raise LoError(f"run already exists: {run.dir}")
    run.dir.mkdir(parents=True, exist_ok=True)
    brief = Path(args.brief).read_text().strip() if args.brief else "<what the user asked for>"
    if not run.plan.exists():
        run.plan.write_text(PLAN_TEMPLATE.format(name=run.dir.name, brief=brief))
    decisions = run.dir / DECISIONS
    if not decisions.exists():
        decisions.write_text("# Decisions\n")
    run.ledger.touch()
    run.append({"type": "init", "version": 2})
    excluded = exclude_from_git(run)
    return {"run": str(run.dir), "plan": str(run.plan), "git_excluded": excluded}


def exclude_from_git(run):
    """Keep .lo/ out of the project's git status. Returns the exclude file used."""
    if run.dir.parent.name != ".lo":
        return None
    code, path, _ = git(run.root, "rev-parse", "--git-path", "info/exclude")
    if code:
        return None
    exclude = Path(path) if Path(path).is_absolute() else run.root / path
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if ".lo/" not in lines:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("\n".join(lines + [".lo/"]) + "\n")
    return str(exclude)


def cmd_approve(args):
    run = Run(args.run)
    if not args.quote.strip():
        raise LoError("--quote needs the user's approving words")
    with run.locked():
        text = run.plan_text()
        tasks = parse_tasks(text)
        events = run.events()
        _, previous = last_approval(events)
        if previous and previous["plan"] == text:
            raise LoError("plan.md is unchanged since the last approval")
        changed = sorted(t for t in tasks if not previous or previous["tasks"].get(t) != tasks[t])
        run.append({"type": "approve", "quote": args.quote, "plan": text, "tasks": tasks})
    return {"approved": True, "tasks": len(tasks), "reverify": changed}


def require_current_approval(run, events):
    idx, approval = last_approval(events)
    if approval is None:
        raise LoError("the plan is not approved; get the user's approval, then run lo approve")
    if run.plan_text() != approval["plan"]:
        raise LoError("plan.md changed since approval; show the user `lo check --diff`, then lo approve")
    return approval


def evidence_for(run, tid, kind):
    folder = run.dir / REVIEWS / tid
    if not folder.is_dir():
        return []
    return sorted(str(p.relative_to(run.dir)) for p in folder.iterdir()
                  if p.is_file() and p.stat().st_size
                  and (p.stem == kind or p.name.startswith(kind + "-") or p.name.startswith(kind + ".")))


def cmd_verdict(args):
    run = Run(args.run)
    findings = [{"key": k, "text": t} for k, t in args.finding or []]
    with run.locked():
        events = run.events()
        approval = require_current_approval(run, events)
        task = approval["tasks"].get(args.id)
        if task is None:
            raise LoError(f"unknown task: {args.id}")
        if args.result == "fail":
            if not findings:
                raise LoError("a fail needs at least one --finding KEY TEXT")
            run.append({"type": "verdict", "id": args.id, "result": "fail",
                        "def": task_hash(task), "findings": findings})
            return {"id": args.id, **task_status(run, run.events(), args.id)}
        approved_at = last_approval(events)[0]
        seen = len(events)
        require_others_pass(run, events, approval, task, args.id)
        repos = resolve_repos(run, task)
        before = check_clean(repos)
        evidence = {}
        for kind, value in task["verify"].items():
            if kind == "commands" or is_na(value):
                continue
            if kind == "human":
                if not (args.quote or "").strip():
                    raise LoError("the human check needs --quote with the user's verdict")
                continue
            files = evidence_for(run, args.id, kind)
            if not files:
                raise LoError(f"no evidence for '{kind}': add a non-empty reviews/{args.id}/{kind}.* file")
            evidence[kind] = files
    # Commands can take minutes; run them without holding the lock.
    results = run_commands(run, args.id, task, repos, args.timeout)
    with run.locked():
        events = run.events()
        require_current_approval(run, events)
        if last_approval(events)[0] != approved_at:
            raise LoError("the plan was approved again while the commands ran; verify again")
        if any(e["type"] == "verdict" and e["id"] == args.id for e in events[seen:]):
            raise LoError(f"another verdict on {args.id} was recorded while the commands ran; review it first")
        require_others_pass(run, events, approval, task, args.id)
        if check_clean(repos) != before:
            raise LoError("the code changed while the commands ran; verify again")
        run.append({"type": "verdict", "id": args.id, "result": "pass", "def": task_hash(task),
                    "commits": before, "commands": results, "evidence": evidence,
                    "findings": findings, "quote": args.quote})
    return {"id": args.id, "status": "pass", "commits": before, "commands": results}


def require_others_pass(run, events, approval, task, tid):
    if not task.get("acceptance"):
        return
    open_ = [t for t in approval["tasks"] if t != tid and task_status(run, events, t)["status"] != "pass"]
    if open_:
        raise LoError(f"acceptance passes only after every other task passes; open: {', '.join(open_)}")


def check_clean(repos):
    commits = {}
    for path, repo, paths in repos:
        if lines := dirty(repo, paths):
            raise LoError(f"{path} has uncommitted changes in the task's paths: {lines[:5]}; commit first")
        commits[path] = git(repo, "rev-parse", "HEAD")[1]
    return commits


def run_commands(run, tid, task, repos, timeout):
    commands = task["verify"].get("commands")
    if not commands or is_na(commands):
        return []
    cwd = repos[0][1] if repos else run.root
    log = run.dir / REVIEWS / tid / "commands.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    results = []
    with open(log, "w") as fh:
        for cmd in commands:
            fh.write(f"$ {cmd}\n")
            fh.flush()
            try:
                p = subprocess.run(cmd, shell=True, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, timeout=timeout)
                code = p.returncode
            except subprocess.TimeoutExpired:
                code = "timeout"
            fh.write(f"[exit {code}]\n")
            results.append({"run": cmd, "exit": code})
            if code != 0:
                raise LoError(f"command failed ({code}): {cmd}; log: {log}")
    return results


def cmd_check(args):
    run = Run(args.run)
    with run.locked():
        events = run.events()
        text = run.plan_text()
        _, approval = last_approval(events)
        lines = [f"Run: {run.dir.name}"]
        if args.diff and approval:
            diff = difflib.unified_diff(approval["plan"].splitlines(), text.splitlines(),
                                        "approved plan.md", "current plan.md", lineterm="")
            lines = ["\n".join(diff) or "plan.md matches the approved copy.", ""] + lines
        if approval is None:
            try:
                tasks = parse_tasks(text)
                lines.append(f"Plan: not approved. {len(tasks)} tasks parse and validate.")
            except LoError as exc:
                lines.append(f"Plan: not approved. Problem: {exc}")
            lines.append("Result: NOT DONE")
            return "\n".join(lines), 1
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(approval["ts"]))
        plan_ok = text == approval["plan"]
        lines.append(f'Plan: approved {when}: "{approval["quote"]}"')
        if not plan_ok:
            lines.append("Plan: CHANGED since approval. Show the user `lo check --diff`, then lo approve.")
            try:
                current = parse_tasks(text)
                changed = sorted(set(current) ^ set(approval["tasks"])
                                 | {t for t in current if approval["tasks"].get(t, current[t]) != current[t]})
                lines.append(f"  tasks changed: {', '.join(changed) or 'none (prose only)'}")
            except LoError as exc:
                lines.append(f"  the edited task block is invalid: {exc}")
        tasks = approval["tasks"]
        width = max(len(t) for t in tasks)
        done = plan_ok
        lines.append("")
        for tid in topo_order(tasks):
            s = task_status(run, events, tid)
            done = done and s["status"] == "pass"
            lines.append(f"  {tid:<{width}}  {describe(s)}{suffix(tasks[tid])}")
            for d in s.get("detail", [])[:5]:
                lines.append(f"  {'':<{width}}    {d}")
        lines += ["", f"Result: {'DONE' if done else 'NOT DONE'}"]
        return "\n".join(lines), 0 if done else 1


def describe(s):
    st = s["status"]
    if st == "pass":
        commits = ", ".join(f"{Path(p).name or p} {sha[:7]}" for p, sha in s["commits"].items())
        return "pass" + (f" @ {commits}" if commits else "")
    if st == "stale":
        return "stale"
    if st == "open":
        return "no verdict"
    finding = s["findings"][0]
    text = f"{finding['key']}: {finding['text']}"
    if st == "needs-you":
        return f"needs you ({s['reason']}); last finding {text}"
    return f"fail ({s['fails']}): {text}"


def suffix(task):
    extras = (["acceptance"] if task.get("acceptance") else []) + \
             (["<- " + ", ".join(task["depends_on"])] if task.get("depends_on") else [])
    return f"  ({'; '.join(extras)})" if extras else ""


# ---------------------------------------------------------------- cli

def build_parser():
    p = argparse.ArgumentParser(prog="lo", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create a run folder with plan.md and decisions.md")
    s.add_argument("run")
    s.add_argument("--brief", help="file with the user's request, copied into plan.md")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("approve", help="record the user's approval of the current plan.md")
    s.add_argument("run")
    s.add_argument("--quote", required=True, help="the user's approving words, verbatim")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("verdict", help="record a verifier's verdict on a task")
    s.add_argument("run")
    s.add_argument("id")
    s.add_argument("result", choices=["pass", "fail"])
    s.add_argument("--finding", nargs=2, action="append", metavar=("KEY", "TEXT"),
                   help="a finding; a fail needs one, reuse KEY when the same defect persists")
    s.add_argument("--quote", help="the user's words, for a task with a human check")
    s.add_argument("--timeout", type=int, default=1800, help="seconds per command (default 1800)")
    s.set_defaults(fn=cmd_verdict)

    s = sub.add_parser("check", help="what is approved, verified, stale, or open; exit 0 only when done")
    s.add_argument("run")
    s.add_argument("--diff", action="store_true", help="show plan.md changes since approval")
    s.set_defaults(fn=cmd_check)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = args.fn(args)
    except LoError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 2
    if isinstance(result, tuple):
        text, code = result
        print(text)
        return code
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
