from __future__ import annotations

import functools
import hashlib
import math
import re
import statistics
import time
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import NamedTuple

from .config import PLATFORM_SUFFIXES, load_config
from .deadline import DeadlineExceeded, check_deadline, current_deadline, deadline_scope
from .fusion import rrf_fuse
from .gitutils import discover_repo_root
from .callresolve import _last_segment
from .models import BundleItem, GraphEdge, GraphNode, RetrievalBundle
from .overrides import find_overriders
from .pathalias import parts_from_labels
from .rank import collapse_to_file_graph, personalized_pagerank
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
    # Instruction words of a task description; they carry no code vocabulary. `re` is the `re-` of
    # `re-enumerates`. Leaving `path` out of the query changes no eval row (the one task that says `path` still
    # finds its file by the other words).
    'find', 'then', 'very', 'all', 'after', 'because', 'every', 'too', 'shows', 'show', 'come', 'back',
    'during', 'handles', 'handle', 'path', 're',
}
# Exact task-term hit on a file stem or symbol name must beat keyword-dense docs.
NAME_MATCH_BONUS = 100.0
# The bonus is proportional to the IDF weight of the task terms the name carries: a name that holds one common
# word of a long task earns `NAME_BONUS_BASE` of it, a name that holds every weighted term earns all of it.
# The base alone does not keep a single exact stem hit above keyword-dense text (a README holding every task term
# scored 76.8 against 62.1 for a stem holding one of seven); `DOC_TEXT_CAP` does that for markdown/text.
NAME_BONUS_BASE = 0.5
# A markdown/text file with no name or path hit of its own: its text score is capped at this much while a code
# file has a stem/alias hit. A doc that restates the task (all its words, none in a file name) must not outrank
# code named after it; it still enters the bundle through the doc budget share.
DOC_TEXT_CAP = NAME_MATCH_BONUS * NAME_BONUS_BASE
# A symbol name inside the file earns the file at most this share of the stem formula (the span keeps its own).
SYMBOL_NAME_FILE_SHARE = 0.5
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
# A span inherits its share from the file's UN-normalised score, at most this much of it: a hub file scores
# several hundred on text alone, and uncapped every span inside it would carry the same bonus whether or not
# it matches the task. (Which whole file to pack is the normalisation's question; a span's own text is
# already length-local, and the files that seed the call expansion are ranked by this same raw score: SEED_FILES.)
SPAN_FILE_SHARE_CAP = 2 * NAME_MATCH_BONUS
# The text score of a code file far above the median size is divided by 1 + LENGTH_NORM * log2(ratio /
# LENGTH_NORM_FROM), ratio = tokens / median tokens of the code files that share a task term: the overlap sum is
# over the set of words in the whole file, so a file that is 10x as long contains nearly every task word. Files
# up to LENGTH_NORM_FROM times the median are untouched (a small fixture's modest file lost a 0.7 point margin
# when 0.14.0 divided from the median). The name/path bonuses are not divided. Tuning range 0.25-1.0 / 3-8.
LENGTH_NORM = 0.5
LENGTH_NORM_FROM = 4.0
# Test/eval/fixture files are demoted unless the task itself is about them.
AUX_PATH_DEMOTION = 0.5
# Translation catalogues and other locale data match almost any task word (they
# restate the UI text) but never implement behaviour, so they are demoted harder
# unless the task is about translation or locales.
LOCALE_PATH_DEMOTION = 0.25
# Files connected by a resolved call or inheritance edge to one of the best-matching
# files are lifted to at least this score (scaled by how strong the match is), so a
# base class, a callee or a caller whose own text shares no word with the task can
# still enter the bundle. Kept below PATH_MATCH_BONUS so it never beats a real
# name or path hit.
CALL_EXPANSION_BONUS = 20.0
CALL_EXPANSION_SEEDS = 8
# A seed linked to more files than this lifts them by the relevance of the linking symbols instead of flat.
CALL_EXPANSION_FANOUT = 20
# A resolved `calls` edge out of a top-ranked symbol is stronger evidence than a shared word:
# the callees of the best-matching symbols (and the bases of their class) are scored from the
# calling symbol's own score, so they outrank files that merely share a term. Callers are not
# expanded (a caller says where the code is used, not what it does).
CALLEE_SEEDS = 6
CALLEES_PER_SEED = 4
CALLEE_HOP_SHARE = (0.9, 0.8)  # score share of a callee at call depth 1 and 2
BASE_CLASS_SHARE = 0.6
# A call through a base-class pointer lands on the base method; the methods of derived classes that
# override it are offered at the callee's own score, the best OVERRIDES_PER_METHOD by task-term overlap.
OVERRIDES_PER_METHOD = 4
# Seeds are real functions: no class nodes, nothing of at most TRIVIAL_SPAN_LINES lines (a factory or an
# accessor says nothing about what the task is about), and the best span of each of the SEED_FILES best
# files is always a seed so one file's methods cannot take every slot. A span that cannot reach a function in
# another file and has no base class never seeds: the walk would return nothing from it, and a small UI span
# dense with the task's words (a QML handler that only sets properties) would take the slot of a function that
# does reach something. A span "reaches" when the walk from it, following calls inside its own file, arrives at
# a function in another file or at one with overriders (`_reaches_beyond_file`): a call to a same-file helper
# is not enough, and a QML same-file call resolves to an `external:qml:<file>:fn` node that leads nowhere.
# Files are ranked for this by the score their spans inherit (`raw_source_scores`, as for SPAN_FILE_SHARE_CAP
# above), not by the length-normalised one: normalisation decides which whole file to pack, a seed is a span, and a
# large file's best function is not worth less because the file around it is long.
SEED_FILES = 5
TRIVIAL_SPAN_LINES = 3
# A computed callee scores from its seed (x0.9 per hop), so enough keyword items that merely share words with the task
# can fill a tight budget before the pass reaches it and the bundle shows none of what the walk found. Only then
# (the normal passes packed no computed callee), the bundle is packed again with the best EXPANSION_RESERVED_ITEMS
# call-expansion spans (by `tie_order`) selected first, up to EXPANSION_BUDGET_SHARE of the budget. A bundle that
# already holds a callee is untouched: reserving in every bundle only reordered which files came first and lowered
# the mean precision@3 of the eval gate (0.6471 -> 0.5882 with 3 items / 0.2; 0.6176 with 2 / 0.1). Tuning range
# 2-4 items, 0.1-0.3 share; 0 items turns the second packing off.
EXPANSION_RESERVED_ITEMS = 3
EXPANSION_BUDGET_SHARE = 0.2
REACH_WALK_NODES = 200  # `_reaches_beyond_file` stops after this many functions of one file and gives the span the benefit of the doubt
# The reasons `bundle['expansion']` gives for a span that did not seed and for a computed callee that is not in the
# packed bundle (`no_candidate`: the callee has no code span to pack; the others are the packing pass that refused it).
# A span the pass refused for size is `too_large` when it is bigger than one item may be (the per-item cap, or the
# whole budget), so only a smaller span can help it; `budget_exhausted` when it would have fitted an empty bundle but
# the budget was already spent when its turn came, so it ranked too low. The two need different fixes.
SEED_REFUSAL_REASONS = ('class', 'constructor', 'trivial', 'no_reach')
CALLEE_DROP_REASONS = ('too_large', 'budget_exhausted', 'file_cap', 'inside_selected_span', 'not_reached', 'no_candidate')
# Of the other-file callees of a function the CALLEES_PER_SEED most relevant are followed, not the first
# ones in call-site order: relevance is 1 when the callee's name shares a task term; beyond
# TRIVIAL_SPAN_LINES lines its body counts too, weighted by how rare the shared terms are (the summed
# term weights over the largest task-term weight, at most 1, at least RELEVANCE_FILE_HIT); RELEVANCE_FILE_HIT
# when only its file shares one, else RELEVANCE_FLOOR. Trivial callees (accessor-shaped names, spans of at
# most TRIVIAL_SPAN_LINES lines, functions called from more places than UTILITY_PERCENTILE of the called
# functions and at least UTILITY_MIN_CALLERS times) are not followed at all unless their name carries a
# task term; a trivial virtual that has overriders is traversed to them but is not itself offered as a candidate
# (`offerable`). Relevance orders, filters, and scales the score only slightly: a callee that shares no word
# with the task is often the cause (the 0.12.0 review), so it keeps CALLEE_RELEVANCE_MIN of its share.
# 0.13.0 scaled by 0.25-1 and lost it (companion fixture recall 1.0 -> 0.67); a floor of 0.85 puts a
# relevant callee ahead of an irrelevant one at the same hop without ever dropping the latter below the
# keyword-only files. Tuning range 0.75-0.95.
RELEVANCE_FILE_HIT = 0.5
RELEVANCE_FLOOR = 0.25
CALLEE_RELEVANCE_MIN = 0.85
UTILITY_PERCENTILE = 0.99
UTILITY_MIN_CALLERS = 20
ACCESSOR_RE = re.compile(r'^(get|set|is|has)([A-Z_0-9]|$)|Changed$')
# One keyword-dense file (every method name-matches) can fill the whole bundle with symbol spans while
# other matching files wait: until nothing else is left to pack, at most a share of the budget goes to the
# spans of one file (its first SPANS_BEFORE_CAP spans are never held back). The share is 1 / the number of
# matching code files, between FILE_SPAN_SHARE_MIN and 1, and never below FILE_SPAN_SHARE while fewer than
# FILE_SPAN_SHARE_FILES files match (a handful of matches keeps today's quarter of the budget). The
# held-back room is released for a second pass, so a lone dense file still gets the whole budget.
FILE_SPAN_SHARE = 0.25
FILE_SPAN_SHARE_MIN = 0.1
FILE_SPAN_SHARE_FILES = 4
SPANS_BEFORE_CAP = 3
# A span larger than its file's share of the budget (see above) is a first-span exemption that can fill the
# bundle by itself: with many matching files, three hub spans of 250 tokens leave a 900-token budget no room
# for the smaller files that follow them. In the capped pass such spans may together take at most this share
# of the budget; the rest wait for the second pass, which packs them if room is left. Measured in tokens, so
# it follows whatever token estimate is in use (the same spans are 180/130/167 tokens with the stdlib
# estimate and 285/205/265 with o200k_base).
OVERSIZE_SPAN_SHARE = 0.8
# A file with many similarly named methods (a hub: every `deviceX()` name-matches) would hold the top of the
# list with its spans while a small file that is the answer waits. The k-th ranked span of one file (k = 0 for
# its best) scores x SPAN_REPEAT_DECAY ** k: the second method of a file is worth less than the first of
# another. Tuning range 0.6-0.9.
SPAN_REPEAT_DECAY = 0.9
# A file with no task term in its path or text, no call-expansion lift and a graph score below GRAPH_EVIDENCE_MIN
# scores recency and a trace of graph mass only (`no_evidence`). While anything with evidence exists, such a file
# is not packed into the budget left over: the first unrelated file by path is not an answer. (A weak match is not
# held back: a file that shares one common task word is packed whatever its score relative to the best.)
GRAPH_EVIDENCE_MIN = 0.5
LOCALE_PATH_RE = re.compile(r'(^|/)(i18n|l10n|locales?|translations?)(/|$)')
LOCALE_SUFFIXES = frozenset({'.ts', '.po', '.pot', '.xlf', '.xliff', '.qm'})
LOCALE_INTENT_TERMS = {
    'translation', 'translations', 'translate', 'translated', 'locale', 'locales', 'i18n', 'l10n',
    'localization', 'localisation', 'language', 'languages',
}
AUX_PATH_RE = re.compile(r'(^|/)(tests?|testing|evals?|fixtures?|examples?|benchmarks?|samples?)(/|$)')
AUX_INTENT_TERMS = {
    'test', 'tests', 'testing', 'eval', 'evals', 'evaluation', 'fixture', 'fixtures',
    'benchmark', 'benchmarks', 'example', 'examples', 'sample', 'samples',
}
# Platform variants (`wm_devicechange_win.cpp`, `mac/`) are what a Windows/macOS/... task is about and noise to
# every other one: the file is multiplied by PLATFORM_DEMOTION, and its overriders are ranked after the other
# overriders of a method, unless a task term names the platform (`PLATFORM_SUFFIXES`, plus the products of the
# `platform_terms` config map).
PLATFORM_DEMOTION = 0.5
_PLATFORM_OF_WORD = {word: key for key, words in PLATFORM_SUFFIXES.items() for word in words | {key}}
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


