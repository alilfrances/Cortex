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
    # P0-2: gold file findable only via body text -- "power-cycle" and
    # "gateway" appear only inside the error string literal in
    # app/messages.py, not in its filename, path, or the (unindexed)
    # constant name. Exercises the FTS5 fusion signal in generate_bundle,
    # not just cortex_search_text directly. Isolated in its own repo (see
    # _build_body_text_repo) so it can't perturb any other task's IDF-based
    # term weights.
    GoldTask(
        repo="body_text_repo",
        description="Locate the power-cycle gateway retry connection error message text",
        expected_files=("app/messages.py",),
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
    GoldTask(
        repo="noisy_lib",
        description="How does rank_nodes score and order graph nodes",
        expected_files=("src/ranker.py",),
        expected_symbols=("src/ranker.py:rank_nodes",),
    ),
    GoldTask(
        repo="noisy_lib",
        description="Where is the token budget applied when packing output",
        expected_files=("src/packer.py",),
        expected_symbols=("src/packer.py:apply_budget",),
    ),
    GoldTask(
        repo="refresh_distractors",
        description="fix the stale index detection in the auto refresh path",
        expected_files=("src/cortex/mcp/tools.py",),
        expected_symbols=("src/cortex/mcp/tools.py:_ensure_fresh",),
    ),
    GoldTask(
        repo="qt_app",
        description="Where is the deviceConnected signal emitted and which slot receives it",
        expected_files=(
            "include/DeviceManager.hpp",
            "src/DeviceManager.cpp",
            "include/DeviceModel.hpp",
            "src/DeviceModel.cpp",
        ),
        expected_symbols=(
            "include/DeviceManager.hpp:deviceConnected",
            "include/DeviceModel.hpp:onDeviceConnected",
        ),
    ),
    GoldTask(
        repo="qt_app",
        description="Find the QML delegate component and its declared click signal",
        expected_files=("qml/DeviceDelegate.qml", "qml/Main.qml"),
        expected_symbols=(
            "qml/DeviceDelegate.qml:DeviceDelegate",
            "qml/DeviceDelegate.qml:clicked",
        ),
    ),
    GoldTask(
        repo="qt_app",
        description="Find the Qt build files that register the QML scene and delegate for compilation",
        expected_files=("CMakeLists.txt", "resources.qrc"),
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


def _build_body_text_repo(base: Path) -> Path:
    """Small standalone fixture for the P0-2 body-text-only gold task.

    Deliberately isolated from the other fixture repos: python_app,
    web_service, etc. are each reused by several unrelated gold tasks, and
    `_term_weights`'s IDF is computed over every source in a repo, so
    dropping a new distinctive-vocabulary file into one of them would
    shift every other task's term weights and candidate pool slightly
    (confirmed by a regression run: adding the error-message file to
    python_app measurably moved precision on five unrelated python_app
    tasks). A dedicated repo keeps this task's fixture from perturbing any
    other task's baseline.
    """
    repo = base / "body_text_repo"
    _init_repo(repo)
    # The distinctive words ("gateway", "power-cycle") live only inside the
    # string literal, not in the constant name or the file name/path, and a
    # module-level string assignment is not extracted as a symbol node
    # (ast_extract.py only extracts functions/classes) -- so this file is
    # discoverable only through body-text/content search, never through
    # name or symbol matching.
    _write(repo / "app/messages.py", """
DEVICE_OFFLINE_ERROR = "please power-cycle the gateway before retrying the connection"
""")
    # Distractor sharing generic vocabulary ("retry", "connection") with the
    # gold file's string, as identifiers rather than the message text, so
    # the task genuinely exercises ranking instead of trivially resolving
    # to the only file in the repo.
    _write(repo / "app/network.py", """
def retry_connection(client, attempts=3):
    for _ in range(attempts):
        if client.connect():
            return True
    return False
""")
    _write(repo / "README.md", """
# Body Text Repo

Fixture repository exercising Cortex's FTS5 body-text search: the gold
error message lives only inside a string literal, not in any file name,
path, or indexed symbol name.
""")
    _commit_all(repo, "add device error message and network retry helper")
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


def _build_noisy_lib(base: Path) -> Path:
    """Repo with keyword-dense docs and a stale build/ duplicate — the noise that hides implementation files."""
    repo = base / "noisy_lib"
    _init_repo(repo)
    _write(repo / ".gitignore", """
build/
""")
    _write(repo / "README.md", """
# Noisy Lib

Noisy Lib ranks graph nodes and packs bundle output under a token budget.
The ranker scores nodes, orders graph nodes, and the packer applies the
token budget when packing output. Rank nodes, score nodes, order nodes:
this README repeats every keyword an implementation question would use,
including token budget, packing output, and graph node ordering.
""")
    _write(repo / "docs/plan.md", """
# Plan

Milestone 1: rank_nodes scores and orders graph nodes.
Milestone 2: apply_budget packs output under the token budget.
""")
    _write(repo / "src/ranker.py", """
def score_node(node, weights):
    return sum(weights.get(edge, 0.0) for edge in node["edges"])


def rank_nodes(nodes, weights):
    return sorted(nodes, key=lambda node: -score_node(node, weights))
""")
    _write(repo / "src/packer.py", """
def apply_budget(items, budget):
    packed, used = [], 0
    for item in items:
        if used + item["tokens"] <= budget:
            packed.append(item)
            used += item["tokens"]
    return packed
""")
    _commit_all(repo, "add noisy lib")
    # Stale untracked duplicate that ingest must exclude.
    _write(repo / "build/lib/ranker.py", """
def rank_nodes(nodes, weights):
    return nodes
""")
    return repo


def _build_refresh_distractors(base: Path) -> Path:
    repo = base / "refresh_distractors"
    _init_repo(repo)
    _write(repo / "src/cortex/mcp/tools.py", """
def _ensure_fresh(store, repo_root):
    status = detect_stale_index(store, repo_root)
    if status["stale"]:
        status["auto_refreshed"] = refresh_index_incrementally(store, repo_root)
    return status
""")
    common = """
def helper():
    return "fix the in a of for to and with from by path"
"""
    for path in (
        "src/cortex/cli.py",
        "tests/test_watch.py",
        "src/cortex/ingest.py",
        "src/cortex/report.py",
        "src/cortex/store.py",
        "docs/noise.md",
        "CHANGELOG.md",
        "README.md",
    ):
        _write(repo / path, common)
    _commit_all(repo, "add refresh distractor fixture")
    return repo


def _build_qt_app(base: Path) -> Path:
    """Small Qt/C++/QML fixture: two QObject classes wired via connect(), plus a QML
    scene that instantiates a local component and defines handlers. Second commit
    co-changes a .cpp and a .qml so COCHANGE edges form between them."""
    repo = base / "qt_app"
    _init_repo(repo)
    _write(repo / "include/DeviceManager.hpp", """
#pragma once
#include <QObject>

class DeviceManager : public QObject {
    Q_OBJECT
public:
    explicit DeviceManager(QObject *parent = nullptr);
    void scan();

signals:
    void deviceConnected(int deviceId);

public slots:
    void onDeviceConnected(int deviceId);
};
""")
    _write(repo / "src/DeviceManager.cpp", """
#include "DeviceManager.hpp"
#include "DeviceModel.hpp"

DeviceManager::DeviceManager(QObject *parent) : QObject(parent) {
    auto *model = new DeviceModel(this);
    connect(this, &DeviceManager::deviceConnected, model, &DeviceModel::onDeviceConnected);
}

void DeviceManager::onDeviceConnected(int deviceId) {
    scan();
}
""")
    _write(repo / "include/DeviceModel.hpp", """
#pragma once
#include <QObject>

class DeviceModel : public QObject {
    Q_OBJECT
public:
    explicit DeviceModel(QObject *parent = nullptr);

public slots:
    void onDeviceConnected(int deviceId);

signals:
    void modelUpdated();
};
""")
    _write(repo / "src/DeviceModel.cpp", """
#include "DeviceModel.hpp"

DeviceModel::DeviceModel(QObject *parent) : QObject(parent) {}

void DeviceModel::onDeviceConnected(int deviceId) {
    emit modelUpdated();
}
""")
    _write(repo / "qml/DeviceDelegate.qml", """
import QtQuick 2.15

Item {
    signal deviceConnected(int deviceId)
    signal clicked()

    MouseArea {
        anchors.fill: parent
        onClicked: clicked()
    }
}
""")
    _write(repo / "qml/Main.qml", """
import QtQuick 2.15
import QtQuick.Controls 2.15

ApplicationWindow {
    signal sceneReady()

    DeviceDelegate {
        id: delegate
        onClicked: console.log("delegate clicked")
        onDeviceConnected: console.log("device", deviceId)
    }
}
""")
    _write(repo / "CMakeLists.txt", """
add_executable(qt_app
    src/DeviceManager.cpp
    src/DeviceModel.cpp
)
target_link_libraries(qt_app PRIVATE Qt6::Core Qt6::Quick)
qt_add_qml_module(qt_app URI QtApp QML_FILES qml/Main.qml qml/DeviceDelegate.qml)
""")
    _write(repo / "resources.qrc", """
<RCC>
  <qresource prefix="/">
    <file>qml/Main.qml</file>
    <file>qml/DeviceDelegate.qml</file>
  </qresource>
</RCC>
""")
    _commit_all(repo, "add device manager/model and qml scene")
    _write(repo / "src/DeviceManager.cpp", """
#include "DeviceManager.hpp"
#include "DeviceModel.hpp"

DeviceManager::DeviceManager(QObject *parent) : QObject(parent) {
    auto *model = new DeviceModel(this);
    connect(this, &DeviceManager::deviceConnected, model, &DeviceModel::onDeviceConnected);
}

void DeviceManager::scan() {
    emit deviceConnected(42);
}

void DeviceManager::onDeviceConnected(int deviceId) {
    scan();
}
""")
    _write(repo / "qml/Main.qml", """
import QtQuick 2.15
import QtQuick.Controls 2.15

ApplicationWindow {
    signal sceneReady()

    DeviceDelegate {
        id: delegate
        onClicked: console.log("delegate clicked")
        onDeviceConnected: console.log("device connected", deviceId)
    }
}
""")
    _commit_all(repo, "wire deviceConnected scan and update qml handler")
    return repo


def build_fixture_repos(base: Path) -> dict[str, Path]:
    return {
        "python_app": _build_python_app(base),
        "web_service": _build_web_service(base),
        "noisy_lib": _build_noisy_lib(base),
        "refresh_distractors": _build_refresh_distractors(base),
        "qt_app": _build_qt_app(base),
        "body_text_repo": _build_body_text_repo(base),
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
    return set(_selected_file_list(items))


def _selected_file_list(items: list[dict[str, Any]]) -> list[str]:
    return [
        str(item["path"])
        for item in items
        if item.get("kind") != "commit" and not str(item.get("path", "")).startswith("commit:")
    ]


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
    selected_list = _selected_file_list(items)
    selected = set(selected_list)
    expected_files = set(task.expected_files)
    file_precision, file_recall = _precision_recall(selected, expected_files)
    top3 = selected_list[:3]
    precision_at_3 = len(set(top3) & expected_files) / 3
    symbol_hits = sum(1 for symbol in task.expected_symbols if _symbol_hit(items, symbol))
    symbol_recall = symbol_hits / len(task.expected_symbols) if task.expected_symbols else 1.0
    recall = (file_recall + symbol_recall) / 2 if task.expected_symbols else file_recall
    return {
        "task": task,
        "mode": mode,
        "precision": file_precision,
        "precision_at_3": precision_at_3,
        "recall": recall,
        "file_precision": file_precision,
        "file_recall": file_recall,
        "symbol_recall": symbol_recall,
        "tokens": int(bundle["total_tokens"]),
        "latency_ms": run["latency_ms"],
        "files": selected_list,
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
                "precision_at_3": sum(row["precision_at_3"] for row in mode_rows) / count,
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
        "| Mode | Tasks | Precision | Precision@3 | Recall | Avg Tokens | Avg Latency ms |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in aggregates:
        lines.append(
            f"| {row['mode']} | {row['tasks']} | {row['precision']:.3f} | {row['precision_at_3']:.3f} | "
            f"{row['recall']:.3f} | {row['tokens']} | {row['latency_ms']:.1f} |"
        )
    lines.extend(
        [
            "",
            "## Per Task",
            "",
            "| Task | Mode | Precision | Precision@3 | Recall | File Recall | Symbol Recall | Tokens | Latency ms | Files |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in rows:
        task = row["task"]
        files = ", ".join(row["files"])
        lines.append(
            f"| {task.description} | {row['mode']} | {row['precision']:.3f} | {row['precision_at_3']:.3f} | {row['recall']:.3f} | "
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
