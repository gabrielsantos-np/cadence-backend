"""End-to-end acceptance run against a live backend.

    uv run python scripts/acceptance.py --dry-run        # what it would ask, free
    uv run python scripts/acceptance.py                  # one pass
    uv run python scripts/acceptance.py --repeat 3       # measure reliability

Six questions asked once is not evidence that an app is reliable — the analyst
is non-deterministic, and the failure that matters is the one that happens a
third of the time. `--repeat` asks each case several times and reports the pass
*rate*, which is the number worth quoting.

This costs real money and real minutes: roughly $0.35 and two minutes per case
per repetition. `scripts/tune_retrieval.py` and `tests/test_retrieval_quality.py`
cover the retrieval half for free; use this for the half that needs a model.
"""

import argparse
import asyncio
import collections
import json
import pathlib
import re
import time

import httpx

from cadence_backend.core.config import get_settings

ROOT = pathlib.Path(__file__).resolve().parent.parent
CASES = ROOT / "data" / "acceptance_cases.json"
API = "http://127.0.0.1:8000"


def _answer_text(blocks: list) -> str:
    """Everything the reader can see, flattened. Assertions run over this."""
    return json.dumps(blocks)


async def ask(client: httpx.AsyncClient, question: str) -> dict:
    r = await client.post(
        f"{API}/api/chat",
        json={"question": question},
        headers={"Origin": "http://localhost:3000"},
        timeout=420.0,
    )
    r.raise_for_status()
    steps, blocks = [], []
    for chunk in r.text.split("\n\n"):
        kind = re.search(r"^event: (\S+)", chunk, re.M)
        data = re.search(r"^data: (.*)$", chunk, re.M)
        if not (kind and data):
            continue
        payload = json.loads(data.group(1))
        if kind.group(1) == "step":
            steps.append(payload["step"])
        elif kind.group(1) == "answer":
            blocks = (payload.get("answer") or payload).get("blocks", [])
    return {"steps": steps, "blocks": blocks}


#: Conditions that mean the run never happened, rather than happened and was
#: wrong. Counting a spent rate limit as a failed assertion manufactures
#: distrust in working code — the first version of this script reported a case
#: as "67% reliable" when the app had answered correctly every time it was
#: allowed to answer at all.
INFRA = re.compile(
    r"could not complete this answer|error code: (?:402|429|5\d\d)|"
    r"in_flight_budget|rate.?limit|insufficient credit|timed? out",
    re.I,
)


def classify(case: dict, run: dict) -> tuple[str, list[str]]:
    """Return ('pass'|'fail'|'error', reasons)."""
    text = _answer_text(run["blocks"])
    if not run["blocks"]:
        return "error", ["produced no answer"]
    if INFRA.search(text):
        return "error", ["the model or gateway did not complete the turn"]
    failures = check(case, run)
    return ("pass" if not failures else "fail"), failures


def check(case: dict, run: dict) -> list[str]:
    """Return the list of assertion failures; empty means the case passed."""
    failures = []
    text = _answer_text(run["blocks"])

    for pattern in case.get("must_include", []):
        if not re.search(pattern, text, re.I):
            failures.append(f"missing /{pattern}/")
    for pattern in case.get("must_not_include", []):
        if re.search(pattern, text, re.I):
            failures.append(f"present but forbidden /{pattern}/")
    if case.get("must_search") and not any(s.get("kind") == "search" for s in run["steps"]):
        failures.append("no document search step")
    if pattern := case.get("must_sql_match"):
        sql = " ".join(s.get("sql") or "" for s in run["steps"])
        if not re.search(pattern, sql, re.I):
            failures.append(f"no SQL matching /{pattern}/")
    return failures