def _is_locale_source(path: str, kind: str) -> bool:
    """A locale/translation file: under an i18n-style directory, or a translation
    catalogue by extension (`.ts` only when it was indexed as text, i.e. Qt Linguist
    XML rather than TypeScript)."""
    suffix = Path(path).suffix.lower()
    if suffix == '.ts':
        return kind != 'code'
    return bool(LOCALE_PATH_RE.search(path)) or suffix in LOCALE_SUFFIXES


class CallLinks(NamedTuple):
    """What `_call_graph_links` derives from the resolved `calls` / `inherits` edges."""

    neighbors: dict[str, set[str]]  # file -> linked files
    symbols: dict[tuple[str, str], set[str]]  # (file, other file) -> the symbols of `file` on those edges
    calls_from: dict[str, list[str]]  # caller symbol -> callee symbols in call-site order
    bases_of: dict[str, list[str]]  # class symbol -> base class symbols
    overridden_by: dict[str, list[str]]  # base method symbol -> same-named methods of derived classes
    utilities: frozenset[str]  # symbols called from so many places that calling one says nothing
    # class symbol -> its constructor symbols (functions labelled like the class); only when overrides are derived
    constructors_of: Mapping[str, Sequence[str]] = MappingProxyType({})


def _constructors_by_class(nodes: Iterable[GraphNode]) -> dict[str, list[str]]:
    """class symbol id -> sorted ids of its constructors, for the classes that have a constructor symbol.

    A constructor is a function or method whose label is the label of its class: the smallest class
    (other than itself) whose span holds it, else the class its `Cls::` qualifier names when that label
    is unique (an out-of-line `Foo::Foo`). Destructors (`~Foo`) have another label and are not constructors.
    An inline constructor usually shares the class's own node id; its calls then hang on the class node
    and no separate constructor symbol exists.
    """
    symbols = [node for node in nodes if node.granularity == 'symbol']
    classes: defaultdict[str, list[GraphNode]] = defaultdict(list)
    for node in symbols:
        if node.kind == 'class':
            classes[node.label].append(node)
    if not classes:
        return {}
    classes_by_file: defaultdict[str, list[GraphNode]] = defaultdict(list)
    class_label: dict[str, str] = {}
    for group in classes.values():
        for cls in group:
            classes_by_file[cls.source_ref].append(cls)
            class_label[cls.node_id] = cls.label
    found: defaultdict[str, set[str]] = defaultdict(set)
    for node in symbols:
        if node.kind not in ('func', 'method') or node.label not in classes:
            continue
        owner: str | None = None
        if node.span_start is not None:
            holders = [
                cls for cls in classes_by_file.get(node.source_ref, ())
                if cls.node_id != node.node_id and cls.span_start is not None and cls.span_end is not None
                and cls.span_start <= node.span_start <= cls.span_end
            ]
            if holders:
                owner = min(holders, key=lambda cls: (cls.span_end - cls.span_start, cls.node_id)).node_id
        if owner is None:
            qualifier = node.metadata.get('qualifier') if isinstance(node.metadata, dict) else None
            matches = classes.get(_last_segment(qualifier), []) if isinstance(qualifier, str) and qualifier else []
            owner = matches[0].node_id if len(matches) == 1 else None
        if owner is not None and class_label[owner] == node.label:
            found[owner].add(node.node_id)
    return {class_id: sorted(ids) for class_id, ids in found.items()}


def _call_graph_links(nodes: list[GraphNode], edges: list[GraphEdge], *, with_overrides: bool = True) -> CallLinks:
    """One pass over the resolved `calls`/`inherits` edges between symbols of different files.

    Returns the file links, the call lists in call-site order (same-file callees included), the base
    classes of each class and, derived from those, the overriding methods (`overrides.find_overriders`;
    the graph holds no `overrides` relation), the utility functions (see `UTILITY_PERCENTILE`) and the
    constructors of each class (`_constructors_by_class`). The overriders and constructors, which the
    file links do not need, are skipped with `with_overrides=False`.
    Only edges whose endpoints are both known symbols count; `name:`/`module:` targets are unresolved
    and same-file pairs add nothing to the file links.
    """
    file_of = {node.node_id: node.source_ref for node in nodes if node.granularity == 'symbol'}
    neighbors: defaultdict[str, set[str]] = defaultdict(set)
    symbols: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    ordered_calls: defaultdict[str, list[tuple[int, str]]] = defaultdict(list)
    bases: defaultdict[str, list[str]] = defaultdict(list)
    callers: Counter[str] = Counter()
    for edge in edges:
        if edge.relation not in ('calls', 'inherits'):
            continue
        source_file = file_of.get(edge.source)
        target_file = file_of.get(edge.target)
        if source_file is None or target_file is None:
            continue
        if edge.relation == 'inherits':
            bases[edge.source].append(edge.target)
        else:
            # Same-file calls are kept too: the walk passes through them without spending a hop.
            line = edge.metadata.get('lineno') if isinstance(edge.metadata, dict) else None
            ordered_calls[edge.source].append((int(line) if isinstance(line, int) else 0, edge.target))
            callers[edge.target] += 1
        if source_file == target_file:
            continue
        neighbors[source_file].add(target_file)
        neighbors[target_file].add(source_file)
        symbols[(source_file, target_file)].add(edge.source)
        symbols[(target_file, source_file)].add(edge.target)
    calls_from = {
        caller: list(dict.fromkeys(target for _line, target in sorted(targets)))
        for caller, targets in ordered_calls.items()
    }
    utilities: frozenset[str] = frozenset()
    if callers:
        counts = sorted(callers.values())
        threshold = max(counts[int(UTILITY_PERCENTILE * (len(counts) - 1))], UTILITY_MIN_CALLERS - 1)
        utilities = frozenset(target for target, count in callers.items() if count > threshold)
    return CallLinks(
        dict(neighbors), dict(symbols), calls_from, dict(bases),
        find_overriders(nodes, bases) if with_overrides else {}, utilities,
        _constructors_by_class(nodes) if with_overrides else MappingProxyType({}),
    )


