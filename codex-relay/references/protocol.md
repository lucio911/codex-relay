# Relay protocol

Three JSON messages, one direction each. Schemas live in `../assets/plan.schema.json` and
`../assets/review.schema.json` and are passed to Codex via `--output-schema`, so a non-conforming
reply is a hard failure rather than a silent surprise.

Every file carries a `_relay` block for traceability. Codex never writes `_relay`; the script does.

---

## 1. PLAN — Codex → WorkBuddy

Direction: Codex (read-only) → WorkBuddy.
Produced by: `codex_relay.py plan`

```json
{
  "objective": "One sentence restating the goal as Codex understood it.",
  "assumptions": ["What the brief did not state but the plan depends on."],
  "steps": [
    {
      "id": 1,
      "description": "What this step accomplishes, in plain language.",
      "command": "exact shell command, no placeholders",
      "cwd": "relative to the plan's --cwd, may be omitted",
      "expected_effect": "Observable change that proves the step worked.",
      "requires_confirmation": false
    }
  ],
  "acceptance_criteria": ["Objectively checkable from captured output alone."],
  "risks": [{ "risk": "...", "mitigation": "..." }],
  "_relay": {
    "phase": "plan",
    "created_at": "2026-10-13T14:52:01+08:00",
    "cwd": "/path/to/workspace",
    "iteration": 1,
    "session_id": "01a13408-aeb2-...",
    "task": "The original user goal, verbatim",
    "planner": "codex (read-only)"
  }
}
```

Field notes:

- `steps[].id` must be contiguous from 1. The validator rejects gaps — WorkBuddy executes in id
  order, so a gap would silently drop a step.
- `requires_confirmation: true` marks destructive or hard-to-reverse steps. **WorkBuddy must ask
  the user before running these**, regardless of how confident the plan looks.
- `acceptance_criteria` is the reviewer's contract. Criteria that cannot be checked from captured
  stdout/stderr and exit codes are useless — the reviewer never sees anything else.
- `session_id` is what lets `review` resume the planning session, so Codex remembers the plan
  without it being re-sent in full.

---

## 2a. EXECUTION REPORT — WorkBuddy → Codex

Direction: WorkBuddy → Codex.
Produced by: `codex_relay.py report` (one call per step) then sealed by `collect`.

```json
{
  "objective": "Copied from the plan.",
  "acceptance_criteria": ["Copied from the plan."],
  "results": [
    {
      "step_id": 1,
      "status": "done",
      "exit_code": 0,
      "stdout_excerpt": "verbatim captured output, truncated at 4000 chars",
      "artifacts": ["hello.txt"],
      "note": "What actually happened, in plain language.",
      "recorded_at": "2026-10-13T14:53:20+08:00"
    }
  ],
  "_relay": {
    "phase": "execution",
    "cwd": "/path/to/workspace",
    "plan_file": ".codex-relay/plan.json",
    "iteration": 1,
    "session_id": "01a13408-aeb2-...",
    "updated_at": "...",
    "sealed_at": "...",
    "complete": true,
    "failed_steps": []
  }
}
```

`status` values — this is the honesty contract:

| status | Use when |
|---|---|
| `done` | Ran, exit code as expected, effect observed |
| `failed` | Ran, non-zero exit or wrong effect. **Report it as failed** |
| `skipped` | Deliberately not run. `--note` must say why |
| `deviated` | Ran something other than the planned command |

`collect` refuses to seal unless every planned `id` has a result and no result references an
undeclared step. It reports `failed_steps` but still seals — judging is Codex's job, not the
script's.

**The rule that makes review meaningful:** exit code 0 is not proof. If the expected effect was
not observed, the status is `failed` or `deviated` regardless of the exit code.

---

## 2b. REPORT REQUEST — WorkBuddy → Codex

Not a file. `codex_relay.py review` renders plan and report into a single prompt, diffing each
planned command against its reported result:

```
### Step 1: <description>
PLANNED COMMAND : <command>
EXPECTED EFFECT : <expected_effect>
REPORTED STATUS : <status>
EXIT CODE       : <exit_code>
NOTE            : <note>
ARTIFACTS       : <artifacts>
CAPTURED OUTPUT :
  <stdout_excerpt, or "(no output captured)">
```

Steps absent from the report are rendered as `REPORTED: (missing)` rather than omitted, so
Codex sees the gap explicitly.

---

## 3. REVIEW — Codex → WorkBuddy

Direction: Codex (read-only) → WorkBuddy.
Produced by: `codex_relay.py review`

```json
{
  "verdict": "approved | needs_revision | rejected",
  "summary": "2-4 sentences on what the evidence shows.",
  "findings": [
    {
      "step_id": 1,
      "status": "met | not_met | deviated | unverifiable",
      "comment": "Why this status.",
      "evidence_checked": "Which part of the report supports the finding."
    }
  ],
  "acceptance_check": [
    { "criterion": "...", "satisfied": true, "reason": "..." }
  ],
  "next_action": "On needs_revision: the specific corrective step. On approved: what to look at.",
  "_relay": {
    "phase": "review",
    "reviewed_at": "...",
    "session_id": "...",
    "plan_file": "...",
    "report_file": "..."
  }
}
```

Process exit code: `0` when `approved`, `3` when `needs_revision` or `rejected`, `2` on any
`RelayError`. This lets a driver script branch without parsing stdout.

---

## Error codes

| Code | Meaning | Response |
|---|---|---|
| `MODEL_REJECTED` | Account cannot use the configured model | Retry with `--model <id>`, or `--model ''` for codex's own default |
| `CODEX_TIMEOUT` | No response within `--timeout` (default 900s) | Raise timeout or shrink the brief |
| `CODEX_TRANSPORT` | Backend unreachable, often a dead MCP server | Retry; the script already sends `--ignore-user-config` |
| `CODEX_REFUSED` | Codex returned prose instead of JSON | Re-run; usually a transient schema miss |
| `CODEX_BAD_JSON` | Unparseable output | Inspect the detail block |
| `PLAN_SCHEMA_VIOLATION` | Plan missing required fields | Re-run `plan` with clearer `--context` |
| `INCOMPLETE_EXECUTION` | A planned step has no result | Record it, or `--status skipped` with a note |
| `EXTRA_RESULTS` | Result references an undeclared step | Re-plan — Codex must not review undeclared work |
| `MAX_ITERATIONS` | Revision cap hit | Stop and escalate to the user |

## Session storage

`<plan-dir>/session.json` holds `session_id`, `plan`, `updated_at`. `review` resumes from it so
Codex keeps the plan in context. `--fresh` ignores it, which is correct only when the plan itself
is being discarded.
