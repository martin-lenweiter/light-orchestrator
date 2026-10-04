#!/usr/bin/env python3
"""lo: run state for plan -> execute -> verify agent workflows.

All state lives in one run directory. ledger.jsonl is the append-only source
of truth; state.json is a snapshot rebuilt from it on demand. Every command
takes an exclusive lock, validates the transition, appends one event, and
rewrites the snapshot atomically. Agents must change state only through lo.
"""

import argparse
import contextlib
import fcntl
import json
import os
import re
import uuid
import sys
import time
from pathlib import Path

LEDGER = "ledger.jsonl"
STATE = "state.json"
LOCK = ".lock"

DEFAULTS = {"max_repairs": 2, "lease_seconds": 1800, "max_parallel": 0}

# Run phases and the phases each one may move to.
PHASES = {
    "clarifying": {"awaiting-answers", "planning"},
    "awaiting-answers": {"clarifying", "planning"},
    "planning": {"awaiting-approval", "clarifying"},
    "awaiting-approval": {"planning", "executing"},
    "executing": {"verifying"},
    "verifying": {"executing", "done", "partial"},
    "done": set(),
    "partial": {"verifying"},  # only through lo resolve
}
TASK_STATUSES = {"todo", "running", "done", "verified", "needs-human"}
TERMINAL = {"verified", "needs-human"}


class OrchestratorError(Exception):
    pass


def now():
    return time.time()


# ---------------------------------------------------------------- state model

def empty_state():
    return {
        "phase": None,
        "round": 1,
        "config": dict(DEFAULTS),
        "plan_approved": False,
        "amendments": [],
        "tasks": {},
        "order": [],
        "events": 0,
    }


TASK_SPEC_KEYS = ("id", "goal", "done_when", "depends_on", "acceptance", "uses", "model",
                  "effort", "checkpoint")


def new_task(state, spec):
    return {
        "id": spec["id"],
        "goal": spec["goal"],
        "done_when": spec["done_when"],
        "depends_on": spec.get("depends_on", []),
        "acceptance": bool(spec.get("acceptance")),
        "uses": spec.get("uses", []),
        "model": spec.get("model"),
        "effort": spec.get("effort"),
        "checkpoint": "human" if spec.get("checkpoint") == "human" else bool(spec.get("checkpoint")),
        "status": "todo",
        "attempts": 0,
        "owner": None,
        "lease_until": None,
        "output": None,
        "last_findings": [],
        "history": [],
    }


def apply(state, ev):
    """Pure state transition. Validation happens before events are written."""
    t = ev["type"]
    tasks = state["tasks"]
    if t == "init":
        state["phase"] = "clarifying"
        state["config"].update(ev.get("config", {}))
        state["config"].setdefault("resources", {})
        state["title"] = ev.get("title", "")
    elif t == "phase":
        state["phase"] = ev["to"]
        if ev.get("round_up"):
            state["round"] += 1
    elif t == "tasks-set":
        tasks.clear()
        state["order"] = []
        for spec in ev["tasks"]:
            tasks[spec["id"]] = new_task(state, spec)
            state["order"].append(spec["id"])
    elif t == "amend":
        # Dropped tasks leave the graph (the ledger keeps them). Listed tasks get
        # their new spec and start over; their dependents and the acceptance
        # task are reopened. Unaffected verified work stays.
        for tid in ev.get("drop", []):
            del tasks[tid]
            state["order"].remove(tid)
        for spec in ev["tasks"]:
            fresh = new_task(state, spec)
            if spec["id"] in tasks:
                fresh["history"] = tasks[spec["id"]]["history"]
            else:
                state["order"].append(spec["id"])
            tasks[spec["id"]] = fresh
        for spec in ev["tasks"]:
            invalidate_descendants(state, spec["id"], recover=True)
        for task in tasks.values():
            if task["acceptance"]:
                task.update(status="todo", output=None, stop_reason=None)
        state["amendments"].append({"by": ev.get("by"), "note": ev["note"], "tasks": [x["id"] for x in ev["tasks"]],
                                   "dropped": ev.get("drop", [])})
        if state["phase"] in ("verifying", "done", "partial"):
            state["phase"] = "executing"
    elif t == "approve":
        state["plan_approved"] = True
        state["phase"] = "executing"
    elif t == "claim":
        task = tasks[ev["id"]]
        task.update(status="running", owner=ev["owner"], lease_until=ev["lease_until"],
                    token=ev.get("token"), output_dir=ev.get("output_dir"))
    elif t == "done":
        task = tasks[ev["id"]]
        task.update(status="done", output=ev.get("output"), owner=None, lease_until=None)
    elif t == "pass":
        task = tasks[ev["id"]]
        task["status"] = "verified"
        task["notes"] = ev.get("notes", [])
    elif t == "fail":
        task = tasks[ev["id"]]
        task["attempts"] += 1
        task["history"].append({"round": state["round"], "findings": ev["findings"]})
        task["last_findings"] = ev["findings"]
        task["status"] = ev["next_status"]
        task["stop_reason"] = ev.get("stop_reason")
        invalidate_descendants(state, ev["id"])
    elif t == "release":
        for tid in ev["ids"]:
            tasks[tid].update(status="todo", owner=None, lease_until=None)
    elif t == "resolve":
        task = tasks[ev["id"]]
        invalidate_descendants(state, ev["id"], recover=True)
        task.update(status="todo" if ev.get("retry") else "done", stop_reason=None, resolution=ev["note"])
        if ev.get("retry"):
            task.update(output=None, token=None, owner=None, lease_until=None)
        if ev.get("output"):
            task["output"] = ev["output"]
        state["phase"] = "executing" if ev.get("retry") else "verifying"
    elif t == "escalate":
        for tid in ev["ids"]:
            tasks[tid]["status"] = "needs-human"
            tasks[tid]["stop_reason"] = ev["reason"]
    else:
        raise OrchestratorError(f"unknown event type {t}")
    state["events"] += 1
    return state