def _body_relevance(
    body_terms: set[str] | frozenset[str], term_weights: dict[str, float], max_weight: float | None = None
) -> float:
    """Relevance of a callee whose body shares `body_terms` with the task: the summed term weights over
    the largest task-term weight (so one rare term counts as much as several common ones), capped at 1
    and floored at RELEVANCE_FILE_HIT."""
    if max_weight is None:
        max_weight = max(term_weights.values(), default=0.0)
    if max_weight <= 0:
        return 1.0 if body_terms else RELEVANCE_FILE_HIT
    total = sum(term_weights.get(term, 1.0) for term in body_terms)
    return max(RELEVANCE_FILE_HIT, min(1.0, total / max_weight))


def _callee_scores(
    seeds: list[tuple[str, float]],
    calls_from: dict[str, list[str]],
    file_of: Callable[[str], str | None],
    class_of: Callable[[str], str | None],
    bases_of: dict[str, list[str]],
    *,
    relevance: Callable[[str], float] | None = None,
    overriders: Callable[[str], Sequence[str]] | None = None,
    routes: dict[str, str] | None = None,
    constructors_of: Mapping[str, Sequence[str]] | None = None,
    offerable: Callable[[str], bool] | None = None,
) -> dict[str, tuple[float, str, int, float]]:
    """callee symbol id -> (score, seed symbol id, hop, relevance) for the best route from any seed.

    A hop is a call into another file. From each seed the walk follows resolved calls: callees
    in another file at hop 1 score CALLEE_HOP_SHARE[0] of the seed, at hop 2 CALLEE_HOP_SHARE[1];
    a callee in the same file as its caller is walked through at the same hop (a function that
    delegates to a sibling still leads on). At most CALLEES_PER_SEED other-file callees are taken
    per function: the most relevant ones (`relevance(callee)`; 0 = not worth following), call-site order
    breaking ties; without `relevance` call-site order decides. A callee's score is scaled by
    CALLEE_RELEVANCE_MIN + (1 - CALLEE_RELEVANCE_MIN) * relevance(callee), so an irrelevant callee keeps
    CALLEE_RELEVANCE_MIN of its share; the fourth element is `relevance(callee)` (0.0 without it, and then
    no scaling) and orders callees whose scores tie. The bases of the seed's class score BASE_CLASS_SHARE,
    unscaled.

    `overriders(method)` lists the methods of derived classes that override a reached callee; each is
    offered at the callee's own score and hop and walked from there (their callees follow the normal hop
    budget), and `routes` (when given) receives overrider -> the base method it was reached through.
    A reached callee for which `offerable(callee)` is False (a one-line virtual that has overriders and whose
    name carries no task term) is still traversed, so its overriders are offered and walked, but it is not
    itself returned: it is no bundle candidate.

    A `new X` resolves to the class node, whose body is only a declaration: a callee that is a class with
    entries in `constructors_of` (class id -> constructor ids) is replaced by those constructors, at the same
    hop and under the same relevance rules. The class node stays a candidate when it carries calls itself
    (an inline constructor shares its id); a class without a constructor symbol is offered as before.
    """
    best: dict[str, tuple[float, str, int, float]] = {}

    def offer(
        symbol_id: str,
        score: float,
        seed_id: str,
        hop: int,
        via: str | None = None,
        *,
        scaled: bool = True,
        origin: str | None = None,
    ) -> None:
        if symbol_id == seed_id:
            return
        level = relevance(symbol_id) if relevance is not None else 0.0
        if scaled and relevance is not None:
            score *= CALLEE_RELEVANCE_MIN + (1 - CALLEE_RELEVANCE_MIN) * level
        if symbol_id not in best or score > best[symbol_id][0]:
            best[symbol_id] = (score, origin or seed_id, hop, level)
            if routes is not None:
                if via is None:
                    routes.pop(symbol_id, None)
                else:
                    routes[symbol_id] = via

    for seed_id, seed_score in seeds:
        # 0-1 BFS: a same-file call costs no hop and goes to the front of the queue, so every
        # function is settled (expanded) at its smallest hop count, whatever order calls are met in.
        settled: set[str] = set()
        queue: deque[tuple[str, int, str]] = deque([(seed_id, 0, seed_id)])
        while queue:
            current, hop, origin = queue.popleft()
            if current in settled:
                continue
            settled.add(current)
            if hop >= 1:
                score = seed_score * CALLEE_HOP_SHARE[hop - 1]
                if offerable is None or offerable(current):
                    offer(current, score, seed_id, hop, origin=origin)
                if overriders is not None:
                    for overrider in list(overriders(current))[:OVERRIDES_PER_METHOD]:
                        offer(overrider, score, seed_id, hop, via=current, origin=origin)
                        if overrider not in settled:
                            # The virtual call is one call: the overrider's own callees follow at the
                            # same hop (front of the queue, like a same-file call) and name the
                            # overrider as the function they were reached from.
                            queue.appendleft((overrider, hop, overrider))
            current_file = file_of(current)
            crossing: list[tuple[float, int, str]] = []
            targets: list[str] = []
            for callee in calls_from.get(current, ()):
                built = constructors_of.get(callee) if constructors_of else None
                if built:
                    targets.extend(built)
                    if callee in calls_from:
                        targets.append(callee)
                else:
                    targets.append(callee)
            for order, callee in enumerate(targets):
                if callee in settled:
                    continue
                if file_of(callee) == current_file:
                    queue.appendleft((callee, hop, origin))
                elif hop + 1 <= len(CALLEE_HOP_SHARE):
                    factor = relevance(callee) if relevance is not None else 1.0
                    if factor > 0:
                        crossing.append((-factor, order, callee))
            crossing.sort()
            for _negative, _order, callee in crossing[:CALLEES_PER_SEED]:
                queue.append((callee, hop + 1, origin))
        owner = class_of(seed_id)
        for base in bases_of.get(owner, ()) if owner else ():
            offer(base, seed_score * BASE_CLASS_SHARE, seed_id, 1, scaled=False)
    return best


def _select_seeds(
    items: list[BundleItem],
    symbol_by_id: dict[str, GraphNode],
    file_scores: dict[str, float],
    constructors: Iterable[str] = (),
    reaches: Callable[[str], bool] | None = None,
    refusals: Counter[str] | None = None,
) -> list[BundleItem]:
    """The spans whose callees are followed: the best CALLEE_SEEDS functions that are not trivial.

    Class nodes, the `constructors` (a hub constructor calls everything the object needs, and outscores
    the method the task is about) and spans of at most TRIVIAL_SPAN_LINES lines never seed (they outscore the function the
    task is about but call nothing worth following); the best span of each of the SEED_FILES best-scoring
    files is taken first, then the rest by score. When `reaches` is given, a span for which it is false (no
    call that leaves the file and no base class: the walk would return nothing from it) never seeds either.
    `refusals` (when given) counts the spans turned away, by reason: class, constructor, trivial, no_reach.
    """
    eligible = []
    constructor_ids = frozenset(constructors)
    for item in items:
        symbol = symbol_by_id.get(item.metadata['node_id'])
        if symbol is None or symbol.span_start is None or symbol.span_end is None:
            continue
        reason = (
            'class' if symbol.kind == 'class'
            else 'constructor' if symbol.node_id in constructor_ids
            else 'trivial' if symbol.span_end - symbol.span_start + 1 <= TRIVIAL_SPAN_LINES
            else 'no_reach' if reaches is not None and not reaches(symbol.node_id)
            else None
        )
        if reason is not None:
            if refusals is not None:
                refusals[reason] += 1
            continue
        eligible.append(item)
    eligible.sort(key=lambda item: (-item.score, item.item_id))
    chosen: dict[str, BundleItem] = {}
    files = sorted({item.path for item in eligible}, key=lambda path: (-file_scores.get(path, 0.0), path))
    for path in files[:SEED_FILES]:
        best = next(item for item in eligible if item.path == path)
        chosen[best.item_id] = best
    for item in eligible:
        if len(chosen) >= CALLEE_SEEDS:
            break
        chosen.setdefault(item.item_id, item)
    return sorted(chosen.values(), key=lambda item: (-item.score, item.item_id))


