from __future__ import annotations

import hashlib
import math
import re
import time
from collections import defaultdict
from pathlib import Path

from .config import load_config
from .fusion import rrf_fuse
from .gitutils import discover_repo_root
from .models import BundleItem, GraphEdge, GraphNode, RetrievalBundle
from .rank import personalized_pagerank
from .store import CortexStore, default_db_path
from .structural.regex_backend import _QT_SECTION_RE
from .tokenizer import count_text_tokens, truncate_text_to_budget

PAGERANK_SCORE_MULTIPLIER = 10.0
SKELETON_MARKER = '[skeleton: bodies elided]'
ELISION_MARKER = '[body elided] ...'
STOPWORDS = {
    'a', 'an', 'the', 'in', 'on', 'of', 'for', 'to', 'is', 'are', 'and', 'or',
    'with', 'from', 'by', 'at', 'it', 'this', 'that', 'be', 'as', 'do', 'does',
    'how', 'what', 'where', 'when', 'why', 'i', 'we', 'you',
}
# Exact task-term hit on a file stem or symbol name must beat keyword-dense docs.
NAME_MATCH_BONUS = 100.0
# Task-term hit on a directory segment of the path ("ui", "backend", "mcp")
# is a weaker locality signal than a stem/symbol hit but must still beat
# body-text keyword density.
PATH_MATCH_BONUS = 40.0
# Markdown share of the budget when code candidates also match the task.
DOC_BUDGET_SHARE = 0.4
# No single item may swallow the whole bundle: when several candidates match,
# cap each item's share so the bundle returns multiple ranked snippets instead
# of one budget-filling file dump (oversized items degrade to skeleton/truncated
# form via the existing packing fallbacks).
ITEM_BUDGET_SHARE = 0.4
# Symbol spans inherit a small share of their file's keyword score so file
# context still counts without letting spans outrank the file wholesale.
SYMBOL_FILE_SCORE_SHARE = 0.25
# Test/eval/fixture files are demoted unless the task itself is about them.
AUX_PATH_DEMOTION = 0.5
AUX_PATH_RE = re.compile(r'(^|/)(tests?|testing|evals?|fixtures?|examples?|benchmarks?|samples?)(/|$)')
AUX_INTENT_TERMS = {
    'test', 'tests', 'testing', 'eval', 'evals', 'evaluation', 'fixture', 'fixtures',
    'benchmark', 'benchmarks', 'example', 'examples', 'sample', 'samples',
}
# When the task names a language/extension, boost same-language files and demote
# other code languages so e.g. a QML task does not resolve to C++. Only tokens
# that unambiguously denote a language are used, to avoid false hints from
# common English words. Bare extension mentions (".qml") tokenize to these too.
LANG_MATCH_BOOST = 1.5
LANG_MISMATCH_DEMOTION = 0.5
LANGUAGE_HINT_SUFFIXES: dict[str, frozenset[str]] = {
    'qml': frozenset({'.qml'}),
    'cpp': frozenset({'.cpp', '.cc', '.cxx', '.hpp', '.hh', '.hxx', '.h'}),
    'cplusplus': frozenset({'.cpp', '.cc', '.cxx', '.hpp', '.hh', '.hxx', '.h'}),
    'python': frozenset({'.py'}),
    'javascript': frozenset({'.js', '.jsx'}),
    'typescript': frozenset({'.ts', '.tsx'}),
    'golang': frozenset({'.go'}),
    'rust': frozenset({'.rs'}),
    'java': frozenset({'.java'}),
    'ruby': frozenset({'.rb'}),
    'swift': frozenset({'.swift'}),
    'kotlin': frozenset({'.kt', '.kts'}),
}
# P0-2: RRF fusion contribution scale. rrf_fuse's per-list max contribution
# is 1/(k+1) ~= 0.0164 (k=60); with up to four lists fused in
# generate_bundle the max raw fusion score is well under 0.1, so this
# multiplier must stay far below NAME_MATCH_BONUS (100) / PATH_MATCH_BONUS
# (40) and typical keyword scores (~10 per matched term): it should only
# ever break ties or lift a body-text-only file into the candidate set,
# never override an exact stem/symbol name hit. Tuned against the eval
# suite (see evals/run_evals.py) -- retune here, not by changing k, if a
# regression shows fusion is over/under-weighted.
FUSION_SCORE_MULTIPLIER = 40.0
# How many FTS5 body-text hits feed the fusion's ranked list -- generous
# relative to typical fixture/repo sizes so a real gold file rarely misses
# the cut before RRF even sees it.
FTS_CANDIDATE_LIMIT = 50
# Hotspot ranking is deliberately opt-in. A normalized score keeps the signal
# bounded and the default path byte-for-byte unchanged; even the hottest file
# gets at most this many extra multiples of its existing relevance score.
HOTSPOT_BOOST_MAX = 3.0


