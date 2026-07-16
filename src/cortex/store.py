from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from .models import BundleItem, CommitRecord, Community, GraphEdge, GraphNode, RetrievalBundle, SourceRecord

LEGACY_DIR_NAME = ".cortex"


def _split_identifier(token: str) -> list[str]:
    normalized = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', token.replace('_', ' '))
    return [part.lower() for part in re.findall(r'[A-Za-z0-9]+', normalized) if part]


def _search_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in re.findall(r'[A-Za-z0-9_]+', text):
        tokens.extend(_split_identifier(raw))
    return list(dict.fromkeys(tokens))


def _normalized_identifier(text: str) -> str:
    return ''.join(_search_tokens(text))


def data_root() -> Path:
    """Base directory for all central per-repo data dirs."""
    override = os.environ.get("CORTEX_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".cortex" / "data"


def repo_data_dir(repo_path: Path) -> Path:
    """Central data dir for one repo, keyed by hash of its resolved path."""
    resolved = repo_path.resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    return data_root() / digest


def default_db_path(repo_path: Path) -> Path:
    root = repo_path.resolve()
    legacy = root / LEGACY_DIR_NAME / "cortex.db"
    if legacy.exists():
        return legacy
    return repo_data_dir(root) / "cortex.db"


def write_repo_meta(db_path: Path, repo_root: Path) -> None:
    """Record which repo a central data dir belongs to, for `cortex gc` and debugging."""
    parent = db_path.parent
    if parent.name == LEGACY_DIR_NAME:
        return
    parent.mkdir(parents=True, exist_ok=True)
    meta = {"repo_path": str(repo_root.resolve()), "updated_at": int(time.time())}
    (parent / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


class CortexStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.initialize_schema()

    def initialize_schema(self) -> None:
        self.connection.executescript(
            '''
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS repos (
                repo_path TEXT PRIMARY KEY,
                updated_at INTEGER NOT NULL,
                fingerprint TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS sources (
                repo_path TEXT NOT NULL,
                path TEXT NOT NULL,
                kind TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                modified_at REAL NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL DEFAULT '',
                mtime_ns INTEGER NOT NULL DEFAULT 0,
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
                granularity TEXT NOT NULL DEFAULT 'file',
                signature TEXT NOT NULL DEFAULT '',
                span_start INTEGER,
                span_end INTEGER,
                metadata_json TEXT NOT NULL,
                PRIMARY KEY (repo_path, node_id)
            );

            CREATE TABLE IF NOT EXISTS graph_edges (
                repo_path TEXT NOT NULL,
                edge_id TEXT NOT NULL,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                relation TEXT NOT NULL,
                layer TEXT NOT NULL DEFAULT 'HEADING',
                confidence TEXT NOT NULL DEFAULT 'EXTRACTED',
                weight REAL NOT NULL,
                metadata_json TEXT NOT NULL,
                source_file TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (repo_path, edge_id)
            );

            CREATE TABLE IF NOT EXISTS communities (
                repo_path TEXT NOT NULL,
                community_id INTEGER NOT NULL,
                node_ids_json TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (repo_path, community_id)
            );

            CREATE TABLE IF NOT EXISTS llm_cache (
                content_hash TEXT NOT NULL,
                provider TEXT NOT NULL,
                nodes_json TEXT NOT NULL,
                edges_json TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (content_hash, provider)
            );

            CREATE TABLE IF NOT EXISTS cost (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_path TEXT NOT NULL,
                run_at INTEGER NOT NULL,
                provider TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL
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

            -- P0-1 token-savings ledger: one row per successful MCP tool call.
            -- `response_tokens` is the actual payload size; `baseline_tokens`
            -- is the deterministic "what an agent would have spent without
            -- Cortex" estimate computed by mcp/tools._estimate_baseline. A
            -- brand-new table only needs CREATE TABLE IF NOT EXISTS -- no
            -- ALTER migration required for upgraded databases.
            CREATE TABLE IF NOT EXISTS tool_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_path TEXT NOT NULL,
                called_at INTEGER NOT NULL,
                tool TEXT NOT NULL,
                response_tokens INTEGER NOT NULL,
                baseline_tokens INTEGER NOT NULL,
                meta_json TEXT NOT NULL DEFAULT '{}'
            );
            '''
        )
        self.connection.commit()
        self._migrate_existing_schema()

    def _migrate_existing_schema(self) -> None:
        migrations = [
            "ALTER TABLE sources ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE graph_edges ADD COLUMN layer TEXT NOT NULL DEFAULT 'HEADING'",
            "ALTER TABLE graph_edges ADD COLUMN confidence TEXT NOT NULL DEFAULT 'EXTRACTED'",
            "ALTER TABLE graph_nodes ADD COLUMN granularity TEXT NOT NULL DEFAULT 'file'",
            "ALTER TABLE graph_nodes ADD COLUMN signature TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE graph_nodes ADD COLUMN span_start INTEGER",
            "ALTER TABLE graph_nodes ADD COLUMN span_end INTEGER",
            "ALTER TABLE repos ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''",
            # P0-3 fast-path incremental ingest: nanosecond mtime for stat-first
            # scans, and a real source_file column on edges so delta deletes
            # don't need a json_extract() scan.
            "ALTER TABLE sources ADD COLUMN mtime_ns INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE graph_edges ADD COLUMN source_file TEXT NOT NULL DEFAULT ''",
        ]
        for sql in migrations:
            try:
                self.connection.execute(sql)
                self.connection.commit()
            except Exception:
                pass  # column already exists

        # Index creation happens after the ALTERs above so it's safe to run
        # unconditionally on both fresh and upgraded databases (the columns
        # they reference are guaranteed to exist by this point).
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_nodes_source_ref ON graph_nodes(repo_path, source_ref)",
            "CREATE INDEX IF NOT EXISTS idx_edges_source_file ON graph_edges(repo_path, source_file)",
            "CREATE INDEX IF NOT EXISTS idx_tool_usage_repo_time ON tool_usage(repo_path, called_at)",
        ]
        for sql in indexes:
            try:
                self.connection.execute(sql)
                self.connection.commit()
            except Exception:
                pass  # index already exists / column not ready in some odd legacy state

    def reset_repo(self, repo_path: Path, fingerprint: str = '') -> None:
        repo_key = str(repo_path.resolve())
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO repos(repo_path, updated_at, fingerprint) VALUES(?, ?, ?)",
                (repo_key, int(time.time()), fingerprint),
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

    def set_repo_fingerprint(self, repo_path: Path, fingerprint: str) -> None:
        repo_key = str(repo_path.resolve())
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO repos(repo_path, updated_at, fingerprint)
                VALUES(?, ?, ?)
                ON CONFLICT(repo_path) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    fingerprint = excluded.fingerprint
                """,
                (repo_key, int(time.time()), fingerprint),
            )

    def get_repo_fingerprint(self, repo_path: Path) -> str:
        repo_key = str(repo_path.resolve())
        row = self.connection.execute(
            "SELECT fingerprint FROM repos WHERE repo_path = ?",
            (repo_key,),
        ).fetchone()
        return "" if row is None else row["fingerprint"]

    def save_sources(self, repo_path: Path, sources: list[SourceRecord]) -> None:
        repo_key = str(repo_path.resolve())
        with self.connection:
            self.connection.executemany(
                '''
                INSERT OR REPLACE INTO sources(repo_path, path, kind, size_bytes, modified_at, content, content_hash, mtime_ns)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                [
                    (
                        repo_key,
                        source.path,
                        source.kind,
                        source.size_bytes,
                        source.modified_at,
                        source.content,
                        source.content_hash,
                        source.mtime_ns,
                    )
                    for source in sources
                ],
            )

    def delete_sources(self, repo_path: Path, paths: list[str]) -> None:
        repo_key = str(repo_path.resolve())
        with self.connection:
            self.connection.executemany(
                'DELETE FROM sources WHERE repo_path = ? AND path = ?',
                [(repo_key, path) for path in paths],
            )

    def fetch_source_stats(self, repo_path: Path) -> dict[str, tuple[int, int, str]]:
        """Lightweight (size_bytes, mtime_ns, content_hash) lookup for the
        stat-first incremental scan — does not select the `content` column,
        so this is cheap even for large repos (P0-3)."""
        repo_key = str(repo_path.resolve())
        rows = self.connection.execute(
            'SELECT path, size_bytes, mtime_ns, content_hash FROM sources WHERE repo_path = ?',
            (repo_key,),
        ).fetchall()
        return {row['path']: (row['size_bytes'], row['mtime_ns'], row['content_hash']) for row in rows}

    def replace_graph(self, repo_path: Path, nodes: list[GraphNode], edges: list[GraphEdge]) -> None:
        """Replace the repo graph wholesale — save_graph upserts, so stale rows survive it.

        O(repo) per call: still used by full (non-incremental) ingest. The
        incremental path uses delete_graph_for_sources + append_graph instead
        so an unrelated file's rows are never rewritten (P0-3).
        """
        repo_key = str(repo_path.resolve())
        with self.connection:
            self.connection.execute('DELETE FROM graph_nodes WHERE repo_path = ?', (repo_key,))
            self.connection.execute('DELETE FROM graph_edges WHERE repo_path = ?', (repo_key,))
        self.save_graph(repo_path, nodes, edges)

    def delete_graph_for_sources(self, repo_path: Path, paths: list[str]) -> None:
        """Delete graph rows owned by specific source files: nodes by
        source_ref, edges by their tagged source_file. COCHANGE-layer edges
        (cochange/touches) are not owned by a single file and are untouched
        here — see delete_cochange_layer."""
        if not paths:
            return
        repo_key = str(repo_path.resolve())
        with self.connection:
            self.connection.executemany(
                'DELETE FROM graph_nodes WHERE repo_path = ? AND source_ref = ?',
                [(repo_key, path) for path in paths],
            )
            self.connection.executemany(
                'DELETE FROM graph_edges WHERE repo_path = ? AND source_file = ?',
                [(repo_key, path) for path in paths],
            )

    def delete_cochange_layer(self, repo_path: Path) -> None:
        """Drop the COCHANGE layer (cochange pair edges, commit nodes, and
        commit->file touches edges) so it can be rebuilt fresh. Used by the
        incremental path only when commit history changed or a file was
        deleted, to avoid dangling references (P0-3)."""
        repo_key = str(repo_path.resolve())
        with self.connection:
            self.connection.execute(
                "DELETE FROM graph_edges WHERE repo_path = ? AND layer = 'COCHANGE'",
                (repo_key,),
            )
            self.connection.execute(
                "DELETE FROM graph_nodes WHERE repo_path = ? AND kind = 'commit'",
                (repo_key,),
            )

    def append_graph(self, repo_path: Path, nodes: list[GraphNode], edges: list[GraphEdge]) -> None:
        """Write new/updated graph rows without touching unrelated repo rows.
        This is an upsert (same as save_graph) — the name documents intent at
        incremental call sites: pair with delete_graph_for_sources /
        delete_cochange_layer first so this only ever "appends" fresh state."""
        self.save_graph(repo_path, nodes, edges)

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
                '''
                INSERT OR REPLACE INTO graph_nodes(repo_path, node_id, kind, label, source_ref, granularity, signature, span_start, span_end, metadata_json)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                [
                    (
                        repo_key,
                        node.node_id,
                        node.kind,
                        node.label,
                        node.source_ref,
                        node.granularity,
                        node.signature,
                        node.span_start,
                        node.span_end,
                        json.dumps(node.metadata),
                    )
                    for node in nodes
                ],
            )
            self.connection.executemany(
                '''
                INSERT OR REPLACE INTO graph_edges(repo_path, edge_id, source, target, relation, layer, confidence, weight, metadata_json, source_file)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                [
                    (
                        repo_key,
                        edge.edge_id,
                        edge.source,
                        edge.target,
                        edge.relation,
                        edge.layer,
                        edge.confidence,
                        edge.weight,
                        json.dumps(edge.metadata),
                        edge.metadata.get('source_file', '') or '',
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
            'SELECT path, content, kind, size_bytes, modified_at, content_hash, mtime_ns FROM sources WHERE repo_path = ? ORDER BY path',
            (repo_key,),
        ).fetchall()
        return [
            SourceRecord(
                path=row['path'],
                content=row['content'],
                kind=row['kind'],
                size_bytes=row['size_bytes'],
                modified_at=row['modified_at'],
                content_hash=row['content_hash'],
                mtime_ns=row['mtime_ns'],
            )
            for row in rows
        ]

    def fetch_source_content(self, repo_path: Path, path: str) -> str | None:
        repo_key = str(repo_path.resolve())
        row = self.connection.execute(
            "SELECT content FROM sources WHERE repo_path = ? AND path = ?",
            (repo_key, path),
        ).fetchone()
        return None if row is None else str(row["content"])

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
            'SELECT node_id, kind, label, source_ref, granularity, signature, span_start, span_end, metadata_json FROM graph_nodes WHERE repo_path = ? ORDER BY node_id',
            (repo_key,),
        ).fetchall()
        edge_rows = self.connection.execute(
            'SELECT edge_id, source, target, relation, layer, confidence, weight, metadata_json FROM graph_edges WHERE repo_path = ? ORDER BY edge_id',
            (repo_key,),
        ).fetchall()
        nodes = [
            GraphNode(
                node_id=row['node_id'],
                kind=row['kind'],
                label=row['label'],
                source_ref=row['source_ref'],
                granularity=row['granularity'],
                signature=row['signature'],
                span_start=row['span_start'],
                span_end=row['span_end'],
                metadata=json.loads(row['metadata_json']),
            )
            for row in node_rows
        ]
        edges = [
            GraphEdge(
                edge_id=row['edge_id'],
                source=row['source'],
                target=row['target'],
                relation=row['relation'],
                layer=row['layer'],
                confidence=row['confidence'],
                weight=row['weight'],
                metadata=json.loads(row['metadata_json']),
            )
            for row in edge_rows
        ]
        return nodes, edges

    def query_edges(
        self,
        repo_path: Path,
        relation: str | None = None,
        endpoint_substr: str | None = None,
        direction: str = "both",
        limit: int = 50,
    ) -> list[GraphEdge]:
        repo_key = str(repo_path.resolve())
        if direction not in {"out", "in", "both"}:
            raise ValueError("direction must be 'out', 'in', or 'both'")

        where = ["e.repo_path = ?"]
        params: list[object] = [repo_key]
        if relation:
            where.append("e.relation = ?")
            params.append(relation)
        if endpoint_substr:
            pattern = f"%{endpoint_substr}%"
            if direction == "out":
                where.append("(e.source LIKE ? OR source_node.label LIKE ?)")
                params.extend([pattern, pattern])
            elif direction == "in":
                where.append("(e.target LIKE ? OR target_node.label LIKE ?)")
                params.extend([pattern, pattern])
            else:
                where.append(
                    "(e.source LIKE ? OR e.target LIKE ? OR source_node.label LIKE ? OR target_node.label LIKE ?)"
                )
                params.extend([pattern, pattern, pattern, pattern])
        params.append(limit)

        rows = self.connection.execute(
            f"""
            SELECT e.edge_id, e.source, e.target, e.relation, e.layer, e.confidence, e.weight, e.metadata_json
            FROM graph_edges AS e
            LEFT JOIN graph_nodes AS source_node
              ON source_node.repo_path = e.repo_path AND source_node.node_id = e.source
            LEFT JOIN graph_nodes AS target_node
              ON target_node.repo_path = e.repo_path AND target_node.node_id = e.target
            WHERE {' AND '.join(where)}
            ORDER BY e.edge_id
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return [
            GraphEdge(
                edge_id=row['edge_id'],
                source=row['source'],
                target=row['target'],
                relation=row['relation'],
                layer=row['layer'],
                confidence=row['confidence'],
                weight=row['weight'],
                metadata=json.loads(row['metadata_json']),
            )
            for row in rows
        ]

    def get_nodes(self, repo_path: Path, node_ids: list[str]) -> dict[str, GraphNode]:
        if not node_ids:
            return {}
        repo_key = str(repo_path.resolve())
        placeholders = ", ".join("?" for _ in node_ids)
        rows = self.connection.execute(
            f"""
            SELECT node_id, kind, label, source_ref, granularity, signature, span_start, span_end, metadata_json
            FROM graph_nodes
            WHERE repo_path = ? AND node_id IN ({placeholders})
            """,
            (repo_key, *node_ids),
        ).fetchall()
        return {
            row['node_id']: GraphNode(
                node_id=row['node_id'],
                kind=row['kind'],
                label=row['label'],
                source_ref=row['source_ref'],
                granularity=row['granularity'],
                signature=row['signature'],
                span_start=row['span_start'],
                span_end=row['span_end'],
                metadata=json.loads(row['metadata_json']),
            )
            for row in rows
        }

    def search_nodes(self, repo_path: Path, query: str, limit: int = 20) -> list[GraphNode]:
        repo_key = str(repo_path.resolve())
        tokens = _search_tokens(query)
        if not tokens:
            return []
        patterns = [f"%{token}%" for token in tokens]
        # Whole-identifier pattern: query tokens in order with anything between,
        # so "DeviceListModel", "device_list_model", and "device list model" all
        # match a label spelled either way (SQLite LIKE is ASCII case-insensitive).
        sequence_pattern = "%" + "%".join(tokens) + "%"
        label_clause = " OR ".join("label LIKE ?" for _ in tokens)
        signature_clause = " OR ".join("signature LIKE ?" for _ in tokens)
        source_clause = " OR ".join("source_ref LIKE ?" for _ in tokens)
        where = f"(({label_clause}) OR ({signature_clause}) OR ({source_clause}))"
        # Fetch order guards against LIMIT dropping the real target:
        # 1. whole-identifier label matches first — a symbol matching the *full*
        #    query must survive even when a common sub-token ("flow") matches
        #    hundreds of other labels;
        # 2. then by how many query tokens hit the label (LIKE yields 0/1 in
        #    SQLite, so summing counts matched tokens);
        # 3. then signature/path matches.
        priority = (
            f"CASE WHEN label LIKE ? THEN 0 "
            f"WHEN ({label_clause}) THEN 1 "
            f"WHEN ({signature_clause}) THEN 2 ELSE 3 END"
        )
        label_hit_count = " + ".join("(label LIKE ?)" for _ in tokens)
        params: list[Any] = [repo_key]
        params.extend(patterns)  # label clause (WHERE)
        params.extend(patterns)  # signature clause (WHERE)
        params.extend(patterns)  # source_ref clause (WHERE)
        params.append(sequence_pattern)  # whole-identifier (ORDER BY priority)
        params.extend(patterns)  # label clause (ORDER BY priority)
        params.extend(patterns)  # signature clause (ORDER BY priority)
        params.extend(patterns)  # label hit count (ORDER BY)
        params.append(max(limit * 8, 50))
        rows = self.connection.execute(
            f"""
            SELECT node_id, kind, label, source_ref, granularity, signature, span_start, span_end, metadata_json
            FROM graph_nodes
            WHERE repo_path = ?
              AND granularity = 'symbol'
              AND {where}
            ORDER BY {priority}, ({label_hit_count}) DESC, length(label)
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        candidates = [
            GraphNode(
                node_id=row['node_id'],
                kind=row['kind'],
                label=row['label'],
                source_ref=row['source_ref'],
                granularity=row['granularity'],
                signature=row['signature'],
                span_start=row['span_start'],
                span_end=row['span_end'],
                metadata=json.loads(row['metadata_json']),
            )
            for row in rows
        ]

        query_lower = query.lower()
        query_norm = _normalized_identifier(query)
        token_set = set(tokens)

        def rank(node: GraphNode) -> tuple[int, int, int, str, str]:
            label_lower = node.label.lower()
            label_norm = _normalized_identifier(node.label)
            label_tokens = set(_search_tokens(node.label))
            label_overlap = len(token_set & label_tokens)
            signature = node.signature or ""
            signature_lower = signature.lower()
            signature_tokens = set(_search_tokens(signature))
            source_tokens = set(_search_tokens(node.source_ref or ""))
            # Bucket strictly by where and how completely the query hit,
            # best-to-worst. A whole-identifier match (equal / prefix /
            # contiguous substring of the full query) always outranks symbols
            # matching only some sub-tokens, so a common token like "flow"
            # cannot flood out the one symbol matching the entire query.
            # Partial name matches are graded by how many query tokens they
            # cover, and file-path (source_ref) hits rank below every match
            # against the symbol name or signature.
            if label_lower == query_lower or label_norm == query_norm:
                bucket = 0  # exact symbol name
            elif label_lower.startswith(query_lower) or label_norm.startswith(query_norm):
                bucket = 1  # symbol name prefix
            elif query_norm and query_norm in label_norm:
                bucket = 2  # whole query embedded in the symbol name
            elif token_set <= label_tokens:
                bucket = 3  # all query tokens present in the symbol name
            elif label_overlap:
                bucket = 4  # some query tokens in the symbol name
            elif token_set <= signature_tokens or query_lower in signature_lower:
                bucket = 5  # matched in the signature only
            elif token_set <= source_tokens:
                bucket = 6  # matched only via the file path
            else:
                bucket = 7
            return (bucket, -label_overlap, len(node.label), label_lower, node.node_id)

        ranked = sorted(candidates, key=rank)
        return ranked[:limit]

    def save_communities(self, repo_path: Path, communities: list[Community]) -> None:
        repo_key = str(repo_path.resolve())
        with self.connection:
            self.connection.execute('DELETE FROM communities WHERE repo_path = ?', (repo_key,))
            self.connection.executemany(
                'INSERT INTO communities(repo_path, community_id, node_ids_json, label) VALUES(?, ?, ?, ?)',
                [
                    (repo_key, c.community_id, json.dumps(c.node_ids), c.label)
                    for c in communities
                ],
            )

    def fetch_communities(self, repo_path: Path) -> list[Community]:
        repo_key = str(repo_path.resolve())
        rows = self.connection.execute(
            'SELECT community_id, node_ids_json, label FROM communities WHERE repo_path = ? ORDER BY community_id',
            (repo_key,),
        ).fetchall()
        return [
            Community(
                community_id=row['community_id'],
                node_ids=json.loads(row['node_ids_json']),
                label=row['label'],
            )
            for row in rows
        ]

    def get_llm_cache(self, content_hash: str, provider: str) -> dict | None:
        row = self.connection.execute(
            'SELECT nodes_json, edges_json, input_tokens, output_tokens FROM llm_cache WHERE content_hash = ? AND provider = ?',
            (content_hash, provider),
        ).fetchone()
        if row is None:
            return None
        return {
            'nodes': json.loads(row['nodes_json']),
            'edges': json.loads(row['edges_json']),
            'input_tokens': row['input_tokens'],
            'output_tokens': row['output_tokens'],
        }

    def set_llm_cache(self, content_hash: str, provider: str, nodes: list, edges: list, input_tokens: int, output_tokens: int) -> None:
        import time as _time
        with self.connection:
            self.connection.execute(
                '''
                INSERT OR REPLACE INTO llm_cache(content_hash, provider, nodes_json, edges_json, input_tokens, output_tokens, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ''',
                (content_hash, provider, json.dumps(nodes), json.dumps(edges), input_tokens, output_tokens, int(_time.time())),
            )

    def record_cost(self, repo_path: Path, provider: str, input_tokens: int, output_tokens: int) -> None:
        import time as _time
        repo_key = str(repo_path.resolve())
        with self.connection:
            self.connection.execute(
                'INSERT INTO cost(repo_path, run_at, provider, input_tokens, output_tokens) VALUES(?, ?, ?, ?, ?)',
                (repo_key, int(_time.time()), provider, input_tokens, output_tokens),
            )

    def fetch_cumulative_cost(self, repo_path: Path) -> dict:
        repo_key = str(repo_path.resolve())
        row = self.connection.execute(
            'SELECT SUM(input_tokens) as total_in, SUM(output_tokens) as total_out, COUNT(*) as runs FROM cost WHERE repo_path = ?',
            (repo_key,),
        ).fetchone()
        return {
            'total_input_tokens': row['total_in'] or 0,
            'total_output_tokens': row['total_out'] or 0,
            'runs': row['runs'] or 0,
        }

    def record_tool_usage(
        self,
        repo_path: Path,
        tool: str,
        response_tokens: int,
        baseline_tokens: int,
        meta: dict | None = None,
    ) -> None:
        """Append one row to the token-savings ledger (P0-1).

        Callers (mcp/tools.py) must treat failures here as non-fatal: a
        locked/busy DB must never surface as an error on the underlying MCP
        tool response.
        """
        import time as _time
        repo_key = str(repo_path.resolve())
        with self.connection:
            self.connection.execute(
                '''
                INSERT INTO tool_usage(repo_path, called_at, tool, response_tokens, baseline_tokens, meta_json)
                VALUES(?, ?, ?, ?, ?, ?)
                ''',
                (repo_key, int(_time.time()), tool, response_tokens, baseline_tokens, json.dumps(meta or {})),
            )

    def fetch_tool_usage(self, repo_path: Path) -> list[dict]:
        repo_key = str(repo_path.resolve())
        rows = self.connection.execute(
            '''
            SELECT id, called_at, tool, response_tokens, baseline_tokens, meta_json
            FROM tool_usage WHERE repo_path = ? ORDER BY called_at
            ''',
            (repo_key,),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "called_at": row["called_at"],
                "tool": row["tool"],
                "response_tokens": row["response_tokens"],
                "baseline_tokens": row["baseline_tokens"],
                "meta": json.loads(row["meta_json"]) if row["meta_json"] else {},
            }
            for row in rows
        ]

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