def _reaches_beyond_file(
    symbol_id: str,
    links: CallLinks,
    file_of: Callable[[str], str | None],
    class_of: Callable[[str], str | None],
) -> bool:
    """True when the call walk from `symbol_id` can leave its file: its class has a base class, or following resolved
    calls through callees in the same file (the walk's own 0-hop rule) reaches a function of another file or one that
    has overriders. `external:` nodes (an unresolved or same-file QML call) lead nowhere and never count. After
    REACH_WALK_NODES functions the answer is True: a hub that large is not shown to be a dead end."""
    if links.bases_of.get(class_of(symbol_id) or ''):
        return True
    origin = file_of(symbol_id)
    seen = {symbol_id}
    queue = deque([symbol_id])
    while queue:
        for callee in links.calls_from.get(queue.popleft(), ()):
            if callee in seen or callee.startswith('external:'):
                continue
            if file_of(callee) != origin or callee in links.overridden_by:
                return True
            if len(seen) >= REACH_WALK_NODES:
                return True
            seen.add(callee)
            queue.append(callee)
    return False


def _empty_expansion() -> dict:
    """The call-expansion diagnostics of a bundle where nothing was walked; the shape is the same when it was."""
    return {
        'seeds': [],
        'seeds_cross_file': 0,
        'seed_refusals': dict.fromkeys(SEED_REFUSAL_REASONS, 0),
        'callees_computed': 0,
        'callee_files': 0,
        'callees_packed': 0,
        'callees_dropped': dict.fromkeys(CALLEE_DROP_REASONS, 0),
        'reserved': False,
    }


def _call_graph_file_neighbors(nodes: list[GraphNode], edges: list[GraphEdge]) -> dict[str, set[str]]:
    """File -> files linked to it by a resolved `calls` or `inherits` edge."""
    return _call_graph_links(nodes, edges, with_overrides=False).neighbors


def _is_aux_path(path: str) -> bool:
    stem = Path(path).stem
    return bool(AUX_PATH_RE.search(path)) or stem.startswith('test_') or stem.endswith('_test')


_UI_WORDS = frozenset({'windows'})


@functools.lru_cache(maxsize=65536)
def _platform_of(path: str) -> str | None:
    """The PLATFORM_SUFFIXES key of the platform a path is a variant for: a stem ending in `_<word>` or a
    directory segment equal to a word other than `windows` (`wm_devicechange_win.cpp`, `src/mac/x.mm`), else None."""
    lowered = path.lower()
    *directories, name = lowered.split('/')
    stem = name.rsplit('.', 1)[0] if '.' in name else name
    if '_' in stem:
        key = _PLATFORM_OF_WORD.get(stem.rsplit('_', 1)[1])
        if key is not None:
            return key
    # A `windows/` directory is usually window management in UI code, not the OS: directories count only
    # for the unambiguous words.
    return next(
        (_PLATFORM_OF_WORD[segment] for segment in directories if segment in _PLATFORM_OF_WORD and segment not in _UI_WORDS),
        None,
    )


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


def _render_class_body(
    lines: list[str],
    symbol: GraphNode,
    children: list[GraphNode],
    full_body_ids: set[str],
    children_of: dict[str, list[GraphNode]] | None = None,
) -> list[str]:
    """Lines worth keeping from inside a class-like symbol's span in a
    skeleton: each direct child's signature (body elided), plus
    brace-language section scaffolding that has no symbol node of its own --
    `Q_OBJECT` and Qt `signals:`/`slots:` section markers (P1-6 Qt parity).
    Everything else in the span (statements, prototypes the structural
    backend didn't index) is silently dropped, same as before this symbol
    was reached."""
    child_at_line: dict[int, GraphNode] = {}
    for candidate in children:
        previous = child_at_line.get(candidate.span_start)
        # A JavaScript external placeholder can share a line with a QML
        # handler/binding. Prefer the declaration for a concise skeleton.
        if previous is None or (previous.metadata.get('qml_kind') == 'external' and candidate.metadata.get('qml_kind') != 'external'):
            child_at_line[candidate.span_start] = candidate
    out: list[str] = []
    skip_until = symbol.span_start
    for lineno in range(symbol.span_start + 1, symbol.span_end):
        if lineno <= skip_until:
            continue
        child = child_at_line.get(lineno)
        if child is not None:
            nested = sorted((children_of or {}).get(child.node_id, []), key=lambda item: item.span_start)
            out.extend(_render_symbol_entry(lines, child, nested, full_body_ids, children_of))
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
    children_of: dict[str, list[GraphNode]] | None = None,
) -> list[str]:
    """Render one symbol's skeleton entry: full body if selected via
    full_body_ids, else its signature plus (for a class/component with
    children) each child's own entry, else a single elision marker when the
    symbol actually has a body to elide."""
    if symbol.node_id in full_body_ids:
        return list(lines[symbol.span_start - 1:symbol.span_end])
    signature_lines = _declaration_lines(lines, symbol)
    out = list(signature_lines)
    if (symbol.kind == 'class' or symbol.metadata.get('qml_kind') in {'component', 'object', 'grouped_property', 'inline_component'}) and children:
        out.extend(_render_class_body(lines, symbol, children, full_body_ids, children_of))
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
        out.extend(_render_symbol_entry(lines, symbol, children, full_body_ids, children_of))
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
    out.extend(_render_symbol_entry(lines, target, children, set(), children_of))
    return '\n'.join(out)


def file_span_share(matching_files: int) -> float:
    """The share of the budget that the spans of one file may take while other matching files wait."""
    share = max(FILE_SPAN_SHARE_MIN, 1.0 / max(1, matching_files))
    return max(share, FILE_SPAN_SHARE) if matching_files < FILE_SPAN_SHARE_FILES else share


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


_CAMEL_SPLIT_RE = re.compile(r'([a-z0-9])([A-Z])')
_ALNUM_RUN_RE = re.compile(r'[A-Za-z0-9]+')
_IDENTIFIER_RUN_RE = re.compile(r'[A-Za-z0-9_]+')
# `iOS`, `macOS`, `tvOS`: a lower-case prefix and an `OS` run are one word, not `os`. Other acronym runs
# (`getURL`, `isUSB`) stay split: they are real word boundaries and the parts occur on their own.
_OS_NAME_RE = re.compile(r'[a-z]+OS')


@functools.lru_cache(maxsize=65536)
def _split_identifier(token: str) -> tuple[str, ...]:
    parts: list[str] = []
    for segment in token.split('_'):
        if _OS_NAME_RE.fullmatch(segment):
            parts.append(segment.lower())
            continue
        normalized = _CAMEL_SPLIT_RE.sub(r'\1 \2', segment)
        parts.extend(part.lower() for part in _ALNUM_RUN_RE.findall(normalized) if part)
    return tuple(parts)


def _tokenize_text(text: str, *, drop_stopwords: bool = False, labels: Iterable[str] = ()) -> set[str]:
    """Lower-cased word parts of `text`.

    `labels` are the class/qualifier names declared in the file the text names (a path): a lower-case
    concatenated token such as the stem `widgetwaitready` also yields the parts of a label it is a
    prefix of (`WidgetWaitReadyStep` -> widget, wait, ready, step). The unsplit token is kept.
    """
    terms: set[str] = set()
    labels = tuple(labels)
    for token in _IDENTIFIER_RUN_RE.findall(text):
        for part in _split_identifier(token):
            if drop_stopwords and part in STOPWORDS:
                continue
            terms.add(part)
        if labels:
            terms.update(parts_from_labels(token, labels))
    return terms


_STEM_ES_AFTER = ('s', 'x', 'z', 'ch', 'sh')


@functools.lru_cache(maxsize=65536)
def _stem(term: str) -> str:
    """Light suffix stemming for matching only (`removed`/`removes`/`remove` -> `remov`, `entries` -> `entry`).

    A plural step (`ies` -> `y`; `es` after s, x, z, ch, sh; `s` not after s, u, i), then `ed` or `ing` when 4 or
    more letters are left. A final `e` of a long word goes last so `cache`/`caches` and `remove`/`removed` meet. Terms with a
    digit or other non-letter, and short ones, are left alone. Never used on the text that is indexed.
    """
    if not term.isalpha():
        return term
    if term.endswith('ies') and len(term) >= 6:
        term = term[:-3] + 'y'
    elif term.endswith('es') and len(term) >= 5 and term[:-2].endswith(_STEM_ES_AFTER):
        term = term[:-2]
    elif term.endswith('s') and len(term) >= 4 and term[-2] not in 'sui':
        term = term[:-1]
    if term.endswith('ed') and len(term) >= 6:
        term = term[:-2]
    elif term.endswith('ing') and len(term) >= 7:
        term = term[:-3]
    if len(term) >= 5 and term.endswith('e'):
        term = term[:-1]
    return term


def _tokenize_match(text: str) -> set[str]:
    """`_tokenize_text` with every term stemmed: the form term sets are intersected in."""
    return {_stem(term) for term in _tokenize_text(text)}


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


