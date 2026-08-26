"""Run the current pipeline against evals/queries.yaml and score with the judge.

Examples:
  python evals/run_smoke.py
  python evals/run_baseline.py --smoke
  python evals/run_baseline.py --ids q11,q14
  python evals/run_baseline.py
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from stdio_utf8 import configure_utf8_stdio  # noqa: E402

configure_utf8_stdio()
load_dotenv(ROOT / ".env")

import yaml  # noqa: E402

from coordinator import run_deep_research  # noqa: E402
from evals.judge import SCORE_KEYS, judge_report  # noqa: E402
from token_meter import meter  # noqa: E402

QUERIES_PATH = Path(__file__).resolve().parent / "queries.yaml"
BASELINES_DIR = Path(__file__).resolve().parent / "baselines"
SMOKE_ID = "q11"


def _empty_tokens() -> dict:
    return {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "calls_without_usage": 0,
        "by_model": {},
    }


def load_queries() -> list[dict]:
    with QUERIES_PATH.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    queries = payload["queries"]
    for item in queries:
        item["query"] = " ".join(str(item["query"]).split())
        if item.get("expected") is not None:
            item["expected"] = " ".join(str(item["expected"]).split())
    return queries


def select_queries(queries: list[dict], ids: list[str] | None, limit: int | None) -> list[dict]:
    selected = queries
    if ids:
        wanted = set(ids)
        selected = [q for q in queries if q["id"] in wanted]
        missing = wanted - {q["id"] for q in selected}
        if missing:
            raise SystemExit(f"Unknown query ids: {sorted(missing)}")
    if limit is not None:
        selected = selected[:limit]
    return selected


def summarize(results: list[dict]) -> dict:
    if not results:
        return {
            "n": 0,
            "n_passed": 0,
            "pass_rate": 0.0,
            "mean": 0.0,
            "tokens_total": 0,
            "tokens_mean": 0.0,
            "tokens_by_id": {},
            "by_tag": {},
        }

    n = len(results)
    n_passed = sum(1 for r in results if r["judge"]["passed"])
    mean = sum(r["judge"]["mean"] for r in results) / n

    tokens_by_id = {
        r["id"]: int((r.get("tokens") or {}).get("total_tokens") or 0) for r in results
    }
    tokens_total = sum(tokens_by_id.values())
    tokens_mean = tokens_total / n

    tag_buckets: dict[str, list[dict]] = defaultdict(list)
    for result in results:
        for tag in result.get("tags") or ["untagged"]:
            tag_buckets[tag].append(result)

    by_tag = {}
    for tag, bucket in sorted(tag_buckets.items()):
        by_tag[tag] = {
            "n": len(bucket),
            "n_passed": sum(1 for r in bucket if r["judge"]["passed"]),
            "pass_rate": round(sum(1 for r in bucket if r["judge"]["passed"]) / len(bucket), 4),
            "mean": round(sum(r["judge"]["mean"] for r in bucket) / len(bucket), 4),
            "tokens_total": sum(
                int((r.get("tokens") or {}).get("total_tokens") or 0) for r in bucket
            ),
        }

    score_means = {
        key: round(sum(r["judge"][key] for r in results) / n, 4) for key in SCORE_KEYS
    }
    return {
        "n": n,
        "n_passed": n_passed,
        "pass_rate": round(n_passed / n, 4),
        "mean": round(mean, 4),
        "score_means": score_means,
        "tokens_total": tokens_total,
        "tokens_mean": round(tokens_mean, 1),
        "tokens_by_id": tokens_by_id,
        "by_tag": by_tag,
    }


def run(ids: list[str] | None, limit: int | None) -> Path:
    queries = select_queries(load_queries(), ids, limit)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = BASELINES_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Baseline run_id={run_id}")
    print(f"Queries: {[q['id'] for q in queries]}")
    print(f"Output: {out_dir}")
    print("This is a multi-agent research run and is expensive. Prefer --smoke (q11) or --ids.")

    results = []
    for index, query in enumerate(queries, start=1):
        query_id = query["id"]
        print(f"\n=== [{index}/{len(queries)}] {query_id} ===")
        print(query["query"][:200])
        tokens = _empty_tokens()
        try:
            as_of = query.get("as_of")
            report = run_deep_research(
                query["query"],
                interactive=False,
                as_of=str(as_of) if as_of else None,
            )
            if not isinstance(report, str):
                report = str(report)
            # Snapshot research tokens before the judge (judge also uses LiteLLM).
            tokens = meter.snapshot()
            error = None
        except Exception as exc:
            report = f"# FAILED\n\n`{type(exc).__name__}: {exc}`\n"
            error = f"{type(exc).__name__}: {exc}"
            tokens = meter.snapshot()
            print(f"Pipeline error on {query_id}: {error}")

        report_path = out_dir / f"{query_id}.md"
        report_path.write_text(report, encoding="utf-8")

        try:
            judgement = judge_report(query, report)
        except Exception as exc:
            judgement = {
                **{key: 0.0 for key in SCORE_KEYS},
                "mean": 0.0,
                "passed": False,
                "rationale": f"Judge failed: {type(exc).__name__}: {exc}",
                "model": "error",
            }

        result = {
            "id": query_id,
            "tags": query.get("tags", []),
            "as_of": query.get("as_of"),
            "error": error,
            "report_path": str(report_path.relative_to(ROOT)),
            "tokens": tokens,
            "judge": judgement,
        }
        results.append(result)
        print(
            f"  mean={judgement['mean']:.3f} passed={judgement['passed']} "
            f"tokens_total={tokens.get('total_tokens', 0)} "
            f"error={bool(error)}"
        )

    scores = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "n_requested": len(queries),
        "ids": [q["id"] for q in queries],
        "summary": summarize(results),
        "results": results,
    }
    scores_path = out_dir / "scores.json"
    scores_path.write_text(json.dumps(scores, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {scores_path}")
    print(json.dumps(scores["summary"], indent=2, ensure_ascii=False))
    print("\nTokens by query:")
    for query_id, total in scores["summary"].get("tokens_by_id", {}).items():
        print(f"  {query_id}: {total}")
    print(
        f"  batch_total={scores['summary'].get('tokens_total', 0)} "
        f"batch_mean={scores['summary'].get('tokens_mean', 0)}"
    )
    return scores_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run research evals and score a baseline.")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help=f"Run only {SMOKE_ID} (WEB lookup). Ignores --ids and --limit.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only the first N selected queries.",
    )
    parser.add_argument(
        "--ids",
        type=str,
        default=None,
        help="Comma-separated query ids, e.g. q11,q14",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.smoke:
        run(ids=[SMOKE_ID], limit=None)
        return
    ids = [item.strip() for item in args.ids.split(",") if item.strip()] if args.ids else None
    run(ids=ids, limit=args.limit)


if __name__ == "__main__":
    main()
