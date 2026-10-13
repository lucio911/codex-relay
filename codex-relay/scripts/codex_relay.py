#!/usr/bin/env python3
"""Codex Relay — Codex plans, WorkBuddy executes, Codex reviews.

Subcommands:
  plan      Ask Codex (read-only) for an execution plan.        Codex -> WorkBuddy
  report    Record one step's real execution result.            WorkBuddy -> Codex
  collect   Verify every planned step has a result; seal report.
  review    Ask Codex to judge the report against the plan.     Codex -> WorkBuddy

Codex is always invoked with --sandbox read-only so it cannot execute anything.
Requires the `codex` CLI on PATH and an authenticated session.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"
PLAN_SCHEMA = ASSETS / "plan.schema.json"
REVIEW_SCHEMA = ASSETS / "review.schema.json"

DEFAULT_MODEL = ""   # no -m flag: let codex pick a model its account can actually use
DEFAULT_MODEL_HELP = ("Codex model id. Empty (default) sends no -m flag, letting codex fall back "
                      "to a model the signed-in account supports -- safest, since a locally pinned "
                      "model is often rejected by ChatGPT-account tiers.")
MAX_ITERATIONS = 3
STDOUT_LIMIT = 4000
DEFAULT_TIMEOUT = 900

SESSION_RE = re.compile(r"session id:\s*([0-9a-fA-F-]{36})")
MODEL_ERR_RE = re.compile(r"model is not supported|model_not_supported", re.I)


class RelayError(RuntimeError):
    """Fatal problem that should stop the relay loop."""

    def __init__(self, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def render(self) -> str:
        out = f"[{self.code}] {self.message}"
        if self.detail:
            body = self.detail.strip()
            if len(body) > 2000:
                body = body[:1000] + f"\n... [{len(body) - 2000} chars elided] ...\n" + body[-1000:]
            out += f"\n--- detail ---\n{body}"
        return out


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RelayError("MISSING_FILE", f"Required file not found: {path}")
    except json.JSONDecodeError as exc:
        raise RelayError("BAD_JSON", f"{path} is not valid JSON: {exc}")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def session_file_for(plan_path: Path) -> Path:
    return plan_path.parent / "session.json"


def load_session(plan_path: Path) -> str | None:
    sf = session_file_for(plan_path)
    if not sf.exists():
        return None
    try:
        return json.loads(sf.read_text(encoding="utf-8")).get("session_id")
    except json.JSONDecodeError:
        return None


def save_session(plan_path: Path, session_id: str, plan_path_str: str) -> None:
    write_json(
        session_file_for(plan_path),
        {"session_id": session_id, "plan": plan_path_str, "updated_at": now_iso()},
    )


def validate_against_schema(payload: dict, schema_path: Path) -> list[str]:
    """Structural validation: required keys, types, enums, contiguous step ids.

    Also enforces the API's strict-mode rule that `required` covers every key in
    `properties`, so a malformed schema is caught here rather than as a 400.
    """
    schema = read_json(schema_path)
    problems: list[str] = []

    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in payload:
            problems.append(f"missing required field: {key}")
    uncovered = set(props) - set(schema.get("required", []))
    if uncovered:
        problems.append(
            f"schema defect: {sorted(uncovered)} in properties but not required "
            "(the API rejects this in strict mode)"
        )

    def type_ok(value, spec) -> bool:
        allowed = spec.get("type")
        if isinstance(allowed, list):
            allowed_types = set(allowed)
        elif isinstance(allowed, str):
            allowed_types = {allowed}
        else:
            return True
        if "null" in allowed_types and value is None:
            return True
        actual = {
            "array": list, "object": dict, "string": str,
            "integer": int, "boolean": bool, "number": (int, float),
        }
        return any(isinstance(value, actual[t]) for t in allowed_types if t in actual)

    for key, value in payload.items():
        spec = props.get(key)
        if not spec:
            continue
        if not type_ok(value, spec):
            problems.append(f"{key} has the wrong type (expected {spec.get('type')})")
        if "enum" in spec and value not in spec["enum"]:
            problems.append(f"{key}={value!r} not in {spec['enum']}")

    steps = payload.get("steps")
    if isinstance(steps, list):
        if not steps:
            problems.append("steps is empty; a plan needs at least one step")
        for i, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                problems.append(f"steps[{i}] is not an object")
                continue
            for key in ("id", "description", "command", "expected_effect"):
                if step.get(key) in (None, ""):
                    problems.append(f"steps[{i}] missing or empty '{key}'")
            if step.get("id") != i:
                problems.append(
                    f"steps[{i}].id is {step.get('id')}, expected {i} "
                    "(ids must be contiguous from 1 — WorkBuddy executes in id order)"
                )

    findings = payload.get("findings")
    if isinstance(findings, list):
        allowed_status = {"met", "not_met", "deviated", "unverifiable"}
        for i, f in enumerate(findings, start=1):
            if not isinstance(f, dict):
                problems.append(f"findings[{i}] is not an object")
                continue
            if "status" in f and f["status"] not in allowed_status:
                problems.append(f"findings[{i}].status={f['status']!r} invalid")
            if not f.get("comment"):
                problems.append(f"findings[{i}] missing 'comment'")

    return problems


def summarize_log(text: str, limit: int = 12, per_line: int = 300) -> str:
    """Keep only diagnostic lines from codex output.

    Raw codex output can contain multi-kilobyte encoded blobs that bury the one
    line that actually explains the failure, so filter to signal-bearing lines.
    """
    keep = []
    patterns = ("ERROR", "error:", "Reconnecting", "warning:", "status", "not supported",
                "Transport", "timed out", "handshake", "session id", "model:", "sandbox:")
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if any(p in line for p in patterns):
            keep.append(line[:per_line])
        if len(keep) >= limit:
            break
    if not keep:
        keep = [l.strip()[:per_line] for l in (text or "").splitlines() if l.strip()][:limit]
    return "\n".join(keep)


def resolve_codex() -> list[str]:
    """Return an argv prefix that launches codex on this platform.

    On Windows the npm shim is codex.cmd, which CreateProcess cannot exec directly,
    so it must be launched through cmd.exe. shutil.which resolves .CMD via PATHEXT,
    which is why the suffix check matters.
    """
    if os.name == "nt":
        found = shutil.which("codex")
        if not found:
            raise RelayError(
                "CODEX_NOT_FOUND",
                "`codex` is not on PATH.",
                "Install with: npm i -g @openai/codex",
            )
        if found.lower().endswith((".cmd", ".bat")):
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            return [comspec, "/c", found]
        return [found]

    found = shutil.which("codex")
    if not found:
        raise RelayError(
            "CODEX_NOT_FOUND",
            "`codex` is not on PATH.",
            "Install with: npm i -g @openai/codex",
        )
    return [found]


def run_codex(prompt: str, schema_path: Path, cwd: str, resume: str | None,
              model: str, timeout: int, retries: int = 2) -> tuple[dict, str | None, str]:
    """Invoke codex read-only and return (payload, session_id, raw_last_message).

    Transient network/TLS failures are retried; a rejected model or a malformed
    reply is not, since retrying cannot fix either.
    """
    launcher = resolve_codex()

    with tempfile.TemporaryDirectory() as td:
        out_file = Path(td) / "last_message.txt"

        # Flags accepted by both `codex exec` and `codex exec resume`.
        shared = [
            "--skip-git-repo-check",
            "--ignore-user-config",   # skips MCP servers + unusable local model
            "--output-schema", str(schema_path),
        ]
        # `resume` has its own parser and rejects --sandbox / -C / --color, so these
        # only go on a fresh session. The resumed session keeps its original sandbox.
        fresh_only = ["--sandbox", "read-only", "-C", cwd, "--color", "never"]

        if resume:
            cmd = launcher + ["exec", "resume", resume] + shared
        else:
            cmd = launcher + ["exec"] + fresh_only + shared
        if model:
            cmd += ["-m", model]
        cmd += ["-o", str(out_file), "-"]

        combined = ""
        for attempt in range(retries + 1):
            try:
                proc = subprocess.run(
                    cmd, input=prompt, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                raise RelayError(
                    "CODEX_TIMEOUT",
                    f"Codex did not respond within {timeout}s.",
                    "Raise --timeout, or simplify the task brief.",
                )
            except OSError as exc:
                raise RelayError("CODEX_SPAWN_FAILED", f"Could not launch codex: {exc}")

            raw = out_file.read_text(encoding="utf-8", errors="replace") if out_file.exists() else ""
            stderr = proc.stderr or ""
            combined = summarize_log(stderr + "\n" + (proc.stdout or ""))

            transient = ("Transport channel closed" in combined
                         or "handshake" in combined.lower()
                         or "Reconnecting" in combined
                         or "backend-api" in combined)

            if proc.returncode == 0 and raw.strip():
                break

            # Non-transient failures: fail now, retrying cannot help.
            if MODEL_ERR_RE.search(combined) or MODEL_ERR_RE.search(raw[:2000]):
                raise RelayError(
                    "MODEL_REJECTED",
                    "Codex account rejected the requested model."
                    + (f" (model='{model}')" if model else " (codex default)"),
                    combined + "\n\nFix: pass --model <id> for a model your account supports, "
                              "or omit --model to let codex choose.",
                )
            if not transient:
                break
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            raise RelayError(
                "CODEX_TRANSPORT",
                f"Codex could not reach its backend after {retries + 1} attempts.",
                combined + "\n\nThis is usually a transient TLS/websocket drop to "
                          "chatgpt.com/backend-api. Retry; if it persists, check the network.",
            )

        session_id = None
        full_log = (proc.stderr or "") + "\n" + (proc.stdout or "")
        match = SESSION_RE.search(full_log)
        if match:
            session_id = match.group(1)

        if not raw.strip():
            raise RelayError(
                "CODEX_FAILED",
                f"Codex exited {proc.returncode} with no usable output.",
                combined,
            )

        if raw.lstrip().startswith("<"):
            raise RelayError(
                "CODEX_REFUSED",
                "Codex returned prose instead of JSON — the schema was not honoured.",
                summarize_log(raw),
            )

        try:
            return json.loads(raw), session_id, raw
        except json.JSONDecodeError as exc:
            if MODEL_ERR_RE.search(raw[:2000]):
                raise RelayError("MODEL_REJECTED", "Codex account rejected the requested model.", summarize_log(raw))
            raise RelayError("CODEX_BAD_JSON", f"Could not parse Codex output as JSON: {exc}",
                             summarize_log(raw))


def cmd_plan(args: argparse.Namespace) -> int:
    if not Path(args.cwd).is_dir():
        raise RelayError("BAD_CWD", f"--cwd is not a directory: {args.cwd}")
    out = Path(args.out)
    prior = load_session(out) if not args.fresh else None
    iteration = args.iteration
    max_it = args.max_iterations
    if iteration > max_it:
        raise RelayError(
            "MAX_ITERATIONS",
            f"Iteration {iteration} exceeds the cap of {max_it}. Escalate to the user.",
            "Do not keep looping automatically — report the history and ask how to proceed.",
        )

    prompt = f"""You are the PLANNER in a plan-execute-review relay. You do NOT execute
