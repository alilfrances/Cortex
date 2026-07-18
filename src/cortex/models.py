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
    content_hash: str = ''
    # Nanosecond-precision mtime (os.stat().st_mtime_ns). `modified_at` above is
    # a float of whole seconds (sqlite REAL) and is too coarse to reliably
    # detect a same-second edit; the stat-first incremental scan (P0-3) uses
    # this field instead to decide whether a file needs to be re-read.
    mtime_ns: int = 0

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


SIGNATURE_DISPLAY_LIMIT = 200


@dataclass(slots=True)
class GraphNode:
    node_id: str
    kind: str
    label: str
    source_ref: str
    granularity: str = 'file'
    signature: str = ''
    span_start: int | None = None
    span_end: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        signature = payload.get('signature') or ''
        if len(signature) > SIGNATURE_DISPLAY_LIMIT:
            payload['signature'] = signature[:SIGNATURE_DISPLAY_LIMIT] + '…'
        return payload


@dataclass(slots=True)
class GraphEdge:
    edge_id: str
    source: str
    target: str
    relation: str
    layer: str = 'HEADING'
    confidence: str = 'EXTRACTED'
    weight: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Community:
    community_id: int
    node_ids: list[str]
    label: str = ''

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
        payload['items'] = [item.to_dict() for item in self.items]
        return payload
