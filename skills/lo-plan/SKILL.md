---
name: lo-plan
description: Plan a new lo (light-orchestrator) run, or revise a run's plan before approval. Define tasks and what must be verified for each, obtain an independent critique, and request user approval. Use when the user asks to use lo or light-orchestrator, or for a large multi-step job that benefits from an approved plan and independent checking.
---

# Plan

A lo run carries a large job to a verified result. You own `plan.md` and
`decisions.md`. The `lo` CLI records only the user's approval and the
verifiers' verdicts, and `lo check` reports what is approved, verified, stale,
or open.

If one agent can finish the job in this session, do it without a run.

## Start

Unless the user names a place, the run is `.lo/<short-name>` in the working
directory. Write the user's request to a file and run
`lo init <run> --brief <file>`. It creates `plan.md` with the request,
`decisions.md`, and hides `.lo/` from git.

Ask the user only about missing information that changes the result or your
authority: scope, costs, external writes. Put the answers into the request
section of `plan.md`, so it states the current request.

## Write the plan

`plan.md` is the single description of what will be built. It holds the
request, the approach, anything workers and verifiers need to know, and the
tasks in one `json lo-tasks` block:

````markdown
```json lo-tasks
[
  {"id": "engine", "goal": "Compute weekly hours from the vault notes",
   "repos": {"~/code/app": ["server/library.mjs", "server/library.test.mjs"]},
   "verify": {
     "commands": ["npm test"],
     "review": "A reviewer reads the diff against the metric rules in this plan",
     "live": "GET /api/library on the dev server returns totals that match 10 hand-computed notes"}},
  {"id": "ui", "goal": "Add the Library tab", "depends_on": ["engine"],
   "repos": {"~/code/app": ["app/src/library/"]},
   "verify": {
     "commands": ["npm run lint", "npm test", "npm run build"],
     "review": "A reviewer reads the diff against the goal",
     "live": "Screenshots at desktop and 390px width; each number equals the API",
     "human": "The user approves the layout before the release task starts"}},
  {"id": "release", "goal": "Ship and run one real intake", "depends_on": ["ui"], "acceptance": true,
   "verify": {"live": "The installed service shows the tab, and one real intake reaches done"}}
]
```
````

- `verify` lists what must be verified, one entry per kind of check. Each
  entry is a concrete criterion: what to run, open, or read, and the expected
  result. Write `"n/a: <reason>"` for a kind that does not apply.
- `commands` are shell commands. `lo verdict` runs them itself in the first
  repository of the task and refuses a pass if one fails.
- A task with `repos` changes code, so it needs `commands`, `review`, and
  `live`, each with a criterion or `n/a: <reason>`. `review` means a
  reviewer in a fresh context reads the diff. `live` means the real app,
  API, or command is exercised the way a user reaches it.
- Add other kinds where they help, for example `data` for a source check.
  `human` marks a result that only the user can judge, when the answer
  changes later work; do not add it only for a final sign-off.
- `repos` maps each repository to the paths the task writes (`[]` for the
  whole repository). A verdict goes stale when those paths change after it.
  Output outside git, such as a live service or a cron job, belongs in a
  `live` check of the acceptance task.
- Exactly one task has `"acceptance": true` and checks the whole request.
- Set `model` and `effort` on each task.
- If a repository has no command that starts the app and drives its main
  flows, add a first task that creates one, or state in the plan why not.
- If the user's environment has a skill that defines good work of this kind,
  name it in the plan so workers and verifiers apply it.

`lo check <run>` validates the task block before approval.

## Critique and approval

Ask a fresh agent to critique the plan against the request: missing
coverage, unnecessary tasks, and checks that would pass a bad result. Resolve
the material findings in `plan.md` and record them in `decisions.md`.

Show the user the deliverables, what is verified for each, the human checks,
the agents and models, and any costs or external writes. Ask for approval.

Run `lo approve <run> --quote "<the user's words>"` only after the user
explicitly approves the plan you showed. A correction or a new requirement is
not an approval: revise the plan, show what changed, and ask again. Then
continue with the lo-run skill in the same conversation.