anything — a separate executor (WorkBuddy) will run the commands you produce.

CONTEXT: you run sandboxed read-only. Do not attempt to run commands or edit files.
Your only output is a JSON plan matching the supplied schema.

The executor is literal-minded: it runs the commands you give, in the order you give
them, and reports back exactly what happened. So every command must be concrete,
runnable as-is, and free of placeholders.

TASK (from the user):
{args.task}
"""

    if args.context:
        prompt += f"""
ADDITIONAL CONTEXT (from WorkBuddy):
{args.context}
"""

    if iteration > 1:
        prompt += f"""
REVISION ROUND {iteration} of {max_it}: your previous plan was reviewed and REJECTED.
Findings from the reviewer:
{args.context or '(see above)'}
Produce a corrected plan. Do not simply repeat the failing step — change the approach,
or remove the step if it is not actually required.
"""

    prompt += f"""
Produce a plan with these qualities:
- Minimal steps that achieve the objective. No exploratory or "just in case" steps.
- Each command must run unattended and terminate.
- Put destructive or hard-to-reverse operations behind requires_confirmation: true.
- acceptance_criteria must be objectively checkable from command output alone,
  because the reviewer only ever sees captured stdout/stderr and exit codes.
- If the task is ambiguous, state the ambiguity in `assumptions` and plan for the most
  likely reading. Do not ask questions — you cannot receive an answer.