def _language_hint_suffixes(task: str, task_terms: set[str]) -> frozenset[str]:
    """File suffixes implied by a language/extension named in the task.

    Matches curated language tokens (``task_terms`` are already camel/snake
    split and lowercased) plus explicit ``c++`` mentions the tokenizer drops.
    Returns an empty set when the task names no language, leaving ranking
    untouched.
    """
    suffixes: set[str] = set()
    for term in task_terms:
        suffixes.update(LANGUAGE_HINT_SUFFIXES.get(term, ()))
    if 'c++' in task.lower():
        suffixes.update(LANGUAGE_HINT_SUFFIXES['cplusplus'])
    return frozenset(suffixes)


def _is_aux_path(path: str) -> bool:
    stem = Path(path).stem
    return bool(AUX_PATH_RE.search(path)) or stem.startswith('test_') or stem.endswith('_test')


def _symbol_qualname(node: GraphNode) -> str:
    # `symbol:<path>:<name>` ids carry the (possibly dotted) qualname; other
    # symbol-granularity ids (e.g. a CMake `target:<name>`) fall back to label.
    parts = node.node_id.split(':', 2)
    return parts[2] if len(parts) == 3 else node.label


def _leading_ws(text: str) -> str:
    return text[: len(text) - len(text.lstrip())]


def _signature_lines(lines: list[str], symbol: GraphNode) -> list[str]:
    if symbol.span_start is None:
        return [symbol.signature] if symbol.signature else []
    line = lines[symbol.span_start - 1] if 0 < symbol.span_start <= len(lines) else ''
    if line.strip():
        return [line]
    return [symbol.signature] if symbol.signature else []


# A QML `onFoo: <expression>` handler (P0-4's `qt: handler` tag) packs its
# bound expression onto the same line as its own "signature" -- there's no
# separate body block to elide the way a `{...}`-bodied function has. Match
# the `onFoo:` prefix so the expression itself can be swapped for the
# elision marker (P1-6 Qt parity).
_QML_HANDLER_LINE_RE = re.compile(r'^(?P<prefix>\s*on[A-Z]\w*\s*:)\s*\S.*$')
# QML `id: <name>` property -- a component instance's id has no symbol node
# of its own (only signals/handlers do), so it's kept by this literal
# pattern match rather than the child-symbol path (P1-6 Qt parity: "keeps
# ... component ids").
_QML_ID_LINE_RE = re.compile(r'^\s*id\s*:\s*[A-Za-z_]\w*\s*$')


def _declaration_lines(lines: list[str], symbol: GraphNode) -> list[str]:
    """The line(s) that stand in for a symbol's own declaration in a
    skeleton: normally just `_signature_lines`, but a single-line QML
    handler has its bound expression elided too (see _QML_HANDLER_LINE_RE)."""
    signature_lines = _signature_lines(lines, symbol)
    if symbol.metadata.get('qt') == 'handler' and len(signature_lines) == 1:
        match = _QML_HANDLER_LINE_RE.match(signature_lines[0])
        if match:
            return [f"{match.group('prefix')} {ELISION_MARKER}"]
    return signature_lines