# ------------------------------------------------------------------- storage

class Run:
    def __init__(self, path):
        self.dir = Path(path)
        self.ledger = self.dir / LEDGER
        self.snapshot = self.dir / STATE

    @contextlib.contextmanager
    def locked(self):
        if not self.dir.is_dir():
            raise OrchestratorError(f"no run directory: {self.dir}")
        with open(self.dir / LOCK, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def events(self):
        if not self.ledger.exists():
            return []
        out = []
        with open(self.ledger) as fh:
            for number, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict) or "type" not in event:
                        raise OrchestratorError(f"invalid ledger record at line {number}")
                    out.append(event)
                except json.JSONDecodeError as exc:
                    raise OrchestratorError(f"corrupt ledger at line {number}: {exc.msg}") from exc
        return out

    def rebuild(self):
        state = empty_state()
        for ev in self.events():
            try:
                apply(state, ev)
            except (KeyError, TypeError, ValueError) as exc:
                raise OrchestratorError(f"invalid ledger event {state['events'] + 1}: {exc}") from exc
        return state

    def load(self):
        # The ledger is authoritative; rebuilding also validates every record.
        return self.rebuild()

    def append(self, state, ev):
        ev = {"ts": round(now(), 3), **ev}
        line = json.dumps(ev, ensure_ascii=False) + "\n"
        if self.ledger.exists() and self.ledger.stat().st_size:
            with open(self.ledger, "rb") as tail:
                tail.seek(-1, os.SEEK_END)
                if tail.read(1) != b"\n":
                    line = "\n" + line
        with open(self.ledger, "a") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        apply(state, ev)
        self.write_snapshot(state)
        return state

    def write_snapshot(self, state):
        tmp = self.snapshot.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        os.replace(tmp, self.snapshot)


# ------------------------------------------------------------------ commands

def require_phase(state, *phases):
    if state["phase"] not in phases:
        raise OrchestratorError(f"phase is {state['phase']}; expected one of {', '.join(phases)}")


def get_task(state, tid):
    if tid not in state["tasks"]:
        raise OrchestratorError(f"unknown task {tid}")
    return state["tasks"][tid]


def counts(state):
    c = {s: 0 for s in sorted(TASK_STATUSES)}
    for t in state["tasks"].values():
        c[t["status"]] += 1
    return c


def invalidate_descendants(state, tid, recover=False):
    affected = {tid}
    changed = True
    while changed:
        changed = False
        for task in state["tasks"].values():
            if task["id"] not in affected and affected.intersection(task["depends_on"]):
                affected.add(task["id"])
                changed = True
                if task["status"] in ("done", "verified") or (
                    recover and task["status"] == "needs-human"
                    and task.get("stop_reason", "").startswith("blocked dependency")
                ):
                    task.update(status="todo", output=None, stop_reason=None)