def _match_terms(task: str, synonyms: dict[str, list[str]] | None = None) -> tuple[set[str], dict[str, tuple[str, str]]]:
    """The stemmed query terms the ranking intersects with stemmed text, and the concatenations among them.

    Adjacent query parts also yield their stemmed concatenation (`GizmoPods` -> `gizmopod`), which matches a
    code name that is one token (`Gizmopod`); `_term_weights` weights it like its rarer part.
    """
    terms = {_stem(term) for term in _tokenize_query(task, synonyms)}
    parts = [part for token in _IDENTIFIER_RUN_RE.findall(task) for part in _split_identifier(token)]
    concatenations: dict[str, tuple[str, str]] = {}
    for left, right in zip(parts, parts[1:]):
        if left in STOPWORDS or right in STOPWORDS:
            continue
        joined = _stem(left + right)
        if joined not in terms:
            concatenations.setdefault(joined, (_stem(left), _stem(right)))
    return terms | set(concatenations), concatenations


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
    haystack_terms = _tokenize_match(text) - noise_terms
    overlap = task_terms & haystack_terms
    if term_weights is None:
        return len(overlap) * 10.0 + recency_weight
    return sum(term_weights.get(term, 1.0) for term in overlap) * 10.0 + recency_weight


def _term_weights(
    task_terms: set[str],
    sources: list,
    docs: list[set[str]] | None = None,
    concatenations: dict[str, tuple[str, str]] | None = None,
) -> dict[str, float]:
    if not task_terms:
        return {}
    if docs is None:
        docs = [_tokenize_match(f'{source.path}\n{source.content}') for source in sources]
    total = len(docs)
    if total == 0:
        return {term: 1.0 for term in task_terms}
    weights: dict[str, float] = {}
    for term in task_terms:
        df = sum(1 for doc in docs if term in doc)
        weights[term] = math.log((total + 1) / df) if df else math.log(total + 1)
    for joined, parts in (concatenations or {}).items():
        if joined in weights:
            weights[joined] = max(weights.get(part, 0.0) for part in parts)
    return weights


def _name_bonus(
    matched: set[str],
    term_weights: dict[str, float],
    total_weight: float,
) -> float:
    """`NAME_MATCH_BONUS` scaled by the IDF-weighted share of the task a name carries; 0 for no hit."""
    if not matched:
        return 0.0
    if total_weight <= 0:
        return NAME_MATCH_BONUS
    share = min(1.0, sum(term_weights.get(term, 1.0) for term in matched) / total_weight)
    return NAME_MATCH_BONUS * (NAME_BONUS_BASE + (1.0 - NAME_BONUS_BASE) * share)


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


# Share of the call's time budget kept back for candidate building and packing.
RANK_RESERVE_SHARE = 0.25
RANK_RESERVE_MIN_SECONDS = 2.0


def _bounded_pagerank(
    graph: tuple[list[GraphNode], list[GraphEdge]], seed_scores: dict[str, float]
) -> tuple[dict[str, float], bool]:
    """Personalized PageRank that cannot use up the whole call budget.

    The rank bonus only orders files with no keyword hit, so under a deadline it is
    the first thing to give up: the inner scope always expires before the outer one,
    leaving a reserve to build and pack the bundle. Returns `(scores, degraded)`;
    without a deadline it runs to completion.
    """
    outer = current_deadline()
    if outer is None or not outer.enabled:
        return personalized_pagerank(*graph, seed_scores), False
    allowance = outer.remaining - max(RANK_RESERVE_MIN_SECONDS, RANK_RESERVE_SHARE * outer.budget_seconds)
    if allowance <= 0:
        return {}, True
    try:
        with deadline_scope(allowance, tool_name='pagerank'):
            return personalized_pagerank(*graph, seed_scores), False
    except DeadlineExceeded:
        return {}, True


