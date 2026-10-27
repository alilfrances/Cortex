from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
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
    # Optional tasks are excluded from the default/off matrix so existing
    # rows and aggregate metrics remain unchanged.  They are eligible only
    # for an explicitly requested semantic-on run with a real local model.
    tags: tuple[str, ...] = ()


# Focused QML acceptance golds are kept separate from the historical default
# matrix so existing benchmark rows remain comparable across releases.
QML_GOLD_TASKS: tuple[GoldTask, ...] = (
    GoldTask("qml_full_repo", "Find QML properties and aliases", ("Main.qml",), ("Main.qml:title", "Main.qml:selected")),
    GoldTask("qml_full_repo", "Trace the binding dependency for card value", ("Main.qml", "Card.qml"), ("Main.qml:card",)),
    GoldTask("qml_full_repo", "Find inline component and module exports", ("Main.qml", "qmldir"), ("Main.qml:Header",)),
)


# Report 0.11 #6: spans of two files that all match the task must leave room for the helper
# files they call into. Kept out of GOLD_TASKS (like the QML golds) so the 17 historical
# rows stay comparable; tests/test_bundle_spans.py runs it.
SPREAD_GOLD_TASKS: tuple[GoldTask, ...] = (
    GoldTask(
        repo="spread_repo",
        description="fix the device reconnect timeout handling",
        expected_files=("src/device_session.cpp", "src/device_pool.cpp", "src/base.cpp", "src/manager.cpp"),
        budget=1500,
        tight_budget=1500,
    ),
)


# 0.12.0 review: the cause sits in the callees of the top match (a base-class implementation
# and a registry), reached only by resolved `calls` edges. Kept out of GOLD_TASKS; run by
# tests/test_bundle_call_expansion.py. See docs/plan-0.12.1-review.md, Task B.0.
COMPANION_GOLD_TASKS: tuple[GoldTask, ...] = (
    GoldTask(
        repo="companion_repo",
        description="device is reported and then deleted when its accessory case is replugged",
        expected_files=("src/device_manager.cpp", "src/peripheral_core.cpp", "src/registry.cpp"),
        budget=900,
        tight_budget=900,
    ),
)