def ready(state, task):
    for dep in task.get("depends_on", []):
        source = state["tasks"][dep]
        status = source["status"]
        if status == "verified" or (status == "done" and not source.get("checkpoint")):
            continue
        return False
    return True


def settle_blocked(run, state):
    while True:
        ids = [t["id"] for t in state["tasks"].values() if t["status"] == "todo"
               and any(state["tasks"][d]["status"] == "needs-human" for d in t["depends_on"])]
        if not ids:
            return
        run.append(state, {"type": "escalate", "ids": ids, "reason": "blocked dependency needs resolution"})


def write_report(run, state):
    lines = [f"# {state.get('title', 'Run')}", "", f"Status: {state['phase']}", ""]
    for tid in state["order"]:
        task = state["tasks"][tid]
        lines.append(f"- {tid}: {task['status']} — {task['goal']}")
        if task.get("output"):
            lines.append(f"  Output: [{task['output']}]({task['output']})")
        if task["status"] == "verified":
            for note in task.get("notes", []):
                lines.append(f"  Verification note: {note['text']}")
        if task.get("stop_reason"):
            lines.append(f"  Unresolved: {task['stop_reason']}")
        for finding in task.get("last_findings", []) if task["status"] != "verified" else []:
            lines.append(f"  Finding: {finding['text']}")
    if state.get("amendments"):
        lines += ["", "## Amendments", ""]
    for n, amendment in enumerate(state.get("amendments", []), 1):
        lines.append(f"- Amendment {n} by {amendment['by']}: {amendment['note']}")
        if amendment.get("dropped"):
            lines.append(f"  Dropped: {', '.join(amendment['dropped'])}")
    tmp = run.dir / "report.md.tmp"
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, run.dir / "report.md")


def blocked_todo(state):
    return [t["id"] for t in state["tasks"].values() if t["status"] == "todo" and not ready(state, t)]


def capacity_block(state, task):
    """Name of a resource or limit that stops this task from starting now, else None.

    Resources are declared per run with a capacity (how many running tasks may
    use them at once). Read-only surfaces get high capacity; paid calls and
    writes get 1. Undeclared resources are unlimited.
    """
    running = [t for t in state["tasks"].values() if t["status"] == "running"]
    limit = state["config"].get("max_parallel") or 0
    if limit and len(running) >= limit:
        return f"max_parallel {limit}"
    caps = state["config"].get("resources", {})
    for r in task.get("uses", []):
        if r in caps and sum(r in t.get("uses", []) for t in running) >= caps[r]:
            return f"resource {r} at capacity {caps[r]}"
    return None


def awaiting_review(state, human):
    """Completed tasks awaiting a verdict, from the user (human) or the verifier."""
    return [t["id"] for t in state["tasks"].values()
            if t["status"] == "done" and (t.get("checkpoint") == "human") == human]


def next_action(state):
    """One-line instruction for whichever agent reads the run next."""
    p = state["phase"]
    c = counts(state)
    if p == "clarifying":
        return "planner: read brief.md; write questions to questions.md and move to awaiting-answers, or move to planning"
    if p == "awaiting-answers":
        return "human: answer questions.md, then planner moves to clarifying or planning"
    if p == "planning":
        return "planner: write plan.md and tasks.json, lo tasks set, then move to awaiting-approval"
    if p == "awaiting-approval":
        return "human: review plan.md, then lo approve (or move back to planning)"
    if p == "executing":
        blocked = len(blocked_todo(state))
        if c["todo"] - blocked or c["running"]:
            return f"orchestrator: {c['todo']} todo, {c['running']} running; claim and execute tasks"
        return "orchestrator: all runnable tasks executed; move to verifying" + (
            f" ({blocked} task(s) wait for dependencies)" if blocked else "")
    if p == "verifying":
        agent, human = awaiting_review(state, False), awaiting_review(state, True)
        if agent:
            return f"verifier: {len(agent)} task(s) awaiting verification"
        if human:
            return (f"human: review {', '.join(human)}, then lo verdict <run> <id> pass, "
                    "or fail --note \"what to change\"")
        return "verifier: lo finish (moves to executing for repairs, or done/partial)"
    return f"run is {p}"


def expire_leases(run, state):
    stale = [t["id"] for t in state["tasks"].values()
             if t["status"] == "running" and (t["lease_until"] or 0) < now()]
    if stale:
        run.append(state, {"type": "escalate", "ids": stale, "reason": "lease expired; stop the old worker and inspect external writes before resolving"})
    return stale


