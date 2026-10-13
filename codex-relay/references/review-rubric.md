# Review rubric

What Codex checks before returning a verdict, and what WorkBuddy should do with each outcome.
The rubric is enforced by the prompt in `cmd_review`, not by this document — but WorkBuddy should
read it to predict verdicts and avoid obvious failures.

## Evidence rules

The reviewer sees **only**: the plan, and per-step `status`, `exit_code`, `stdout_excerpt`,
`artifacts`, `note`. It cannot re-run anything and cannot look at the workspace.

Therefore:

- **Exit code 0 is not evidence.** If `expected_effect` is "file X contains Y" and stdout is
  empty, the step is `unverifiable` at best. Say so honestly in `--note`.
- **`--note` is read closely.** It is the executor's own words; vague notes produce `unverifiable`.
- **Truncation is expected.** stdout is capped at 4000 chars. Put the decisive line early in the
  note if output is long.
- **Missing steps are rendered explicitly** as `REPORTED: (missing)`, so an omission reads as a
  failure, never as a pass.

## Per-step status

| status | Codex's criterion |
|---|---|
| `met` | Captured output demonstrates the stated `expected_effect` |
| `not_met` | Evidence shows the step did not achieve its effect |
| `deviated` | The executor ran something other than the planned command |
| `unverifiable` | Insufficient evidence to judge either way |

## Verdict rules

**`approved`** — every step `met`, every acceptance criterion satisfied, no unresolved risk.
Codex does not approve on "exit 0 across the board"; unevidenced steps block approval.

**`needs_revision`** — the objective is still reachable but the execution or the plan is wrong.
Typical causes:
- a step failed and the plan assumed success
- a command was substituted (`deviated`) because the planned one did not work
- an acceptance criterion cannot be checked from captured output
- the plan contained a step that was quietly skipped

Codex must name the specific corrective step. WorkBuddy's correct response is to **re-plan**
(`plan --iteration N+1`) with the findings as `--context` — not to re-run the failing command,
and not to hand-edit `review.json`.

**`rejected`** — the approach itself is wrong: the objective is misconceived, the plan is unsafe,
or the same failure recurred after a full re-plan. The loop stops and the user decides.

## Failure modes to avoid

1. **Skipping a step and marking it `done`.** Use `--status skipped` with a `--note` explaining
   why. `collect` accepts a recorded skip; it does not accept silence.
2. **Substituting a command without recording `deviated`.** If the planned command could not run
   and you ran something else, the status is `deviated` and the note must name both.
3. **Empty stdout with a successful exit.** Add a `--note` stating what you observed, or the
   reviewer will mark it `unverifiable`.
4. **Self-approval.** WorkBuddy never decides the work is acceptable. Only Codex does.
5. **Looping past the cap.** `--max-iterations` defaults to 3. On the third rejection, stop and
   show the user the plan, the evidence, and the reviewer's reasoning.

## Iteration and escalation

```
iteration 1  plan -> exec -> review
                            |- approved      -> report to user, show artifacts
                            |- needs_revision -> plan (iteration 2, findings as context)
                            |- rejected      -> stop, ask the user

iteration 2  plan -> exec -> review
                            |- approved      -> report to user
                            |- needs_revision -> plan (iteration 3, findings as context)
                            |- rejected      -> stop, ask the user

iteration 3  plan -> exec -> review
                            |- approved      -> report to user
                            |- needs_revision -> MAX_ITERATIONS: stop, escalate
                            |- rejected      -> stop, ask the user
```

On escalation, give the user: the objective, the plan, what actually happened (statuses and key
output), and Codex's `next_action`. Do not attempt a fourth pass on your own initiative.
