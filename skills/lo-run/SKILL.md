---
name: lo-run
description: Coordinate the work of an approved lo (light-orchestrator) plan. Delegate tasks, keep the plan and decisions current, amend the plan with the user's approval, send work to verification, and handle repairs until lo check reports done.
---

# Run

Start with `lo check <run>`. It shows the approval and each task's state:
no verdict, pass, stale, fail, or needs you. When the user asks to see the
run, show its output in a code block.

## Delegate

Start tasks whose dependencies are done, in parallel where they are
independent. Use the model and effort set on each task. Give each worker:

- the goal, the paths it may write, and the task's `verify` entries;
- the decisions and inputs it needs, with upstream results pasted in;
- what it must not do;
- the report you want: what changed, what it ran and saw, decisions under
  `## Decisions`, and open questions under `## Needs decision`.

Workers commit their changes, because a verdict covers committed code only.
Run at most one task at a time on a shared surface such as one browser
profile. Track who is working on what yourself; the run does not record it.

Before you repeat an external write, check the destination for the earlier
write. A timeout does not show that the write failed.

## Decide and amend

Record decisions in `decisions.md` with who made them. Decide minor
questions yourself. Bring anything material to the user: scope, product
behavior, a design the user will see, costs, or external writes.

When a decision makes `plan.md` wrong, amend the plan:

1. Edit `plan.md`. From then on, `lo verdict` refuses to record anything.
2. Show the user the change with `lo check <run> --diff`, and why.
3. After the user explicitly approves, run
   `lo approve <run> --quote "<the user's words>"`. Tasks whose definition
   changed, and the acceptance task, need new verdicts.

For a large change, have a fresh agent with the lo-plan skill draft the
amendment and another fresh agent critique it before you show it.

## Verify and repair

When a task is done, have a fresh agent verify it with the lo-verify skill,
on a different model family from the worker when one is available. For a
`human` check, show the user the result and ask for a verdict; the verifier
records the user's words.

After a fail, give a worker the findings and keep the passing work. A task
that shows "needs you" goes to the user with its findings. A stale task is
verified again.

## Finish

The run is done only when `lo check <run>` prints `Result: DONE` and exits
0. Report from its output: what was delivered, how each task was verified,
and anything still open.