def cmd_init(args):
    d = Path(args.run)
    if (d / LEDGER).exists():
        raise OrchestratorError(f"run already exists: {d}")
    d.mkdir(parents=True, exist_ok=True)
    (d / "out").mkdir(exist_ok=True)
    if args.brief:
        (d / "brief.md").write_text(Path(args.brief).read_text())
    elif not (d / "brief.md").exists():
        (d / "brief.md").write_text("# Brief\n\nGoal:\n\nInputs:\n\nOutput:\n\nAcceptance criteria:\n")
    config = {k: getattr(args, k) for k in DEFAULTS if getattr(args, k) is not None}
    resources = {}
    for item in args.resource or []:
        name, _, cap = item.partition("=")
        if not name or not cap.isdigit() or int(cap) < 1:
            raise OrchestratorError(f"--resource needs name=capacity (capacity >= 1): {item}")
        resources[name] = int(cap)
    config["resources"] = resources
    run = Run(d)
    with run.locked():
        run.append(empty_state(), {"type": "init", "title": args.title or d.name, "config": config})
    return {"run": str(d), "phase": "clarifying"}


def cmd_status(args):
    run = Run(args.run)
    with run.locked():
        state = run.load()
    return {
        "phase": state["phase"],
        "round": state["round"],
        "plan_approved": state["plan_approved"],
        "amendments": state["amendments"],
        "counts": counts(state),
        "next": next_action(state),
        "tasks": [
            {k: state["tasks"][tid].get(k) for k in ("id", "status", "attempts", "owner", "token", "output_dir", "output", "model", "effort", "stop_reason")}
            for tid in state["order"]
        ],
    }


PIPELINE = ["clarifying", "planning", "approval", "executing", "verifying", "done"]
PIPELINE_SLOT = {"awaiting-answers": "clarifying", "awaiting-approval": "approval", "partial": "done"}
MARKS = [("x", "verified"), ("~", "done, awaiting verification"), (">", "running"),
         (" ", "ready"), (".", "waiting on dependencies"), ("!", "needs human")]


def render_graph(state, width=60):
    """Plain-text task graph, layered by dependency depth, showing where the run is."""
    phase = state["phase"]
    slot = PIPELINE_SLOT.get(phase, phase)
    stages = [f"[{phase}]" if s == slot else s for s in PIPELINE]
    tasks = state["tasks"]
    depth = {}

    def level(tid):
        if tid not in depth:
            depth[tid] = 1 + max((level(d) for d in tasks[tid]["depends_on"]), default=-1)
        return depth[tid]

    c = counts(state)
    summary = [f"{c['verified']}/{len(tasks)} verified"]
    summary += [f"{c[s]} {label}" for s, label in (("running", "running"), ("done", "awaiting verification"),
                                                   ("needs-human", "need human")) if c[s]]
    lines = [f"{state.get('title') or 'Run'} · round {state['round']}", "",
             " > ".join(stages), ""]
    if tasks:
        lines += [" · ".join(summary), ""]
    idw = max((len(t) for t in tasks), default=0)
    goalw = min(width, max((len(t["goal"]) for t in tasks.values()), default=0))
    for lv in sorted({level(t) for t in state["order"]}):
        for n, tid in enumerate(t for t in state["order"] if level(t) == lv):
            task = tasks[tid]
            goal = task["goal"] if len(task["goal"]) <= width else task["goal"][:width - 1] + "…"
            row = f"{f'L{lv}' if n == 0 else '':<4}[{task_mark(state, task)}] {tid:<{idw}}  {goal:<{goalw}}"
            extras = []
            if task["acceptance"]:
                extras.append("acceptance")
            if task["status"] == "running" and task.get("owner"):
                extras.append(task["owner"])
            if task["depends_on"]:
                extras.append("<- " + ", ".join(task["depends_on"]))
            lines.append((row + (f"  ({'; '.join(extras)})" if extras else "")).rstrip())
    if tasks:
        lines += ["", "  ".join(f"[{m}] {label}" for m, label in MARKS)]
    lines += ["", f"Next: {next_action(state)}"]
    return "\n".join(lines)


def task_mark(state, task):
    if task["status"] == "todo":
        return " " if ready(state, task) else "."
    return {"verified": "x", "done": "~", "running": ">", "needs-human": "!"}[task["status"]]


