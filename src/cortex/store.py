from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .models import BundleItem, CommitRecord, GraphEdge, GraphNode, RetrievalBundle, SourceRecord


def default_db_path(repo_path: Path) -> Path:
    root = repo_path.resolve()
    return root / ".cortex" / "cortex.db"


class CortexStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.initialize_schema()

    def initialize_schema(self) -> None:
        self.connection.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS repos (
                repo_path TEXT PRIMARY KEY,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sources (
                repo_path TEXT NOT NULL,
                path TEXT NOT NULL,
                kind TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                modified_at REAL NOT NULL,
                content TEXT NOT NULL,
                PRIMARY KEY (repo_path, path)
            );

            CREATE TABLE IF NOT EXISTS commits (
                repo_path TEXT NOT NULL,
                sha TEXT NOT NULL,
                summary TEXT NOT NULL,
                author TEXT NOT NULL,
                authored_at INTEGER NOT NULL,
                files_json TEXT NOT NULL,
                PRIMARY KEY (repo_path, sha)
            );

            CREATE TABLE IF NOT EXISTS graph_nodes (
                repo_path TEXT NOT NULL,
                node_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                label TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                PRIMARY KEY (repo_path, node_id)
            );

            CREATE TABLE IF NOT EXISTS graph_edges (
                repo_path TEXT NOT NULL,
                edge_id TEXT NOT NULL,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                relation TEXT NOT NULL,
                weight REAL NOT NULL,
                metadata_json TEXT NOT NULL,
                PRIMARY KEY (repo_path, edge_id)
            );

            CREATE TABLE IF NOT EXISTS bundles (
                repo_path TEXT NOT NULL,
                bundle_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task TEXT NOT NULL,
                budget INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL,
                generated_at INTEGER NOT NULL,
                confidence_notes_json TEXT NOT NULL,
                open_questions_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bundle_items (
                bundle_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                path TEXT NOT NULL,
                content TEXT NOT NULL,
                token_count INTEGER NOT NULL,
                score REAL NOT NULL,
                metadata_json TEXT NOT NULL,
                PRIMARY KEY (bundle_id, item_id)
            );
            """
        )
        self.connection.commit()

    def reset_repo(self, repo_path: Path) -> None:
        repo_key = str(repo_path.resolve())
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO repos(repo_path, updated_at) VALUES(?, ?)",
                (repo_key, int(time.time())),
            )
            bundle_ids = [
                row["bundle_id"]
                for row in self.connection.execute(
                    "SELECT bundle_id FROM bundles WHERE repo_path = ?",
                    (repo_key,),
                ).fetchall()
            ]
            if bundle_ids:
                self.connection.executemany(
                    "DELETE FROM bundle_items WHERE bundle_id = ?",
                    [(bundle_id,) for bundle_id in bundle_ids],
                )
                self.connection.execute("DELETE FROM bundles WHERE repo_path = ?", (repo_key,))
            for table in ("sources", "commits", "graph_nodes", "graph_edges"):
                self.connection.execute(f"DELETE FROM {table} WHERE repo_path = ?", (repo_key,))

    def save_sources(self, repo_path: Path, sources: list[SourceRecord]) -> None:
        repo_key = str(repo_path.resolve())
        with self.connection:
            self.connection.executemany(
                """
                INSERT OR REPLACE INTO sources(repo_path, path, kind, size_bytes, modified_at, content)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                [
                    (repo_key, source.path, source.kind, source.size_bytes, source.modified_at, source.content)
                    for source in sources
                ],
            )

    def save_commits(self, repo_path: Path, commits: list[CommitRecord]) -> None:
        repo_key = str(repo_path.resolve())
        with self.connection:
            self.connection.executemany(
                """
                INSERT OR REPLACE INTO commits(repo_path, sha, summary, author, authored_at, files_json)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                [
                    (repo_key, commit.sha, commit.summary, commit.author, commit.authored_at, json.dumps(commit.files))
                    for commit in commits
                ],
            )

    def save_graph(self, repo_path: Path, nodes: list[GraphNode], edges: list[GraphEdge]) -> None:
        repo_key = str(repo_path.resolve())
        with self.connection:
            self.connection.executemany(
                """
                INSERT OR REPLACE INTO graph_nodes(repo_path, node_id, kind, label, source_ref, metadata_json)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                [
                    (repo_key, node.node_id, node.kind, node.label, node.source_ref, json.dumps(node.metadata))
                    for node in nodes
                ],
            )
            self.connection.executemany(
                """
                INSERT OR REPLACE INTO graph_edges(repo_path, edge_id, source, target, relation, weight, metadata_json)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        repo_key,
                        edge.edge_id,
                        edge.source,
                        edge.target,
                        edge.relation,
                        edge.weight,
                        json.dumps(edge.metadata),
                    )
                    for edge in edges
                ],
            )

    def save_bundle(self, repo_path: Path, bundle: RetrievalBundle) -> None:
        repo_key = str(repo_path.resolve())
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO bundles(repo_path, task, budget, total_tokens, generated_at, confidence_notes_json, open_questions_json)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo_key,
                    bundle.task,
                    bundle.budget,
                    bundle.total_tokens,
                    bundle.generated_at,
                    json.dumps(bundle.confidence_notes),
                    json.dumps(bundle.open_questions),
                ),
            )
            bundle_id = int(cursor.lastrowid)
            self.connection.executemany(
                """
                INSERT INTO bundle_items(bundle_id, item_id, kind, title, path, content, token_count, score, metadata_json)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        bundle_id,
                        item.item_id,
                        item.kind,
                        item.title,
                        item.path,
                        item.content,
                        item.token_count,
                        item.score,
                        json.dumps(item.metadata),
                    )
                    for item in bundle.items
                ],
            )

    def fetch_sources(self, repo_path: Path) -> list[SourceRecord]:
        repo_key = str(repo_path.resolve())
        rows = self.connection.execute(
            "SELECT path, content, kind, size_bytes, modified_at FROM sources WHERE repo_path = ? ORDER BY path",
            (repo_key,),
        ).fetchall()
        return [SourceRecord(**dict(row)) for row in rows]

    def fetch_commits(self, repo_path: Path) -> list[CommitRecord]:
        repo_key = str(repo_path.resolve())
        rows = self.connection.execute(
            "SELECT sha, summary, author, authored_at, files_json FROM commits WHERE repo_path = ? ORDER BY authored_at DESC",
            (repo_key,),
        ).fetchall()
        return [
            CommitRecord(
                sha=row["sha"],
                summary=row["summary"],
                author=row["author"],
                authored_at=row["authored_at"],
                files=json.loads(row["files_json"]),
            )
            for row in rows
        ]

    def fetch_graph(self, repo_path: Path) -> tuple[list[GraphNode], list[GraphEdge]]:
        repo_key = str(repo_path.resolve())
        node_rows = self.connection.execute(
            "SELECT node_id, kind, label, source_ref, metadata_json FROM graph_nodes WHERE repo_path = ? ORDER BY node_id",
            (repo_key,),
        ).fetchall()
        edge_rows = self.connection.execute(
            "SELECT edge_id, source, target, relation, weight, metadata_json FROM graph_edges WHERE repo_path = ? ORDER BY edge_id",
            (repo_key,),
        ).fetchall()
        nodes = [
            GraphNode(
                node_id=row["node_id"],
                kind=row["kind"],
                label=row["label"],
                source_ref=row["source_ref"],
                metadata=json.loads(row["metadata_json"]),
            )
            for row in node_rows
        ]
        edges = [
            GraphEdge(
                edge_id=row["edge_id"],
                source=row["source"],
                target=row["target"],
                relation=row["relation"],
                weight=row["weight"],
                metadata=json.loads(row["metadata_json"]),
            )
            for row in edge_rows
        ]
        return nodes, edges

    def fetch_latest_bundle(self, repo_path: Path) -> RetrievalBundle | None:
        repo_key = str(repo_path.resolve())
        row = self.connection.execute(
            """
            SELECT bundle_id, task, budget, total_tokens, generated_at, confidence_notes_json, open_questions_json
            FROM bundles
            WHERE repo_path = ?
            ORDER BY bundle_id DESC
            LIMIT 1
            """,
            (repo_key,),
        ).fetchone()
        if row is None:
            return None
        item_rows = self.connection.execute(
            """
            SELECT item_id, kind, title, path, content, token_count, score, metadata_json
            FROM bundle_items
            WHERE bundle_id = ?
            ORDER BY score DESC, item_id ASC
            """,
            (row["bundle_id"],),
        ).fetchall()
        items = [
            BundleItem(
                item_id=item["item_id"],
                kind=item["kind"],
                title=item["title"],
                path=item["path"],
                content=item["content"],
                token_count=item["token_count"],
                score=item["score"],
                metadata=json.loads(item["metadata_json"]),
            )
            for item in item_rows
        ]
        return RetrievalBundle(
            task=row["task"],
            repo_path=repo_key,
            budget=row["budget"],
            total_tokens=row["total_tokens"],
            generated_at=row["generated_at"],
            items=items,
            confidence_notes=json.loads(row["confidence_notes_json"]),
            open_questions=json.loads(row["open_questions_json"]),
        )
