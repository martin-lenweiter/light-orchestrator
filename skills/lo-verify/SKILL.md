---
name: lo-verify
description: Independently verify tasks of a lo (light-orchestrator) run against the approved plan, write the evidence, and record verdicts with lo verdict.
---

# Verify

Work in a fresh context, independent of the workers. Read `plan.md`,
`decisions.md`, and the output of `lo check <run>`. Verify inputs before the
tasks that depend on them, and the acceptance task last.

## Check each task

Perform every entry in the task's `verify` block against the real result,
not the worker's report of it.

- Write the evidence for each kind to `reviews/<task>/<kind>.md`, or to
  files that start with the kind, such as `reviews/ui/live-desktop.png`.
  State what you ran, opened, or read, and what you saw. `lo verdict`
  refuses a pass when a planned kind has no evidence.
- `lo verdict` runs the task's `commands` itself and saves the output in
  `reviews/<task>/commands.log`.
- A check you could not perform is not a pass. Record a fail with the reason.
- A departure from `plan.md` that `decisions.md` does not cover is a finding.
- Do not lower the bar, and do not add requirements that the plan does not
  contain.
- For a `human` check, the user decides. Record the user's own words with
  `--quote`; never write them yourself.

## Record the verdict

```sh
lo verdict <run> <task> pass [--finding <key> "<note>"] [--quote "<user's words>"]
lo verdict <run> <task> fail --finding <key> "<what is wrong, with evidence>"
```

A pass needs committed code in the task's paths; uncommitted changes there
mean the work is not finished. Reuse a finding's key when the same defect
persists, so lo can send a stuck task to the user.

Finish with `lo check <run>` and report each task's state to the
orchestrator.
