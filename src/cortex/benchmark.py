from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .bundle import generate_bundle
from .ingest import ingest_repository
from .store import CortexStore, default_db_path
from .tokenizer import count_text_tokens

DEFAULT_QUESTIONS = [
    "how does authentication work",
    "what is the main entry point",
    "how are errors handled",
    "what connects the data layer to the api",
    "what are the core abstractions",
]


def run_benchmark(
    repo_path: Path,
    *,
    commit_limit: int = 50,
    budget: int = 4000,
    db_path: Path | None = None,
    questions: list[str] | None = None,
) -> dict[str, Any]:
    summary = ingest_repository(repo_path, commit_limit=commit_limit, db_path=db_path)
    repo_root = Path(str(summary["repo_path"]))
    store = CortexStore(db_path or default_db_path(repo_root))
    sources = store.fetch_sources(repo_root)

    corpus_tokens = sum(count_text_tokens(source.content) for source in sources)
    per_question: list[dict[str, Any]] = []
    prompts = questions or DEFAULT_QUESTIONS

    for question in prompts:
        bundle = generate_bundle(repo_root, task=question, budget=budget, db_path=db_path, output_format="json")
        query_tokens = int(bundle["total_tokens"])
        reduction = round(corpus_tokens / query_tokens, 1) if query_tokens > 0 else 0.0
        per_question.append(
            {
                "question": question,
                "query_tokens": query_tokens,
                "reduction": reduction,
                "items": len(bundle["items"]),
            }
        )

    avg_query_tokens = sum(item["query_tokens"] for item in per_question) // max(1, len(per_question))
    reduction_ratio = round(corpus_tokens / avg_query_tokens, 1) if avg_query_tokens > 0 else 0.0
    return {
        "repo_path": str(repo_root),
        "corpus_tokens": corpus_tokens,
        "source_count": len(sources),
        "budget": budget,
        "avg_query_tokens": avg_query_tokens,
        "reduction_ratio": reduction_ratio,
        "per_question": per_question,
    }


def format_benchmark(result: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(result, indent=2)

    lines = [
        "# Cortex Token Benchmark",
        "",
        f"- Repo: {result['repo_path']}",
        f"- Sources: {result['source_count']}",
        f"- Corpus Tokens: {result['corpus_tokens']}",
        f"- Budget: {result['budget']}",
        f"- Average Query Tokens: {result['avg_query_tokens']}",
        f"- Reduction Ratio: {result['reduction_ratio']}x",
        "",
        "## Questions",
    ]
    lines.extend(
        f"- {item['question']} -> {item['query_tokens']} tokens ({item['reduction']}x, {item['items']} items)"
        for item in result["per_question"]
    )
    return "\n".join(lines).strip()
