from __future__ import annotations

import math
import re
import time
from pathlib import Path

from .gitutils import discover_repo_root
from .models import BundleItem, RetrievalBundle
from .store import CortexStore, default_db_path
from .tokenizer import count_text_tokens, truncate_text_to_budget


def _tokenize_query(task: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[A-Za-z0-9_]+", task) if token}


def _score_text(task_terms: set[str], text: str, recency_weight: float = 0.0) -> float:
    haystack_terms = {token.lower() for token in re.findall(r"[A-Za-z0-9_]+", text)}
    overlap = len(task_terms & haystack_terms)
    return overlap * 10.0 + recency_weight


def _bundle_markdown(bundle: RetrievalBundle) -> str:
    lines = [
        "# Cortex Retrieval Bundle",
        "",
        f"- Task: {bundle.task}",
        f"- Budget: {bundle.budget}",
        f"- Total Tokens: {bundle.total_tokens}",
        "",
        "## Confidence Notes",
    ]
    lines.extend(f"- {note}" for note in bundle.confidence_notes)
    lines.extend(["", "## Items"])
    for item in bundle.items:
        lines.extend(
            [
                f"### {item.title}",
                f"- Kind: {item.kind}",
                f"- Path: {item.path}",
                f"- Tokens: {item.token_count}",
                f"- Score: {item.score:.2f}",
                "",
                item.content,
                "",
            ]
        )
    if bundle.open_questions:
        lines.append("## Open Questions")
        lines.extend(f"- {question}" for question in bundle.open_questions)
    return "\n".join(lines).strip()


def generate_bundle(
    repo_path: Path,
    task: str,
    budget: int,
    db_path: Path | None = None,
    output_format: str = "md",
) -> str | dict:
    repo_root = discover_repo_root(repo_path)
    store = CortexStore(db_path or default_db_path(repo_root))
    sources = store.fetch_sources(repo_root)
    commits = store.fetch_commits(repo_root)
    task_terms = _tokenize_query(task)
    candidates: list[BundleItem] = []

    newest_commit = max((commit.authored_at for commit in commits), default=0)
    for source in sources:
        score = _score_text(task_terms, f"{source.path}\n{source.content}")
        token_count = count_text_tokens(source.content)
        candidates.append(
            BundleItem(
                item_id=f"source:{source.path}",
                kind=source.kind,
                title=source.path,
                path=source.path,
                content=source.content,
                token_count=token_count,
                score=score,
                metadata={"modified_at": source.modified_at},
            )
        )

    for commit in commits:
        recency_weight = 0.0
        if newest_commit:
            recency_weight = max(0.0, 5.0 - math.log2(max(1, newest_commit - commit.authored_at + 1)))
        content = f"{commit.summary}\nFiles: {', '.join(commit.files)}"
        candidates.append(
            BundleItem(
                item_id=f"commit:{commit.sha}",
                kind="commit",
                title=commit.summary,
                path=commit.sha,
                content=content,
                token_count=count_text_tokens(content),
                score=_score_text(task_terms, content, recency_weight=recency_weight),
                metadata={"sha": commit.sha, "files": commit.files, "authored_at": commit.authored_at},
            )
        )

    candidates.sort(key=lambda item: (-item.score, item.path))

    selected: list[BundleItem] = []
    total_tokens = 0
    for item in candidates:
        if total_tokens + item.token_count <= budget:
            selected.append(item)
            total_tokens += item.token_count
            continue
        remaining = budget - total_tokens
        if remaining <= 16:
            continue
        truncated = truncate_text_to_budget(item.content, remaining)
        truncated_tokens = count_text_tokens(truncated)
        if truncated_tokens <= 0:
            continue
        selected.append(
            BundleItem(
                item_id=item.item_id,
                kind=item.kind,
                title=item.title,
                path=item.path,
                content=truncated,
                token_count=truncated_tokens,
                score=item.score,
                metadata={**item.metadata, "truncated": True},
            )
        )
        total_tokens += truncated_tokens

    bundle = RetrievalBundle(
        task=task,
        repo_path=str(repo_root),
        budget=budget,
        total_tokens=total_tokens,
        generated_at=int(time.time()),
        items=selected,
        confidence_notes=[
            "Deterministic ranking based on task-term overlap and commit recency.",
            "Token counts use Cortex's byte-safe local estimator.",
        ],
        open_questions=[] if selected else ["No matching sources were found in the ingested dataset."],
    )
    store.save_bundle(repo_root, bundle)

    if output_format == "json":
        return bundle.to_dict()
    return _bundle_markdown(bundle)