async def main_async(args: argparse.Namespace) -> int:
    cases = json.loads(CASES.read_text())
    if args.only:
        cases = [c for c in cases if c["id"] in args.only.split(",")]

    # A case that needs both warehouses cannot pass in single-source mode, and
    # failing it there would report a configuration as a defect. Skipped and
    # named, rather than silently dropped.
    mode = get_settings().market_source
    skipped = [c for c in cases if c.get("requires") and c["requires"] != mode]
    cases = [c for c in cases if c not in skipped]
    for c in skipped:
        print(f"  [SKIP] {c['id']:<28} needs MARKET_SOURCE={c['requires']}, running {mode}")
    if skipped:
        print()
    if args.dry_run:
        print(
            f"{len(cases)} cases x {args.repeat} repetition(s) "
            f"~ ${0.35 * len(cases) * args.repeat:.2f}, "
            f"~{2 * len(cases) * args.repeat // max(args.concurrency, 1)} min\n"
        )
        for c in cases:
            print(f"  {c['id']:<28} {c['question']}")
            print(f"  {'':<28} why: {c['why']}\n")
        return 0

    async with httpx.AsyncClient() as client:
        try:
            await client.get(f"{API}/health", timeout=10)
        except Exception:
            print(f"backend not reachable at {API} — start it with `make dev`")
            return 2

        sem = asyncio.Semaphore(args.concurrency)
        results: dict[str, list] = collections.defaultdict(list)

        async def run_one(case: dict, rep: int) -> None:
            async with sem:
                started = time.perf_counter()
                try:
                    run = await ask(client, case["question"])
                    verdict, failures = classify(case, run)
                except Exception as exc:  # noqa: BLE001 — one bad run must not abort the suite
                    verdict, failures = "error", [f"{type(exc).__name__}: {exc}"]
                    run = {"steps": [], "blocks": []}
                results[case["id"]].append(
                    {
                        "rep": rep,
                        "verdict": verdict,
                        "ok": verdict == "pass",
                        "failures": failures,
                        "steps": len(run["steps"]),
                        "seconds": round(time.perf_counter() - started, 1),
                        # Keep what a failure actually said. A run that records only
                        # that an assertion failed cannot be diagnosed afterwards,
                        # and a flake reproduces on its own schedule.
                        "answer": None if verdict == "pass" else _answer_text(run["blocks"]),
                        "queries": None
                        if verdict == "pass"
                        else [s_.get("label") for s_ in run["steps"]],
                    }
                )
                mark = {"pass": "PASS", "fail": "FAIL", "error": "ERR "}[verdict]
                print(
                    f"  [{mark}] {case['id']:<28} rep {rep + 1}  "
                    f"{results[case['id']][-1]['seconds']:>5.1f}s  {len(run['steps'])} steps"
                )
                for f in failures:
                    print(f"           {f}")

        print(f"running {len(cases)} cases x {args.repeat}\n")
        await asyncio.gather(*(run_one(c, r) for c in cases for r in range(args.repeat)))

    print(f"\n{'case':<30}{'pass rate':>11}{'graded':>8}{'errors':>8}{'median s':>10}")
    failed_any = False
    for c in cases:
        rs = results[c["id"]]
        graded = [r for r in rs if r["verdict"] != "error"]
        errors = len(rs) - len(graded)
        rate = (sum(r["ok"] for r in graded) / len(graded)) if graded else float("nan")
        med = sorted(r["seconds"] for r in rs)[len(rs) // 2]
        flag = ""
        if not graded:
            flag = "  <- NOT MEASURED, all runs errored"
        elif rate < 1:
            flag = "  <- FLAKY" if rate > 0 else "  <- FAILING"
            failed_any = True
        shown = "  n/a" if not graded else f"{rate:>10.0%}"
        print(f"{c['id']:<30}{shown:>10}{len(graded):>8}{errors:>8}{med:>10.1f}{flag}")

    total_err = sum(1 for rs in results.values() for r in rs if r["verdict"] == "error")
    if total_err:
        print(f"\n{total_err} run(s) never completed and are excluded from the rates above.")
        print("Lower --concurrency or top up credit; rates over few graded runs mean little.")

    out = ROOT / "data" / "acceptance_results.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nwritten to {out}")
    return 1 if failed_any else 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repeat", type=int, default=1)
    # Two, not four. Concurrent turns share the gateway's in-flight budget, and
    # exhausting it returns 402 mid-answer — which reads as a flaky app rather
    # than a throttled one.
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--only", default="")
    p.add_argument("--dry-run", action="store_true")
    raise SystemExit(asyncio.run(main_async(p.parse_args())))


if __name__ == "__main__":
    main()
