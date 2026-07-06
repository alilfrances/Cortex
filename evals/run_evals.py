from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cortex import bundle as bundle_mod  # noqa: E402
from cortex.ingest import ingest_repository  # noqa: E402


@dataclass(frozen=True)
class GoldTask:
    repo: str
    description: str
    expected_files: tuple[str, ...]
    expected_symbols: tuple[str, ...] = ()
    budget: int = 900
    tight_budget: int = 180


GOLD_TASKS: tuple[GoldTask, ...] = (
    GoldTask(
        repo="python_app",
        description="Trace password login token issuance and session audit",
        expected_files=("app/auth.py", "app/session.py", "app/audit.py"),
        expected_symbols=("app/auth.py:AuthService.login", "app/session.py:SessionStore.create"),
    ),
    GoldTask(
        repo="python_app",
        description="Find where expired sessions are pruned from storage",
        expected_files=("app/session.py", "app/cleanup.py"),
        expected_symbols=("app/session.py:SessionStore.prune_expired", "app/cleanup.py:cleanup_sessions"),
    ),
    GoldTask(
        repo="python_app",
        description="Explain order checkout payment capture and receipt email",
        expected_files=("app/orders.py", "app/payments.py", "app/emailer.py"),
        expected_symbols=("app/orders.py:OrderService.checkout", "app/payments.py:PaymentGateway.capture"),
    ),
    GoldTask(
        repo="python_app",
        description="Locate retry behavior for failed payment captures",
        expected_files=("app/payments.py", "app/retry.py"),
        expected_symbols=("app/retry.py:retry_operation",),
    ),
    GoldTask(
        repo="python_app",
        description="Find markdown setup guidance for plugin installation",
        expected_files=("README.md", "docs/setup.md"),
    ),
    GoldTask(
        repo="python_app",
        description="Investigate audit logging for order checkout and login events",
        expected_files=("app/audit.py", "app/auth.py", "app/orders.py"),
        expected_symbols=("app/audit.py:AuditLog.record",),
    ),
    GoldTask(
        repo="web_service",
        description="Trace API route for creating incidents and notifying Slack",
        expected_files=("server.py", "handlers/incidents.py", "integrations/slack.py"),
        expected_symbols=("handlers/incidents.py:create_incident", "integrations/slack.py:SlackNotifier.send"),
    ),
    GoldTask(
        repo="web_service",
        description="Find repository code that saves incidents to SQLite",
        expected_files=("handlers/incidents.py", "storage/repository.py"),
        expected_symbols=("storage/repository.py:IncidentRepository.save",),
    ),
    GoldTask(
        repo="web_service",
        description="Explain health check route and configuration loading",
        expected_files=("server.py", "config.py"),
        expected_symbols=("server.py:health_check", "config.py:load_config"),
    ),
    GoldTask(
        repo="web_service",
        description="Locate tests for incident creation notifications",
        expected_files=("tests/test_incidents.py", "handlers/incidents.py"),
        expected_symbols=("tests/test_incidents.py:test_create_incident_notifies_slack",),
    ),
)


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")


def _commit_all(repo: Path, message: str) -> None:
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-m", message], repo)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    _run(["git", "init"], repo)
    _run(["git", "config", "user.email", "evals@example.test"], repo)
    _run(["git", "config", "user.name", "Cortex Evals"], repo)


