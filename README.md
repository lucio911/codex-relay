# codex-relay

**Codex plans. WorkBuddy executes. Codex reviews.**

A WorkBuddy skill that sets up a strict three-phase relay loop between the OpenAI
Codex CLI and WorkBuddy, so that planning and review come from one model while all
machine-touching work happens in one auditable place.

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

## Why

An AI that plans a task and then executes it tends to grade its own homework. Splitting the
roles removes that failure mode:

- **Codex runs `--sandbox read-only`**, so it *cannot* execute anything even if it tries.
- **WorkBuddy never reviews its own work.** Verification always comes from a Codex call that
  sees only the captured evidence — not the executor's account of what it intended.

## Install

Copy the `codex-relay/` directory into your skills folder:

```powershell
# user-level, all projects
Copy-Item -Recurse codex-relay "$env:USERPROFILE\.workbuddy\skills\"
```

Requires the Codex CLI, logged in:

```bash
npm i -g @openai/codex
codex          # sign in once
```

## Use

From WorkBuddy, just ask:

> 用 codex-relay 帮我把这个 CSV 转成 parquet 并校验行数

WorkBuddy will call Codex for a plan, **show you the plan for confirmation**, execute it,
report the real results, and send them back to Codex for review.

To drive the three phases yourself:

```bash
PY="C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe"
RELAY="scripts/codex_relay.py"

# 1. PLAN — Codex (read-only) writes a plan
$PY $RELAY plan --cwd . --task "..." --context "..." --out .codex-relay/plan.json

# 2. EXEC — run each step, then record what actually happened
$PY $RELAY report --plan .codex-relay/plan.json --step 1 --exit-code 0 \
     --stdout-file step1.out --artifacts out.csv --note "..." \
     --out .codex-relay/execution.json

#    seal the report; refuses to proceed if a step is unaccounted for
$PY $RELAY collect --plan .codex-relay/plan.json --out .codex-relay/execution.json

# 3. REVIEW — Codex judges the evidence against the plan
$PY $RELAY review --plan .codex-relay/plan.json --report .codex-relay/execution.json \
     --out .codex-relay/review.json --cwd .
```

Full worked examples: [`references/usage-examples.md`](references/usage-examples.md)

## The three messages

| File | Direction | Purpose |
|---|---|---|
| `plan.json` | Codex → WorkBuddy | steps, commands, acceptance criteria, risks |
| `execution.json` | WorkBuddy → Codex | per-step status, exit codes, captured output |
| `review.json` | Codex → WorkBuddy | `approved` / `needs_revision` / `rejected` |

Shapes are pinned by JSON Schema and passed to Codex via `--output-schema`, so a
non-conforming reply fails loudly instead of silently corrupting the loop.

The honesty contract for `execution.json`:

> **Exit code 0 is not proof.** If the expected effect was not observed, record
> `failed` or `deviated`, and say what you actually saw in `--note`. The reviewer only
> ever sees captured output — it cannot check your machine, so your note is the evidence.

## Verdict handling

| Verdict | Exit code | What to do |
|---|---|---|
| `approved` | 0 | Report success, show artifacts |
| `needs_revision` | 3 | Re-plan with `--iteration N+1`, feeding findings back as `--context`. **Do not** re-run the failing command by hand |
| `rejected` | 3 | Stop. The approach itself is wrong — ask the user |

Capped at 3 iterations (`--max-iterations`); on overflow the script raises
`MAX_ITERATIONS` and you escalate to the user rather than looping again.

## Guard rails

`collect` refuses to seal a report with unaccounted-for steps (`INCOMPLETE_EXECUTION`)
or steps not in the plan (`EXTRA_RESULTS`). `review` refuses an unsealed report
(`REPORT_NOT_SEALED`). These exist to stop a step being quietly dropped — the failure
mode that makes agent-run pipelines report success on work that never happened.

## Notes from real use

Verified on Windows + Codex CLI 0.153.4:

- **Don't pass `--model` unless you must.** A model pinned in `~/.codex/config.toml` is
  often rejected by ChatGPT-account tiers (`400 ... model is not supported`). Sending no
  `-m` flag lets codex fall back to one the account can actually use.
- **`--ignore-user-config` is always sent.** User config registers MCP servers that add
  ~40s per call and can abort the run outright.
- **Transient TLS drops to `chatgpt.com` are retried** (2 retries, 5s/10s backoff).
- **Pass native Windows paths.** The script runs under Windows Python, so Git Bash POSIX
  paths like `/tmp/x` resolve to a drive-root path instead.
- **Codex emits commands for the platform it detects** — expect `.ps1`/cmd syntax on
  Windows, not bash.

## Layout

```
codex-relay/
├── SKILL.md
├── scripts/codex_relay.py         # plan / report / collect / review
├── assets/plan.schema.json        # message contract: Codex → WorkBuddy
├── assets/review.schema.json      # message contract: Codex → WorkBuddy
└── references/
    ├── usage-examples.md          # six worked end-to-end examples
    ├── protocol.md                # full field-level message spec
    └── review-rubric.md           # what Codex checks before approving
```
