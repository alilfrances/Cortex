from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class SourceRecord:
    path: str
    content: str
    kind: str
    size_bytes: int
    modified_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CommitRecord:
    sha: str
    summary: str
    author: str
    authored_at: int
    files: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class GraphNode:
    node_id: str
    kind: str
    label: str
    source_ref: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class GraphEdge:
    edge_id: str
    source: str
    target: str
    relation: str
    weight: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BundleItem:
    item_id: str
    kind: str
    title: str
    path: str
    content: str
    token_count: int
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RetrievalBundle:
    task: str
    repo_path: str
    budget: int
    total_tokens: int
    generated_at: int
    items: list[BundleItem]
    confidence_notes: list[str]
    open_questions: list[str]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["items"] = [item.to_dict() for item in self.items]
        return payload