Output only the JSON object."""

    payload, session_id, _ = run_codex(
        prompt, PLAN_SCHEMA, args.cwd, prior, args.model, args.timeout
    )

    problems = validate_against_schema(payload, PLAN_SCHEMA)
    if problems:
        raise RelayError(
            "PLAN_SCHEMA_VIOLATION",
            "Codex's plan does not satisfy the schema.",
            "\n".join(f"- {p}" for p in problems),
        )

    payload["_relay"] = {
        "phase": "plan",
        "created_at": now_iso(),
        "cwd": args.cwd,
        "iteration": iteration,
        "session_id": session_id,
        "task": args.task,
        "planner": "codex (read-only)",
    }
    write_json(out, payload)
    if session_id:
        save_session(out, session_id, str(out))

    print(f"Plan written to {out}")
    print(f"Objective: {payload.get('objective')}")
    print(f"Session: {session_id or '(none — review will start fresh)'}")
    print(f"Steps: {len(payload.get('steps', []))}  |  "
          f"risks: {len(payload.get('risks', []))}  |  iteration: {iteration}/{max_it}")
    print("\n--- STEPS (present these to the user for confirmation) ---")
    for step in payload.get("steps", []):
        flag = "  [CONFIRM REQUIRED]" if step.get("requires_confirmation") else ""
        print(f"  {step.get('id')}. {step.get('description')}{flag}")
        print(f"     $ {step.get('command')}")
        print(f"     expect: {step.get('expected_effect')}")
    print("\n--- ACCEPTANCE CRITERIA ---")
    for c in payload.get("acceptance_criteria", []):
        print(f"  - {c}")
    if payload.get("risks"):
        print("\n--- RISKS ---")
        for r in payload["risks"]:
            print(f"  ! {r.get('risk')}\n    mitigation: {r.get('mitigation')}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    plan = read_json(Path(args.plan))
    out = Path(args.out)
    plan_steps = plan.get("steps", [])
    valid_ids = {s.get("id") for s in plan_steps}
    if args.step not in valid_ids:
        raise RelayError(
            "UNKNOWN_STEP",
            f"--step {args.step} is not in the plan.",
            f"Planned step ids: {sorted(i for i in valid_ids if isinstance(i, int))}",
        )

    stdout_excerpt = ""
    if args.stdout_file:
        p = Path(args.stdout_file)
        if p.exists():
            raw_out = p.read_text(encoding="utf-8", errors="replace")
            stdout_excerpt = raw_out[:STDOUT_LIMIT]
            if len(raw_out) > STDOUT_LIMIT:
                stdout_excerpt += f"\n... [truncated, {len(raw_out)} chars total]"

    out = Path(args.out)
    execution = read_json(out) if out.exists() else {}

    results = execution.get("results", [])
    record = {
        "step_id": args.step,
        "status": args.status,
        "exit_code": args.exit_code,
        "stdout_excerpt": stdout_excerpt,
        "artifacts": args.artifacts or [],
        "note": args.note or "",
        "recorded_at": now_iso(),
    }
    results = [r for r in results if r.get("step_id") != args.step]
    results.append(record)
    results.sort(key=lambda r: r.get("step_id", 0))

    execution["results"] = results
    execution["objective"] = plan.get("objective")
    execution["_relay"] = {
        "phase": "execution",
        "cwd": args.cwd or plan.get("_relay", {}).get("cwd"),
        "plan_file": str(args.plan),
        "iteration": plan.get("_relay", {}).get("iteration", 1),
        "session_id": plan.get("_relay", {}).get("session_id"),
        "updated_at": now_iso(),
    }
    execution.setdefault("acceptance_criteria", plan.get("acceptance_criteria", []))
    write_json(out, execution)

    print(f"Recorded step {args.step} ({args.status}, exit={args.exit_code}) -> {out}")
    print(f"Progress: {len(results)}/{len(plan_steps)} steps recorded.")
    remaining = sorted(valid_ids - {r.get("step_id") for r in results})
    if remaining:
        print(f"Still to record: {remaining}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    plan = read_json(Path(args.plan))
    out = Path(args.out)
    execution = read_json(out) if out.exists() else {}
    plan_steps = plan.get("steps", [])
    plan_ids = [s.get("id") for s in plan_steps]
    recorded = {r.get("step_id") for r in execution.get("results", [])}

    missing = [i for i in plan_ids if i not in recorded]
    extra = sorted(i for i in recorded if i not in plan_ids)

    if extra:
        raise RelayError(
            "EXTRA_RESULTS",
            f"Report contains results for steps not in the plan: {extra}",
            "Codex must not be asked to review undeclared work.",
        )
    if missing:
        raise RelayError(
            "INCOMPLETE_EXECUTION",
            f"No recorded result for step(s) {missing} of {len(plan_ids)}.",
            "Record every step (use --status skipped with a --note if it was intentionally skipped) "
            "before requesting review. Silently dropping a step is a protocol violation.",
        )

    failed = [r["step_id"] for r in execution["results"] if r.get("status") in ("failed", "deviated")]
    execution["_relay"]["sealed_at"] = now_iso()
    execution["_relay"]["complete"] = True
    execution["_relay"]["failed_steps"] = failed
    write_json(out, execution)

    print(f"Report sealed: {out}")
    print(f"All {len(plan_ids)} steps accounted for.")
    if failed:
        print(f"Steps reporting problems: {failed}")
        print("Note: sealing does not mean success — send it to review anyway and let Codex judge.")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    plan = read_json(Path(args.plan))
    report = read_json(Path(args.report))

    results = report.get("results", [])
    if not report.get("_relay", {}).get("complete"):
        raise RelayError(
            "REPORT_NOT_SEALED",
            "Execution report was never sealed by `collect`.",
            "Run: codex_relay.py collect --plan ... --out ...  before requesting review.",
        )

    lines = []
    for step in plan.get("steps", []):
        match = next((r for r in results if r.get("step_id") == step.get("id")), None)
        lines.append(f"\n### Step {step.get('id')}: {step.get('description')}")
        lines.append(f"PLANNED COMMAND : {step.get('command')}")
        lines.append(f"EXPECTED EFFECT : {step.get('expected_effect')}")
        if match is None:
            lines.append("REPORTED        : (missing)")
            continue
        lines.append(f"REPORTED STATUS : {match.get('status')}")
        lines.append(f"EXIT CODE       : {match.get('exit_code')}")
        if match.get("note"):
            lines.append(f"NOTE            : {match.get('note')}")
        if match.get("artifacts"):
            lines.append(f"ARTIFACTS       : {', '.join(match['artifacts'])}")
        excerpt = (match.get("stdout_excerpt") or "").strip()
        lines.append("CAPTURED OUTPUT :")
        lines.append(excerpt if excerpt else "  (no output captured)")

    prompt = f"""You are the REVIEWER in a plan-execute-review relay. You did not execute