def generate_bundle(
    repo_path: Path,
    task: str,
    budget: int,
    db_path: Path | None = None,
    output_format: str = 'md',
    rank: str = 'pagerank',
    hotspot_boost: bool = False,
    *,
    sources: list | None = None,
    graph: tuple[list[GraphNode], list[GraphEdge]] | None = None,
    rank_graph: tuple[list[GraphNode], list[GraphEdge]] | None = None,
    call_links: CallLinks | None = None,
) -> str | dict:
    """Build a retrieval bundle for `task`.

    `sources` and `graph` let a caller that already loaded them (the MCP query
    tool needs both again afterwards) avoid a second full read of the database;
    `rank_graph` is the matching `collapse_to_file_graph` result and `call_links` the
    `_call_graph_links` result, for a caller that memoises them.
    """
    repo_root = discover_repo_root(repo_path)
    store = CortexStore(db_path or default_db_path(repo_root))
    if sources is None:
        sources = store.fetch_sources(repo_root)
    commits = store.fetch_commits(repo_root)
    nodes, edges = graph if graph is not None else store.fetch_graph(repo_root)

    config = load_config(repo_root)
    # `intent_terms` (raw words: test/translation/language intent) and `task_terms` (stemmed, plus the
    # concatenations of adjacent parts) are different sets; everything compared with file text uses the latter.
    intent_terms = _tokenize_query(task, config.synonyms)
    task_terms, concatenations = _match_terms(task, config.synonyms)
    noise_terms = frozenset(
        term for identifier in config.noise_identifiers for term in _tokenize_match(identifier)
    )
    check_deadline()
    # Words of concatenated lowercase stems (`widgetwaitready.cpp` -> widget wait ready), set at ingest.
    path_aliases: dict[str, str] = {}
    for node in nodes:
        if node.kind == 'file' and node.granularity != 'symbol':
            aliases = node.metadata.get('path_aliases')
            if aliases:
                path_aliases[node.source_ref] = ' '.join(aliases)
    # Tokenized once: term weights, the symbol-span prefilter and the semantic gate all use it.
    file_tokens: dict[str, set[str]] = (
        {
            source.path: _tokenize_match(f'{source.path} {path_aliases.get(source.path, "")}\n{source.content}')
            for source in sources
        }
        if task_terms
        else {}
    )
    term_weights = _term_weights(
        task_terms, sources, [file_tokens[source.path] for source in sources] if task_terms else None, concatenations
    )
    # The weight of the whole task; a concatenation (`gizmopod`) repeats the weight of its parts, so it is not
    # counted again.
    total_term_weight = sum(weight for term, weight in term_weights.items() if term not in concatenations)
    check_deadline()
    # Stemmed like `task_terms`, which they are compared with; a platform the task names is not demoted.
    platform_names = {key: {_stem(word) for word in words | {key}} for key, words in PLATFORM_SUFFIXES.items()}
    for product, platform in config.platform_terms.items():
        platform_names[platform] |= _tokenize_match(product)
    named_platforms = frozenset(key for key, names in platform_names.items() if task_terms & names)

    def platform_demoted(path: str) -> bool:
        key = _platform_of(path)
        return key is not None and key not in named_platforms

    demote_aux = not (intent_terms & AUX_INTENT_TERMS)
    demote_locale = not (intent_terms & LOCALE_INTENT_TERMS)
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
    lang_suffixes = _language_hint_suffixes(task, intent_terms)
    adj = _build_adjacency(edges)

    symbols_by_path: defaultdict[str, list[GraphNode]] = defaultdict(list)
    symbol_names_by_path: defaultdict[str, set[str]] = defaultdict(set)
    for node in nodes:
        if node.granularity == 'symbol':
            symbols_by_path[node.source_ref].append(node)
            symbol_names_by_path[node.source_ref].update(
                _tokenize_match(_symbol_qualname(node).rsplit('.', 1)[-1])
            )

    # Counted once here: the length normalisation below and the candidates use the same numbers.
    token_counts: dict[str, int] = {}
    for position, source in enumerate(sources):
        if position % 500 == 0:
            check_deadline()
        token_counts[source.path] = count_text_tokens(source.content, kind=source.kind)
    matching_sizes = [
        token_counts[source.path]
        for source in sources
        if task_terms and source.kind == 'code' and task_terms & (file_tokens[source.path] - noise_terms)
    ]
    median_tokens = max(1.0, statistics.median(matching_sizes)) if matching_sizes else None

    stem_terms_by_path = {
        source.path: _tokenize_match(f'{Path(source.path).stem} {path_aliases.get(source.path, "")}')
        for source in sources
    }
    code_has_name_hit = any(
        source.kind == 'code' and task_terms & stem_terms_by_path[source.path] for source in sources
    )

    newest_commit = max((c.authored_at for c in commits), default=0)
    base_scores: dict[str, float] = {}
    raw_base_scores: dict[str, float] = {}
    for position, source in enumerate(sources):
        if position % 500 == 0:
            check_deadline()
        recency_weight = 0.0
        if newest_commit:
            recency_weight = max(0.0, 5.0 - math.log2(max(1, newest_commit - int(source.modified_at) + 1)))
        stem_terms = stem_terms_by_path[source.path]
        name_bonus = max(
            _name_bonus(task_terms & stem_terms, term_weights, total_term_weight),
            SYMBOL_NAME_FILE_SHARE
            * _name_bonus(task_terms & symbol_names_by_path.get(source.path, set()), term_weights, total_term_weight),
        )
        dir_tokens = _tokenize_match("/".join(Path(source.path).parts[:-1]))
        path_bonus = PATH_MATCH_BONUS if task_terms & dir_tokens else 0.0
        haystack = _strip_boilerplate(source.content) if source.kind == 'code' else source.content
        text_score = _score_text(
            task_terms,
            f'{source.path} {path_aliases.get(source.path, "")}\n{haystack}',
            recency_weight,
            term_weights,
            noise_terms,
        )
        if code_has_name_hit and source.kind != 'code' and name_bonus == 0 and path_bonus == 0:
            text_score = min(text_score, DOC_TEXT_CAP)
        score = raw_score = name_bonus + path_bonus + text_score
        if source.kind == 'code' and median_tokens is not None and text_score > 0:
            ratio = token_counts[source.path] / median_tokens
            if ratio > LENGTH_NORM_FROM:
                score = name_bonus + path_bonus + text_score / (1.0 + LENGTH_NORM * math.log2(ratio / LENGTH_NORM_FROM))
        if hotspot_boost and score > 0 and max_hotspot_score > 0:
            values = hotspot_by_path.get(source.path, {})
            hotspot_score = float(values.get('score', values.get('churn', 0) * values.get('complexity', 0)))
            factor = 1.0 + HOTSPOT_BOOST_MAX * (hotspot_score / max_hotspot_score)
            score *= factor
            raw_score *= factor
        base_scores[source.path] = score
        raw_base_scores[source.path] = raw_score

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
            if task_terms & (_tokenize_match(content_by_path.get(path, '')) - noise_terms)
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
                if task_terms and task_terms & file_tokens[source.path]
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
    # The same scores without the length normalisation: what the spans of a file inherit.
    raw_source_scores: dict[str, float] = {}
    for source in sources:
        fusion_part = fusion_scores.get(source.path, 0.0) * FUSION_SCORE_MULTIPLIER
        final = []
        for score in (base_scores[source.path] + fusion_part, raw_base_scores[source.path] + fusion_part):
            if demote_aux and _is_aux_path(source.path):
                score *= AUX_PATH_DEMOTION
            if demote_locale and _is_locale_source(source.path, source.kind):
                score *= LOCALE_PATH_DEMOTION
            if platform_demoted(source.path):
                score *= PLATFORM_DEMOTION
            if lang_suffixes and score > 0:
                # Multiplicative so unrelated files (score 0) are never seeded by
                # language alone; only reorders files the task already matches.
                suffix = Path(source.path).suffix.lower()
                if suffix in lang_suffixes:
                    score *= LANG_MATCH_BOOST
                elif source.kind == 'code' and suffix:
                    score *= LANG_MISMATCH_DEMOTION
            final.append(score)
        source_scores[source.path], raw_source_scores[source.path] = final

    check_deadline()
    lifted: set[str] = set()  # files the call-expansion bonus raised
    top_score = max(source_scores.values(), default=0.0)
    links = CallLinks({}, {}, {}, {}, {}, frozenset())

    source_by_path = {source.path: source for source in sources}
    symbol_by_id = {symbol.node_id: symbol for symbols in symbols_by_path.values() for symbol in symbols}
    live_terms = task_terms - noise_terms
    source_lines: dict[str, list[str]] = {}
    term_cache: dict[str, tuple[set[str], set[str]]] = {}

    def terms_of(symbol: GraphNode) -> tuple[set[str], set[str]]:
        """(task terms in the symbol's name, task terms in its span's body)"""
        cached = term_cache.get(symbol.node_id)
        if cached is None:
            source = source_by_path.get(symbol.source_ref)
            lines = source_lines.get(symbol.source_ref)
            if lines is None and source is not None:
                lines = source_lines[symbol.source_ref] = source.content.splitlines()
            span = '\n'.join((lines or [])[(symbol.span_start or 1) - 1:symbol.span_end or 0])
            cached = (live_terms & _tokenize_match(symbol.label), live_terms & _tokenize_match(span))
            term_cache[symbol.node_id] = cached
        return cached

    max_term_weight = max((term_weights.get(term, 1.0) for term in live_terms), default=0.0)

    def is_trivial(symbol: GraphNode) -> bool:
        return (
            symbol.span_end - symbol.span_start + 1 <= TRIVIAL_SPAN_LINES
            or ACCESSOR_RE.search(symbol.label) is not None
            or symbol.node_id in links.utilities
        )

    def relevance(callee_id: str) -> float:
        symbol = symbol_by_id.get(callee_id)
        if symbol is None or symbol.span_start is None or symbol.span_end is None:
            return RELEVANCE_FLOOR
        in_name, in_body = terms_of(symbol)
        if in_name:
            return 1.0
        if is_trivial(symbol):
            # A one-line virtual is still the way to its overrides.
            return RELEVANCE_FLOOR if callee_id in links.overridden_by and callee_id not in links.utilities else 0.0
        if in_body:
            return _body_relevance(in_body, term_weights, max_term_weight)
        return RELEVANCE_FLOOR if not (live_terms & file_tokens.get(symbol.source_ref, set())) else RELEVANCE_FILE_HIT

    def offerable(callee_id: str) -> bool:
        """False for a trivial virtual that has overriders and no task term in its name: the walk goes through it
        to its overriders (the T1 route) but the one-line base itself is not a bundle candidate."""
        if callee_id not in links.overridden_by:
            return True
        symbol = symbol_by_id.get(callee_id)
        if symbol is None or symbol.span_start is None or symbol.span_end is None:
            return True
        return bool(terms_of(symbol)[0]) or not is_trivial(symbol)

    if top_score > 0:
        links = call_links if call_links is not None else _call_graph_links(nodes, edges)
        file_neighbors = links.neighbors
        seeds = sorted(
            ((path, score) for path, score in source_scores.items() if score > 0),
            key=lambda item: (-item[1], item[0]),
        )[:CALL_EXPANSION_SEEDS]
        kind_by_path = {source.path: source.kind for source in sources}
        for path, score in seeds:
            seed_bonus = CALL_EXPANSION_BONUS * (score / top_score)
            hub = len(file_neighbors.get(path, ())) > CALL_EXPANSION_FANOUT
            for neighbor in sorted(file_neighbors.get(path, ())):
                bonus = seed_bonus
                if hub:
                    # A file linked to dozens of others says nothing about each: the lift is worth what the
                    # symbols that link the two files are worth to the task, and nothing when they share no
                    # term with it (`RELEVANCE_FLOOR` is the relevance of a symbol that does not), so the
                    # first of a hundred helpers by path is not lifted.
                    linked = max(
                        (relevance(symbol_id) for symbol_id in links.symbols.get((neighbor, path), ())), default=0.0
                    )
                    bonus = seed_bonus * linked if linked > RELEVANCE_FLOOR else 0.0
                if bonus <= 0 or neighbor not in source_scores or source_scores[neighbor] >= bonus:
                    continue
                if demote_aux and _is_aux_path(neighbor):
                    continue
                if demote_locale and _is_locale_source(neighbor, kind_by_path.get(neighbor, 'code')):
                    continue
                source_scores[neighbor] = bonus
                raw_source_scores[neighbor] = max(raw_source_scores[neighbor], bonus)
                lifted.add(neighbor)
    seed_scores = {f'file:{path}': score for path, score in source_scores.items() if score > 0}
    degraded = False
    if rank == 'bfs':
        proximity = _bfs_proximity(set(seed_scores), adj, max_depth=2)
        pagerank_scores: dict[str, float] = {}
    elif rank == 'pagerank':
        proximity = {}
        pagerank_scores = {}
        if seed_scores:
            if rank_graph is None:
                rank_graph = collapse_to_file_graph(nodes, edges)
            pagerank_scores, degraded = _bounded_pagerank(rank_graph, seed_scores)
    else:
        raise ValueError(f'Unknown rank mode: {rank}')

    check_deadline()
    candidates: list[BundleItem] = []

    for position, source in enumerate(sources):
        if position % 500 == 0:
            check_deadline()
        file_node_id = f'file:{source.path}'
        keyword_score = source_scores[source.path]
        graph_bonus = proximity.get(file_node_id, 0.0)
        if rank == 'pagerank':
            graph_bonus = pagerank_scores.get(file_node_id, 0.0) * PAGERANK_SCORE_MULTIPLIER
        final_score = keyword_score + graph_bonus

        token_count = token_counts[source.path]
        candidates.append(
            BundleItem(
                item_id=f'source:{source.path}',
                kind=source.kind,
                title=source.path,
                path=source.path,
                content=source.content,
                token_count=token_count,
                score=final_score,
                metadata={
                    'modified_at': source.modified_at,
                    'graph_bonus': graph_bonus,
                    # No task term in the path or text, no call-expansion lift and next to no graph score: what is
                    # left is recency and a trace of graph mass.
                    'no_evidence': (
                        source.path not in lifted
                        and graph_bonus < GRAPH_EVIDENCE_MIN
                        and not (task_terms & (file_tokens[source.path] - noise_terms))
                    ),
                },
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

    symbol_items_by_path: defaultdict[str, list[BundleItem]] = defaultdict(list)
    symbol_scores: dict[str, float] = {}
    for position, (path, path_symbols) in enumerate(symbols_by_path.items()):
        if position % 500 == 0:
            check_deadline()
        source = source_by_path.get(path)
        if source is None or source.kind != 'code':
            continue
        # A span can only score through a task term it contains, and its text is a run of whole
        # lines of the file, so a file that shares no (non-noise) term with the task can only
        # yield spans whose symbol name matches: skip everything else without cutting the text.
        file_matches = bool(task_terms & (file_tokens.get(path, set()) - noise_terms))
        if not file_matches:
            path_symbols = [s for s in path_symbols if task_terms & _tokenize_match(s.label)]
            if not path_symbols:
                continue
        lines = source.content.splitlines()
        file_share = SYMBOL_FILE_SCORE_SHARE * min(raw_source_scores.get(path, 0.0), SPAN_FILE_SHARE_CAP)
        for symbol in path_symbols:
            if symbol.span_start is None or symbol.span_end is None:
                continue
            span_lines = lines[symbol.span_start - 1:symbol.span_end]
            if not span_lines:
                continue
            span_text = '\n'.join(span_lines)
            name_bonus = _name_bonus(task_terms & _tokenize_match(symbol.label), term_weights, total_term_weight)
            span_score = _score_text(task_terms, _strip_boilerplate(span_text), 0.0, term_weights, noise_terms)
            # The file share is a tiebreaker for spans that match the task
            # themselves, not a qualifier for spans that match nothing.
            if name_bonus + span_score <= 0:
                continue
            score = name_bonus + span_score + file_share
            # Test/fixture files are demoted whole (above); their spans must not escape that policy, which the
            # proportional name bonus exposed (a test named after four task terms outranking the production span).
            if demote_aux and _is_aux_path(path):
                score *= AUX_PATH_DEMOTION
            item = BundleItem(
                item_id=f'symbol-span:{symbol.node_id}',
                kind='symbol',
                title=f'{path}:{symbol.label}',
                path=path,
                content=span_text,
                token_count=count_text_tokens(span_text, kind=source.kind),
                score=score,
                metadata={
                    'source_kind': source.kind,
                    'node_id': symbol.node_id,
                    'span_start': symbol.span_start,
                    'span_end': symbol.span_end,
                },
            )
            candidates.append(item)
            symbol_items_by_path[path].append(item)
            symbol_scores[symbol.node_id] = score
    expansion = _empty_expansion()
    callee_item_ids: dict[str, str] = {}  # computed callee symbol id -> id of the candidate that carries it
    callee_paths: dict[str, str] = {}  # computed callee symbol id -> its file
    if links.calls_from or links.bases_of:
        classes_by_label: defaultdict[str, list[GraphNode]] = defaultdict(list)
        for symbol in symbol_by_id.values():
            if symbol.kind == 'class':
                classes_by_label[symbol.label].append(symbol)

        def class_of(symbol_id: str) -> str | None:
            """Smallest class whose span holds the symbol, else its unique `Cls::` qualifier."""
            symbol = symbol_by_id.get(symbol_id)
            if symbol is None or symbol.span_start is None:
                return None
            holders = [
                cls for cls in symbols_by_path.get(symbol.source_ref, ())
                if cls.kind == 'class' and cls.node_id != symbol_id
                and cls.span_start is not None and cls.span_end is not None
                and cls.span_start <= symbol.span_start <= cls.span_end
            ]
            if holders:
                return min(holders, key=lambda cls: (cls.span_end - cls.span_start, cls.node_id)).node_id
            qualifier = symbol.metadata.get('qualifier') if isinstance(symbol.metadata, dict) else None
            matches = classes_by_label.get(str(qualifier), []) if qualifier else []
            return matches[0].node_id if len(matches) == 1 else None

        def file_of_symbol(symbol_id: str) -> str | None:
            return symbol_by_id[symbol_id].source_ref if symbol_id in symbol_by_id else None

        seed_refusals: Counter[str] = Counter()
        seed_items = _select_seeds(
            [item for item in candidates if item.kind == 'symbol' and item.score > 0], symbol_by_id, raw_source_scores,
            [constructor for built in links.constructors_of.values() for constructor in built],
            reaches=lambda symbol_id: _reaches_beyond_file(symbol_id, links, file_of_symbol, class_of),
            refusals=seed_refusals,
        )
        def overriders(method_id: str) -> list[str]:
            ranked = []
            for overrider_id in links.overridden_by.get(method_id, ()):
                overrider = symbol_by_id.get(overrider_id)
                source = source_by_path.get(overrider.source_ref) if overrider is not None else None
                if overrider is None or source is None or source.kind != 'code':
                    continue
                if demote_aux and _is_aux_path(overrider.source_ref):
                    continue
                in_name, in_body = terms_of(overrider)
                ranked.append((platform_demoted(overrider.source_ref), -len(in_name | in_body), overrider.source_ref, overrider_id))
            return [overrider_id for _demoted, _overlap, _path, overrider_id in sorted(ranked)]

        override_routes: dict[str, str] = {}
        callees = _callee_scores(
            [(item.metadata['node_id'], item.score) for item in seed_items],
            links.calls_from,
            file_of_symbol,
            class_of,
            links.bases_of,
            relevance=relevance,
            overriders=overriders,
            routes=override_routes,
            constructors_of=links.constructors_of,
            offerable=offerable,
        )
        file_candidate = {item.path: item for item in candidates if item.item_id.startswith('source:')}
        seed_by_id = {item.metadata['node_id']: item for item in seed_items}
        reaching_seeds = {
            origin for callee_id, (_score, origin, _hop, _rel) in callees.items()
            if origin in seed_by_id and file_of_symbol(callee_id) != seed_by_id[origin].path
        }
        expansion = {
            'seeds': [{'id': item.metadata['node_id'], 'path': item.path, 'score': round(item.score, 2)} for item in seed_items],
            'seeds_cross_file': len(reaching_seeds),
            'seed_refusals': {reason: seed_refusals.get(reason, 0) for reason in SEED_REFUSAL_REASONS},
            'callees_computed': len(callees),
            'callee_files': len({path for path in map(file_of_symbol, callees) if path is not None}),
            # Filled in after packing.
            'callees_packed': 0,
            'callees_dropped': dict.fromkeys(CALLEE_DROP_REASONS, 0),
            'reserved': False,
        }
        for callee_id, (callee_score, seed_id, hop, callee_relevance) in sorted(callees.items()):
            symbol = symbol_by_id.get(callee_id)
            source = source_by_path.get(symbol.source_ref) if symbol is not None else None
            if symbol is None or source is None or source.kind != 'code':
                continue
            if symbol.span_start is None or symbol.span_end is None:
                continue
            # `relevance` orders tied candidates below; the `why` renderer reads the other keys only.
            route = {'from': seed_id, 'hop': hop, 'relevance': callee_relevance}
            if callee_id in override_routes:
                route = {**route, 'type': 'override', 'via': override_routes[callee_id]}
            existing = next((i for i in symbol_items_by_path.get(symbol.source_ref, ()) if i.metadata.get('node_id') == callee_id), None)
            item_id = existing.item_id if existing is not None else f'symbol-span:{callee_id}'
            if existing is not None:
                if callee_score > existing.score:
                    existing.score = callee_score
                    existing.metadata['call_expansion'] = route
                    symbol_scores[callee_id] = callee_score
            else:
                span_text = '\n'.join(source.content.splitlines()[symbol.span_start - 1:symbol.span_end])
                if not span_text.strip():
                    continue
                item = BundleItem(
                    item_id=item_id,
                    kind='symbol',
                    title=f'{symbol.source_ref}:{symbol.label}',
                    path=symbol.source_ref,
                    content=span_text,
                    token_count=count_text_tokens(span_text, kind=source.kind),
                    score=callee_score,
                    metadata={
                        'source_kind': source.kind,
                        'node_id': callee_id,
                        'span_start': symbol.span_start,
                        'span_end': symbol.span_end,
                        'call_expansion': route,
                    },
                )
                candidates.append(item)
                symbol_items_by_path[symbol.source_ref].append(item)
                symbol_scores[callee_id] = callee_score
            callee_item_ids[callee_id] = item_id
            callee_paths[callee_id] = symbol.source_ref
            owner_file = file_candidate.get(symbol.source_ref)
            if owner_file is not None:
                owner_file.score = max(owner_file.score, callee_score * SYMBOL_FILE_SCORE_SHARE)
                # The file may be packed whole instead of the span: let it say why it is there.
                previous = owner_file.metadata.get('call_expansion')
                if previous is None or callee_score > previous.get('score', 0.0):
                    owner_file.metadata['call_expansion'] = {**route, 'to': callee_id, 'score': callee_score}
    for items in symbol_items_by_path.values():
        items.sort(key=lambda item: (-item.score, item.item_id))
        # The sort is by the undecayed score and the decay is monotone, so the order stands.
        for rank_in_file, item in enumerate(items):
            if rank_in_file:
                item.score *= SPAN_REPEAT_DECAY**rank_in_file

    def tie_order(item: BundleItem) -> tuple[float, float, int]:
        """(-score, -relevance, hop): callees that tie on score are ordered by relevance, then hop."""
        route = item.metadata.get('call_expansion') or {}
        return (-item.score, -route.get('relevance', 0.0), route.get('hop', 0))

    candidates.sort(key=lambda item: (*tie_order(item), item.path, item.item_id))

    has_code_matches = any(item.kind == 'code' and item.score > 0 for item in candidates)
    doc_cap = int(budget * DOC_BUDGET_SHARE) if has_code_matches else budget
    positive_matches = sum(1 for item in candidates if item.score > 0 and item.kind != 'symbol')
    # With multiple matches, cap each item so the top file cannot swallow the
    # whole budget; lone matches keep the full budget.
    item_cap = int(budget * ITEM_BUDGET_SHARE) if positive_matches > 1 else budget
    evidence_exists = any(
        item.kind != 'commit' and item.score > 0 and not item.metadata.get('no_evidence') for item in candidates
    )

    file_item_by_path = {item.path: item for item in candidates if item.item_id.startswith('source:')}

    matching_code_files = sum(1 for item in candidates if item.kind == 'code' and item.score > 0)
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

    selected_spans: dict[str, list[tuple[int, int]]] = defaultdict(list)
    span_tokens: defaultdict[str, int] = defaultdict(int)
    oversize_tokens = 0

    def inside_selected_span(item: BundleItem) -> bool:
        start, end = item.metadata.get('span_start'), item.metadata.get('span_end')
        if start is None or end is None:
            return False
        # Either way round: a span inside a selected one, or one enclosing a selected one, repeats its lines
        # (the method can outscore its class and be selected first).
        return any((lo <= start and end <= hi) or (start <= lo and hi <= end) for lo, hi in selected_spans[item.path])

    def over_file_cap(item: BundleItem, file_cap: int | None) -> bool:
        """True when one more span of this file would pass its share of the budget (see FILE_SPAN_SHARE), or would take
        the oversize spans past OVERSIZE_SPAN_SHARE of it."""
        if file_cap is None:
            return False
        if (
            matching_code_files >= FILE_SPAN_SHARE_FILES
            and item.token_count > file_cap
            and oversize_tokens + item.token_count > int(budget * OVERSIZE_SPAN_SHARE)
        ):
            return True
        return len(selected_spans[item.path]) >= SPANS_BEFORE_CAP and span_tokens[item.path] + item.token_count > file_cap

    # Why a call-expansion span was last turned away by `pack` (a counter in `bundle['expansion']`, nothing more).
    turned_away: dict[str, str] = {}
    expansion_item_ids = frozenset(callee_item_ids.values())

    def select_span(item: BundleItem, file_cap: int | None = None) -> None:
        nonlocal oversize_tokens
        span_tokens[item.path] += item.token_count
        if file_cap is not None and item.token_count > file_cap:
            oversize_tokens += item.token_count
        select(item)
        if item.metadata.get('span_start') is not None and item.metadata.get('span_end') is not None:
            selected_spans[item.path].append((item.metadata['span_start'], item.metadata['span_end']))

    def reset_packing() -> None:
        """Back to an empty bundle: the state every pass reads and writes, so a second packing starts clean."""
        nonlocal total_tokens, doc_tokens, oversize_tokens
        selected.clear()
        selected_ids.clear()
        files_fully_selected.clear()
        paths_with_symbols.clear()
        selected_spans.clear()
        span_tokens.clear()
        turned_away.clear()
        total_tokens = doc_tokens = oversize_tokens = 0

    def select_symbol(item: BundleItem, allowed: int, file_cap: int | None) -> None:
        """A symbol candidate: the whole file when it fits outright, else the span if the file's share of the budget
        allows it. Selects nothing when the file is already in, or the span is inside a selected one."""
        if item.path in files_fully_selected:
            return
        if inside_selected_span(item):
            if item.item_id in expansion_item_ids:
                turned_away[item.item_id] = 'inside_selected_span'
            return
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
            return
        if 0 < item.token_count <= allowed and not over_file_cap(item, file_cap):
            select_span(item, file_cap)
            paths_with_symbols.add(item.path)
        elif item.item_id in expansion_item_ids:
            if item.token_count <= allowed:
                turned_away[item.item_id] = 'file_cap'
            elif item.token_count > min(item_cap, budget):
                turned_away[item.item_id] = 'too_large'
            else:
                turned_away[item.item_id] = 'budget_exhausted'

    def pack(file_cap: int | None = None) -> None:
        """One pass over the candidates by score. With `file_cap`, a file's spans stop at that many tokens
        (its first SPANS_BEFORE_CAP spans excepted); whole files and skeletons are not held back."""
        for item in candidates:
            if item.score <= 0 or item.item_id in selected_ids:
                continue
            if item.token_count <= 0:
                continue  # an empty file or span adds nothing to a bundle
            if item.metadata.get('no_evidence') and evidence_exists:
                continue  # leftover budget is for matches, not for the first unrelated file by path
            allowed = min(budget - total_tokens, item_cap)
            if item.kind == 'symbol':
                select_symbol(item, allowed, file_cap)
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
                    if symbol_item.item_id in selected_ids or inside_selected_span(symbol_item):
                        continue
                    if over_file_cap(symbol_item, file_cap):
                        continue
                    if 0 < symbol_item.token_count <= remaining:
                        select_span(symbol_item, file_cap)
                        remaining -= symbol_item.token_count
                        picked_symbol = True
                if picked_symbol:
                    paths_with_symbols.add(item.path)
                    continue
                symbols = symbols_by_path.get(item.path, [])
                if symbols:
                    skeleton = _skeleton_item(item, symbols, symbol_scores, remaining)
                    if skeleton is not None:
                        select(skeleton)
                    # Code files with indexed symbols never degrade to file[0:N].
                    continue
            token_kind = str(item.metadata.get('source_kind', item.kind))
            truncated = truncate_text_to_budget(item.content, remaining, kind=token_kind)
            truncated_tokens = count_text_tokens(truncated, kind=token_kind)
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

    # First the capped pass, so a dense file leaves room for the other matches; then the same pass without
    # the cap, which only adds what the cap held back and what is still left of the budget.
    first_pass_cap = max(1, int(budget * file_span_share(matching_code_files)))
    pack(first_pass_cap)
    pack()

    def callee_is_packed(callee_id: str) -> bool:
        symbol = symbol_by_id[callee_id]
        path = callee_paths[callee_id]
        return (
            callee_item_ids[callee_id] in selected_ids
            or path in files_fully_selected
            or any(lo <= symbol.span_start and symbol.span_end <= hi for lo, hi in selected_spans[path])
        )

    def reserve_expansion() -> None:
        """Select the best EXPANSION_RESERVED_ITEMS call-expansion spans (the candidates are in `tie_order`) through the
        same path as a normal pass, while they take at most EXPANSION_BUDGET_SHARE of the budget."""
        reserve_end = total_tokens + int(budget * EXPANSION_BUDGET_SHARE)
        reserved = 0
        for item in candidates:
            if reserved >= EXPANSION_RESERVED_ITEMS or total_tokens >= reserve_end:
                break
            if item.item_id not in expansion_item_ids or item.score <= 0 or item.token_count <= 0:
                continue
            if item.item_id in selected_ids or item.path in files_fully_selected:
                continue
            before = len(selected)
            select_symbol(item, min(budget - total_tokens, item_cap, reserve_end - total_tokens), first_pass_cap)
            reserved += len(selected) > before

    if (
        EXPANSION_RESERVED_ITEMS > 0
        and callee_item_ids
        and not any(callee_is_packed(callee_id) for callee_id in callee_item_ids)
    ):
        # The walk found callees and the budget went to other items before the passes reached them.
        reset_packing()
        reserve_expansion()
        turned_away.clear()  # the reservation's refusals say nothing about the passes that follow
        pack(first_pass_cap)
        pack()
        expansion['reserved'] = True

    if expansion['callees_computed']:
        dropped = expansion['callees_dropped']
        for callee_id, item_id in callee_item_ids.items():
            if callee_is_packed(callee_id):
                expansion['callees_packed'] += 1
            else:
                dropped[turned_away.get(item_id, 'not_reached')] += 1
        dropped['no_candidate'] = expansion['callees_computed'] - len(callee_item_ids)

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
            *(['Graph ranking skipped to fit the time budget.'] if degraded else []),
        ],
        open_questions=[] if selected else ['No matching sources found. Run cortex ingest . first.'],
    )
    store.save_bundle(repo_root, bundle)

    if output_format == 'json':
        result = bundle.to_dict()
        result['expansion'] = expansion
        if degraded:
            result['degraded'] = ['graph_rank']
        return result
    return _bundle_markdown(bundle)