def _build_python_app(base: Path) -> Path:
    repo = base / "python_app"
    _init_repo(repo)
    _write(repo / "app/audit.py", """
class AuditLog:
    def __init__(self):
        self.events = []

    def record(self, event_name, payload):
        self.events.append({"event": event_name, "payload": payload})
        return self.events[-1]
""")
    _write(repo / "app/session.py", """
from app.audit import AuditLog


class SessionStore:
    def __init__(self, audit=None):
        self.audit = audit or AuditLog()
        self.sessions = {}

    def create(self, user_id):
        token = f"token-{user_id}"
        self.sessions[token] = {"user_id": user_id, "expires": 999999}
        self.audit.record("session.create", {"user_id": user_id})
        return token

    def prune_expired(self, now):
        expired = [token for token, row in self.sessions.items() if row["expires"] < now]
        for token in expired:
            del self.sessions[token]
        return expired
""")
    _write(repo / "app/auth.py", """
from app.audit import AuditLog
from app.session import SessionStore


class AuthService:
    def __init__(self, users, sessions=None, audit=None):
        self.users = users
        self.audit = audit or AuditLog()
        self.sessions = sessions or SessionStore(self.audit)

    def login(self, username, password):
        user = self.users[username]
        if user["password"] != password:
            self.audit.record("auth.failed", {"username": username})
            raise ValueError("bad password")
        token = self.sessions.create(user["id"])
        self.audit.record("auth.login", {"username": username})
        return token
""")
    _write(repo / "README.md", """
# Python App

Install the Cortex plugin by pointing Claude Code or Codex at this repository.
Run cortex ingest before asking plugin questions.
""")
    _commit_all(repo, "add auth session audit")
    _write(repo / "app/retry.py", """
def retry_operation(operation, attempts=3):
    last_error = None
    for _ in range(attempts):
        try:
            return operation()
        except RuntimeError as exc:
            last_error = exc
    raise last_error
""")
    _write(repo / "app/payments.py", """
from app.retry import retry_operation


class PaymentGateway:
    def __init__(self, client):
        self.client = client

    def capture(self, order_id, amount):
        return retry_operation(lambda: self.client.capture(order_id, amount))
""")
    _write(repo / "app/emailer.py", """
class ReceiptEmailer:
    def send_receipt(self, order):
        return f"receipt:{order['id']}"
""")
    _write(repo / "app/orders.py", """
from app.audit import AuditLog
from app.emailer import ReceiptEmailer
from app.payments import PaymentGateway


class OrderService:
    def __init__(self, payment_client, audit=None, emailer=None):
        self.audit = audit or AuditLog()
        self.gateway = PaymentGateway(payment_client)
        self.emailer = emailer or ReceiptEmailer()

    def checkout(self, order):
        charge = self.gateway.capture(order["id"], order["total"])
        self.audit.record("order.checkout", {"order_id": order["id"]})
        self.emailer.send_receipt(order)
        return charge
""")
    _commit_all(repo, "add checkout payments")
    _write(repo / "app/cleanup.py", """
from app.session import SessionStore


def cleanup_sessions(store: SessionStore, now):
    return store.prune_expired(now)
""")
    _write(repo / "docs/setup.md", """
# Plugin setup

Claude Code uses claude plugin with this plugin directory. Codex uses the
.codex-plugin manifest plus an mcp_servers.cortex configuration entry.
""")
    _commit_all(repo, "add cleanup and setup docs")
    return repo


def _build_web_service(base: Path) -> Path:
    repo = base / "web_service"
    _init_repo(repo)
    _write(repo / "config.py", """
def load_config(env):
    return {"database": env.get("DATABASE_URL", "sqlite:///incidents.db")}
""")
    _write(repo / "storage/repository.py", """
class IncidentRepository:
    def __init__(self, connection):
        self.connection = connection

    def save(self, incident):
        self.connection.execute("insert into incidents values (?, ?)", (incident["id"], incident["title"]))
        return incident
""")
    _write(repo / "integrations/slack.py", """
class SlackNotifier:
    def __init__(self, client):
        self.client = client

    def send(self, channel, message):
        return self.client.post(channel=channel, text=message)
""")
    _commit_all(repo, "add config storage slack")
    _write(repo / "handlers/incidents.py", """
from integrations.slack import SlackNotifier
from storage.repository import IncidentRepository


def create_incident(payload, connection, slack_client):
    incident = {"id": payload["id"], "title": payload["title"]}
    saved = IncidentRepository(connection).save(incident)
    SlackNotifier(slack_client).send("#incidents", saved["title"])
    return saved
""")
    _write(repo / "server.py", """
from config import load_config
from handlers.incidents import create_incident


def health_check():
    return {"ok": True}


def route_request(path, payload, env, connection, slack_client):
    config = load_config(env)
    if path == "/health":
        return health_check()
    if path == "/incidents":
        return create_incident(payload, connection, slack_client)
    return {"error": "not found", "config": config}
""")
    _commit_all(repo, "add incident handler and routes")
    _write(repo / "tests/test_incidents.py", """
from handlers.incidents import create_incident


def test_create_incident_notifies_slack(fake_connection, fake_slack):
    result = create_incident({"id": "i-1", "title": "disk full"}, fake_connection, fake_slack)
    assert result["id"] == "i-1"
    assert fake_slack.messages[-1]["text"] == "disk full"
""")
    _commit_all(repo, "add incident test")
    return repo


def build_fixture_repos(base: Path) -> dict[str, Path]:
    return {
        "python_app": _build_python_app(base),
        "web_service": _build_web_service(base),
    }


def _precision_recall(selected: set[str], expected: set[str]) -> tuple[float, float]:
    if not selected:
        precision = 0.0 if expected else 1.0
    else:
        precision = len(selected & expected) / len(selected)
    recall = len(selected & expected) / len(expected) if expected else 1.0
    return precision, recall


def _symbol_hit(items: list[dict[str, Any]], expected_symbol: str) -> bool:
    path, _, qualname = expected_symbol.partition(":")
    names = [part for part in qualname.replace(".", ":").split(":") if part]
    for item in items:
        if item.get("path") != path:
            continue
        content = str(item.get("content", ""))
        if all(name in content for name in names):
            return True
    return False


def _selected_files(items: list[dict[str, Any]]) -> set[str]:
    return {
        str(item["path"])
        for item in items
        if item.get("kind") != "commit" and not str(item.get("path", "")).startswith("commit:")
    }