# 0.13.0 field review, section F "Synthetic fixtures": the five shapes behind the T1-T4 recall
# misses, built without field code. Kept out of GOLD_TASKS (the historical rows stay comparable);
# run in all four eval modes by tests/test_bundle_field_tasks.py, one strict xfail per task naming
# the Phase 5 task that fixes it (docs/plan-0.14-field-review.md, Task 5.0). Every task uses one
# budget in all modes.
FIELD_GOLD_TASKS: tuple[GoldTask, ...] = (
    GoldTask(
        repo="field_hub_repo",
        description="slow when the charge max wait setting is on",
        expected_files=("src/gadgetwaitchargemax.cpp",),
        budget=900,
        tight_budget=900,
    ),
    GoldTask(
        repo="field_tiebreak_repo",
        description="firmware reset sequence aborts after the sync completes",
        expected_files=("z/ops/resetop.cpp",),
        budget=900,
        tight_budget=900,
    ),
    GoldTask(
        repo="field_override_repo",
        description="peripheral reattach demotes the entry and removes it",
        expected_files=("src/secondmanager.cpp", "src/accessory.cpp"),
        budget=900,
        tight_budget=900,
    ),
    GoldTask(
        repo="field_override_pure_repo",
        description="peripheral reattach demotes the entry and removes it",
        expected_files=("src/secondmanager.cpp", "src/accessory.cpp"),
        budget=900,
        tight_budget=900,
    ),
    GoldTask(
        repo="field_ctor_repo",
        description="controller startup hangs while the session handshake retries",
        expected_files=("src/hub_controller.cpp",),
        budget=900,
        tight_budget=900,
    ),
    GoldTask(
        repo="field_vocab_repo",
        description="Gizmos replug removed on iOS re-enumerates",
        expected_files=("src/gizmo_registry.cpp",),
        budget=900,
        tight_budget=900,
    ),
    GoldTask(
        repo="field_seed_markup_repo",
        description="item changes type and then the item is removed",
        expected_files=("src/itemmodel.cpp", "src/baseitem.cpp"),
        budget=2000,
        tight_budget=2000,
    ),
    GoldTask(
        repo="field_seed_markup_calls_repo",
        description="item changes type and then the item is removed",
        expected_files=("src/itemmodel.cpp", "src/baseitem.cpp"),
        budget=2000,
        tight_budget=2000,
    ),
    GoldTask(
        repo="field_seed_markup_local_repo",
        description="item changes type and then the item is removed",
        expected_files=("src/itemmodel.cpp", "src/baseitem.cpp"),
        budget=2000,
        tight_budget=2000,
    ),
    GoldTask(
        repo="field_seed_markup_flood_repo",
        description="item changes type and then the item is removed",
        expected_files=("src/itemmodel.cpp", "src/baseitem.cpp"),
        budget=1500,
        tight_budget=1500,
    ),
)


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
    GoldTask(
        repo="semantic_python_gap",
        description="Where is the credential verification lifecycle implemented?",
        expected_files=("zz_auth.py", "zz_session.py"),
        expected_symbols=("zz_auth.py:AuthService.login", "zz_session.py:SessionStore.create"),
        budget=120,
        tags=("optional", "semantic", "vocabulary-gap"),
    ),
    GoldTask(
        repo="semantic_qt_gap",
        description="Where is the button click handled?",
        expected_files=("zz/Delegate.qml", "zz/Main.qml", "zz/Controls.qml"),
        expected_symbols=(
            "zz/Delegate.qml:MouseArea.onClicked",
            "zz/Main.qml:Delegate.onClicked",
            "zz/Controls.qml:onClicked",
        ),
        budget=100,
        tags=("optional", "semantic", "qt"),
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


def _build_companion_repo(base: Path) -> Path:
    """0.12.0 review: the top match calls into a base-class implementation and a registry
    whose file names share no word with the task, among many files that only share words."""
    repo = base / "companion_repo"
    _init_repo(repo)
    pad = "\n".join(f"    int pad{i} = state_ + {i};" for i in range(40))
    big = "\n".join(f"    int pad{i} = device + {i};" for i in range(150))
    _write(repo / "src/device_manager.h", (
        "class PeripheralCore;\nclass DeviceManager {\npublic:\n  void onAccessoryCaseReplugged(PeripheralCore* d);\n"
        "  void reportDevice(PeripheralCore* d);\n  void deleteDevice(PeripheralCore* d);\n  int state_ = 0;\n};\n"
    ))
    _write(repo / "src/device_manager.cpp", (
        '#include "device_manager.h"\n#include "peripheral_core.h"\n\n'
        "void DeviceManager::onAccessoryCaseReplugged(PeripheralCore* d) {\n"
        "    // accessory case replugged: the device is reported again\n    reportDevice(d);\n}\n\n"
        f"void DeviceManager::reportDevice(PeripheralCore* d) {{\n    // device reported\n    d->assignCompanion();\n{pad}\n}}\n\n"
        f"void DeviceManager::deleteDevice(PeripheralCore* d) {{\n    // device deleted\n{pad}\n}}\n"
    ))
    _write(repo / "src/peripheral_core.h", "class PeripheralCore {\npublic:\n  void assignCompanion();\n  void releaseCompanion();\n  int pairing_ = 0;\n};\n")
    _write(repo / "src/peripheral_core.cpp", (
        '#include "peripheral_core.h"\n#include "registry.h"\n\n'
        "void PeripheralCore::assignCompanion() {\n    if (pairing_ == 0) {\n        releaseCompanion();\n    }\n}\n\n"
        "void PeripheralCore::releaseCompanion() {\n    Registry::drop(pairing_);\n}\n"
    ))
    _write(repo / "src/registry.h", "class Registry {\npublic:\n  static void drop(int id);\n};\n")
    _write(repo / "src/registry.cpp", '#include "registry.h"\nvoid Registry::drop(int id) {\n    int gone = id;\n    gone += 1;\n}\n')
    _write(repo / "src/tag_helper.cpp", f"// device tag helper: reported device names, case tags, accessory labels\nint tagFor(int device) {{\n{big}\n    return device;\n}}\n")
    _write(repo / "src/admin_control.cpp", (
        '#include "device_manager.h"\n// admin control: delete a reported device, replugged accessory case\n'
        f"void adminDelete(DeviceManager* m, PeripheralCore* d) {{\n    m->deleteDevice(d);\n{pad}\n}}\n"
    ))
    _write(repo / "qml/DevicePopup.qml", (
        "import QtQuick 2.15\nItem {\n    // popup: device reported, deleted, accessory case replugged\n"
        '    property string text: "Device deleted"\n' + "".join(f"    property int p{i}: {i}\n" for i in range(30)) + "}\n"
    ))
    names = ("device_list_model", "case_status", "accessory_icons", "report_log", "replug_watch")
    for name in names + tuple(f"device_widget_{k}" for k in range(30)):
        words = name.replace("_", " ")
        _write(repo / f"src/{name}.cpp", (
            f"// {words}: device reported, accessory case, deleted entries\nint {name}_fn(int device) {{\n{big}\n    return device;\n}}\n"
        ))
    _commit_all(repo, "add companion fixture")
    return repo


def _build_spread_repo(base: Path) -> Path:
    """Two files whose many methods all match the task, which call into two helper
    files that share no word with it: the spans must not use up the whole budget."""
    repo = base / "spread_repo"
    _init_repo(repo)

    def method(cls: str, name: str) -> str:
        steps = "\n".join(f"    int step{i} = value_ + {i};  // {name} reconnect timeout handling" for i in range(6))
        return f"int {cls}::{name}(int value_) {{\n{steps}\n    Base::init(value_);\n    Manager::track(value_);\n    return value_;\n}}\n\n"

    session = ("reconnectDevice", "reconnectTimeout", "handleReconnect", "timeoutReconnect", "reconnectDelay", "reconnectLimit")
    pool = ("reconnectAll", "reconnectPending", "timeoutPool", "reconnectBackoff", "poolReconnect")
    includes = '#include "base.h"\n#include "manager.h"\n'
    _write(repo / "src/device_session.cpp", includes + "\n" + "".join(method("DeviceSession", n) for n in session))
    _write(repo / "src/device_pool.cpp", includes + "\n" + "".join(method("DevicePool", n) for n in pool))
    _write(repo / "src/device_session.h", "class DeviceSession {\npublic:\n" + "".join(f"  int {n}(int);\n" for n in session) + "};\n")
    _write(repo / "src/device_pool.h", "class DevicePool {\npublic:\n" + "".join(f"  int {n}(int);\n" for n in pool) + "};\n")
    _write(repo / "src/base.h", "class Base { public: static void init(int); };\n")
    _write(repo / "src/manager.h", "class Manager { public: static void track(int); };\n")
    _write(repo / "src/base.cpp", '#include "base.h"\nvoid Base::init(int v) {\n    int state = v;\n    state += 1;\n}\n')
    _write(repo / "src/manager.cpp", '#include "manager.h"\nvoid Manager::track(int v) {\n    int seen = v;\n    seen += 2;\n}\n')
    _commit_all(repo, "add spread fixture")
    return repo


# Fixed vocabulary for the hub fixture: the most common English words plus the task's own words,
# so a hub's comments contain nearly every task term (review section F, causes 1-2).
_HUB_WORDS = tuple(dict.fromkeys(
    "the be to of and a in that have it for not on with as you do at this but by from they we say her she or an "
    "will my one all would there their what so up out if about who get which go me when make can like time no "
    "just him know take people into year your good some could them see other than then now look only come its "
    "over think also back after use two how our work first well way even new want because any these give day "
    "most us find very every too shows during handles path device info state event update value slow charge "
    "max wait setting on off level mode queue buffer timeout count flag reset sync read write open close start "
    "stop load save check apply report status power battery cable port link signal rate limit step cycle".split()
))


def _build_field_hub_repo(base: Path, seed: int = 5023) -> Path:
    """Hub flood: four ~3k-line files whose comments use every common word and whose hundreds of
    `deviceX()` methods collect a name hit through `device`, next to a small file that is the answer.
    `seed` drives the generated words and names (5023 is the committed fixture)."""
    import random

    repo = base / "field_hub_repo"
    _init_repo(repo)
    rng = random.Random(seed)
    words = _HUB_WORDS

    def sentence(count: int) -> str:
        return " ".join(rng.choice(words) for _ in range(count))

    def generate(stem: str, methods: int, statements: int) -> None:
        cls = "".join(part.capitalize() for part in stem.split("_"))
        seen: set[str] = set()
        parts = [f"// {sentence(14)}\n"]
        while len(seen) < methods:
            lead = rng.choice(("wait", "charge", "max", "setting", "slow") if rng.random() < 0.6 else words)
            name = "device" + lead.capitalize() + rng.choice(words).capitalize()
            if name in seen:
                continue
            seen.add(name)
            body = "".join(f"    int v{k} = state_ + {k};  // {sentence(6)}\n" for k in range(rng.randint(2, statements)))
            parts.append(f"void {cls}::{name}() {{\n    // {sentence(12)}\n{body}}}\n\n")
        _write(repo / f"src/{stem}.cpp", "".join(parts))

    for hub in ("core_device", "core_session", "core_events", "core_state"):
        generate(hub, 300, 14)
    # The smaller items that ride along with the hubs in the field: a header, a workflow item, an operation.
    for satellite in ("device_info", "workflow_item", "android_operation", "shared_defs", "device_header", "info_step"):
        generate(satellite, 6, 14)
    _write(repo / "src/gadgetwaitchargemax.cpp", (
        "class GadgetWaitChargeMax {\npublic:\n    int level_ = 0;\n    int polls_ = 0;\n    int total_ = 0;\n"
        "    int limit_ = 1000;\n    int step_ = 3;\n    int last_ = 0;\n    bool armed_ = false;\n"
        "    void apply(int level) {\n        while (polls_ < level) {\n            total_ += polls_ * step_;\n"
        "            polls_ += 1;\n        }\n        level_ = total_ > limit_ ? limit_ : total_;\n    }\n};\n"
    ))
    _commit_all(repo, "add hub flood fixture")
    return repo


def _build_field_tiebreak_repo(base: Path) -> Path:
    """Tie-break: a sequencer reaches 30 cross-file one-line virtuals under `a/` (each with an
    overrider) and one relevant callee in `z/ops/resetop.cpp`; every callee scores seed x 0.9."""
    repo = base / "field_tiebreak_repo"
    _init_repo(repo)
    stages = []
    for stage in range(10):
        calls = "".join(f"    V{3 * stage + k:02d}::probe{3 * stage + k:02d}();\n" for k in range(3))
        if stage == 9:
            calls += "    ResetOp::execute(code);\n"
        stages.append(f"void Sequencer::stage{stage}(int code) {{\n{calls}    code += 1;\n    code *= 2;\n}}\n\n")
    run = "".join(f"    stage{stage}(code);\n" for stage in range(10))
    _write(repo / "src/sequencer.cpp", (
        "// firmware reset sequence: runs every stage, aborts after the sync completes\n"
        f"void Sequencer::runResetSequence(int code) {{\n    // reset sequence aborts after the sync completes\n{run}}}\n\n" + "".join(stages)
    ))
    for index in range(30):
        _write(repo / f"a/v{index:02d}.h", f"class V{index:02d} {{\npublic:\n    virtual int probe{index:02d}() {{ return 0; }}\n}};\n")
        _write(repo / f"a/w{index:02d}.h", (
            f'#include "v{index:02d}.h"\nclass W{index:02d} : public V{index:02d} {{\npublic:\n'
            f"    int probe{index:02d}() override {{ return 1; }}\n}};\n"
        ))
    _write(repo / "z/ops/resetop.h", "class ResetOp {\npublic:\n    static void execute(int code);\n};\n")
    _write(repo / "z/ops/resetop.cpp", (
        '#include "resetop.h"\n\nvoid ResetOp::execute(int code) {\n'
        "    // reset the sequence: abort pending work, then sync again\n"
        "    int pending = code;\n    pending -= 1;\n    pending *= 3;\n}\n"
    ))
    _commit_all(repo, "add tie-break fixture")
    return repo


def _build_field_override_repo(base: Path, *, pure: bool = False) -> Path:
    """Override chain (T1 shape): the manager calls a virtual on a base manager; the behaviour is
    in the second manager's override, which calls into an accessory class that shares no task word.

    `pure=True` declares the base method `= 0` (the field's actual shape); the default keeps the
    `{}` body that the fixture had while the extractor emitted no symbol for a pure virtual."""
    repo = base / ("field_override_pure_repo" if pure else "field_override_repo")
    _init_repo(repo)
    pad = "\n".join(f"    int pad{i} = value + {i};" for i in range(40))
    _write(repo / "src/manager.h", "class BaseManager;\nclass Manager {\npublic:\n    void onReattach(BaseManager* base);\n};\n")
    _write(repo / "src/manager.cpp", (
        '#include "manager.h"\n#include "basemanager.h"\n\n'
        "void Manager::onReattach(BaseManager* base) {\n"
        "    // peripheral reattach: demotes the entry, then removes it\n"
        "    int attempts = 3;\n    base->createForCompanion();\n    attempts -= 1;\n}\n"
    ))
    _write(repo / "src/basemanager.h", (
        "class BaseManager {\npublic:\n    virtual void createForCompanion() = 0;\n};\n" if pure
        else "class BaseManager {\npublic:\n    virtual void createForCompanion() {}\n};\n"
    ))
    _write(repo / "src/secondmanager.h", (
        '#include "basemanager.h"\nclass SecondManager : public BaseManager {\npublic:\n    void createForCompanion() override;\n};\n'
    ))
    _write(repo / "src/secondmanager.cpp", (
        '#include "secondmanager.h"\n#include "accessory.h"\n\n'
        "void SecondManager::createForCompanion() {\n    // routes the entry\n    int slot = 0;\n"
        "    Accessory::assignCompanion(slot);\n    slot += 1;\n}\n"
    ))
    _write(repo / "src/accessory.h", "class Accessory {\npublic:\n    static void assignCompanion(int slot);\n};\n")
    _write(repo / "src/accessory.cpp", (
        '#include "accessory.h"\n\nvoid Accessory::assignCompanion(int slot) {\n'
        "    int bound = slot;\n    bound += 2;\n    bound *= 3;\n    bound -= 1;\n}\n"
    ))
    # Spans that score above the routing function: other files about the same words.
    for k in range(12):
        _write(repo / f"src/noise_{k}.cpp", (
            f"// peripheral entry: demotes and removes ({k})\n"
            f"void step{k}(int value) {{\n    // demotes the entry, removes it after the peripheral reattach\n{pad}\n}}\n"
        ))
    _commit_all(repo, "add override chain fixture")
    return repo


def _build_field_override_pure_repo(base: Path) -> Path:
    """`field_override_repo` with the base method pure virtual (`= 0`)."""
    return _build_field_override_repo(base, pure=True)


def _build_field_ctor_repo(base: Path) -> Path:
    """Constructor flood: a 300-line hub constructor calls 100 resolved helpers, none of which shares
    a word with the task except the one named `session_handshake`."""
    repo = base / "field_ctor_repo"
    _init_repo(repo)
    calls = []
    for index in range(100):
        name = "SessionHandshake" if index == 60 else f"Helper{index:03d}"
        calls.append((index, name))
        stem = "session_handshake" if index == 60 else f"helper{index:03d}"
        method = "run" if index == 60 else f"init{index:03d}"
        _write(repo / f"src/helpers/{stem}.h", f"class {name} {{\npublic:\n    static void {method}(int v);\n}};\n")
        _write(repo / f"src/helpers/{stem}.cpp", (
            f'#include "{stem}.h"\n\nvoid {name}::{method}(int v) {{\n    int cell = v;\n    cell += {index};\n'
            "    cell *= 2;\n    cell -= 1;\n}\n"
        ))
    body = []
    for index, name in calls:
        method = "run" if name == "SessionHandshake" else f"init{index:03d}"
        body.append(f"    // controller startup hangs while the session handshake retries ({index})\n")
        body.append(f"    {name}::{method}(index_);\n")
        body.append("    index_ += 1;\n")
    _write(repo / "src/hub_controller.h", "class HubController {\npublic:\n    HubController();\n    void retryHandshake(int tries);\n    int index_ = 0;\n};\n")
    _write(repo / "src/hub_controller.cpp", (
        '#include "hub_controller.h"\n\nHubController::HubController() {\n' + "".join(body) + "}\n\n"
        "void HubController::retryHandshake(int tries) {\n    // handshake retries\n    index_ = tries;\n    index_ += 1;\n    index_ *= 2;\n}\n"
    ))
    # Files that merely share words with the task and outscore a file that is only linked by a call, so
    # the bundle is full before the callees of the constructor's file are reached.
    for k in range(12):
        steps = "".join(f"    cell += {k + step};\n    cell *= 2;\n" for step in range(5))
        _write(repo / f"src/startup/aux_{k}.cpp", (
            f"// controller startup hangs while the session handshake retries ({k})\n"
            f"int auxRun{k}(int cell) {{\n{steps}    return cell;\n}}\n"
        ))
    _commit_all(repo, "add constructor flood fixture")
    return repo


def _build_field_seed_markup_repo(base: Path, *, ui_calls: bool = False) -> Path:
    """Call-expansion seeds (0.14.0 field review, section 1.4): six small QML screens dense with the task's
    words and no resolved call, and one large C++ model file whose `onTypeChanged` calls a virtual with two
    overriders. The screens outscore the model file on raw text and the model file loses on length-normalised
    score, so the seeds go to the screens, which reach nothing. With `ui_calls`, `onItemTypeChanged` and
    `removeItem` of each screen call a helper of the same file (`hideItem`), so their spans have an outgoing `calls`
    edge and survive a filter on "reaches something"; `onItemTypeChanged` carries three task words in its name and
    outscores the model file's `onTypeChanged` (126.5 against 121.5), so the sixth seed slot, which goes by score,
    does not fall to the model file either. Only ranking the files by the score their spans inherit seeds it."""
    repo = base / ("field_seed_markup_calls_repo" if ui_calls else "field_seed_markup_repo")
    _init_repo(repo)
    for k in range(6):
        helper = (
            f"    function hideItem() {{\n        // hide the item that was removed ({k})\n"
            "        listItem.visible = false\n        removedItem.opacity = 0\n        itemTypeLabel.text = itemType\n    }\n"
        ) if ui_calls else ""
        call = "        hideItem()\n" if ui_calls else ""
        _write(repo / f"ui/ItemScreen{k}.qml", (
            "import QtQuick 2.15\nItem {\n    id: root\n    property int itemType: 0\n"
            f"    function onItemTypeChanged() {{\n        // item type changed then the item is removed ({k})\n"
            f"        removedItem.visible = false\n{call}        itemTypeLabel.text = itemType\n        listItem.opacity = 0\n    }}\n"
            f"    function removeItem() {{\n        // remove item when the item type changed\n{call}"
            "        listItem.visible = false\n        removedItem.opacity = 1\n        itemType = 0\n    }\n"
            f"{helper}}}\n"
        ))
    _write(repo / "src/baseitem.h", "class BaseItem {\npublic:\n    virtual ~BaseItem();\n    virtual void detach(int slot);\n};\n")
    _write(repo / "src/baseitem.cpp", (
        '#include "baseitem.h"\nBaseItem::~BaseItem() {}\n'
        "void BaseItem::detach(int slot) {\n    int cell = slot;\n    cell += 1;\n    cell *= 2;\n}\n"
    ))
    for name in ("TextEntry", "ImageEntry"):
        stem = name.lower()
        _write(repo / f"src/kinds/{stem}.h", (
            f'#include "../baseitem.h"\nclass {name} : public BaseItem {{\npublic:\n    void detach(int slot) override;\n}};\n'
        ))
        _write(repo / f"src/kinds/{stem}.cpp", (
            f'#include "{stem}.h"\nvoid {name}::detach(int slot) {{\n    int cell = slot;\n    cell -= 1;\n    cell *= 3;\n}}\n'
        ))
    declarations = "".join(f"    void refresh{index:03d}(int v);\n" for index in range(150))
    filler = "".join(
        f"void ItemModel::refresh{index:03d}(int v) {{\n    // item model refresh {index}\n    int cell = v;\n"
        f"    cell += {index};\n    cell *= 2;\n    cell -= 1;\n}}\n\n"
        for index in range(150)
    )
    _write(repo / "src/itemmodel.h", (
        '#include "baseitem.h"\nclass ItemModel {\npublic:\n    void onTypeChanged(BaseItem* entry, int slot);\n' + declarations + "};\n"
    ))
    _write(repo / "src/itemmodel.cpp", (
        '#include "itemmodel.h"\n\nvoid ItemModel::onTypeChanged(BaseItem* entry, int slot) {\n'
        "    // the item type changed: the old item is removed\n    int removed = slot;\n    removed += 1;\n"
        "    entry->detach(removed);\n}\n\n" + filler
    ))
    _commit_all(repo, "add call-expansion seed fixture")
    return repo


def _build_field_seed_markup_calls_repo(base: Path) -> Path:
    """`field_seed_markup_repo` with a same-file helper call in each screen's `removeItem`."""
    return _build_field_seed_markup_repo(base, ui_calls=True)


def _build_field_seed_markup_local_repo(base: Path, *, flood: int = 0) -> Path:
    """`field_seed_markup_calls_repo` with the screens turned up until they take every seed slot whatever the
    ranking (0.15.1 Task 3, finding 1 gap a). Ten QML screens, each with four functions that call a same-file
    helper of five lines: a QML same-file call resolves to an `external:qml:<screen>:hideItem` node, so every one of
    those spans has an outgoing `calls` edge and passes a filter on "reaches something", but nothing they call
    lives in another file. They carry more of the task's words than `src/itemmodel.cpp`, which is small enough to
    escape length normalisation (a model file of ten lines, no filler) and whose `onTypeChanged` still calls
    `entry->detach(...)` into `baseitem.cpp`, the virtual with two overriders. With `flood` (0.15.1 Task 4), that many large
    screens named after the task (`ui/ItemTypeRemoved<k>.qml`) are added: they outscore the callees of the model file
    and fill a small budget, so the callees are computed and then left out of the bundle."""
    repo = base / ("field_seed_markup_flood_repo" if flood else "field_seed_markup_local_repo")
    _init_repo(repo)
    for k in range(10):
        helper = (
            f"    function hideItem() {{\n        // hide the item that was removed ({k})\n"
            "        listItem.visible = false\n        removedItem.opacity = 0\n        itemTypeLabel.text = itemType\n    }\n"
        )
        _write(repo / f"ui/ItemScreen{k}.qml", (
            "import QtQuick 2.15\nItem {\n    id: root\n    property int itemType: 0\n"
            f"    function onItemTypeChanged() {{\n        // item type changed then the item is removed ({k})\n"
            "        removedItem.visible = false\n        hideItem()\n        itemTypeLabel.text = itemType\n        listItem.opacity = 0\n    }\n"
            "    function removeItem() {\n        // remove item when the item type changed\n        hideItem()\n"
            "        listItem.visible = false\n        removedItem.opacity = 1\n        itemType = 0\n    }\n"
            "    function changeItemType() {\n        // the item changes type and the item is removed\n        hideItem()\n"
            "        itemType = 1\n        removedItem.visible = true\n        itemTypeLabel.text = itemType\n    }\n"
            "    function itemRemoved() {\n        // the removed item changes the type of the item\n        hideItem()\n"
            "        itemType = 2\n        removedItem.opacity = 1\n        listItem.visible = false\n    }\n"
            f"{helper}}}\n"
        ))
    _write(repo / "src/baseitem.h", "class BaseItem {\npublic:\n    virtual ~BaseItem();\n    virtual void detach(int slot);\n};\n")
    _write(repo / "src/baseitem.cpp", (
        '#include "baseitem.h"\nBaseItem::~BaseItem() {}\n'
        "void BaseItem::detach(int slot) {\n    int cell = slot;\n    cell += 1;\n    cell *= 2;\n}\n"
    ))
    for name in ("TextEntry", "ImageEntry"):
        stem = name.lower()
        _write(repo / f"src/kinds/{stem}.h", (
            f'#include "../baseitem.h"\nclass {name} : public BaseItem {{\npublic:\n    void detach(int slot) override;\n}};\n'
        ))
        _write(repo / f"src/kinds/{stem}.cpp", (
            f'#include "{stem}.h"\nvoid {name}::detach(int slot) {{\n    int cell = slot;\n    cell -= 1;\n    cell *= 3;\n}}\n'
        ))
    _write(repo / "src/itemmodel.h", (
        '#include "baseitem.h"\nclass ItemModel {\npublic:\n    void onTypeChanged(BaseItem* entry, int slot);\n};\n'
    ))
    _write(repo / "src/itemmodel.cpp", (
        '#include "itemmodel.h"\n\nvoid ItemModel::onTypeChanged(BaseItem* entry, int slot) {\n'
        "    int old = slot;\n    old += 1;\n    entry->detach(old);\n}\n"
    ))
    for k in range(flood):
        functions = "".join(
            f"    function itemTypeChanged{j}() {{\n        // the item changes type and then the item is removed ({k}.{j})\n"
            f"        removedItem.visible = false\n        itemTypeLabel.text = itemType + {j}\n        listItem.opacity = 0\n"
            f"        itemType = {j}\n    }}\n"
            for j in range(12)
        )
        _write(repo / f"ui/ItemTypeRemoved{k}.qml", f"import QtQuick 2.15\nItem {{\n    property int itemType: 0\n{functions}}}\n")
    _commit_all(repo, "add local-call seed fixture" + (" with a flood of keyword screens" if flood else ""))
    return repo


def _build_field_seed_markup_flood_repo(base: Path) -> Path:
    """`field_seed_markup_local_repo` plus twelve large screens named after the task (0.15.1 Task 4)."""
    return _build_field_seed_markup_local_repo(base, flood=12)


def _build_field_vocab_repo(base: Path) -> Path:
    """Vocabulary: the task says `Gizmos`, `iOS` and `re-enumerates`; the code says `Gizmo`, `reconnect`
    and `remove`, and the other files talk about `os` and `re` so those split parts would match."""
    repo = base / "field_vocab_repo"
    _init_repo(repo)
    _write(repo / "src/gizmo_registry.cpp", (
        "class Gizmo {\npublic:\n    void reconnect(int id);\n    void remove(int id);\n};\n\n"
        "void Gizmo::reconnect(int id) {\n    int slot = id;\n    slot += 1;\n    slot *= 2;\n}\n\n"
        "void Gizmo::remove(int id) {\n    int slot = id;\n    slot -= 1;\n    slot *= 3;\n}\n"
    ))
    for k in range(30):
        _write(repo / f"src/os_probe_{k}.cpp", (
            f"// os version probe {k}: os name, os build, re-init of the os layer\n"
            f"int osProbe{k}(int os) {{\n    int re = os;\n    re += {k};\n    re *= 2;\n    re -= 1;\n    re += os;\n    return re;\n}}\n"
        ))
        _write(repo / f"src/re_init_{k}.cpp", (
            f"// re init {k}: re-read the os settings, re-open, re-try\n"
            f"int reInit{k}(int re) {{\n    int os = re;\n    os += {k};\n    os *= 3;\n    os -= 2;\n    os += re;\n    return os;\n}}\n"
        ))
    _commit_all(repo, "add vocabulary fixture")
    return repo


def _build_qt_app(base: Path) -> Path:
    """Small Qt/C++/QML fixture: two QObject classes wired via connect(), plus a QML
    scene that instantiates a local component and a real C++ ``DeviceManager``
    type and defines handlers. Second commit co-changes a .cpp and a .qml so
    COCHANGE edges form between them."""
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

    DeviceManager {}

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
    const auto deviceId = 42;
    switch (deviceId) {
    case 42:
        emit deviceConnected(deviceId);
        break;
    case 7:
        emit deviceConnected(deviceId);
        break;
    default:
        break;
    }
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

    DeviceManager {}

    DeviceDelegate {
        id: delegate
        onClicked: console.log("delegate clicked")
        onDeviceConnected: console.log("device connected", deviceId)
    }
}
""")
    _commit_all(repo, "wire deviceConnected scan and update qml handler")
    return repo


def _semantic_noise_markdown(title: str) -> str:
    """Large, vocabulary-neutral prose for controlled semantic-gap evals."""

    return "\n".join(
        [
            f"# {title}",
            "",
            ("Inventory telemetry scheduling and deterministic storage notes. "
             "This document is intentionally unrelated to the target behavior. " * 18),
        ]
    )


def _build_semantic_python_gap(base: Path) -> Path:
    """Isolated vocabulary-gap fixture: late gold files, early distractors.

    The query words credential/verification/lifecycle do not occur in any
    path, identifier, body, or prose. A small budget means lexical/off mode
    selects the alphabetically early markdown distractors; the real local
    model must bridge to the password/login/session implementation files.
    """

    repo = base / "semantic_python_gap"
    _init_repo(repo)
    # Write gold files first so later distractor mtimes win the otherwise-tied
    # recency fallback in semantic-off mode.
    _write(repo / "zz_auth.py", """