def _looks_like_import(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(('import ', 'from ', '#include ', 'use ', 'require ', 'package '))


_COMMENT_PREFIXES = ('#', '//', '/*', '*')


def _is_comment_only(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and stripped.startswith(_COMMENT_PREFIXES)


def _strip_boilerplate(content: str) -> str:
    """Drop import/include and comment-only lines so license headers and
    include blocks stop being keyword magnets when scoring code."""
    return '\n'.join(
        line for line in content.splitlines()
        if not _looks_like_import(line) and not _is_comment_only(line)
    )


def _import_lines(lines: list[str], spanned: list[GraphNode]) -> list[str]:
    """Import/include lines that sit outside every symbol's span."""
    out = []
    for lineno, line in enumerate(lines, start=1):
        inside_symbol = any(s.span_start <= lineno <= s.span_end for s in spanned)
        if not inside_symbol and _looks_like_import(line):
            out.append(line)
    return out


def _nest_by_span(spanned: list[GraphNode]) -> tuple[list[GraphNode], dict[str, list[GraphNode]]]:
    """Partition symbols into top-level entries and each one's direct
    children, purely by span containment.

    Deliberately *not* qualname-based (a `.` in `_symbol_qualname`): that
    only identifies nesting for backends that dot-qualify child names
    (Python's `ast_extract.py`, e.g. `Class.method`). The regex/tree-sitter
    structural backend's C++/QML symbols (`structural/regex_backend.py`) are
    never dot-qualified -- a Qt header's `signals:`/`slots:` members and a
    QML component's `signal`/`onFoo:` children all get flat
    `symbol:<path>:<name>` ids -- so a qualname-only scheme would leave them
    stranded as spurious extra "top-level" entries instead of nested under
    their class/component. Span containment recovers the true nesting for
    both conventions (P1-6 Qt parity), since a child's span is always inside
    its parent's regardless of naming scheme. The "tightest enclosing span"
    is chosen so a signal/slot inside a nested block only ever attaches to
    its immediate container, not an outer ancestor.
    """
    def span_size(node: GraphNode) -> int:
        return node.span_end - node.span_start

    parent: dict[str, GraphNode] = {}
    for symbol in spanned:
        best: GraphNode | None = None
        for other in spanned:
            if other is symbol:
                continue
            if other.span_start <= symbol.span_start and symbol.span_end <= other.span_end and (
                other.span_start != symbol.span_start or other.span_end != symbol.span_end
            ):
                if best is None or span_size(other) < span_size(best):
                    best = other
        if best is not None:
            parent[symbol.node_id] = best

    top_level = sorted((s for s in spanned if s.node_id not in parent), key=lambda s: s.span_start)
    children_of: defaultdict[str, list[GraphNode]] = defaultdict(list)
    for symbol in spanned:
        holder = parent.get(symbol.node_id)
        if holder is not None:
            children_of[holder.node_id].append(symbol)
    return top_level, children_of


def _render_class_body(lines: list[str], symbol: GraphNode, children: list[GraphNode]) -> list[str]:
    """Lines worth keeping from inside a class-like symbol's span in a
    skeleton: each direct child's signature (body elided), plus
    brace-language section scaffolding that has no symbol node of its own --
    `Q_OBJECT` and Qt `signals:`/`slots:` section markers (P1-6 Qt parity).
    Everything else in the span (statements, prototypes the structural
    backend didn't index) is silently dropped, same as before this symbol
    was reached."""
    child_at_line = {child.span_start: child for child in children}
    out: list[str] = []
    skip_until = symbol.span_start
    for lineno in range(symbol.span_start + 1, symbol.span_end):
        if lineno <= skip_until:
            continue
        child = child_at_line.get(lineno)
        if child is not None:
            child_lines = _declaration_lines(lines, child)
            out.extend(child_lines)
            # A handler's own line already carries its elision inline (see
            # _declaration_lines) -- a trailing marker would be redundant.
            if child.span_end > child.span_start and child.metadata.get('qt') != 'handler':
                indent = _leading_ws(child_lines[-1]) if child_lines else '    '
                out.append(f'{indent}    {ELISION_MARKER}')
            skip_until = child.span_end
            continue
        line = lines[lineno - 1] if 0 < lineno <= len(lines) else ''
        if line.strip() == 'Q_OBJECT' or _QT_SECTION_RE.match(line) or _QML_ID_LINE_RE.match(line):
            out.append(line)
    return out


def _render_symbol_entry(
    lines: list[str],
    symbol: GraphNode,
    children: list[GraphNode],
    full_body_ids: set[str],
) -> list[str]:
    """Render one symbol's skeleton entry: full body if selected via
    full_body_ids, else its signature plus (for a class/component with
    children) each child's own entry, else a single elision marker when the
    symbol actually has a body to elide."""
    if symbol.node_id in full_body_ids:
        return list(lines[symbol.span_start - 1:symbol.span_end])
    signature_lines = _declaration_lines(lines, symbol)
    out = list(signature_lines)
    if symbol.kind == 'class' and children:
        out.extend(_render_class_body(lines, symbol, children))
    elif symbol.span_end > symbol.span_start and symbol.metadata.get('qt') != 'handler':
        indent = _leading_ws(signature_lines[-1]) if signature_lines else ''
        out.append(f'{indent}    {ELISION_MARKER}')
    return out


def _render_skeleton(content: str, symbols: list[GraphNode], full_body_ids: set[str]) -> str:
    """Import/include lines + symbol signatures; full bodies only for full_body_ids."""
    lines = content.splitlines()
    spanned = [s for s in symbols if s.span_start is not None and s.span_end is not None]
    top_level, children_of = _nest_by_span(spanned)

    out = [SKELETON_MARKER]
    out.extend(_import_lines(lines, spanned))

    for symbol in top_level:
        out.append('')
        children = sorted(children_of.get(symbol.node_id, []), key=lambda s: s.span_start)
        out.extend(_render_symbol_entry(lines, symbol, children, full_body_ids))
    return '\n'.join(out)


def _render_symbol_skeleton(content: str, all_symbols: list[GraphNode], target: GraphNode) -> str:
    """Skeleton scoped to one symbol (P1-6 `cortex_read_symbol` mode="skeleton"):
    whole-file import/include lines outside any symbol span, the target's own
    signature, and -- for a class/component -- its children's signatures with
    bodies elided. `all_symbols` should be every spanned symbol in the
    target's file so import-line detection and child discovery see the whole
    file, not just the target."""
    lines = content.splitlines()
    spanned = [s for s in all_symbols if s.span_start is not None and s.span_end is not None]
    _, children_of = _nest_by_span(spanned)
    children = sorted(children_of.get(target.node_id, []), key=lambda s: s.span_start)

    out = [SKELETON_MARKER]
    out.extend(_import_lines(lines, spanned))
    out.append('')
    out.extend(_render_symbol_entry(lines, target, children, set()))
    return '\n'.join(out)


def _skeleton_item(
    item: BundleItem,
    symbols: list[GraphNode],
    node_scores: dict[str, float],
    remaining: int,
) -> BundleItem | None:
    """Skeleton fit under remaining budget, greedily inlining top-scoring bodies. None if even all-signatures overflows."""
    skeleton = _render_skeleton(item.content, symbols, set())
    tokens = count_text_tokens(skeleton, kind=item.kind)
    if tokens <= 0 or tokens > remaining:
        return None

    full_body_ids: set[str] = set()
    ordered = sorted(symbols, key=lambda s: (-node_scores.get(s.node_id, 0.0), s.node_id))
    for symbol in ordered:
        trial_ids = full_body_ids | {symbol.node_id}
        trial = _render_skeleton(item.content, symbols, trial_ids)
        trial_tokens = count_text_tokens(trial, kind=item.kind)
        if trial_tokens <= remaining:
            skeleton, tokens, full_body_ids = trial, trial_tokens, trial_ids

    elided_spans = [
        [s.span_start, s.span_end]
        for s in symbols
        if s.node_id not in full_body_ids and s.span_start is not None
    ]
    return BundleItem(
        item_id=item.item_id,
        kind=item.kind,
        title=item.title,
        path=item.path,
        content=skeleton,
        token_count=tokens,
        score=item.score,
        metadata={
            **item.metadata,
            'skeleton': True,
            'content_hash': hashlib.sha256(item.content.encode()).hexdigest(),
            'elided_spans': elided_spans,
        },
    )


def _split_identifier(token: str) -> list[str]:
    normalized = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', token.replace('_', ' '))
    return [part.lower() for part in re.findall(r'[A-Za-z0-9]+', normalized) if part]


def _tokenize_text(text: str, *, drop_stopwords: bool = False) -> set[str]:
    terms: set[str] = set()
    for token in re.findall(r'[A-Za-z0-9_]+', text):
        for part in _split_identifier(token):
            if drop_stopwords and part in STOPWORDS:
                continue
            terms.add(part)
    return terms


def _expand_synonyms(terms: set[str], synonyms: dict[str, list[str]]) -> set[str]:
    """Two-way task-term expansion: a matched key pulls its values' tokens and
    a matched value pulls the key's tokens."""
    expanded = set(terms)
    for key, values in synonyms.items():
        key_tokens = _tokenize_text(key)
        value_token_sets = [_tokenize_text(value) for value in values]
        if key_tokens and key_tokens <= terms:
            for value_tokens in value_token_sets:
                expanded |= value_tokens
        if any(value_tokens and value_tokens <= terms for value_tokens in value_token_sets):
            expanded |= key_tokens
    return expanded


def _tokenize_query(task: str, synonyms: dict[str, list[str]] | None = None) -> set[str]:
    terms = _tokenize_text(task, drop_stopwords=True)
    if synonyms:
        terms = _expand_synonyms(terms, synonyms)
    return terms


_IDENTIFIER_TOKEN_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_:]*')
_CAMEL_BOUNDARY_RE = re.compile(r'[a-z0-9][A-Z]')


def _looks_like_identifier_query(task: str) -> bool:
    """True when the task text contains an identifier-shaped token:
    camelCase, snake_case, or a `::`-qualified name.

    Semble-style adaptive weighting (P0-2 step 5): a query naming a
    specific symbol -- `MyClass::mySignal`, `deviceConnected`,
    `device_list_model` -- almost certainly wants that exact definition,
    not whichever file's body text happens to share the most common
    sub-words with a natural-language phrasing of the same question.
    generate_bundle uses this to double-weight the lexical/name ranked
    list over the FTS body-text list during fusion.
    """
    for token in _IDENTIFIER_TOKEN_RE.findall(task):
        if '::' in token:
            return True
        if '_' in token.strip('_'):
            return True
        if _CAMEL_BOUNDARY_RE.search(token):
            return True
    return False


def _score_text(
    task_terms: set[str],
    text: str,
    recency_weight: float = 0.0,
    term_weights: dict[str, float] | None = None,
    noise_terms: frozenset[str] = frozenset(),
) -> float:
    haystack_terms = _tokenize_text(text) - noise_terms
    overlap = task_terms & haystack_terms
    if term_weights is None:
        return len(overlap) * 10.0 + recency_weight
    return sum(term_weights.get(term, 1.0) for term in overlap) * 10.0 + recency_weight


def _term_weights(task_terms: set[str], sources: list) -> dict[str, float]:
    if not task_terms:
        return {}
    docs = [_tokenize_text(f'{source.path}\n{source.content}') for source in sources]
    total = len(docs)
    if total == 0:
        return {term: 1.0 for term in task_terms}
    weights: dict[str, float] = {}
    for term in task_terms:
        df = sum(1 for doc in docs if term in doc)
        weights[term] = math.log((total + 1) / df) if df else math.log(total + 1)
    return weights


def _build_adjacency(edges: list[GraphEdge]) -> dict[str, list[tuple[str, float]]]:
    """Build undirected adjacency list: node_id -> [(neighbor_id, weight), ...]"""
    adj: defaultdict[str, list[tuple[str, float]]] = defaultdict(list)
    for edge in edges:
        adj[edge.source].append((edge.target, edge.weight))
        adj[edge.target].append((edge.source, edge.weight))
    return dict(adj)


def _bfs_proximity(
    seed_ids: set[str],
    adj: dict[str, list[tuple[str, float]]],
    max_depth: int = 2,
) -> dict[str, float]:
    """
    BFS from seed nodes -> proximity bonus scores for neighbors.
    Depth 1 gets +5 * edge_weight, depth 2 gets +2 * edge_weight.
    """
    depth_bonus = {1: 5.0, 2: 2.0}
    scores: dict[str, float] = {}
    frontier = set(seed_ids)
    visited = set(seed_ids)

    for depth in range(1, max_depth + 1):
        bonus = depth_bonus.get(depth, 0.0)
        next_frontier: set[str] = set()
        for nid in frontier:
            for neighbor, weight in adj.get(nid, []):
                if neighbor not in visited:
                    candidate_score = bonus * weight
                    scores[neighbor] = max(scores.get(neighbor, 0.0), candidate_score)
                    next_frontier.add(neighbor)
                    visited.add(neighbor)
        frontier = next_frontier

    return scores


def _bundle_markdown(bundle: RetrievalBundle) -> str:
    lines = [
        '# Cortex Retrieval Bundle',
        '',
        f'- Task: {bundle.task}',
        f'- Budget: {bundle.budget}',
        f'- Total Tokens: {bundle.total_tokens}',
        '',
        '## Confidence Notes',
    ]
    lines.extend(f'- {note}' for note in bundle.confidence_notes)
    lines.extend(['', '## Items'])
    for item in bundle.items:
        lines.extend(
            [
                f'### {item.title}',
                f'- Kind: {item.kind}',
                f'- Path: {item.path}',
                f'- Tokens: {item.token_count}',
                f'- Score: {item.score:.2f}',
                '',
                item.content,
                '',
            ]
        )
    if bundle.open_questions:
        lines.append('## Open Questions')
        lines.extend(f'- {question}' for question in bundle.open_questions)
    return '\n'.join(lines).strip()


def generate_bundle(
    repo_path: Path,
    task: str,
    budget: int,
    db_path: Path | None = None,
    output_format: str = 'md',
    rank: str = 'pagerank',
    hotspot_boost: bool = False,
) -> str | dict:
    repo_root = discover_repo_root(repo_path)
    store = CortexStore(db_path or default_db_path(repo_root))
    sources = store.fetch_sources(repo_root)
    commits = store.fetch_commits(repo_root)
    nodes, edges = store.fetch_graph(repo_root)

    config = load_config(repo_root)
    task_terms = _tokenize_query(task, config.synonyms)
    noise_terms = frozenset(
        term for identifier in config.noise_identifiers for term in _tokenize_text(identifier)
    )
    term_weights = _term_weights(task_terms, sources)
    demote_aux = not (task_terms & AUX_INTENT_TERMS)
    hotspot_by_path: dict[str, dict] = {}
    for node in nodes:
        if node.kind != 'file' or not isinstance(node.metadata.get('hotspot'), dict):
            continue
        hotspot_by_path[node.source_ref] = node.metadata['hotspot']
    max_hotspot_score = max(
        (
            float(values.get('score', values.get('churn', 0) * values.get('complexity', 0)))
            for values in hotspot_by_path.values()
        ),
        default=0.0,
    )
    lang_suffixes = _language_hint_suffixes(task, task_terms)
    adj = _build_adjacency(edges)

    symbols_by_path: defaultdict[str, list[GraphNode]] = defaultdict(list)
    symbol_names_by_path: defaultdict[str, set[str]] = defaultdict(set)
    for node in nodes:
        if node.granularity == 'symbol':
            symbols_by_path[node.source_ref].append(node)
            symbol_names_by_path[node.source_ref].update(
                _tokenize_text(_symbol_qualname(node).rsplit('.', 1)[-1])
            )

    newest_commit = max((c.authored_at for c in commits), default=0)
    base_scores: dict[str, float] = {}
    for source in sources:
        recency_weight = 0.0
        if newest_commit:
            recency_weight = max(0.0, 5.0 - math.log2(max(1, newest_commit - int(source.modified_at) + 1)))
        name_candidates = _tokenize_text(Path(source.path).stem) | symbol_names_by_path.get(source.path, set())
        name_bonus = NAME_MATCH_BONUS if task_terms & name_candidates else 0.0
        dir_tokens = _tokenize_text("/".join(Path(source.path).parts[:-1]))
        path_bonus = PATH_MATCH_BONUS if task_terms & dir_tokens else 0.0
        haystack = _strip_boilerplate(source.content) if source.kind == 'code' else source.content
        score = name_bonus + path_bonus + _score_text(
            task_terms,
            f'{source.path}\n{haystack}',
            recency_weight,
            term_weights,
            noise_terms,
        )
        if hotspot_boost and score > 0 and max_hotspot_score > 0:
            values = hotspot_by_path.get(source.path, {})
            hotspot_score = float(values.get('score', values.get('churn', 0) * values.get('complexity', 0)))
            score *= 1.0 + HOTSPOT_BOOST_MAX * (hotspot_score / max_hotspot_score)
        base_scores[source.path] = score

    # P0-2: fuse the existing name/keyword ranking with an FTS5 body-text
    # ranked list (plus a definition-boost list) via reciprocal rank fusion,
    # so a file whose only relevance signal is body text -- an error
    # string, a docstring, Markdown prose -- can surface even when its
    # keyword-overlap score alone is weak, without having to calibrate
    # BM25's scale against the hand-tuned NAME_MATCH_BONUS/PATH_MATCH_BONUS
    # bonuses (RRF only cares about rank position, not raw score
    # magnitude). Fusion runs on base_scores, *before* the aux-path
    # demotion and language boost/demotion applied below, so those existing
    # signals uniformly cover FTS-sourced candidates too instead of needing
    # a duplicate noise penalty.
    name_rank_list = [
        path for path, score in sorted(base_scores.items(), key=lambda kv: (-kv[1], kv[0]))
        if score > 0
    ]
    fts_hits = store.search_fulltext(repo_root, task, limit=FTS_CANDIDATE_LIMIT) if task_terms else []
    fts_rank_list = [path for path, _bm25, _snippet in fts_hits if path in base_scores]
    if noise_terms:
        # Mirror _score_text's noise filtering on the FTS body-text list: a
        # hit whose only overlap with the task is a configured noise
        # identifier must not ride into the bundle via rank fusion.
        content_by_path = {source.path: source.content for source in sources}
        fts_rank_list = [
            path for path in fts_rank_list
            if task_terms & (_tokenize_text(content_by_path.get(path, '')) - noise_terms)
        ]
    # Definition boost (semble-style, P0-2 step 5): a file that *defines* a
    # queried identifier (its own symbol names overlap the task terms) gets
    # extra RRF list membership beyond a plain FTS body-text mention, so it
    # outranks a file that merely references the identifier in prose.
    definition_rank_list = sorted(
        path for path, names in symbol_names_by_path.items()
        if task_terms & names and path in base_scores
    )
    # P1-7: an optional local Model2Vec list is appended only when a managed
    # local model and same-model chunk vectors are available.  The helper is
    # lazy/soft-imported so an absent extra takes the exact pre-semantic path.
    semantic_rank_list: list[str] = []
    lexical_signal_paths: set[str] = set()
    try:
        from .semantic import ranked_paths, semantic_enabled

        if semantic_enabled():
            # Preserve lexical/name rankings exactly for ordinary tasks:
            # semantic fusion is most valuable when the task vocabulary has no
            # direct indexed overlap (the P1-7 vocabulary-gap case). Avoiding a
            # model encode in the ordinary case also keeps optional semantic
            # latency out of the established path.
            lexical_signal_paths = {
                source.path
                for source in sources
                if task_terms and task_terms & _tokenize_text(f"{source.path}\n{source.content}")
            }
            if not lexical_signal_paths:
                semantic_rank_list = [
                    path for path in ranked_paths(store, repo_root, task)
                    if path in base_scores
                ]
    except Exception:
        semantic_rank_list = []
    fusion_lists: list[list[str]] = [name_rank_list, fts_rank_list, definition_rank_list]
    if semantic_rank_list and not lexical_signal_paths:
        fusion_lists.append(semantic_rank_list)
    if _looks_like_identifier_query(task):
        # Adaptive weighting: an identifier-shaped query double-counts the
        # lexical/name list so exact-symbol relevance outweighs generic FTS
        # body-text term frequency.
        fusion_lists.append(name_rank_list)
    fusion_scores = rrf_fuse(fusion_lists) if any(fusion_lists) else {}

    source_scores: dict[str, float] = {}
    for source in sources:
        score = base_scores[source.path] + fusion_scores.get(source.path, 0.0) * FUSION_SCORE_MULTIPLIER
        if demote_aux and _is_aux_path(source.path):
            score *= AUX_PATH_DEMOTION
        if lang_suffixes and score > 0:
            # Multiplicative so unrelated files (score 0) are never seeded by
            # language alone; only reorders files the task already matches.
            suffix = Path(source.path).suffix.lower()
            if suffix in lang_suffixes:
                score *= LANG_MATCH_BOOST
            elif source.kind == 'code' and suffix:
                score *= LANG_MISMATCH_DEMOTION
        source_scores[source.path] = score

    seed_scores = {f'file:{path}': score for path, score in source_scores.items() if score > 0}
    if rank == 'bfs':
        proximity = _bfs_proximity(set(seed_scores), adj, max_depth=2)
        pagerank_scores: dict[str, float] = {}
    elif rank == 'pagerank':
        proximity = {}
        pagerank_scores = personalized_pagerank(nodes, edges, seed_scores) if seed_scores else {}
    else:
        raise ValueError(f'Unknown rank mode: {rank}')

    candidates: list[BundleItem] = []

    for source in sources:
        file_node_id = f'file:{source.path}'
        keyword_score = source_scores[source.path]
        graph_bonus = proximity.get(file_node_id, 0.0)
        if rank == 'pagerank':
            graph_bonus = pagerank_scores.get(file_node_id, 0.0) * PAGERANK_SCORE_MULTIPLIER
        final_score = keyword_score + graph_bonus

        token_count = count_text_tokens(source.content, kind=source.kind)
        candidates.append(
            BundleItem(
                item_id=f'source:{source.path}',
                kind=source.kind,
                title=source.path,
                path=source.path,
                content=source.content,
                token_count=token_count,
                score=final_score,
                metadata={'modified_at': source.modified_at, 'graph_bonus': graph_bonus},
            )
        )

    for commit in commits:
        recency_weight = 0.0
        if newest_commit:
            recency_weight = max(0.0, 5.0 - math.log2(max(1, newest_commit - commit.authored_at + 1)))
        content = f'{commit.summary}\nFiles: {chr(44).join(commit.files)}'
        candidates.append(
            BundleItem(
                item_id=f'commit:{commit.sha}',
                kind='commit',
                title=commit.summary,
                path=commit.sha,
                content=content,
                token_count=count_text_tokens(content),
                score=_score_text(task_terms, content, recency_weight=recency_weight, term_weights=term_weights, noise_terms=noise_terms),
                metadata={'sha': commit.sha, 'files': commit.files, 'authored_at': commit.authored_at},
            )
        )

    source_by_path = {source.path: source for source in sources}
    symbol_items_by_path: defaultdict[str, list[BundleItem]] = defaultdict(list)
    for path, path_symbols in symbols_by_path.items():
        source = source_by_path.get(path)
        if source is None or source.kind != 'code':
            continue
        lines = source.content.splitlines()
        file_share = SYMBOL_FILE_SCORE_SHARE * source_scores.get(path, 0.0)
        for symbol in path_symbols:
            if symbol.span_start is None or symbol.span_end is None:
                continue
            span_lines = lines[symbol.span_start - 1:symbol.span_end]
            if not span_lines:
                continue
            span_text = '\n'.join(span_lines)
            name_bonus = NAME_MATCH_BONUS if task_terms & _tokenize_text(symbol.label) else 0.0
            span_score = _score_text(task_terms, _strip_boilerplate(span_text), 0.0, term_weights, noise_terms)
            # The file share is a tiebreaker for spans that match the task
            # themselves, not a qualifier for spans that match nothing.
            if name_bonus + span_score <= 0:
                continue
            score = name_bonus + span_score + file_share
            item = BundleItem(
                item_id=f'symbol-span:{symbol.node_id}',
                kind='symbol',
                title=f'{path}:{symbol.label}',
                path=path,
                content=span_text,
                token_count=count_text_tokens(span_text),
                score=score,
                metadata={
                    'node_id': symbol.node_id,
                    'span_start': symbol.span_start,
                    'span_end': symbol.span_end,
                },
            )
            candidates.append(item)
            symbol_items_by_path[path].append(item)
    for items in symbol_items_by_path.values():
        items.sort(key=lambda item: (-item.score, item.item_id))

    candidates.sort(key=lambda item: (-item.score, item.path, item.item_id))

    has_code_matches = any(item.kind == 'code' and item.score > 0 for item in candidates)
    doc_cap = int(budget * DOC_BUDGET_SHARE) if has_code_matches else budget
    positive_matches = sum(1 for item in candidates if item.score > 0 and item.kind != 'symbol')
    # With multiple matches, cap each item so the top file cannot swallow the
    # whole budget; lone matches keep the full budget.
    item_cap = int(budget * ITEM_BUDGET_SHARE) if positive_matches > 1 else budget

    file_item_by_path = {item.path: item for item in candidates if item.item_id.startswith('source:')}

    selected: list[BundleItem] = []
    selected_ids: set[str] = set()
    files_fully_selected: set[str] = set()
    paths_with_symbols: set[str] = set()
    total_tokens = 0
    doc_tokens = 0

    def select(item: BundleItem) -> None:
        nonlocal total_tokens, doc_tokens
        selected.append(item)
        selected_ids.add(item.item_id)
        total_tokens += item.token_count
        if item.kind == 'markdown':
            doc_tokens += item.token_count

    for item in candidates:
        if item.score <= 0 or item.item_id in selected_ids:
            continue
        allowed = min(budget - total_tokens, item_cap)
        if item.kind == 'symbol':
            if item.path in files_fully_selected:
                continue
            file_item = file_item_by_path.get(item.path)
            # Prefer the whole file when it fits outright; symbol spans exist
            # to surface relevant code from files that cannot be included whole.
            if (
                file_item is not None
                and file_item.item_id not in selected_ids
                and file_item.token_count <= allowed
            ):
                select(file_item)
                files_fully_selected.add(file_item.path)
                continue
            if 0 < item.token_count <= allowed:
                select(item)
                paths_with_symbols.add(item.path)
            continue
        if item.kind == 'code' and item.path in paths_with_symbols:
            continue
        is_doc = item.kind == 'markdown'
        if is_doc:
            allowed = min(allowed, doc_cap - doc_tokens)
        if item.token_count <= allowed:
            select(item)
            if item.kind == 'code':
                files_fully_selected.add(item.path)
            continue
        remaining = allowed
        if remaining <= 16:
            continue
        if item.kind == 'code':
            picked_symbol = False
            for symbol_item in symbol_items_by_path.get(item.path, []):
                if symbol_item.item_id in selected_ids:
                    continue
                if 0 < symbol_item.token_count <= remaining:
                    select(symbol_item)
                    remaining -= symbol_item.token_count
                    picked_symbol = True
            if picked_symbol:
                paths_with_symbols.add(item.path)
                continue
            symbols = symbols_by_path.get(item.path, [])
            if symbols:
                skeleton = _skeleton_item(item, symbols, pagerank_scores, remaining)
                if skeleton is not None:
                    select(skeleton)
                # Code files with indexed symbols never degrade to file[0:N].
                continue
        truncated = truncate_text_to_budget(item.content, remaining, kind=item.kind)
        truncated_tokens = count_text_tokens(truncated, kind=item.kind)
        if truncated_tokens <= 0:
            continue
        select(
            BundleItem(
                item_id=item.item_id,
                kind=item.kind,
                title=item.title,
                path=item.path,
                content=truncated,
                token_count=truncated_tokens,
                score=item.score,
                metadata={**item.metadata, 'truncated': True},
            )
        )

    bundle = RetrievalBundle(
        task=task,
        repo_path=str(repo_root),
        budget=budget,
        total_tokens=total_tokens,
        generated_at=int(time.time()),
        items=selected,
        confidence_notes=[
            f'Graph-aware packing: keyword-matched files + {rank} graph ranking.',
            'STRUCTURAL (AST) and COCHANGE (git) edges inform neighbor selection.',
            'Token counts use Cortex byte-safe local estimator.',
        ],
        open_questions=[] if selected else ['No matching sources found. Run cortex ingest . first.'],
    )
    store.save_bundle(repo_root, bundle)

    if output_format == 'json':
        return bundle.to_dict()
    return _bundle_markdown(bundle)