def cmd_graph(args):
    run = Run(args.run)
    with run.locked():
        state = run.load()
    return render_graph(state)


def cmd_phase(args):
    run = Run(args.run)
    with run.locked():
        state = run.load()
        cur, to = state["phase"], args.to
        if to not in PHASES.get(cur, set()):
            raise OrchestratorError(f"cannot move {cur} -> {to}")
        if to == "executing" and cur == "awaiting-approval":
            raise OrchestratorError("use lo approve to start execution")
        if to == "verifying":
            waiting = set(blocked_todo(state))
            if any(t["status"] == "running" or (t["status"] == "todo" and t["id"] not in waiting)
                   for t in state["tasks"].values()):
                raise OrchestratorError("tasks still todo or running")
        if to == "awaiting-approval" and not state["tasks"]:
            raise OrchestratorError("no tasks set")
        if cur == "verifying":
            raise OrchestratorError("use lo finish to leave verifying")
        run.append(state, {"type": "phase", "from": cur, "to": to})
    return {"phase": to}


def read_specs(path):
    specs = json.loads(Path(path).read_text())
    return specs.get("tasks", []) if isinstance(specs, dict) else specs


def validate_specs(specs):
    seen = set()
    for s in specs:
        for key in ("id", "goal", "done_when"):
            if not s.get(key):
                raise OrchestratorError(f"task missing {key}: {s}")
        if s["id"] in seen:
            raise OrchestratorError(f"duplicate task id {s['id']}")
        if not isinstance(s["id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", s["id"]):
            raise OrchestratorError("task id must contain only letters, numbers, underscores and hyphens")
        if s.get("checkpoint") not in (None, True, False, "human"):
            raise OrchestratorError(f"task {s['id']} checkpoint must be true, false or \"human\"")
        for field in ("model", "effort"):
            if s.get(field) is not None and (not isinstance(s[field], str) or not s[field].strip()):
                raise OrchestratorError(f"task {s['id']} {field} must be a nonempty string")
        seen.add(s["id"])
    for s in specs:
        for d in s.get("depends_on", []):
            if d not in seen or d == s["id"]:
                raise OrchestratorError(f"task {s['id']} has invalid dependency {d}")
    deps = {s["id"]: s.get("depends_on", []) for s in specs}
    visiting, done = set(), set()

    def visit(tid):
        if tid in done:
            return
        if tid in visiting:
            raise OrchestratorError(f"dependency cycle at {tid}")
        visiting.add(tid)
        for d in deps[tid]:
            visit(d)
        visiting.discard(tid)
        done.add(tid)

    for tid in deps:
        visit(tid)

    # Exactly one acceptance task checks the whole result against the brief, so
    # it must (transitively) depend on every other task.
    accept = [s["id"] for s in specs if s.get("acceptance")]
    if len(accept) != 1:
        raise OrchestratorError("plan needs exactly one task with \"acceptance\": true")

    def upstream(tid, acc):
        for d in deps[tid]:
            if d not in acc:
                acc.add(d)
                upstream(d, acc)
        return acc

    missing = set(deps) - {accept[0]} - upstream(accept[0], set())
    if missing:
        raise OrchestratorError(f"acceptance task {accept[0]} must depend on: {', '.join(sorted(missing))}")


def cmd_tasks_set(args):
    specs = read_specs(args.file)
    validate_specs(specs)
    run = Run(args.run)
    with run.locked():
        state = run.load()
        require_phase(state, "planning")
        if state["plan_approved"]:
            raise OrchestratorError("plan is approved and frozen")
        run.append(state, {"type": "tasks-set", "tasks": specs})
    return {"tasks": len(specs)}


def cmd_amend(args):
    """Record an approved requirement change: add, revise, or drop tasks."""
    specs = read_specs(args.file)
    if not specs:
        raise OrchestratorError("amendment has no tasks")
    ids = [c.get("id") for c in specs]
    if len(ids) != len(set(ids)):
        raise OrchestratorError("amendment lists a task id more than once")
    drops = [c["id"] for c in specs if c.get("drop")]
    changes = [c for c in specs if not c.get("drop")]
    run = Run(args.run)
    with run.locked():
        state = run.load()
        require_phase(state, "executing", "verifying", "done", "partial")
        tasks = state["tasks"]
        unknown = [tid for tid in drops if tid not in tasks]
        if unknown:
            raise OrchestratorError(f"cannot drop unknown task {', '.join(unknown)}")
        merged = []
        for c in changes:
            base = tasks.get(c.get("id"))
            spec = {k: base[k] for k in TASK_SPEC_KEYS} if base else {}
            merged.append({**spec, **c})
        by_id = {tid: {k: tasks[tid][k] for k in TASK_SPEC_KEYS}
                 for tid in state["order"] if tid not in drops}
        new_ids = [c.get("id") for c in changes if c.get("id") not in tasks]
        by_id.update({m.get("id"): m for m in merged})
        validate_specs(list(by_id.values()))
        # Refuse to reset work that a worker still holds.
        affected = {m["id"] for m in merged} | {tid for tid, sp in by_id.items() if sp.get("acceptance")}
        changed = True
        while changed:
            changed = False
            for tid, sp in by_id.items():
                if tid not in affected and affected.intersection(sp.get("depends_on", [])):
                    affected.add(tid)
                    changed = True
        busy = sorted(tid for tid in affected | set(drops) if tid in tasks and tasks[tid]["status"] == "running")
        if busy:
            raise OrchestratorError(f"stop the workers on {', '.join(busy)} before amending them")
        run.append(state, {"type": "amend", "tasks": merged, "drop": drops, "by": args.by, "note": args.note})
        reopened = [tid for tid in state["order"] if tid in affected]
    return {"phase": state["phase"], "amendment": len(state["amendments"]),
            "added": new_ids, "dropped": drops, "reopened": reopened}


def cmd_approve(args):
    run = Run(args.run)
    with run.locked():
        state = run.load()
        require_phase(state, "awaiting-approval")
        run.append(state, {"type": "approve", "by": args.by})
    return {"phase": "executing", "tasks": len(state["tasks"])}


def cmd_claim(args):
    run = Run(args.run)
    with run.locked():
        state = run.load()
        require_phase(state, "executing")
        expire_leases(run, state)
        candidates = [args.id] if args.id else state["order"]
        held = {}
        for tid in candidates:
            task = get_task(state, tid)
            if task["status"] == "todo" and ready(state, task):
                block = capacity_block(state, task)
                if block:
                    held[tid] = block
                    continue
                lease = args.lease or state["config"]["lease_seconds"]
                if lease <= 0:
                    raise OrchestratorError("lease must be positive")
                token = uuid.uuid4().hex
                output_dir = f"out/{tid}/{token}"
                (run.dir / output_dir).mkdir(parents=True, exist_ok=True)
                run.append(state, {"type": "claim", "id": tid, "owner": args.owner,
                                   "lease_until": now() + lease, "token": token, "output_dir": output_dir})
                return {"claimed": tid, "goal": task["goal"], "done_when": task["done_when"],
                        "model": task.get("model"), "effort": task.get("effort"),
                        "token": token, "output_dir": output_dir,
                        "uses": task.get("uses", []),
                        "attempts": task["attempts"], "last_findings": task["last_findings"],
                        "dependencies": {d: {"status": state["tasks"][d]["status"],
                                             "output": state["tasks"][d]["output"]}
                                         for d in task["depends_on"]}}
        return {"claimed": None, "waiting_on_dependencies": blocked_todo(state),
                "waiting_on_capacity": held}


def cmd_done(args):
    run = Run(args.run)
    with run.locked():
        state = run.load()
        require_phase(state, "executing")
        task = get_task(state, args.id)
        if task["status"] != "running":
            raise OrchestratorError(f"task {args.id} is {task['status']}, not running")
        if task.get("token"):
            if args.token != task["token"] or task["lease_until"] <= now():
                raise OrchestratorError("claim token does not match or lease expired")
            if not args.output:
                raise OrchestratorError("output required")
            target = (run.dir / args.output).resolve()
            if not target.is_relative_to((run.dir / task["output_dir"]).resolve()):
                raise OrchestratorError("output must be within the current attempt output_dir")
        if args.output and not (run.dir / args.output).is_file():
            raise OrchestratorError(f"output not found: {args.output}")
        run.append(state, {"type": "done", "id": args.id, "output": args.output})
    return {"id": args.id, "status": "done"}


def cmd_block(args):
    """Record a worker blocker without pretending implementation completed."""
    run = Run(args.run)
    with run.locked():
        state = run.load()
        require_phase(state, "executing")
        task = get_task(state, args.id)
        if task["status"] != "running" or task.get("token") != args.token:
            raise OrchestratorError("block requires the active claim token")
        run.append(state, {"type": "escalate", "ids": [args.id], "reason": args.reason})
    return {"id": args.id, "status": "needs-human"}


def read_findings(path):
    findings = json.loads(Path(path).read_text())
    if isinstance(findings, dict):
        findings = findings.get("findings", [])
    for f in findings:
        if f.get("severity") not in ("blocking", "note") or not f.get("key") or not f.get("text"):
            raise OrchestratorError(f"finding needs key, severity (blocking|note) and text: {f}")
    return findings


def cmd_verdict(args):
    run = Run(args.run)
    with run.locked():
        state = run.load()
        require_phase(state, "verifying")
        task = get_task(state, args.id)
        if task["status"] != "done":
            raise OrchestratorError(f"task {args.id} is {task['status']}, not done")
        findings = read_findings(args.findings) if args.findings else []
        if args.note:
            findings.append({"key": f"review-{task['attempts']}",
                             "severity": "blocking" if args.verdict == "fail" else "note",
                             "text": args.note})
        blocking = [f for f in findings if f["severity"] == "blocking"]
        if args.verdict == "pass":
            if blocking:
                raise OrchestratorError("pass given with blocking findings")
            run.append(state, {"type": "pass", "id": args.id, "notes": findings})
            return {"id": args.id, "status": "verified"}
        if not blocking:
            raise OrchestratorError("fail needs at least one blocking finding")
        cfg = state["config"]
        previous = {f["key"] for f in task["last_findings"] if f["severity"] == "blocking"}
        repeated = sorted(previous & {f["key"] for f in blocking})
        if repeated:
            status, reason = "needs-human", f"stalled: repeated finding(s) {', '.join(repeated)}"
        elif task["attempts"] + 1 > cfg["max_repairs"]:
            status, reason = "needs-human", f"repair cap reached ({cfg['max_repairs']})"
        else:
            status, reason = "todo", None
        run.append(state, {"type": "fail", "id": args.id, "findings": findings,
                           "next_status": status, "stop_reason": reason})
    return {"id": args.id, "status": status, "stop_reason": reason}


def cmd_finish(args):
    """Close a verify round: loop back for repairs, or end the run."""
    run = Run(args.run)
    with run.locked():
        state = run.load()
        require_phase(state, "verifying")
        c = counts(state)
        if awaiting_review(state, False):
            raise OrchestratorError(f"{c['done']} task(s) still await a verdict")
        if awaiting_review(state, True):
            raise OrchestratorError(next_action(state))
        if c["running"]:
            raise OrchestratorError("tasks still running")
        settle_blocked(run, state)
        if counts(state)["todo"]:
            repairs = [t["id"] for t in state["tasks"].values() if t["status"] == "todo" and t["attempts"]]
            run.append(state, {"type": "phase", "from": "verifying", "to": "executing",
                               "round_up": bool(repairs)})
            return {"phase": "executing", "round": state["round"], "repairs": repairs,
                    "ready": [t["id"] for t in state["tasks"].values()
                              if t["status"] == "todo" and ready(state, t)]}
        accept = [t for t in state["tasks"].values() if t.get("acceptance")]
        accepted = all(t["status"] == "verified" for t in accept)
        final = "done" if counts(state)["needs-human"] == 0 and accepted else "partial"
        run.append(state, {"type": "phase", "from": "verifying", "to": final})
        write_report(run, state)
    return {"phase": final, "counts": counts(state)}


def cmd_resolve(args):
    """A human fixed a needs-human task; send it back through verification."""
    run = Run(args.run)
    with run.locked():
        state = run.load()
        task = get_task(state, args.id)
        if task["status"] != "needs-human":
            raise OrchestratorError(f"task {args.id} is {task['status']}, not needs-human")
        if state["phase"] not in ("executing", "verifying", "partial"):
            raise OrchestratorError(f"cannot resolve in phase {state['phase']}")
        if any(t["status"] == "running" for t in state["tasks"].values()):
            raise OrchestratorError("tasks still running")
        if args.output and not (run.dir / args.output).is_file():
            raise OrchestratorError(f"output not found: {args.output}")
        if args.retry and args.output:
            raise OrchestratorError("--retry cannot be combined with --output")
        if not args.retry and not (args.output or task.get("output")):
            raise OrchestratorError("provide repaired --output or use --retry after stopping the old worker")
        run.append(state, {"type": "resolve", "id": args.id, "note": args.note,
                           "by": args.by, "output": args.output, "retry": args.retry})
    return {"id": args.id, "status": task["status"], "phase": state["phase"]}


def cmd_resume(args):
    run = Run(args.run)
    with run.locked():
        state = run.rebuild()
        run.write_snapshot(state)
        released = []
        if state["phase"] == "executing":
            if args.force:
                released = [t["id"] for t in state["tasks"].values() if t["status"] == "running"]
                if released:
                    run.append(state, {"type": "release", "ids": released, "reason": "resume --force"})
            else:
                released = expire_leases(run, state)
        if state["phase"] in ("done", "partial"):
            write_report(run, state)
    return {"phase": state["phase"], "released": released, "next": next_action(state)}


def build_parser():
    p = argparse.ArgumentParser(prog="lo", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create a run directory")
    s.add_argument("run")
    s.add_argument("--brief")
    s.add_argument("--title")
    for k in DEFAULTS:
        s.add_argument(f"--{k.replace('_', '-')}", dest=k, type=int)
    s.add_argument("--resource", action="append",
                   help="name=capacity, e.g. web-search=8 chrome=4 clay=1 (repeatable)")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("status", help="phase, task counts and next action")
    s.add_argument("run")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("graph", help="draw the task graph and show where the run is")
    s.add_argument("run")
    s.set_defaults(fn=cmd_graph)

    s = sub.add_parser("phase", help="move the run to another phase")
    s.add_argument("run")
    s.add_argument("to", choices=sorted(PHASES))
    s.set_defaults(fn=cmd_phase)

    s = sub.add_parser("tasks", help="set the task list from a JSON file (planning only)")
    s.add_argument("action", choices=["set"])
    s.add_argument("run")
    s.add_argument("file")
    s.set_defaults(fn=cmd_tasks_set)

    s = sub.add_parser("approve", help="approve and freeze the plan; start execution")
    s.add_argument("run")
    s.add_argument("--by", default="human")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("amend", help="record a requirement change after approval: add or revise tasks")
    s.add_argument("run")
    s.add_argument("file", help='JSON task specs; an existing id revises that task (given fields only); {"id": ..., "drop": true} removes it')
    s.add_argument("--note", required=True, help="the requirement change and who asked for it")
    s.add_argument("--by", default="human")
    s.set_defaults(fn=cmd_amend)

    s = sub.add_parser("claim", help="claim the next todo task (or --id)")
    s.add_argument("run")
    s.add_argument("--owner", required=True)
    s.add_argument("--id")
    s.add_argument("--lease", type=int)
    s.set_defaults(fn=cmd_claim)

    s = sub.add_parser("done", help="mark a claimed task executed")
    s.add_argument("run")
    s.add_argument("id")
    s.add_argument("--output")
    s.add_argument("--token")
    s.set_defaults(fn=cmd_done)

    s = sub.add_parser("block", help="record why a claimed task cannot continue")
    s.add_argument("run")
    s.add_argument("id")
    s.add_argument("--token", required=True)
    s.add_argument("--reason", required=True)
    s.set_defaults(fn=cmd_block)

    s = sub.add_parser("verdict", help="record the verifier's verdict for a task")
    s.add_argument("run")
    s.add_argument("id")
    s.add_argument("verdict", choices=["pass", "fail"])
    s.add_argument("--findings", help="JSON list of {key, severity, text}")
    s.add_argument("--note", help="one finding as text: blocking with fail, a note with pass")
    s.set_defaults(fn=cmd_verdict)

    s = sub.add_parser("finish", help="close a verify round")
    s.add_argument("run")
    s.set_defaults(fn=cmd_finish)

    s = sub.add_parser("resolve", help="record a human fix for a needs-human task and re-verify it")
    s.add_argument("run")
    s.add_argument("id")
    s.add_argument("--note", required=True)
    s.add_argument("--by", default="human")
    s.add_argument("--output")
    s.add_argument("--retry", action="store_true", help="retry after stopping the old worker and checking external writes")
    s.set_defaults(fn=cmd_resolve)

    s = sub.add_parser("resume", help="rebuild state and flag expired claims for resolution")
    s.add_argument("run")
    s.add_argument("--force", action="store_true", help="release running tasks only after stopping old workers and reconciling external writes")
    s.set_defaults(fn=cmd_resume)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = args.fn(args)
    except OrchestratorError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 2
    print(result if isinstance(result, str) else json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