class AuthService:
    def __init__(self, users):
        self.users = users

    def login(self, username, password):
        user = self.users[username]
        if user["password"] != password:
            raise ValueError("bad password")
        return f"token-{user['id']}"
""")
    _write(repo / "zz_session.py", """
class SessionStore:
    def __init__(self):
        self.sessions = {}

    def create(self, user_id):
        token = f"token-{user_id}"
        self.sessions[token] = {"user_id": user_id, "expires": 999999}
        return token

    def prune_expired(self, now):
        return [token for token, row in self.sessions.items() if row["expires"] < now]
""")
    for name in ("aa_notes.md", "ab_inventory.md", "ac_telemetry.md", "ad_scheduler.md"):
        _write(repo / name, _semantic_noise_markdown(name))
    _commit_all(repo, "add controlled credential vocabulary gap fixture")
    return repo


def _build_semantic_qt_gap(base: Path) -> Path:
    """Isolated Qt vocabulary-gap fixture for a QML MouseArea handler."""

    repo = base / "semantic_qt_gap"
    _init_repo(repo)
    # Keep the target's mtime before distractors so off-mode cannot win by
    # recency when the task has no lexical overlap.
    _write(repo / "zz/Delegate.qml", """
import QtQuick 2.15

Item {
    id: delegateRoot

    MouseArea {
        anchors.fill: parent
        onClicked: console.log("activated")
    }
}
""")
    _write(repo / "zz/Main.qml", """
