from __future__ import annotations

import hashlib
import math
import re
import time
from collections import defaultdict
from pathlib import Path

from .fusion import rrf_fuse
from .gitutils import discover_repo_root
from .models import BundleItem, GraphEdge, GraphNode, RetrievalBundle
from .rank import personalized_pagerank
from .store import CortexStore, default_db_path
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
    return node.node_id.split(':', 2)[2]


def _leading_ws(text: str) -> str:
    return text[: len(text) - len(text.lstrip())]


def _signature_lines(lines: list[str], symbol: GraphNode) -> list[str]:
    if symbol.span_start is None:
        return [symbol.signature] if symbol.signature else []
    line = lines[symbol.span_start - 1] if 0 < symbol.span_start <= len(lines) else ''
    if line.strip():
        return [line]
    return [symbol.signature] if symbol.signature else []


def _looks_like_import(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(('import ', 'from ', '#include ', 'use ', 'require ', 'package '))


def _render_skeleton(content: str, symbols: list[GraphNode], full_body_ids: set[str]) -> str:
    """Import/include lines + symbol signatures; full bodies only for full_body_ids."""
    lines = content.splitlines()
    spanned = [s for s in symbols if s.span_start is not None and s.span_end is not None]
    top_level = sorted(
        (s for s in spanned if '.' not in _symbol_qualname(s)),
        key=lambda s: s.span_start,
    )
    children_of: defaultdict[str, list[GraphNode]] = defaultdict(list)
    for symbol in spanned:
        qualname = _symbol_qualname(symbol)
        if '.' in qualname:
            children_of[qualname.rsplit('.', 1)[0]].append(symbol)

    out = [SKELETON_MARKER]
    for lineno, line in enumerate(lines, start=1):
        inside_symbol = any(s.span_start <= lineno <= s.span_end for s in spanned)
        if not inside_symbol and _looks_like_import(line):
            out.append(line)

    for symbol in top_level:
        out.append('')
        if symbol.node_id in full_body_ids:
            out.extend(lines[symbol.span_start - 1:symbol.span_end])
            continue
        signature_lines = _signature_lines(lines, symbol)
        out.extend(signature_lines)
        children = sorted(children_of.get(_symbol_qualname(symbol), []), key=lambda s: s.span_start)
        if symbol.kind == 'class' and children:
            for child in children:
                child_lines = _signature_lines(lines, child)
                out.extend(child_lines)
                indent = _leading_ws(child_lines[-1]) if child_lines else '    '
                out.append(f'{indent}    {ELISION_MARKER}')
        else:
            indent = _leading_ws(signature_lines[-1]) if signature_lines else ''
            out.append(f'{indent}    {ELISION_MARKER}')
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


def _tokenize_query(task: str) -> set[str]:
    return _tokenize_text(task, drop_stopwords=True)


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
) -> float:
    haystack_terms = _tokenize_text(text)
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
) -> str | dict:
    repo_root = discover_repo_root(repo_path)
    store = CortexStore(db_path or default_db_path(repo_root))
    sources = store.fetch_sources(repo_root)
    commits = store.fetch_commits(repo_root)
    nodes, edges = store.fetch_graph(repo_root)

    task_terms = _tokenize_query(task)
    term_weights = _term_weights(task_terms, sources)
    demote_aux = not (task_terms & AUX_INTENT_TERMS)
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
        base_scores[source.path] = name_bonus + path_bonus + _score_text(
            task_terms,
            f'{source.path}\n{source.content}',
            recency_weight,
            term_weights,
        )

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
    # Definition boost (semble-style, P0-2 step 5): a file that *defines* a
    # queried identifier (its own symbol names overlap the task terms) gets
    # extra RRF list membership beyond a plain FTS body-text mention, so it
    # outranks a file that merely references the identifier in prose.
    definition_rank_list = sorted(
        path for path, names in symbol_names_by_path.items()
        if task_terms & names and path in base_scores
    )
    fusion_lists: list[list[str]] = [name_rank_list, fts_rank_list, definition_rank_list]
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
                score=_score_text(task_terms, content, recency_weight=recency_weight, term_weights=term_weights),
                metadata={'sha': commit.sha, 'files': commit.files, 'authored_at': commit.authored_at},
            )
        )

    candidates.sort(key=lambda item: (-item.score, item.path))

    has_code_matches = any(item.kind == 'code' and item.score > 0 for item in candidates)
    doc_cap = int(budget * DOC_BUDGET_SHARE) if has_code_matches else budget
    positive_matches = sum(1 for item in candidates if item.score > 0)
    # With multiple matches, cap each item so the top file cannot swallow the
    # whole budget; lone matches keep the full budget.
    item_cap = int(budget * ITEM_BUDGET_SHARE) if positive_matches > 1 else budget

    selected: list[BundleItem] = []
    total_tokens = 0
    doc_tokens = 0
    for item in candidates:
        if item.score <= 0:
            continue
        is_doc = item.kind == 'markdown'
        allowed = min(budget - total_tokens, item_cap)
        if is_doc:
            allowed = min(allowed, doc_cap - doc_tokens)
        if item.token_count <= allowed:
            selected.append(item)
            total_tokens += item.token_count
            if is_doc:
                doc_tokens += item.token_count
            continue
        remaining = allowed
        if remaining <= 16:
            continue
        if item.kind == 'code':
            symbols = symbols_by_path.get(item.path, [])
            if symbols:
                skeleton = _skeleton_item(item, symbols, pagerank_scores, remaining)
                if skeleton is not None:
                    selected.append(skeleton)
                    total_tokens += skeleton.token_count
                    continue
        truncated = truncate_text_to_budget(item.content, remaining, kind=item.kind)
        truncated_tokens = count_text_tokens(truncated, kind=item.kind)
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
                metadata={**item.metadata, 'truncated': True},
            )
        )
        total_tokens += truncated_tokens
        if is_doc:
            doc_tokens += truncated_tokens

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
