"""Eval harness: measure retrieval Recall@K and answer faithfulness.

Reads `rag/eval/eval_set.yaml` (gitignored — copy from `eval_set.example.yaml`)
and runs each item through the full pipeline.

Metrics
-------
- recall@k        : fraction of items where the expected file/company/person
                    appeared in top-K
- file_match_rate : fraction of items where ALL expected_files matched
- contains_rate   : must_contain string match rate
- not_contains_rate: must_not_contain compliance rate
- avg_latency_s   : mean elapsed seconds per query

Run:
    uv run python -m rag.eval.run_eval
    uv run python -m rag.eval.run_eval --no-generate     # retrieval-only
    uv run python -m rag.eval.run_eval --reranker-on
    uv run python -m rag.eval.run_eval --output rag/eval/results/<run>.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@dataclass
class ItemResult:
    id: str
    category: str
    query: str
    skipped: bool = False
    elapsed_s: float = 0.0
    fused_count: int = 0
    distinct_files: int = 0
    answer: str | None = None
    file_hits: int = 0
    file_expected: int = 0
    company_hits: int = 0
    company_expected: int = 0
    person_hits: int = 0
    person_expected: int = 0
    must_contain_pass: bool = True
    must_not_contain_pass: bool = True
    notes: list[str] = field(default_factory=list)


def _load_eval_set(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(
            f"Eval set not found: {path}\n"
            f"Copy rag/eval/eval_set.example.yaml to rag/eval/eval_set.yaml and fill it in."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("items", [])


def _file_match(expected: str, actual_paths: list[str]) -> bool:
    """Substring match against vault_relative paths."""
    needle = expected.lower()
    return any(needle in (p or "").lower() for p in actual_paths)


def _evaluate_item(
    item: dict[str, Any],
    *,
    use_reranker: bool,
    do_generate: bool,
) -> ItemResult:
    from rag.config import get_settings
    from rag.generation.factory import build_client
    from rag.generation.prompts import SYSTEM_PROMPT_KO, build_user_message
    from rag.retrieval.citations import build_citations
    from rag.retrieval.hybrid_search import hybrid_search
    from rag.retrieval.query_analyzer import analyze_query
    from rag.store.bm25_index import open_bm25_index
    from rag.store.lancedb_store import open_store

    res = ItemResult(
        id=str(item.get("id") or "?"),
        category=str(item.get("category") or "?"),
        query=str(item.get("query") or ""),
    )
    if item.get("skip"):
        res.skipped = True
        return res

    t0 = time.perf_counter()
    s = get_settings()
    cfg = s.retrieval.model_copy(update={"reranker_enabled": use_reranker})

    analysis = analyze_query(res.query)
    result = hybrid_search(
        query=res.query, analysis=analysis,
        store=open_store(), bm25=open_bm25_index(),
        settings=cfg,
    )
    citations = build_citations(result.rows)

    res.fused_count = result.fused_count
    res.distinct_files = result.distinct_files
    actual_paths = [c.vault_relative for c in citations]
    actual_companies = {c.company for c in citations if c.company}
    actual_persons = {c.person for c in citations if c.person}

    expected_files = item.get("expected_files") or []
    res.file_expected = len(expected_files)
    res.file_hits = sum(1 for f in expected_files if _file_match(f, actual_paths))

    expected_companies = item.get("expected_companies") or []
    res.company_expected = len(expected_companies)
    res.company_hits = sum(1 for c in expected_companies if c in actual_companies)

    expected_persons = item.get("expected_persons") or []
    res.person_expected = len(expected_persons)
    res.person_hits = sum(1 for p in expected_persons if p in actual_persons)

    if do_generate and citations:
        # Route through the factory so eval exercises whatever provider the app
        # is configured for. Hard-wiring ClaudeClient meant that after the
        # switch to Gemini every generation failed on an exhausted Anthropic
        # balance — and because the failure was recorded as a per-item note
        # rather than raised, the run still reported a summary, making it look
        # like a retrieval regression.
        client = build_client()
        user_msg = build_user_message(res.query, citations)
        try:
            gen = client.stream(SYSTEM_PROMPT_KO, user_msg, max_tokens=1024)
            res.answer = gen.text
        except Exception as e:
            res.notes.append(f"generation error: {e!r}")
            res.answer = None

    answer_lower = (res.answer or "").lower()
    must_contain = [s.lower() for s in (item.get("must_contain") or [])]
    must_not_contain = [s.lower() for s in (item.get("must_not_contain") or [])]
    # `must_contain_any`: list of OR-groups. Each group is a list of acceptable
    # variants (e.g. [["마이크로", "Micro"], ["디스플레이", "OLEDos"]]). Passes if
    # at least one variant in EACH group is in the answer. Robust to
    # terminology choices the LLM makes.
    must_contain_any = item.get("must_contain_any") or []
    if must_contain:
        res.must_contain_pass = all(s in answer_lower for s in must_contain)
    if must_contain_any and res.must_contain_pass:
        for group in must_contain_any:
            variants = [str(v).lower() for v in (group or [])]
            if variants and not any(v in answer_lower for v in variants):
                res.must_contain_pass = False
                break
    if must_not_contain:
        res.must_not_contain_pass = not any(s in answer_lower for s in must_not_contain)

    res.elapsed_s = time.perf_counter() - t0
    return res


def _summarize(results: list[ItemResult]) -> dict[str, Any]:
    active = [r for r in results if not r.skipped]
    n = max(len(active), 1)
    file_recall = sum(1 for r in active if r.file_expected and r.file_hits == r.file_expected) / n
    company_recall = sum(1 for r in active if r.company_expected and r.company_hits == r.company_expected) / n
    person_recall = sum(1 for r in active if r.person_expected and r.person_hits == r.person_expected) / n
    must_contain_rate = sum(1 for r in active if r.must_contain_pass) / n
    must_not_rate = sum(1 for r in active if r.must_not_contain_pass) / n
    avg_latency = sum(r.elapsed_s for r in active) / n
    cats = Counter(r.category for r in active)

    return {
        "total": len(results),
        "evaluated": len(active),
        "skipped": len(results) - len(active),
        "categories": dict(cats),
        "file_match_rate": round(file_recall, 3),
        "company_match_rate": round(company_recall, 3),
        "person_match_rate": round(person_recall, 3),
        "must_contain_rate": round(must_contain_rate, 3),
        "must_not_contain_rate": round(must_not_rate, 3),
        "avg_latency_seconds": round(avg_latency, 2),
        # Generation failures are recorded per item and would otherwise be
        # invisible in the summary — a run where every answer errored still
        # printed a full set of rates, which read as a retrieval regression.
        "generation_errors": sum(
            1 for r in active if any(n.startswith("generation error") for n in r.notes)
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KRUN RAG eval harness")
    parser.add_argument(
        "--eval-set",
        default=str(Path(__file__).resolve().parent / "eval_set.yaml"),
        help="Path to eval_set.yaml (default: rag/eval/eval_set.yaml)",
    )
    parser.add_argument(
        "--no-generate",
        action="store_true",
        help="Skip Claude generation; retrieval-only metrics",
    )
    parser.add_argument(
        "--reranker-on",
        action="store_true",
        help="Force the cross-encoder reranker on for this run",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional path to write JSON results",
    )
    args = parser.parse_args(argv)

    eval_path = Path(args.eval_set)
    items = _load_eval_set(eval_path)
    if not items:
        print(f"[FAIL] No items in {eval_path}")
        return 1

    print("=" * 60)
    print("KRUN RAG — Eval run")
    print("=" * 60)
    print(f"Eval set       : {eval_path}  ({len(items)} items)")
    print(f"Reranker       : {'ON' if args.reranker_on else 'OFF (config default)'}")
    print(f"Generate       : {'NO (retrieval only)' if args.no_generate else 'YES'}\n")

    results: list[ItemResult] = []
    for i, item in enumerate(items, start=1):
        print(f"[{i:2d}/{len(items)}] {item.get('id'):30s}  ", end="", flush=True)
        try:
            r = _evaluate_item(
                item,
                use_reranker=args.reranker_on,
                do_generate=not args.no_generate,
            )
        except Exception as e:
            print(f"FAIL ({e!r})")
            r = ItemResult(
                id=str(item.get("id") or "?"),
                category=str(item.get("category") or "?"),
                query=str(item.get("query") or ""),
                notes=[f"exception: {e!r}"],
            )
            results.append(r)
            continue
        results.append(r)
        if r.skipped:
            print("SKIPPED")
            continue
        flags = [
            f"files {r.file_hits}/{r.file_expected}" if r.file_expected else "",
            f"co {r.company_hits}/{r.company_expected}" if r.company_expected else "",
            f"per {r.person_hits}/{r.person_expected}" if r.person_expected else "",
            "✓must" if r.must_contain_pass else "✗must",
            "✓notMust" if r.must_not_contain_pass else "✗notMust",
        ]
        print("  ".join(f for f in flags if f) + f"  {r.elapsed_s:.1f}s")

    summary = _summarize(results)
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    for k, v in summary.items():
        print(f"  {k:24s} {v}")

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                {"summary": summary, "items": [asdict(r) for r in results]},
                ensure_ascii=False, indent=2, default=str,
            ),
            encoding="utf-8",
        )
        print(f"\nWrote: {out_path}")

    # Phase 1D Gate (per claude.md): retrieval Recall@8 >= 0.7, faithfulness >= 0.85.
    # We report the closest proxies; treat <0.7 file recall as a soft warn.
    if summary.get("generation_errors"):
        print(
            f"\n[WARN] {summary['generation_errors']} item(s) failed to generate an "
            "answer — must_contain rates below are not meaningful. Check the "
            "provider key before reading any score as a retrieval result."
        )
    if summary["file_match_rate"] < 0.7:
        print("\n[WARN] file_match_rate < 0.7 — Phase 1D Gate not yet met for retrieval.")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