import QtQuick 2.15

Item {
    Delegate {
        id: delegate
        onClicked: console.log("forwarded")
    }
}
""")
    _write(repo / "zz/Controls.qml", """
import QtQuick 2.15

Item {
    MouseArea {
        anchors.fill: parent
        onClicked: console.log("confirmed")
    }
}
""")
    for name in ("aa_catalog.md", "ab_theme.md", "ac_network.md"):
        _write(repo / name, _semantic_noise_markdown(name))
    _commit_all(repo, "add controlled qml handler vocabulary gap fixture")
    return repo


def build_semantic_fixture_repos(base: Path) -> dict[str, Path]:
    """Build only the optional P1-7 fixtures; default rows never see them."""

    return {
        "semantic_python_gap": _build_semantic_python_gap(base),
        "semantic_qt_gap": _build_semantic_qt_gap(base),
    }


def build_fixture_repos(base: Path) -> dict[str, Path]:
    return {
        "python_app": _build_python_app(base),
        "web_service": _build_web_service(base),
        "noisy_lib": _build_noisy_lib(base),
        "refresh_distractors": _build_refresh_distractors(base),
        "spread_repo": _build_spread_repo(base),
        "companion_repo": _build_companion_repo(base),
        "field_hub_repo": _build_field_hub_repo(base),
        "field_tiebreak_repo": _build_field_tiebreak_repo(base),
        "field_override_repo": _build_field_override_repo(base),
        "field_override_pure_repo": _build_field_override_pure_repo(base),
        "field_ctor_repo": _build_field_ctor_repo(base),
        "field_vocab_repo": _build_field_vocab_repo(base),
        "field_seed_markup_repo": _build_field_seed_markup_repo(base),
        "field_seed_markup_calls_repo": _build_field_seed_markup_calls_repo(base),
        "field_seed_markup_local_repo": _build_field_seed_markup_local_repo(base),
        "field_seed_markup_flood_repo": _build_field_seed_markup_flood_repo(base),
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
    previous_semantic = os.environ.get("CORTEX_SEMANTIC")
    # The historical modes are explicitly semantic-off even on a machine that
    # happens to have a local model.  semantic_on is opt-in and is only added
    # by run_evals after a real local model has been loaded successfully.
    os.environ["CORTEX_SEMANTIC"] = "1" if mode == "semantic_on" else "0"
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
        if previous_semantic is None:
            os.environ.pop("CORTEX_SEMANTIC", None)
        else:
            os.environ["CORTEX_SEMANTIC"] = previous_semantic
    latency_ms = (time.perf_counter() - started) * 1000.0
    return {"bundle": result, "latency_ms": latency_ms}


def _packing_metrics(items: list[dict[str, Any]], expected_files: set[str], total_tokens: int) -> tuple[float, int]:
    """What the packer chose, beside what it found: (`gold_token_share`, `expansion_items`).

    The share is the tokens of packed items whose path is an expected file, over the bundle's `total_tokens` (0 for an
    empty bundle); the count is the packed items that carry a `call_expansion` route (a callee span, or its file
    packed whole). Neither is gated: file recall cannot tell a bundle that spends its budget on the gold files from one
    that merely contains them."""
    gold_tokens = sum(int(item.get("token_count", 0)) for item in items if item.get("path") in expected_files)
    share = gold_tokens / total_tokens if total_tokens > 0 else 0.0
    expansion_items = sum(1 for item in items if (item.get("metadata") or {}).get("call_expansion"))
    return share, expansion_items


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
    gold_token_share, expansion_items = _packing_metrics(items, expected_files, int(bundle["total_tokens"]))
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
        "gold_token_share": gold_token_share,
        "expansion_items": expansion_items,
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
                "gold_token_share": sum(row["gold_token_share"] for row in mode_rows) / count,
                "expansion_items": sum(row["expansion_items"] for row in mode_rows) / count,
                "latency_ms": sum(row["latency_ms"] for row in mode_rows) / count,
            }
        )
    return aggregates


def _format_markdown(
    rows: list[dict[str, Any]],
    command: str = "python3 evals/run_evals.py",
) -> str:
    aggregates = _aggregate(rows)
    lines = [
        "# Cortex Eval Results",
        "",
        f"Generated by `{command}`.",
        "",
        "## Aggregate",
        "",
        "| Mode | Tasks | Precision | Precision@3 | Recall | Avg Tokens | Gold Token Share | Expansion Items | Avg Latency ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in aggregates:
        lines.append(
            f"| {row['mode']} | {row['tasks']} | {row['precision']:.3f} | {row['precision_at_3']:.3f} | "
            f"{row['recall']:.3f} | {row['tokens']} | {row['gold_token_share']:.3f} | {row['expansion_items']:.2f} | "
            f"{row['latency_ms']:.1f} |"
        )
    lines.extend(
        [
            "",
            "## Per Task",
            "",
            "| Task | Mode | Precision | Precision@3 | Recall | File Recall | Symbol Recall | Tokens | Gold Token Share | Expansion Items | Latency ms | Files |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in rows:
        task = row["task"]
        files = ", ".join(row["files"])
        lines.append(
            f"| {task.description} | {row['mode']} | {row['precision']:.3f} | {row['precision_at_3']:.3f} | {row['recall']:.3f} | "
            f"{row['file_recall']:.3f} | {row['symbol_recall']:.3f} | {row['tokens']} | "
            f"{row['gold_token_share']:.3f} | {row['expansion_items']} | {row['latency_ms']:.1f} | {files} |"
        )
    return "\n".join(lines) + "\n"


def ingest_overhead_ratio(baseline_seconds: float, semantic_seconds: float) -> float | None:
    """Return the measured semantic/baseline ingest ratio, if meaningful."""

    if baseline_seconds <= 0:
        return None
    return semantic_seconds / baseline_seconds


def measure_ingest_overhead(
    repo: Path,
    *,
    commit_limit: int = 20,
    repeats: int = 3,
) -> dict[str, Any]:
    """Measure optional semantic ingest cost with repeated isolated runs.

    This helper is intentionally not part of the default eval matrix. It
    requires a real local model, performs no setup/download, and reports
    per-run plus median baseline/semantic timings. Median ratio is the P1-7
    ``<2x`` acceptance signal; one tiny-fixture timing is not treated as proof.
    """

    from cortex.semantic import semantic_runtime_ready

    previous_semantic = os.environ.get("CORTEX_SEMANTIC")
    os.environ["CORTEX_SEMANTIC"] = "1"
    try:
        if not semantic_runtime_ready():
            return {"available": False, "reason": "real local semantic model is not ready"}
        sample_count = max(1, int(repeats))
        baseline_samples: list[float] = []
        semantic_samples: list[float] = []
        with tempfile.TemporaryDirectory(prefix="cortex-ingest-overhead-") as tmp:
            base = Path(tmp)
            for index in range(sample_count):
                baseline_db = base / f"baseline-{index}.db"
                semantic_db = base / f"semantic-{index}.db"
                os.environ["CORTEX_SEMANTIC"] = "0"
                started = time.perf_counter()
                ingest_repository(repo, commit_limit=commit_limit, db_path=baseline_db)
                baseline_samples.append(time.perf_counter() - started)
                os.environ["CORTEX_SEMANTIC"] = "1"
                started = time.perf_counter()
                ingest_repository(repo, commit_limit=commit_limit, db_path=semantic_db)
                semantic_samples.append(time.perf_counter() - started)
        ratio_samples = [
            ratio
            for baseline, semantic in zip(baseline_samples, semantic_samples, strict=True)
            if (ratio := ingest_overhead_ratio(baseline, semantic)) is not None
        ]
        baseline_seconds = statistics.median(baseline_samples)
        semantic_seconds = statistics.median(semantic_samples)
        ratio = statistics.median(ratio_samples) if ratio_samples else None
        return {
            "available": True,
            "repeats": sample_count,
            "baseline_seconds": baseline_seconds,
            "semantic_seconds": semantic_seconds,
            "ratio": ratio,
            "baseline_samples": baseline_samples,
            "semantic_samples": semantic_samples,
            "ratio_samples": ratio_samples,
            "under_two_x": ratio is not None and ratio < 2.0,
        }
    finally:
        if previous_semantic is None:
            os.environ.pop("CORTEX_SEMANTIC", None)
        else:
            os.environ["CORTEX_SEMANTIC"] = previous_semantic


def measure_query_latency(
    repo: Path,
    task: str,
    db_path: Path,
    *,
    budget: int = 900,
    repeats: int = 7,
) -> dict[str, Any]:
    """Compare repeated off/on query latency with the local model warmed.

    No setup/download is attempted. The semantic side calls
    ``semantic_runtime_ready`` before timing so model load is excluded from
    the reported median; this is an evidence helper, not a performance gate.
    """

    from cortex.semantic import semantic_runtime_ready

    sample_count = max(1, int(repeats))
    previous_semantic = os.environ.get("CORTEX_SEMANTIC")
    try:
        os.environ["CORTEX_SEMANTIC"] = "0"
        bundle_mod.generate_bundle(repo, task, budget, db_path=db_path, output_format="json")
        off_samples: list[float] = []
        for _ in range(sample_count):
            started = time.perf_counter()
            bundle_mod.generate_bundle(repo, task, budget, db_path=db_path, output_format="json")
            off_samples.append((time.perf_counter() - started) * 1000.0)

        os.environ["CORTEX_SEMANTIC"] = "1"
        if not semantic_runtime_ready():
            return {"available": False, "reason": "real local semantic model is not ready"}
        bundle_mod.generate_bundle(repo, task, budget, db_path=db_path, output_format="json")
        on_samples: list[float] = []
        for _ in range(sample_count):
            started = time.perf_counter()
            bundle_mod.generate_bundle(repo, task, budget, db_path=db_path, output_format="json")
            on_samples.append((time.perf_counter() - started) * 1000.0)
        return {
            "available": True,
            "repeats": sample_count,
            "off_median_ms": statistics.median(off_samples),
            "semantic_on_median_ms": statistics.median(on_samples),
            "off_samples_ms": off_samples,
            "semantic_on_samples_ms": on_samples,
            "model_loaded_before_timing": True,
        }
    finally:
        if previous_semantic is None:
            os.environ.pop("CORTEX_SEMANTIC", None)
        else:
            os.environ["CORTEX_SEMANTIC"] = previous_semantic


# --------------------------------------------------------------------------- #
# OSS fixtures (Task 11): vendored, permissively licensed upstream snapshots.
# --------------------------------------------------------------------------- #
#
# Everything here is offline.  Each snapshot under ``evals/fixtures/oss/<name>/``
# (see its PROVENANCE.md; acquired by ``scripts/vendor_oss_fixtures.py``) is copied
# into a temporary git repository, given a deterministic synthetic history (fixed
# dates, fixed edits) so COCHANGE exists, ingested, and scored on three things that
# are reported apart from the historical 17-task matrix:
#   (i)   OSS retrieval tasks (``tags=("oss",)``; never part of the historical aggregate);
#   (ii)  call-edge resolution: totals plus hand-checked (caller, callee) gold pairs
#         that must exist as resolved ``calls`` edges;
#   (iii) impacted tests: a change inside one named source symbol must select the named
#         test file in ``cortex_risk``'s ``tests_to_run`` with ``structural`` in ``via``.

OSS_ROOT = ROOT / "evals" / "fixtures" / "oss"
FIXED_GIT_DATE = "2026-01-01T00:00:00+0000"
_OSS_EPOCH = 1767225600  # 2026-01-01T00:00:00Z; synthetic commit i is dated EPOCH + i hours
_LINE_COMMENT = {".rb": "#", ".pro": "#", ".pri": "#", ".txt": "#", ".toml": "#"}


@dataclass(frozen=True)
class OssImpactGold:
    source: str  # file whose symbol body the final synthetic commit edits
    anchor: str  # a comment line is inserted right after the first line containing this text
    expected_test: str  # must appear in tests_to_run with "structural" in via
    # Hand-reviewed gold set for precision/recall: runnable test files (Cortex's
    # selection unit) whose execution reaches the changed symbol, judged by reading the
    # snapshot's tests (static review, not coverage instrumentation).  Always contains
    # ``expected_test``.  ``gold_basis`` records the rule used for this fixture.
    expected_tests: tuple[str, ...] = ()
    gold_basis: str = ""


@dataclass(frozen=True)
class OssFixture:
    name: str
    family: str
    tasks: tuple[GoldTask, ...]
    call_pairs: tuple[tuple[str, str], ...]  # (caller symbol id, callee symbol id)
    impact: OssImpactGold
    # Synthetic history: each tuple is one commit touching those files (a trailing comment).
    history: tuple[tuple[str, ...], ...]


def _oss_task(repo: str, description: str, files: tuple[str, ...], symbols: tuple[str, ...] = ()) -> GoldTask:
    return GoldTask(repo=repo, description=description, expected_files=files, expected_symbols=symbols, tags=("oss",))


_SFPM = "cpp_qt_sortfilterproxymodel"
_MUX = "go_mux"
_PQ = "ts_p_queue"
_SEMVER = "rust_semver"
_SPLASH = "swift_splash"
_JP = "java_javapoet"
_JPP = "src/main/java/com/squareup/javapoet"
_JPT = "src/test/java/com/squareup/javapoet"
_RACK = "ruby_rack"
_SPL = "Sources/Splash"

OSS_FIXTURES: tuple[OssFixture, ...] = (
    OssFixture(
        name=_SFPM,
        family="C/C++ with Qt",
        tasks=(
            _oss_task(
                _SFPM,
                "Where does the proxy model decide whether a source row passes its filters",
                ("qqmlsortfilterproxymodel.cpp", "filters/filter.cpp"),
                ("qqmlsortfilterproxymodel.cpp:filterAcceptsRow", "filters/filter.cpp:filterAcceptsRow"),
            ),
            _oss_task(
                _SFPM,
                "How does RangeFilter check a role value against its minimum and maximum bounds",
                ("filters/rangefilter.cpp", "filters/rangefilter.h"),
                ("filters/rangefilter.cpp:filterRow",),
            ),
        ),
        call_pairs=(
            # std::all_of(..., [=](Filter* filter) { return filter->filterAcceptsRow(...); })
            ("symbol:qqmlsortfilterproxymodel.cpp:filterAcceptsRow", "symbol:filters/filter.cpp:filterAcceptsRow"),
            # RangeFilter -> RoleFilter -> Filter; setMaximumValue() calls inherited invalidate()
            ("symbol:filters/rangefilter.cpp:setMaximumValue", "symbol:filters/filter.cpp:invalidate"),
            # parameter `const QQmlSortFilterProxyModel& proxyModel`; proxyModel.sourceData(...)
            ("symbol:filters/expressionfilter.cpp:filterRow", "symbol:qqmlsortfilterproxymodel.cpp:sourceData"),
            # `ProxyRole* proxyRole = ...; proxyRole->roleData(...)`
            ("symbol:qqmlsortfilterproxymodel.cpp:sourceData", "symbol:proxyroles/proxyrole.cpp:roleData"),
        ),
        impact=OssImpactGold(
            "sorters/sorter.h",
            "class Sorter : public QObject",
            "tests/indexsorter.cpp",
            expected_tests=(
                "tests/indexsorter.cpp",
                "tests/tst_delayed.qml",
                "tests/tst_filtersorter.qml",
                "tests/tst_helpers.qml",
                "tests/tst_rolesorter.qml",
                "tests/tst_sortercontainerattached.qml",
                "tests/tst_sorters.qml",
                "tests/tst_stringsorter.qml",
            ),
            gold_basis="C++ Sorter subclasses in the test binary plus every Qt Quick test case that declares a sorter",
        ),
        history=(
            ("filters/rangefilter.cpp", "filters/rangefilter.h"),
            ("sorters/sorter.cpp", "tests/indexsorter.cpp"),
            ("qqmlsortfilterproxymodel.cpp", "qqmlsortfilterproxymodel.h"),
        ),
    ),
    OssFixture(
        name=_PQ,
        family="JavaScript/TypeScript",
        tasks=(
            _oss_task(
                _PQ,
                "How does the queue pause new tasks while the interval rate limit intervalCap is reached",
                ("source/index.ts",),
                ("source/index.ts:#onInterval",),
            ),
            _oss_task(
                _PQ,
                "How does the priority queue insert a job in priority order",
                ("source/priority-queue.ts", "source/lower-bound.ts"),
                ("source/priority-queue.ts:enqueue", "source/lower-bound.ts:lowerBound"),
            ),
        ),
        call_pairs=(
            # import lowerBound from './lower-bound.js'; lowerBound(this.#queue, element, ...)
            ("symbol:source/priority-queue.ts:enqueue", "symbol:source/lower-bound.ts:lowerBound"),
            # add() { const promise = this.#add(function_, options, waitingTask); }
            ("symbol:source/index.ts:add", "symbol:source/index.ts:#add"),
            # `this.#emit('add')` inside the private #add
            ("symbol:source/index.ts:#add", "symbol:source/index.ts:#emit"),
        ),
        impact=OssImpactGold(
            "source/priority-queue.ts",
            "enqueue(run: RunFunction",
            "test/priority-queue.ts",
            expected_tests=(
                "test/advanced.ts",
                "test/basic.ts",
                "test/debug.ts",
                "test/priority-queue.ts",
                "test/rate-limit.ts",
                "test/strict.ts",
                "test/validation.ts",
            ),
            gold_basis="PriorityQueue is PQueue's default queueClass, so every node:test file that adds to a PQueue runs enqueue; "
            "test/types.ts only type-checks, test/helpers.ts is a helper",
        ),
        history=(
            ("source/priority-queue.ts", "test/priority-queue.ts"),
            ("source/index.ts", "test/basic.ts"),
            ("source/index.ts", "source/options.ts"),
        ),
    ),
    OssFixture(
        name=_MUX,
        family="Go",
        tasks=(
            _oss_task(
                _MUX,
                "How does the router match a request to a route and extract its path variables",
                ("mux.go", "route.go", "regexp.go"),
                ("mux.go:Match", "regexp.go:setMatch"),
            ),
            _oss_task(
                _MUX,
                "Where is the CORS method middleware that sets the Access-Control-Allow-Methods header",
                ("middleware.go",),
                ("middleware.go:CORSMethodMiddleware",),
            ),
        ),
        call_pairs=(
            # same package, other file: newRouteRegexp(tpl, typ, routeRegexpOptions{...})
            ("symbol:route.go:addRegexpMatcher", "symbol:regexp.go:newRouteRegexp"),
            # method receiver `r *Router`: r.Match(req, &match)
            ("symbol:mux.go:ServeHTTP", "symbol:mux.go:Match"),
            # r.NewRoute().Path(path).HandlerFunc(f)
            ("symbol:mux.go:HandleFunc", "symbol:mux.go:NewRoute"),
            # in-package test calling the middleware constructor
            ("symbol:middleware_test.go:TestCORSMethodMiddleware", "symbol:middleware.go:CORSMethodMiddleware"),
        ),
        impact=OssImpactGold(
            "middleware.go",
            "func CORSMethodMiddleware(",
            "middleware_test.go",
            expected_tests=("example_cors_method_middleware_test.go", "middleware_test.go"),
            gold_basis="the only _test.go files that call CORSMethodMiddleware",
        ),
        history=(
            ("mux.go", "mux_test.go"),
            ("route.go", "regexp.go"),
            ("middleware.go", "middleware_test.go"),
        ),
    ),
    OssFixture(
        name=_SEMVER,
        family="Rust",
        tasks=(
            _oss_task(
                _SEMVER,
                "How is a version string parsed into major minor patch pre-release and build metadata",
                ("src/parse.rs", "src/lib.rs"),
                ("src/parse.rs:from_str", "src/lib.rs:parse"),
            ),
            _oss_task(
                _SEMVER,
                "How does a version requirement decide whether a version matches its comparators",
                ("src/eval.rs", "src/lib.rs"),
                ("src/eval.rs:matches_req", "src/eval.rs:matches_comparator"),
            ),
        ),
        call_pairs=(
            # Version::parse(text) -> Version::from_str(text)
            ("symbol:src/lib.rs:parse", "symbol:src/parse.rs:from_str"),
            # VersionReq::matches -> crate::eval::matches_req(self, version)
            ("symbol:src/lib.rs:matches", "symbol:src/eval.rs:matches_req"),
            # Comparator::matches -> crate::eval::matches_comparator(self, version)
            ("symbol:src/lib.rs:matches", "symbol:src/eval.rs:matches_comparator"),
            # Prerelease parsing builds identifiers through Identifier::new_unchecked
            ("symbol:src/parse.rs:build_identifier", "symbol:src/identifier.rs:new_unchecked"),
        ),
        # Version::from_str is reached from tests/test_version.rs through the shared
        # helper tests/util/mod.rs::version -> Version::parse -> from_str (depth 3).
        # (tests/test_version_req.rs is *not* a sound structural gold: it imports two
        # different `VersionReq` types under `#[cfg]`, so `req.matches` is ambiguous.)
        impact=OssImpactGold(
            "src/parse.rs",
            "fn from_str(",
            "tests/test_version.rs",
            expected_tests=("tests/test_version.rs", "tests/test_version_req.rs"),
            gold_basis="integration tests that parse a Version (util::version -> Version::parse -> Version::from_str); "
            "test_identifier.rs and test_autotrait.rs never parse a Version",
        ),
        history=(
            ("src/parse.rs", "tests/test_version.rs"),
            ("src/eval.rs", "src/lib.rs"),
            ("src/identifier.rs", "tests/test_identifier.rs"),
        ),
    ),
    OssFixture(
        name=_SPLASH,
        family="Swift",
        tasks=(
            _oss_task(
                _SPLASH,
                "How does the syntax highlighter tokenize code and build the highlighted output",
                (f"{_SPL}/Syntax/SyntaxHighlighter.swift", f"{_SPL}/Tokenizing/Tokenizer.swift"),
                (f"{_SPL}/Syntax/SyntaxHighlighter.swift:highlight",),
            ),
            _oss_task(
                _SPLASH,
                "How are Markdown code blocks decorated with highlighted HTML",
                (f"{_SPL}/Output/MarkdownDecorator.swift", f"{_SPL}/Output/HTMLOutputFormat.swift"),
                (f"{_SPL}/Output/MarkdownDecorator.swift:decorate",),
            ),
        ),
        call_pairs=(
            # private var tokenizer = Tokenizer(); tokenizer.segmentsByTokenizing(code, ...)
            (f"symbol:{_SPL}/Syntax/SyntaxHighlighter.swift:highlight", f"symbol:{_SPL}/Tokenizing/Tokenizer.swift:segmentsByTokenizing"),
            # private let highlighter: SyntaxHighlighter<HTMLOutputFormat>; highlighter.highlight(code)
            (f"symbol:{_SPL}/Output/MarkdownDecorator.swift:decorate", f"symbol:{_SPL}/Syntax/SyntaxHighlighter.swift:highlight"),
            # implicit self inside the struct
            (f"symbol:{_SPL}/Syntax/SyntaxHighlighter.swift:highlight", f"symbol:{_SPL}/Syntax/SyntaxHighlighter.swift:typeOfToken"),
        ),
        impact=OssImpactGold(
            f"{_SPL}/Output/MarkdownDecorator.swift",
            "func decorate(",
            "Tests/SplashTests/Tests/MarkdownTests.swift",
            expected_tests=("Tests/SplashTests/Tests/MarkdownTests.swift",),
            gold_basis="the only test file that references MarkdownDecorator",
        ),
        history=(
            (f"{_SPL}/Syntax/SyntaxHighlighter.swift", "Tests/SplashTests/Core/SyntaxHighlighterTestCase.swift"),
            (f"{_SPL}/Output/HTMLOutputFormat.swift", "Tests/SplashTests/Tests/HTMLOutputFormatTests.swift"),
            (f"{_SPL}/Tokenizing/Tokenizer.swift", f"{_SPL}/Grammar/SwiftGrammar.swift"),
        ),
    ),
    OssFixture(
        name=_JP,
        family="Java",
        tasks=(
            _oss_task(
                _JP,
                "How does JavaFile write a compilation unit with its package and import statements",
                (f"{_JPP}/JavaFile.java", f"{_JPP}/CodeWriter.java"),
                (f"{_JPP}/JavaFile.java:writeTo",),
            ),
            _oss_task(
                _JP,
                "How does LineWrapper wrap long lines at the column limit",
                (f"{_JPP}/LineWrapper.java",),
                (f"{_JPP}/LineWrapper.java:LineWrapper",),
            ),
        ),
        call_pairs=(
            # CodeWriter importsCollector = new CodeWriter(...); importsCollector.suggestedImports()
            (f"symbol:{_JPP}/JavaFile.java:writeTo", f"symbol:{_JPP}/CodeWriter.java:suggestedImports"),
            # new CodeWriter(out).emit("$L", this) inside CodeBlock.toString()
            (f"symbol:{_JPP}/CodeBlock.java:toString", f"symbol:{_JPP}/CodeWriter.java:emit"),
            # MethodSpec.emit(CodeWriter codeWriter, ...) -> codeWriter.popTypeVariables(typeVariables)
            (f"symbol:{_JPP}/MethodSpec.java:emit", f"symbol:{_JPP}/CodeWriter.java:popTypeVariables"),
        ),
        impact=OssImpactGold(
            f"{_JPP}/CodeWriter.java",
            "Map<String, ClassName> suggestedImports() {",
            f"{_JPT}/JavaFileTest.java",
            expected_tests=tuple(
                f"{_JPT}/{name}.java"
                for name in (
                    "AnnotationSpecTest",
                    "FileReadingTest",
                    "FileWritingTest",
                    "JavaFileTest",
                    "MethodSpecTest",
                    "TypeSpecTest",
                )
            ),
            gold_basis="suggestedImports() is only called by JavaFile.writeTo, so the gold is every test class that "
            "renders a JavaFile (toString/writeTo/toJavaFileObject)",
        ),
        history=(
            (f"{_JPP}/JavaFile.java", f"{_JPT}/JavaFileTest.java"),
            (f"{_JPP}/LineWrapper.java", f"{_JPT}/LineWrapperTest.java"),
            (f"{_JPP}/CodeWriter.java", f"{_JPP}/CodeBlock.java"),
        ),
    ),
    OssFixture(
        name=_RACK,
        family="Ruby",
        tasks=(
            _oss_task(
                _RACK,
                "How does Rack ETag middleware compute the ETag header from a digest of the response body",
                ("lib/rack/etag.rb",),
                ("lib/rack/etag.rb:digest_body",),
            ),
            _oss_task(
                _RACK,
                "How does HTTP basic authentication parse credentials and reject unauthorized requests",
                ("lib/rack/auth/basic.rb", "lib/rack/auth/abstract/handler.rb"),
                ("lib/rack/auth/basic.rb:call", "lib/rack/auth/abstract/handler.rb:unauthorized"),
            ),
        ),
        call_pairs=(
            # class Basic < AbstractHandler; `return unauthorized unless auth.provided?`
            ("symbol:lib/rack/auth/basic.rb:call", "symbol:lib/rack/auth/abstract/handler.rb:unauthorized"),
            # Utils.select_best_encoding(ENCODINGS, request.accept_encoding)
            ("symbol:lib/rack/deflater.rb:call", "symbol:lib/rack/utils.rb:select_best_encoding"),
            # request = Rack::Request.new(env); request.get_header(...) (Request includes Env)
            ("symbol:lib/rack/common_logger.rb:log", "symbol:lib/rack/request.rb:get_header"),
        ),
        impact=OssImpactGold(
            "lib/rack/etag.rb",
            "def digest_body(",
            "test/spec_etag.rb",
            expected_tests=("test/spec_etag.rb",),
            gold_basis="the only spec that uses Rack::ETag",
        ),
        history=(
            ("lib/rack/etag.rb", "test/spec_etag.rb"),
            ("lib/rack/auth/basic.rb", "test/spec_auth_basic.rb"),
            ("lib/rack/utils.rb", "lib/rack/request.rb"),
        ),
    ),
)


class _FixedGitDates:
    """Pin git author/committer dates (restoring the caller's values on exit).

    The historical fixture builders commit through plain ``git commit``; without
    this the matrix depends on the wall clock (``bundle`` weights commit recency, so
    two commits straddling a second boundary could flip a row).  The value matches
    ``tests/baseline_helpers.py::FIXED_GIT_DATE`` that the pinned matrix was taken with.
    """

    _KEYS = ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE", "GIT_CONFIG_NOSYSTEM")

    def __enter__(self) -> "_FixedGitDates":
        self._saved = {key: os.environ.get(key) for key in self._KEYS}
        os.environ["GIT_AUTHOR_DATE"] = FIXED_GIT_DATE
        os.environ["GIT_COMMITTER_DATE"] = FIXED_GIT_DATE
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        return self

    def __exit__(self, *_exc: object) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _oss_git(repo: Path, *args: str, when: int | None = None) -> None:
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    if when is not None:
        stamp = f"@{when} +0000"
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
    subprocess.run(
        ["git", "-c", "user.email=evals@example.test", "-c", "user.name=Cortex Evals", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )


def _comment_prefix(path: str) -> str:
    return _LINE_COMMENT.get(Path(path).suffix.lower(), "//")


def _append_comment(repo: Path, rel: str, text: str) -> None:
    target = repo / rel
    content = target.read_bytes()
    if content and not content.endswith(b"\n"):
        content += b"\n"
    target.write_bytes(content + f"{_comment_prefix(rel)} {text}\n".encode("utf-8"))


def _insert_comment_after(repo: Path, rel: str, anchor: str, text: str) -> int:
    target = repo / rel
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    for index, line in enumerate(lines):
        if anchor in line:
            indent = line[: len(line) - len(line.lstrip())]
            lines.insert(index + 1, f"{indent}    {_comment_prefix(rel)} {text}\n")
            target.write_text("".join(lines), encoding="utf-8")
            return index + 2
    raise ValueError(f"{rel}: anchor {anchor!r} not found")


def build_oss_repo(fixture: OssFixture, base: Path) -> Path:
    """Copy a vendored snapshot into ``base`` and give it a deterministic history.

    Commit 0 imports the snapshot; commits 1..N touch ``fixture.history`` file groups
    (a trailing comment each) so COCHANGE edges exist; the last commit inserts a comment
    inside the body of the impacted-test gold's symbol.  Dates are fixed, so SHAs and
    every derived metric are reproducible.
    """
    source = OSS_ROOT / fixture.name
    if not (source / "PROVENANCE.md").is_file():
        raise FileNotFoundError(f"vendored OSS snapshot missing: {source}")
    repo = base / f"oss-{fixture.name}"
    shutil.copytree(source, repo)
    _oss_git(repo, "init", "-q", "-b", "main")
    _oss_git(repo, "add", "-A")
    _oss_git(repo, "commit", "-q", "-m", f"Import {fixture.name} snapshot", when=_OSS_EPOCH)
    for index, group in enumerate(fixture.history, start=1):
        for rel in group:
            _append_comment(repo, rel, f"cortex-eval synthetic history {index}")
        _oss_git(repo, "add", "-A")
        _oss_git(repo, "commit", "-q", "-m", f"Synthetic history {index}", when=_OSS_EPOCH + 3600 * index)
    _insert_comment_after(repo, fixture.impact.source, fixture.impact.anchor, "cortex-eval impacted-test probe")
    _oss_git(repo, "add", "-A")
    _oss_git(repo, "commit", "-q", "-m", "Synthetic change for impacted tests", when=_OSS_EPOCH + 3600 * (len(fixture.history) + 1))
    return repo


def oss_call_report(fixture: OssFixture, nodes: list[Any], edges: list[Any]) -> dict[str, Any]:
    calls = [edge for edge in edges if edge.relation == "calls"]
    # Resolved = the endpoint is a real repo symbol.  QML `external:`/`binding:`
    # endpoints and unresolved `name:` targets are not counted.
    resolved = [edge for edge in calls if edge.target.startswith("symbol:")]
    ambiguous = [edge for edge in calls if edge.metadata.get("ambiguous")]
    present = {(edge.source, edge.target) for edge in resolved}
    tiers: dict[str, int] = {}
    for edge in resolved:
        tier = str(edge.metadata.get("resolution") or "qml")
        tiers[tier] = tiers.get(tier, 0) + 1
    missing = [list(pair) for pair in fixture.call_pairs if pair not in present]
    return {
        "fixture": fixture.name,
        "family": fixture.family,
        "calls_total": len(calls),
        "calls_resolved": len(resolved),
        "calls_ambiguous": len(ambiguous),
        "resolved_ratio": len(resolved) / len(calls) if calls else 0.0,
        "tiers": dict(sorted(tiers.items())),
        "gold_pairs": len(fixture.call_pairs),
        "gold_pairs_present": len(fixture.call_pairs) - len(missing),
        "missing_pairs": missing,
    }


def oss_impact_report(fixture: OssFixture, repo: Path, db_path: Path) -> dict[str, Any]:
    from cortex.risk import analyze_risk

    result = analyze_risk(repo, "HEAD~1..HEAD", db_path=db_path)
    selected = {item["test"]: item for item in result.get("tests_to_run", [])}
    hit = selected.get(fixture.impact.expected_test)
    via = list(hit["via"]) if hit else []
    run_all = bool(result.get("tests_run_all"))
    gold = set(fixture.impact.expected_tests)
    structural = {test for test, item in selected.items() if "structural" in item.get("via", ())}
    return {
        **impact_precision_recall(set(selected), gold, run_all=run_all),
        **{f"structural_{key}": value for key, value in impact_precision_recall(structural, gold, run_all=run_all).items()},
        "gold_tests": sorted(gold),
        "fixture": fixture.name,
        "source": fixture.impact.source,
        "expected_test": fixture.impact.expected_test,
        "selected": hit is not None,
        "via": via,
        "structural": "structural" in via,
        "tests_to_run": sorted(selected),
        "tests_run_all": bool(result.get("tests_run_all")),
        "tests_run_all_reason": result.get("tests_run_all_reason") or "",
        "status": result.get("status"),
    }


def impact_precision_recall(selected: set[str], gold: set[str], *, run_all: bool = False) -> dict[str, Any]:
    """Precision/recall of a ``tests_to_run`` selection against a gold test set.

    ``None`` means undefined: precision of an empty selection, recall of an empty gold
    set, and both when Cortex asked to run everything (``tests_run_all``), because the
    list is then not the selection.
    """
    true_positives = len(selected & gold)
    if run_all:
        return {"selected_count": len(selected), "true_positives": true_positives, "precision": None, "recall": None}
    return {
        "selected_count": len(selected),
        "true_positives": true_positives,
        "precision": true_positives / len(selected) if selected else None,
        "recall": true_positives / len(gold) if gold else None,
    }


def _ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def run_oss_evals(base: Path, *, modes: tuple[str, ...] = ("pagerank", "bfs", "skeleton_on", "skeleton_off")) -> dict[str, Any]:
    """Build, ingest and score every vendored OSS fixture (offline)."""
    from cortex.store import CortexStore

    rows: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    impacts: list[dict[str, Any]] = []
    timings: dict[str, float] = {}
    backends: dict[str, int] = {}
    for fixture in OSS_FIXTURES:
        started = time.perf_counter()
        repo = build_oss_repo(fixture, base)
        db_path = base / f"oss-{fixture.name}.db"
        ingest_repository(repo, commit_limit=20, db_path=db_path)
        store = CortexStore(db_path)
        try:
            nodes, edges = store.fetch_graph(repo)
        finally:
            store.connection.close()
        for node in nodes:
            backend = node.metadata.get("parser_backend")
            # Only code files count: CMake/qmldir/.pro metadata always uses the regex backend.
            if backend and Path(node.source_ref.split("#", 1)[0]).suffix.lower() in _CALL_GRAPH_SUFFIXES:
                backends[str(backend)] = backends.get(str(backend), 0) + 1
        calls.append(oss_call_report(fixture, nodes, edges))
        impacts.append(oss_impact_report(fixture, repo, db_path))
        for task in fixture.tasks:
            for mode in modes:
                rows.append(_score_task(task, mode, repo, db_path))
        timings[fixture.name] = time.perf_counter() - started
    return {"rows": rows, "calls": calls, "impacts": impacts, "timings": timings, "backends": dict(sorted(backends.items()))}


_CALL_GRAPH_SUFFIXES = {
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".js", ".jsx", ".mjs", ".cjs",
    ".ts", ".tsx", ".go", ".rs", ".swift", ".java", ".rb", ".qml",
}


def _oss_backend_note(backends: dict[str, int]) -> list[str]:
    if not backends:
        return []
    counts = ", ".join(f"{name} {count}" for name, count in backends.items())
    lines = [f"Parser backends (code-file nodes): {counts}.", ""]
    if backends.get("regex"):
        lines += [
            "**Regex fallback was used** (`CORTEX_FORCE_REGEX=1` or the managed parser runtime is not",
            "ready): fallback `calls` edges are name-only and never resolved, so resolved ratios and gold",
            "pairs drop to zero and the structural impacted-test tier is weaker. These rows are not",
            "comparable with the tree-sitter numbers and are not what the floors in `tests/test_evals.py` pin.",
            "",
        ]
    return lines


def _format_oss_markdown(oss: dict[str, Any]) -> str:
    rows = oss["rows"]
    lines = [
        "",
        "## OSS Fixtures",
        "",
        "Vendored upstream snapshots under `evals/fixtures/oss/` (licenses and pinned commits in each",
        "`PROVENANCE.md`), run offline with a deterministic synthetic history. These rows are not part of",
        "the historical aggregate above.",
        "",
        *_oss_backend_note(oss.get("backends") or {}),
        "### OSS Retrieval Aggregate",
        "",
        "| Mode | Tasks | Precision | Precision@3 | Recall | Avg Tokens |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in _aggregate(rows):
        lines.append(
            f"| {row['mode']} | {row['tasks']} | {row['precision']:.3f} | {row['precision_at_3']:.3f} | "
            f"{row['recall']:.3f} | {row['tokens']} |"
        )
    lines.extend(
        [
            "",
            "### OSS Retrieval Per Task",
            "",
            "| Fixture | Task | Mode | Precision | Precision@3 | Recall | File Recall | Symbol Recall | Tokens |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in rows:
        task = row["task"]
        lines.append(
            f"| {task.repo} | {task.description} | {row['mode']} | {row['precision']:.3f} | {row['precision_at_3']:.3f} | "
            f"{row['recall']:.3f} | {row['file_recall']:.3f} | {row['symbol_recall']:.3f} | {row['tokens']} |"
        )
    lines.extend(
        [
            "",
            "### OSS Call Resolution",
            "",
            "| Fixture | Family | Calls | Resolved | Ambiguous | Resolved Ratio | Gold Pairs | Tiers |",
            "|---|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in oss["calls"]:
        tiers = ", ".join(f"{name} {count}" for name, count in row["tiers"].items())
        lines.append(
            f"| {row['fixture']} | {row['family']} | {row['calls_total']} | {row['calls_resolved']} | "
            f"{row['calls_ambiguous']} | {row['resolved_ratio']:.3f} | {row['gold_pairs_present']}/{row['gold_pairs']} | {tiers} |"
        )
    lines.extend(
        [
            "",
            "### OSS Impacted Tests",
            "",
            "One synthetic edit per fixture (a comment inside one named symbol). Precision and recall compare",
            "`tests_to_run` with a hand-reviewed gold set of runnable test files that reach the changed",
            "symbol (rule per fixture in `OSS_FIXTURES[*].impact.gold_basis`). The gold sets come from",
            "reading the tests, not from coverage, so they can miss transitive callers; precision is then a",
            "lower bound. They were written during the 0.10.0 release audit with Cortex's selections already",
            "visible (not blind). With one probe per language these are anecdotes, not language-level rates.",
            "`Structural` counts only tests whose `via` includes `structural`; the full selection also has the",
            "`cochange`, `pair` and `changed` tiers, and the synthetic history pairs many files, so its",
            "cochange picks are optimistic about real histories.",
            "",
            "| Fixture | Changed File | Anchor Test | Anchor Selected | Via | Selected | Gold | Hits | Precision | Recall | "
            "Structural Precision | Structural Recall | Run All |",
            "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in oss["impacts"]:
        lines.append(
            f"| {row['fixture']} | {row['source']} | {row['expected_test']} | {'yes' if row['selected'] else 'no'} | "
            f"{','.join(row['via']) or '-'} | {row['selected_count']} | {len(row['gold_tests'])} | {row['true_positives']} | "
            f"{_ratio(row['precision'])} | {_ratio(row['recall'])} | {_ratio(row['structural_precision'])} | "
            f"{_ratio(row['structural_recall'])} | {'yes' if row['tests_run_all'] else 'no'} |"
        )
    defined = [row for row in oss["impacts"] if row["precision"] is not None and row["recall"] is not None]
    if defined:
        mean_p = sum(row["precision"] for row in defined) / len(defined)
        mean_r = sum(row["recall"] for row in defined) / len(defined)
        lines.extend(["", f"Macro average over {len(defined)} fixtures: precision {mean_p:.3f}, recall {mean_r:.3f}."])
    return "\n".join(lines) + "\n"


def run_evals(
    results_path: Path = ROOT / "evals" / "RESULTS.md",
    *,
    semantic: bool = False,
    stdlib_tokens: bool = False,
    oss: bool = True,
) -> tuple[str, list[dict[str, Any]]]:
    """Run the historical matrix (returned as ``rows``) and, unless ``oss=False``, the
    vendored OSS fixtures (appended to the Markdown; also kept on ``run_evals.last_oss``)."""
    rows: list[dict[str, Any]] = []
    oss_result: dict[str, Any] | None = None
    previous_semantic = os.environ.get("CORTEX_SEMANTIC")
    previous_encoding = None
    previous_unavailable = False
    previous_force_stdlib = False
    if stdlib_tokens:
        import cortex.tokenizer as tokenizer

        previous_encoding = tokenizer._tiktoken_encoding
        previous_unavailable = tokenizer._tiktoken_unavailable
        previous_force_stdlib = tokenizer._force_stdlib_segments
        tokenizer._tiktoken_encoding = None
        tokenizer._tiktoken_unavailable = True
        tokenizer._force_stdlib_segments = True
    os.environ["CORTEX_SEMANTIC"] = "1" if semantic else "0"
    semantic_ready = False
    try:
        if semantic:
            from cortex.semantic import semantic_runtime_ready

            semantic_ready = semantic_runtime_ready()
        tasks = [task for task in GOLD_TASKS if not task.tags]
        default_modes = ("pagerank", "bfs", "skeleton_on", "skeleton_off")
        modes = default_modes
        with tempfile.TemporaryDirectory(prefix="cortex-evals-") as tmp:
            base = Path(tmp)
            with _FixedGitDates():
                repos = build_fixture_repos(base)
            if semantic and semantic_ready:
                tasks.extend(task for task in GOLD_TASKS if task.tags)
                # Optional tasks are deliberately run against the same
                # real-model-built DB twice: explicit off proves the gold gap,
                # explicit on proves semantic recovery. Historical rows stay
                # on the unchanged four-mode matrix.
                with _FixedGitDates():
                    repos.update(build_semantic_fixture_repos(base))
                modes = (*default_modes, "semantic_off", "semantic_on")
            db_paths: dict[str, Path] = {}
            for name, repo in repos.items():
                db_path = base / f"{name}.db"
                ingest_repository(repo, commit_limit=20, db_path=db_path)
                db_paths[name] = db_path
            for task in tasks:
                repo = repos[task.repo]
                db_path = db_paths[task.repo]
                for mode in modes:
                    if task.tags:
                        if mode not in {"semantic_off", "semantic_on"}:
                            continue
                    elif mode not in default_modes and mode != "semantic_on":
                        continue
                    rows.append(_score_task(task, mode, repo, db_path))
            if oss:
                oss_base = base / "oss"
                oss_base.mkdir()
                oss_result = run_oss_evals(oss_base, modes=default_modes)
    finally:
        if previous_semantic is None:
            os.environ.pop("CORTEX_SEMANTIC", None)
        else:
            os.environ["CORTEX_SEMANTIC"] = previous_semantic
        if stdlib_tokens:
            tokenizer._tiktoken_encoding = previous_encoding
            tokenizer._tiktoken_unavailable = previous_unavailable
            tokenizer._force_stdlib_segments = previous_force_stdlib
    command = "python3 evals/run_evals.py"
    if semantic:
        command += " --semantic"
    if stdlib_tokens:
        command += " --stdlib-tokens"
    markdown = _format_markdown(rows, command=command)
    if oss_result is not None:
        markdown += _format_oss_markdown(oss_result)
    else:
        markdown += "\nOSS fixtures skipped (`--no-oss`).\n"
    run_evals.last_oss = oss_result  # type: ignore[attr-defined]
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(markdown, encoding="utf-8")
    if semantic and not semantic_ready:
        # Keep this out of the Markdown/JSON rows: a skipped optional task is
        # not a semantic-on result and must not be reported as a failure or
        # success claim.
        print("Semantic eval tasks skipped: no real local model is ready; run `cortex semantic setup` first.", file=sys.stderr)
    return markdown, rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Cortex gold-task evals")
    parser.add_argument("--json", action="store_true", help="Emit raw rows as JSON instead of Markdown")
    parser.add_argument("--results", type=Path, default=ROOT / "evals" / "RESULTS.md")
    parser.add_argument("--semantic", action="store_true", help="Opt into semantic-on evals when a real local model is ready")
    parser.add_argument(
        "--stdlib-tokens",
        action="store_true",
        help="Force the dependency-free calibrated tokenizer path even when optional token packages are installed",
    )
    parser.add_argument(
        "--oss",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Score the vendored OSS fixtures under evals/fixtures/oss (default on; --no-oss skips them)",
    )
    args = parser.parse_args()
    markdown, rows = run_evals(
        args.results,
        semantic=args.semantic,
        stdlib_tokens=args.stdlib_tokens,
        oss=args.oss,
    )
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        print(markdown, end="")


if __name__ == "__main__":
    main()