anything and you cannot execute anything now (you are sandboxed read-only). Judge only
the evidence below.

Be strict and evidence-based:
- A step is `met` only if the captured output actually demonstrates the expected effect.
  Exit code 0 alone is NOT sufficient evidence.
- Use `unverifiable` when the evidence is genuinely insufficient, rather than assuming success.
- Use `deviated` when the executor did something other than what was planned.
- Check every acceptance criterion explicitly in `acceptance_check`.

You must NOT propose to fix anything yourself. Your job is to judge and, if the work
falls short, to state precisely what must change so the planner can re-plan.

ORIGINAL OBJECTIVE:
{plan.get('objective')}

ACCEPTANCE CRITERIA FROM THE PLAN:
{chr(10).join(f'- {c}' for c in plan.get('acceptance_criteria', []))}

PLAN vs REPORT:
{chr(10).join(lines)}

Output only the JSON object."""

    session_id = None if args.fresh else (load_session(Path(args.plan)) or plan.get("_relay", {}).get("session_id"))
    payload, new_session, _ = run_codex(
        prompt, REVIEW_SCHEMA, args.cwd or plan.get("_relay", {}).get("cwd", "."),
        session_id, args.model, args.timeout,
    )

    problems = validate_against_schema(payload, REVIEW_SCHEMA)
    if problems:
        raise RelayError("REVIEW_SCHEMA_VIOLATION", "Codex's review does not satisfy the schema.",
                         "\n".join(f"- {p}" for p in problems))

    payload["_relay"] = {
        "phase": "review",
        "reviewed_at": now_iso(),
        "session_id": new_session or session_id,
        "plan_file": str(args.plan),
        "report_file": str(args.report),
    }
    out = Path(args.out)
    write_json(out, payload)

    verdict = payload.get("verdict")
    print(f"Review written to {out}")
    print(f"VERDICT: {verdict}")
    print(f"Summary : {payload.get('summary')}")
    print("\n--- FINDINGS ---")
    for f in payload.get("findings", []):
        print(f"  step {f.get('step_id')}: {f.get('status')}")
        print(f"    {f.get('comment')}")
        if f.get("evidence_checked"):
            print(f"    evidence: {f['evidence_checked']}")
    print(f"\nNext action: {payload.get('next_action')}")

    if verdict == "approved":
        print("\n==> approved: report success to the user and show the artifacts.")
    elif verdict == "needs_revision":
        it = plan.get("_relay", {}).get("iteration", 1)
        print(f"\n==> needs_revision: re-run `plan` with --iteration {it + 1} "
              f"(cap {args.max_iterations}) feeding these findings back as --context.")
        print("    Do NOT re-run the failing command by hand and do NOT patch around it.")
    else:
        print("\n==> rejected: stop the loop and ask the user how to proceed.")

    return 0 if verdict == "approved" else 3


def main() -> int:
    ap = argparse.ArgumentParser(prog="codex_relay.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--model", default=DEFAULT_MODEL, help=DEFAULT_MODEL_HELP)
        p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Per-call timeout seconds")
        p.add_argument("--fresh", action="store_true", help="Ignore the saved session id")
        p.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS)

    p_plan = sub.add_parser("plan", help="Ask Codex for an execution plan")
    p_plan.add_argument("--task", required=True, help="The user's goal, verbatim")
    p_plan.add_argument("--context", default="", help="Constraints, paths, prior findings")
    p_plan.add_argument("--cwd", default=".", help="Workspace root Codex inspects (default: cwd)")
    p_plan.add_argument("--out", default=".codex-relay/plan.json")
    p_plan.add_argument("--iteration", type=int, default=1)
    common(p_plan)
    p_plan.set_defaults(func=cmd_plan)

    p_rep = sub.add_parser("report", help="Record one step's execution result")
    p_rep.add_argument("--plan", required=True)
    p_rep.add_argument("--step", type=int, required=True)
    p_rep.add_argument("--exit-code", type=int, default=None)
    p_rep.add_argument("--status", default="done",
                       choices=["done", "failed", "skipped", "deviated"])
    p_rep.add_argument("--stdout-file", default=None, help="File holding the step's captured output")
    p_rep.add_argument("--artifacts", nargs="*", default=[], help="Files/dirs created or modified")
    p_rep.add_argument("--note", default="", help="What actually happened")
    p_rep.add_argument("--cwd", default=None)
    p_rep.add_argument("--out", default=".codex-relay/execution.json")
    p_rep.set_defaults(func=cmd_report)

    p_col = sub.add_parser("collect", help="Verify completeness and seal the report")
    p_col.add_argument("--plan", required=True)
    p_col.add_argument("--out", default=".codex-relay/execution.json")
    p_col.set_defaults(func=cmd_collect)

    p_rev = sub.add_parser("review", help="Ask Codex to judge the report")
    p_rev.add_argument("--plan", required=True)
    p_rev.add_argument("--report", required=True)
    p_rev.add_argument("--out", default=".codex-relay/review.json")
    p_rev.add_argument("--cwd", default=None)
    common(p_rev)
    p_rev.set_defaults(func=cmd_review)

    args = ap.parse_args()
    if getattr(args, "model", None) == "":
        args.model = None
    try:
        return args.func(args)
    except RelayError as exc:
        print(exc.render(), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