def _run_bundle(repo: Path, task: GoldTask, mode: str, db_path: Path) -> dict[str, Any]:
    rank = "bfs" if mode == "bfs" else "pagerank"
    budget = task.tight_budget if mode.startswith("skeleton_") else task.budget
    original_skeleton = bundle_mod._skeleton_item
    if mode == "skeleton_off":
        bundle_mod._skeleton_item = lambda *args, **kwargs: None
    started = time.perf_counter()
    try:
        result = bundle_mod.generate_bundle(
            repo,
            task=task.description,
            budget=budget,
            db_path=db_path,
            output_format="json",
            rank=rank,
        )
    finally:
        bundle_mod._skeleton_item = original_skeleton
    latency_ms = (time.perf_counter() - started) * 1000.0
    return {"bundle": result, "latency_ms": latency_ms}


def _score_task(task: GoldTask, mode: str, repo: Path, db_path: Path) -> dict[str, Any]:
    run = _run_bundle(repo, task, mode, db_path)
    bundle = run["bundle"]
    items = list(bundle["items"])
    selected = _selected_files(items)
    expected_files = set(task.expected_files)
    file_precision, file_recall = _precision_recall(selected, expected_files)
    symbol_hits = sum(1 for symbol in task.expected_symbols if _symbol_hit(items, symbol))
    symbol_recall = symbol_hits / len(task.expected_symbols) if task.expected_symbols else 1.0
    recall = (file_recall + symbol_recall) / 2 if task.expected_symbols else file_recall
    return {
        "task": task,
        "mode": mode,
        "precision": file_precision,
        "recall": recall,
        "file_precision": file_precision,
        "file_recall": file_recall,
        "symbol_recall": symbol_recall,
        "tokens": int(bundle["total_tokens"]),
        "latency_ms": run["latency_ms"],
        "files": sorted(selected),
    }


def _aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    modes = sorted({row["mode"] for row in rows})
    aggregates: list[dict[str, Any]] = []
    for mode in modes:
        mode_rows = [row for row in rows if row["mode"] == mode]
        count = len(mode_rows)
        aggregates.append(
            {
                "mode": mode,
                "tasks": count,
                "precision": sum(row["precision"] for row in mode_rows) / count,
                "recall": sum(row["recall"] for row in mode_rows) / count,
                "file_recall": sum(row["file_recall"] for row in mode_rows) / count,
                "symbol_recall": sum(row["symbol_recall"] for row in mode_rows) / count,
                "tokens": round(sum(row["tokens"] for row in mode_rows) / count),
                "latency_ms": sum(row["latency_ms"] for row in mode_rows) / count,
            }
        )
    return aggregates


def _format_markdown(rows: list[dict[str, Any]]) -> str:
    aggregates = _aggregate(rows)
    lines = [
        "# Cortex Eval Results",
        "",
        "Generated by `python3 evals/run_evals.py`.",
        "",
        "## Aggregate",
        "",
        "| Mode | Tasks | Precision | Recall | Avg Tokens | Avg Latency ms |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in aggregates:
        lines.append(
            f"| {row['mode']} | {row['tasks']} | {row['precision']:.3f} | "
            f"{row['recall']:.3f} | {row['tokens']} | {row['latency_ms']:.1f} |"
        )
    lines.extend(
        [
            "",
            "## Per Task",
            "",
            "| Task | Mode | Precision | Recall | File Recall | Symbol Recall | Tokens | Latency ms | Files |",
            "|---|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in rows:
        task = row["task"]
        files = ", ".join(row["files"])
        lines.append(
            f"| {task.description} | {row['mode']} | {row['precision']:.3f} | {row['recall']:.3f} | "
            f"{row['file_recall']:.3f} | {row['symbol_recall']:.3f} | {row['tokens']} | "
            f"{row['latency_ms']:.1f} | {files} |"
        )
    return "\n".join(lines) + "\n"


def run_evals(results_path: Path = ROOT / "evals" / "RESULTS.md") -> tuple[str, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="cortex-evals-") as tmp:
        base = Path(tmp)
        repos = build_fixture_repos(base)
        db_paths: dict[str, Path] = {}
        for name, repo in repos.items():
            db_path = base / f"{name}.db"
            ingest_repository(repo, commit_limit=20, db_path=db_path)
            db_paths[name] = db_path
        for task in GOLD_TASKS:
            repo = repos[task.repo]
            db_path = db_paths[task.repo]
            for mode in ("pagerank", "bfs", "skeleton_on", "skeleton_off"):
                rows.append(_score_task(task, mode, repo, db_path))
    markdown = _format_markdown(rows)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(markdown, encoding="utf-8")
    return markdown, rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Cortex gold-task evals")
    parser.add_argument("--json", action="store_true", help="Emit raw rows as JSON instead of Markdown")
    parser.add_argument("--results", type=Path, default=ROOT / "evals" / "RESULTS.md")
    args = parser.parse_args()
    markdown, rows = run_evals(args.results)
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        print(markdown, end="")


if __name__ == "__main__":
    main()
