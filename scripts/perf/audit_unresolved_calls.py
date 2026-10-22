#!/usr/bin/env python3
"""Why do `calls` edges stay `name:`?  Sample unresolved targets and bucket the reason.

    python scripts/perf/audit_unresolved_calls.py REPO [--ingest] [--sample 200 | --all] [--file src/x.cpp] [--json]

Reads the index of REPO (``--ingest`` builds one first, into ``CORTEX_DATA_DIR`` or a temporary
directory).  Prints the resolution ratios of every `calls` and `inherits` edge (repository wide, and
for ``--file`` alone), then a deterministic sample of ``--sample`` unresolved call edges bucketed by
cause (``--all`` buckets every unresolved edge instead: exact counts), so a resolver change can be
judged by its before/after numbers rather than by example.

Buckets (first matching rule wins, in this order):

* ``external``            the edge carries ``metadata.external`` (set by the resolver for calls into code
                          outside the repository; always empty before that lands)
* ``scoped_namespace``    ``Ns::f()`` whose ``Ns`` labels only namespace nodes of the repository: a
                          namespace lookup that should have resolved and did not
* ``not_cpp_<family>``    the caller is outside the C/C++ family (``not_cpp_js``, ``not_cpp_java``,
                          ``not_cpp_py``, ...): the C++ causes below do not apply, so they are counted apart
* ``cast_local``          receiver typed by a cast function name (``qobject_cast``) -- extractor gap
* ``ambiguous``           the resolver kept ``name:`` on purpose: several candidates
* ``chain``               receiver is a call/field chain (``a->b()->c()``)
* ``untyped_receiver``    ``recv->m()`` where nothing static says what ``recv`` is
* ``type_not_in_repo``    receiver type / ``Scope::`` is not a class of the repository (Qt, std, ...)
* ``member_not_found``    the class is in the repository but no member (or base member) has the name
* ``free_not_in_repo``    plain call whose name is defined nowhere in the repository
* ``free_other``          plain call that the free-function tiers did not accept
* ``other``               everything else
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

CAST_FUNCTIONS = frozenset({
    "qobject_cast", "dynamic_cast", "static_cast", "reinterpret_cast", "const_cast",
    "dynamic_pointer_cast", "static_pointer_cast", "qSharedPointerCast", "qSharedPointerDynamicCast",
})


def _ratio(counter: Counter[str], keys: tuple[str, ...]) -> dict[str, object]:
    total = sum(counter.values())
    out: dict[str, object] = {key: counter.get(key, 0) for key in keys}
    out["total"] = total
    out["symbol_share"] = round(counter.get("symbol", 0) / total, 3) if total else 0.0
    return out


def resolution_ratios(edges, only_file: str | None = None) -> dict[str, object]:
    calls: Counter[str] = Counter()
    inherits: Counter[str] = Counter()
    for edge in edges:
        kind = edge.target.split(":", 1)[0]
        if edge.relation == "calls" and (only_file is None or edge.metadata.get("source_file") == only_file):
            calls[kind] += 1
        elif edge.relation == "inherits" and only_file is None:
            inherits[kind] += 1
    result: dict[str, object] = {"calls": _ratio(calls, ("symbol", "name", "external"))}
    if only_file is None:
        result["inherits"] = _ratio(inherits, ("symbol", "name", "module"))
    return result


def _scope_label(md) -> str | None:
    scope = md.get("scope")
    return str(scope).replace("::", ".").rsplit(".", 1)[-1] if scope else None


def classify(edge, index) -> str:
    from cortex.callresolve import language_family

    md = edge.metadata
    callee = str(md.get("callee", ""))
    if md.get("external"):
        return "external"
    scope_label = _scope_label(md)
    labelled = index.classes_by_label.get(scope_label, []) if scope_label else []
    if labelled and all(index.is_namespace(class_id) for class_id in labelled):
        return "scoped_namespace"
    family = language_family(str(md.get("source_file", "")))
    if family != "c":
        return f"not_cpp_{family.lstrip('.') if family != '?' else 'unknown'}"
    if md.get("ambiguous"):
        return "ambiguous"
    receiver_type = md.get("receiver_type")
    if receiver_type in CAST_FUNCTIONS:
        return "cast_local"
    kind = md.get("receiver_kind") or "none"
    if kind == "chain":
        return "chain"
    label = None
    if scope_label:
        label = scope_label
    elif receiver_type:
        label = str(receiver_type).replace("::", ".").rsplit(".", 1)[-1]
    if label is not None:
        if not index.classes_by_label.get(label) and not index.qualified_defs.get(label):
            return "type_not_in_repo"
        return "member_not_found"
    if kind == "var":
        return "untyped_receiver"
    if kind in ("this", "self"):
        return "member_not_found"
    if kind == "none":
        return "free_other" if index.free_by_label.get(callee) else "free_not_in_repo"
    return "other"


def audit(repo: Path, sample: int, only_file: str | None, all_edges: bool = False) -> dict[str, object]:
    from cortex.callresolve import _Index, is_contract_call, language_family
    from cortex.store import CortexStore, default_db_path

    store = CortexStore(default_db_path(repo))
    nodes, edges = store.fetch_graph(repo)
    report: dict[str, object] = {"repository": resolution_ratios(edges)}
    if only_file:
        report["file"] = {"path": only_file, **resolution_ratios(edges, only_file)}
    unresolved = sorted(
        (e for e in edges
         if is_contract_call(e) and e.target.startswith("name:")
         and (only_file is None or e.metadata.get("source_file") == only_file)),
        key=lambda e: e.edge_id,
    )
    if all_edges:
        picked = unresolved
    else:
        step = max(1, len(unresolved) // sample) if sample else 1
        picked = unresolved[::step][:sample]
    indexes: dict[str, _Index] = {}
    buckets: Counter[str] = Counter()
    for edge in picked:
        family = language_family(str(edge.metadata.get("source_file", "")))
        if family not in indexes:
            indexes[family] = _Index([n for n in nodes if language_family(n.source_ref) == family], [
                e for e in edges if e.relation in ("imports", "inherits")
            ])
        buckets[classify(edge, indexes[family])] += 1
    report["unresolved_calls"] = len(unresolved)
    report["sampled"] = len(picked)
    report["buckets"] = dict(buckets.most_common())
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo", type=Path)
    parser.add_argument("--ingest", action="store_true", help="index REPO first (full refresh)")
    parser.add_argument("--sample", type=int, default=200)
    parser.add_argument("--all", action="store_true", help="bucket every unresolved call edge instead of a sample")
    parser.add_argument("--file", help="also report the resolution ratios of one file's calls, and sample only it")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    repo = args.repo.resolve()
    if args.ingest and "CORTEX_DATA_DIR" not in os.environ:
        os.environ["CORTEX_DATA_DIR"] = tempfile.mkdtemp(prefix="cortex-audit-")
    if args.ingest:
        from cortex.ingest import ingest_repository

        ingest_repository(repo, incremental=False)
    report = audit(repo, args.sample, args.file, all_edges=args.all)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    repo_ratios = report["repository"]
    print(f"calls     {repo_ratios['calls']}")
    print(f"inherits  {repo_ratios['inherits']}")
    if "file" in report:
        print(f"file      {report['file']}")
    print(f"unresolved name: calls {report['unresolved_calls']}, sampled {report['sampled']}")
    for bucket, count in report["buckets"].items():
        print(f"  {bucket:<18} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
