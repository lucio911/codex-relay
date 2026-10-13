---
name: codex-relay
description: Delegate planning and review to the OpenAI Codex CLI while WorkBuddy executes the commands. Use when the user asks Codex to plan/review, wants a Codex+WorkBuddy collaboration loop, says "让 codex 规划"、"codex 审查"、"codex 规划 workbuddy 执行"、"双模型协作"、"codex relay"、"规划-执行-审查闭环", or wants Codex to verify results already produced in the workspace. Enforces a strict role split where Codex never executes and WorkBuddy never plans.
---

# Codex Relay: Codex plans, WorkBuddy executes, Codex reviews

A three-phase relay loop between the **Codex CLI** (planner + reviewer, sandboxed read-only) and
**WorkBuddy** (executor — the only party allowed to touch the machine).

```
   ┌──────────┐  phase 1 PLAN   ┌───────────────────────────┐
   │  CODEX   │ ───────────────> │                           │
   │ planner  │   plan.json     │        WORKBUDDY          │
   │ reviewer │ <─────────────── │        executor           │
   └──────────┘  phase 3 REVIEW │                           │
       ▲       review.json      └───────────────────────────┘
       │                                 │
       └──── phase 2 EXEC: WorkBuddy runs the planned shell commands ────┘
```

Codex is driven non-interactively through `codex exec`. Planning and review both run under
`--sandbox read-only`, so Codex **cannot** execute anything even if it tries. Execution is
WorkBuddy's job alone.

## Role boundaries

| Party | Owns | Must never |
|---|---|---|
| **Codex** — planner | Task decomposition, command design, risk notes, acceptance criteria | Execute shell commands, edit files, claim work is done |
| **WorkBuddy** — executor | Running the commands, capturing real stdout/stderr/exit codes, reporting faithfully | Invent its own steps, silently skip or substitute commands, self-approve its own work |
| **Codex** — reviewer | Judging whether execution matches plan + acceptance criteria | Fix anything itself |
| **User** | Final say on risky steps and on accept/reject | — |

Two invariants keep the loop trustworthy:

1. **Only WorkBuddy writes.** Codex is read-only, so every mutation is an auditable shell command.
2. **WorkBuddy never self-reviews.** Verification always comes from a Codex call that sees only
   the report — never WorkBuddy's own account of what it intended.

## Phase 1 — PLAN

Confirm the goal with the user, then hand a task brief to Codex:

```bash
python scripts/codex_relay.py plan \
  --cwd "<workspace>" \
  --task "<user goal, verbatim>" \
  --context "<known constraints, file paths, prior findings>" \
  --out .codex-relay/plan.json
```

Present the result to the user as a numbered step list **before running anything**. Codex's plan
is a proposal, not an instruction — always get confirmation.

If the plan is wrong, do not hand-edit `plan.json`. Re-run `plan` with sharper `--context`.

## Phase 2 — EXEC

Run steps **in plan order**. For each step:

- Run the command as written. If you believe one is wrong or unsafe, stop and tell the user
  rather than improvising.
- Capture exit code, stdout and stderr **verbatim**. Truncate long output, never fabricate it.
- Note files created or modified.

Record each result:

```bash
python scripts/codex_relay.py report \
  --plan .codex-relay/plan.json \
  --step 1 --exit-code 0 \
  --stdout-file .codex-relay/step1.out \
  --artifacts out/a.csv out/b.csv \
  --note "3 files written under out/" \
  --out .codex-relay/execution.json
```

When all steps are recorded, seal the report:

```bash
python scripts/codex_relay.py collect --plan .codex-relay/plan.json --out .codex-relay/execution.json
```

`collect` fails loudly if any planned step lacks a recorded result — this prevents silently
dropping steps.

## Phase 3 — REVIEW

```bash
python scripts/codex_relay.py review \
  --plan .codex-relay/plan.json \
  --report .codex-relay/execution.json \
  --out .codex-relay/review.json
```

Codex resumes the planning session, so it still remembers the original plan and can compare it
against what was reported. Three possible verdicts:

| Verdict | Meaning | Your next action |
|---|---|---|
| `approved` | Plan met, evidence sufficient | Report success to the user, show artifacts |
| `needs_revision` | A specific step failed or drifted | Loop back to **Phase 1**, feeding the review findings in as `--context`. Do not patch around it |
| `rejected` | The fundamental approach is wrong | Stop, explain to the user, ask how to proceed |

On `needs_revision`, Codex must **re-plan** — never just re-run the failing command, never mark a
step complete by hand. The loop is capped at 3 iterations (`--max-iterations`); on hitting the cap,
stop and escalate to the user with the full history.

## Message format

Three JSON payloads with fixed schemas. WorkBuddy → Codex carries `task` + `context` (planning)
and the `execution_report` (review). Codex → WorkBuddy returns `plan` and `review` objects.

- **plan.json** — `objective`, `steps[]` (`id`, `description`, `command`, `cwd`, `expected_effect`),
  `acceptance_criteria[]`, `risks[]`
- **execution.json** — `results[]` mirroring step ids with `status`
  (`done`/`failed`/`skipped`/`deviated`), `exit_code`, `stdout_excerpt`, `artifacts[]`
- **review.json** — `verdict`, `summary`, `findings[]` (per-step `status` + `comment` +
  `evidence_checked`), `next_action`

Field-level detail: `references/protocol.md`. Codex's approval criteria: `references/review-rubric.md`.

## Practical notes (all verified on this machine)

- **Do not pass `--model` unless you must.** A model pinned in `~/.codex/config.toml` or in
  `--model` is often rejected by ChatGPT-account tiers with
  `400 ... model is not supported`. Here `gpt-6.1-sol` and `gpt-5.1-codex-max` both fail while the
  bare default succeeds. The script therefore sends no `-m` flag unless you pass one.
- **`--ignore-user-config` is always sent.** `~/.codex/config.toml` registers six MCP servers;
  they add ~40s per call and `@modelcontextprotocol/server-memory` fails against `chatgpt.com`,
  aborting the run. This also drops the unusable local model pin.
- **Transient TLS drops are retried** (2 retries, 5s/10s backoff). `chatgpt.com`'s websocket
  endpoint fails intermittently with `tls handshake eof`; `CODEX_TRANSPORT` after retries means
  the network, not your prompt.
- **Pass native Windows paths.** The script runs under Windows Python, so Git Bash POSIX paths
  like `/tmp/foo` resolve to a drive-root path instead. Use `C:/Users/...`.
- **Codex emits commands for the platform it detects** — on Windows you will get `.ps1`/cmd
  syntax, not bash. Run them with the PowerShell tool, not Bash.
- `review` resumes the planning session (options differ from a fresh `exec`: no `--sandbox`,
  `-C`, or `--color`), so Codex still remembers the plan. `--fresh` discards that context and is
  only for when the plan itself is being thrown away.
- Typical full loop: 3 Codex calls ≈ 2–4 minutes.

## Prerequisites

`npm i -g @openai/codex`, then `codex` must be logged in. On Windows the script launches the
`codex.cmd` shim through `cmd.exe`, since `CreateProcess` cannot exec it directly.

Run it with any Python 3.10+ interpreter — the managed runtime if you have one:

```bash
python3 scripts/codex_relay.py plan --task "..."      # macOS / Linux
python  scripts\codex_relay.py plan --task "..."      # Windows
```

## References

- `references/usage-examples.md` — **start here**: six worked end-to-end examples
- `references/protocol.md` — full JSON schemas and field semantics for all three messages
- `references/review-rubric.md` — what Codex checks before approving
