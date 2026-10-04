# lo (light-orchestrator)

`lo` carries a large job, done by a team of AI agents, from your request to a
verified result. It guarantees two things:

1. **What you approved is fixed and visible.** The plan you approved is
   recorded. Any later change shows up and needs your approval again.
2. **Nothing counts as done until it is verified.** Each task lists what must
   be checked: commands, a code review, a live check, your own judgment. A
   pass needs evidence for every check, and it goes stale when the code
   changes afterwards.

It is not an agent. It runs inside an agent environment such as Claude Code,
Codex, or Hermes, which supplies the models and tools. `lo` supplies three
skills that tell agents how to plan, run, and verify, and a small CLI that
records what an agent should not declare about its own work.

## Using it

Open a chat in the folder or repository you want to work in and say:

> Use lo to build a site that compares fast-food prices across delivery apps.

The agent then:

1. writes a plan in which every task says what will be verified, has a fresh
   agent critique it, and asks for your approval;
2. delegates the tasks to workers, in parallel where they are independent;
3. has fresh agents verify each task, preferably on another model family,
   repairs what fails, and brings you anything that is stuck;
4. reports done only when `lo check` says so.

You answer and approve in the conversation; you do not need to type commands.

## Design principle: delete first

The agents choose the approach, the task split, the checks, and the models.
`lo` fixes only what must not depend on an agent's word: your approval, the
evidence behind each verdict, the command results, and whether a verdict
still matches the code. A rule or feature stays only while capable models
fail without it.

## The run folder

| File | Written by | Contents |
|---|---|---|
| `plan.md` | orchestrator | The request, the approach, and the tasks with their checks, in one `json lo-tasks` block. Frozen at approval. |
| `decisions.md` | orchestrator | Every decision made during the run, and who made it |
| `reviews/<task>/` | verifiers, `lo` | Evidence for each check, and `commands.log` |
| `ledger.jsonl` | `lo` only | Approvals (with a copy of the plan) and verdicts |

## A task

```json
{"id": "ui", "goal": "Add the Library tab", "depends_on": ["engine"],
 "repos": {"~/code/app": ["app/src/library/"]},
 "verify": {
   "commands": ["npm run lint", "npm test", "npm run build"],
   "review": "A reviewer reads the diff against the goal",
   "live": "Screenshots at desktop and 390px width; each number equals the API"}}
```

- `verify` has one entry per kind of check, or `"n/a: <reason>"`. A task
  with `repos` changes code, so it needs `commands`, `review`, and `live`.
  `live` means a verifier agent drives the new behavior itself; scripted
  checks of existing flows belong in `commands`, and a task that adds a
  user flow adds it to the repository's verify script. `human` marks a
  check only you can make.
- `repos` names the paths the task writes. A verdict records their commit
  and goes stale when they change.
- Exactly one task has `"acceptance": true` and checks the whole request.
- `model` and `effort` choose the worker.

## Commands

| Command | What it does |
|---|---|
| `lo init <run> [--brief <file>]` | Create the run folder with `plan.md` and `decisions.md`, and hide `.lo/` from git |
| `lo approve <run> --quote "<your words>"` | Validate the plan and record your approval with a copy of it |
| `lo verdict <run> <task> pass` | Refuse unless the plan matches the approved copy, every check kind has evidence, the task's paths are committed, every other task has passed (for the acceptance task), and every command succeeds; then record the commits |
| `lo verdict <run> <task> fail --finding <key> "<text>"` | Record a failure. The same finding twice, or three failures, mark the task "needs you". |
| `lo check <run> [--diff]` | Show the approval, each task's state, and any plan change; exit 0 only when done |

`lo` checks completeness and freshness, not truth. It cannot tell which agent
ran a command or whether a review is good, and it cannot see a quote's
source. Fresh verifiers on another model family, and your own `lo check`,
are the guards for that.

## Amending the plan

The orchestrator edits `plan.md`. From then on `lo check` reports the change
and `lo verdict` refuses to record anything. The orchestrator shows you
`lo check --diff`; after you approve, it runs `lo approve`. Tasks whose
definition changed, and the acceptance task, need new verdicts.

## Setup

Link the script onto your PATH and give your agent the three skills:

```sh
ln -s "$PWD/lo.py" ~/.local/bin/lo
lo --help
```

Python 3, no dependencies.
