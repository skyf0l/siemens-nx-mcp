"""The four gateway tools (tool-surface 2.1).

- ``nx_find_tools``: a scored OR search after stemming, stop words and synonym
  expansion: every tool matching at least one query word is a hit. Per word, the
  best field (name > keyword, label > variant > description) times the word's
  rarity in the catalogue; scaled by the share of words matched; plus bonuses for
  the share of the tool's name words named, a keyword or label phrase in the
  query (a one-word keyword counts less), a word naming the tool's domain and, for a
  query with an action word (create, make, add, set...), tools that create or set
  (``nx_create_*``, ``nx_set_*``) over those that read or export. An exact name
  wins. ``queries`` runs 1-20 one-task searches at once. Nothing matched: close
  spellings and the domain overview; an empty query returns that overview. Words for
  things NX MCP does as another tool ("pocket": an extrude that subtracts) add a note.
- Unknown tool names (``nx_fillet_edges``) are answered with tools found by their
  words (fillet -> nx_create_blend) as well as close spellings.
- ``nx_get_tool_schemas``: input schema, example, prerequisites and evidence for
  1-10 tools or variants (``nx_create_datum_plane#offset``); several variants of one
  tool come as one entry (``server.group_variants``).
- ``nx_invoke``: call any tool; ``intent`` is shown in the NX panel.
- ``nx_invoke_read``: the same for read tools only; others are refused before
  their arguments are validated, so hosts can auto-approve it.
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nx_mcp.core.catalogue import Domain, Effect
from nx_mcp.core.errors import NXError
from nx_mcp.sidecar import schema

NAMES = ("nx_find_tools", "nx_get_tool_schemas", "nx_invoke", "nx_invoke_read")
MAX_QUERIES = 20  # one-task searches per nx_find_tools call
# Words naming something NX MCP does not do, with what to do instead (a find note).
MISSING = {
    "pocket": "No pocket tool: sketch the outline on the face (nx_create_sketch, "
    "nx_create_sketch_curves), then nx_create_extrude into the body with combine "
    "{operation: subtract}",
}
TOOL_NAME = r"^nx_[a-z0-9_]{1,80}$"
SUMMARY_CHARS = 48

# Words agents use for an NX command of another name. Keywords on each tool add
# its own synonyms; these cover terms shared by several tools.
SYNONYMS = {
    "fillet": ("blend",),
    "round": ("blend",),
    "pad": ("extrude",),
    "boss": ("extrude",),
    "protrude": ("extrude",),
    "mate": ("constraint",),
    "screenshot": ("capture", "screenshot"),
    "image": ("capture", "screenshot"),
    "remove": ("delete",),
    "rename": ("name",),
    "inventory": ("list",),
    "show": ("list", "get"),
    "around": ("circular",),
    "polar": ("circular",),
    "cut": ("extrude", "subtract"),
    "pocket": ("extrude", "subtract"),
    "cutout": ("extrude", "subtract"),
    "bevel": ("chamfer",),
    "drill": ("hole",),
    "bore": ("hole",),
    "center": ("centre",),  # "center of gravity": the mass tools' "centre of gravity"
}
WEIGHTS = {"name": 8, "keyword": 4, "variant": 4, "label": 4, "description": 1}
SYNONYM, PREFIX = 0.75, 0.5  # a synonym or a word's prefix ranks below the word itself
EXACT = 0.5  # the word itself is in the name, a keyword or the label (fillet: blend first)
COVERAGE = 0.5  # share of a tool's score that follows the share of query words matched
NAME = 4  # times the share of the tool's name words the query names (pattern: nx_create_pattern)
PHRASE = 6  # bonus when a whole keyword or the label appears in the query
DOMAIN = 3  # bonus when a query word names the tool's domain
KEYWORD = 2  # bonus when a query word is a whole one-word keyword ("pocket": extrude)
# Action words: a query with one ranks tools whose name verb is in CHANGING by ACTION
# above those that read, list, export or activate ("create expression": set_expression
# over export_expressions). Never respelled: "make block" keeps "make".
ACTION_WORDS = frozenset(
    ["create", "make", "add", "new", "set", "insert", "build", "place", "define", "generate"]
)
CHANGING = frozenset({"create", "set"})
ACTION = 3
SPELLING, MAX_SPELLINGS = 0.75, 4  # difflib cutoff and count of close spellings
# The empty-query overview names these first in each domain (others: alphabetical).
KEY_TOOLS = {
    "assembly": ("nx_create_component", "nx_create_assembly_constraint", "nx_list_components"),
    "display": ("nx_capture_view", "nx_set_camera", "nx_set_appearance"),
    "drawing": (
        "nx_create_drawing_sheet",
        "nx_create_drawing_view",
        "nx_create_drawing_dimension",
    ),
    "freeform": ("nx_create_spline", "nx_create_bridge_surface", "nx_create_thicken"),
    "inspection": ("nx_list_objects", "nx_find_geometry", "nx_measure_mass"),
    "modeling": ("nx_create_extrude", "nx_create_revolve", "nx_create_blend", "nx_set_expression"),
    "motion": ("nx_run_motion_sweep",),
    "optimization": (
        "nx_create_topology_study",
        "nx_create_topology_condition",
        "nx_run_topology_study",
    ),
    "parts_files": ("nx_create_part", "nx_open_part", "nx_save_part"),
    "session": ("nx_get_status", "nx_undo_operation", "nx_run_batch"),
    "sheet_metal": ("nx_create_sheet_metal_feature", "nx_export_flat_pattern"),
    "sketch": ("nx_create_sketch", "nx_create_sketch_curves", "nx_finish_sketch"),
}
# Words that carry no meaning in a tool search ("how do I add a part to the assembly").
# Not "new" or "create": "new part" names nx_create_part through its keyword, and
# "create", in most names, already counts little by its rarity.
STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "to",
        "of",
        "for",
        "in",
        "on",
        "at",
        "by",
        "with",
        "into",
        "from",
        "and",
        "or",
        "my",
        "this",
        "that",
        "it",
        "nx",
        "please",
        "using",
        "via",
        "then",
        "all",
        "some",
        "these",
        "those",
        "i",
        "we",
        "you",
        "how",
        "do",
        "can",
        "should",
        "want",
        "need",
        "will",
    ]
)
DOMAIN_WORDS = {
    "part": "parts_files",
    "file": "parts_files",
    "sketch": "sketch",
    "assembly": "assembly",
    "component": "assembly",
    "drawing": "drawing",
    "sheet": "sheet_metal",
    "inspect": "inspection",
    "display": "display",
    "view": "display",
    "session": "session",
    "freeform": "freeform",
    "surface": "freeform",
    "feature": "modeling",
    "model": "modeling",
    "motion": "motion",
    "hinge": "motion",
    "linkage": "motion",
    "swing": "motion",
    "kinematic": "motion",
    "optimization": "optimization",
    "optimize": "optimization",
    "lightweight": "optimization",
}


def stem(word: str) -> str:
    """Lower-case and strip a plural or simple verb ending ("edges" -> "edge")."""
    word = word.casefold()
    for suffix, keep in (("ies", "y"), ("sses", "ss"), ("ing", ""), ("ed", ""), ("es", "e")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + keep
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def words(text: str) -> list[str]:
    return [stem(w) for w in re.findall(r"[a-z0-9]+", str(text).casefold())]


ACTION_STEMS = frozenset(stem(w) for w in ACTION_WORDS)


class Entry:
    """One searchable catalogue tool."""

    def __init__(self, name, *, effect, domain, label, description, keywords=()):
        self.name = name
        self.effect = effect
        self.domain = domain
        self.summary = _clip(label, SUMMARY_CHARS)
        self.description = description
        self.fields = {
            "name": set(words(name.removeprefix("nx_"))),
            "keyword": {w for k in keywords for w in words(k)},
            "label": set(words(label)),
            "description": set(words(description)),
        }
        # Multi-word keywords and the label, matched as phrases in the query (both without
        # stop words: "edges in drawing view" matches the query "edges in drawing view").
        self.phrases = {
            " ".join(p)
            for p in ([w for w in words(k) if w not in STOP_WORDS] for k in (*keywords, label))
            if len(p) > 1
        }
        # One-word keywords, as stemmed query words ("pocket" on nx_create_extrude).
        self.single = {w[0] for w in (words(k) for k in keywords) if len(w) == 1}
        self.verb = name.split("_")[1] if name.count("_") >= 2 else ""
        # Words of the name, label and keywords as written: close spellings come from these.
        self.terms = {
            w for text in (name.removeprefix("nx_"), label, *keywords) for w in _raw(text)
        }

    def match(self, query: list[str], variants=(), rarity=None) -> tuple[float, list[int]]:
        """``(score, indices of the matched query words)``.

        Per matched word: its best field weight (``WEIGHTS``) times the kind of match (whole
        word 1, ``SYNONYM``, ``PREFIX``), plus ``EXACT`` when the word itself is in the
        name, a keyword or the label, times its ``rarity``. The sum is scaled by the share
        of query words matched (``COVERAGE``); bonuses add the share of the name's words
        the query names (``NAME``), keyword or label phrases in the query (``PHRASE``) and
        a word naming the tool's domain (``DOMAIN``). ``variants``: the tool's variant
        values offered on this build (searchable too)."""
        fields = dict(self.fields)
        fields["variant"] = {w for v in variants for w in words(v)}
        total = 0.0
        matched = []
        named: set[str] = set()  # name words the query names (whole word or synonym)
        for position, word in enumerate(query):
            forms = {word, *(stem(s) for s in SYNONYMS.get(word, ()))}
            best = 0.0
            for field, tokens in fields.items():
                for token in tokens:
                    if token == word:
                        kind = 1.0
                    elif token in forms:
                        kind = SYNONYM
                    elif any(_match(f, token) for f in forms):
                        kind = PREFIX
                    else:
                        continue
                    best = max(best, WEIGHTS[field] * kind)
                    if field == "name" and kind > PREFIX:
                        named.add(token)
            if best:
                if any(word in fields[f] for f in ("name", "keyword", "label")):
                    best += EXACT
                matched.append(position)
                total += best * (rarity(word) if rarity else 1.0)
        if not matched:
            return 0.0, matched
        total *= 1 - COVERAGE + COVERAGE * len(matched) / len(query)
        total += NAME * len(named) / max(len(self.fields["name"]), 1)
        text = " " + " ".join(query) + " "
        total += sum(PHRASE for p in self.phrases if f" {p} " in text)
        total += KEYWORD * len(self.single.intersection(query))
        if any(DOMAIN_WORDS.get(w) == self.domain for w in query):
            total += DOMAIN
        if self.verb in CHANGING and ACTION_STEMS.intersection(query):
            total += ACTION
        return total, matched


class Index:
    """Rarity of query words over a fixed set of tools (the whole catalogue, built once):
    a word few tools match ("save") counts more than a common one ("part")."""

    def __init__(self, entries) -> None:
        self.size = 0
        self.tokens: dict[str, set[str]] = {}  # field token -> names of the tools having it
        self.terms: set[str] = set()
        for entry in entries:
            self.size += 1
            self.terms |= entry.terms
            for tokens in entry.fields.values():
                for token in tokens:
                    self.tokens.setdefault(token, set()).add(entry.name)

    def tools(self, word: str) -> set[str]:
        """Names of the tools matching a stemmed word or its synonyms in any field."""
        forms = (word, *(stem(s) for s in SYNONYMS.get(word, ())))
        names: set[str] = set()
        for token, having in self.tokens.items():
            if any(_match(f, token) for f in forms):
                names |= having
        return names

    def rarity(self, word: str) -> float:
        """``1 + log2(N / n)``, n: tools matching the word (at least 1): 1 for a word every
        tool matches, about 8 for one tool in 160."""
        return 1 + math.log2(max(self.size, 1) / max(len(self.tools(word)), 1))

    def respell(self, query: str) -> tuple[str, list[str]]:
        """``(query, spellings)``: the query with each word no tool matches replaced by its
        close spellings among tool names, labels and keywords (difflib); no spellings: the
        query unchanged."""
        kept: list[str] = []
        spellings: list[str] = []
        terms = sorted(self.terms)
        for typed, word in query_words(query):
            if self.tools(word) or word in ACTION_STEMS:
                kept.append(typed)
            else:
                close = difflib.get_close_matches(typed, terms, n=2, cutoff=SPELLING)
                spellings += [c for c in close if c not in spellings]
        spellings = spellings[:MAX_SPELLINGS]
        return (" ".join(kept + spellings), spellings) if spellings else (query, [])


@dataclass(frozen=True)
class Hit:
    entry: Entry
    score: float
    matched: tuple[str, ...]  # the query words the tool matched, as typed


def _match(word: str, token: str) -> bool:
    return token == word or (len(word) >= 3 and token.startswith(word))


def _raw(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text).casefold())


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def query_words(query: str) -> list[tuple[str, str]]:
    """``(typed, stemmed)`` query words without stop words (all of them if every word is
    one) and without repeats."""
    found = [(w, stem(w)) for w in _raw(query)]
    kept = [pair for pair in found if pair[1] not in STOP_WORDS] or found
    seen: set[str] = set()
    out = []
    for typed, stemmed in kept:
        if stemmed not in seen:
            seen.add(stemmed)
            out.append((typed, stemmed))
    return out


def search(entries, query: str, domain=None, effect=None, variants=None) -> list:
    """Ranked entries; ``variants(entry)``: the entry's offered variant values (None: not
    searched). See :func:`ranked`."""
    return [hit.entry for hit in ranked(entries, query, domain, effect, variants)[0]]


def ranked(
    entries, query: str, domain=None, effect=None, variants=None, index: Index | None = None
) -> tuple[list[Hit], bool]:
    """``(hits, partial)``, best score first: every tool matching at least one query word
    (a scored OR, see :meth:`Entry.match`); an exact tool name comes first. ``index``: word
    rarity over the whole catalogue (default: over ``entries``). ``partial``: no tool
    matched every word. An empty query lists every tool by name."""
    typed = query_words(query)
    wanted = [w for _, w in typed]
    index = index or Index(entries)
    rarity = {w: index.rarity(w) for w in wanted}
    exact = query.strip()
    hits = []
    for entry in entries:
        if (domain and entry.domain != domain) or (effect and entry.effect != effect):
            continue
        if entry.name == exact:
            hits.append(Hit(entry, math.inf, tuple(t for t, _ in typed)))
        elif not wanted:
            hits.append(Hit(entry, 0.0, ()))
        else:
            offered = variants(entry) if variants else ()
            score, matched = entry.match(wanted, offered, rarity.__getitem__)
            if matched:
                hits.append(Hit(entry, round(score, 3), tuple(typed[i][0] for i in matched)))
    hits.sort(key=lambda hit: (-hit.score, hit.entry.name))
    partial = bool(hits) and all(len(hit.matched) < len(wanted) for hit in hits)
    return hits, partial


def close_matches(name: str, known) -> list[str]:
    return difflib.get_close_matches(str(name), list(known), n=3, cutoff=0.6)


TYPO = 0.85  # a close spelling this similar is a typo of that name: it comes first


def suggestions(name: str, known, entries=(), index: Index | None = None) -> list[str]:
    """Up to 3 tools for an unknown tool name: a near-identical spelling first (a typo),
    then the best tools for the name's words (``nx_fillet_edges``: "fillet edges" ->
    nx_create_blend), then other close spellings."""
    known = list(known)
    spelled = close_matches(name, known)
    typos = [n for n in spelled if difflib.SequenceMatcher(None, str(name), n).ratio() >= TYPO]
    found: list[str] = []
    text = str(name).removeprefix("nx_").replace("_", " ")
    if entries and text.strip():
        index = index or Index(entries)
        searched = index.respell(text)[0]
        hits, _ = ranked([e for e in entries if e.name in known], searched, index=index)
        found = [h.entry.name for h in hits[:3]]
    out: list[str] = []
    for candidate in (*typos, *found, *spelled):
        if candidate not in out:
            out.append(candidate)
    return out[:3]


def unknown_tool(
    name: str, known, *, field: str | None = "/tool", entries=(), index=None
) -> NXError:
    matches = suggestions(name, known, entries, index)
    return NXError(
        "NX_UNKNOWN_TOOL",
        f"Unknown tool: {name}",
        field=field,
        hint=("Did you mean " + ", ".join(matches) + "? " if matches else "")
        + "Find tools with nx_find_tools.",
        details={"close_matches": matches},
    )


# ---------------------------------------------------------------------------
# Gateway argument models


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FindTools(_Strict):
    query: Annotated[
        str,
        Field(
            max_length=120,
            description="1-4 keywords for ONE task, e.g. 'revolve profile', 'save part'. Common "
            "words are ignored. For several tasks pass queries.",
        ),
    ] = ""
    queries: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=120)]] | None,
        Field(min_length=1, max_length=MAX_QUERIES, description="One task each; top 5 per query"),
    ] = None
    domain: Domain | None = None
    effect: Effect | None = None
    offset: Annotated[int, Field(ge=0, le=10_000)] = 0
    limit: Annotated[int | None, Field(ge=1, le=20)] = None  # 10; 5 per query in queries

    @model_validator(mode="after")
    def _query_or_queries(self) -> FindTools:
        if self.queries is not None and self.query.strip():
            raise ValueError("pass query or queries, not both")
        return self


class GetToolSchemas(_Strict):
    names: Annotated[
        list[
            Annotated[
                str, Field(max_length=100, pattern=r"^nx_[a-z0-9_]{1,80}(#[a-z0-9_]{1,40})?$")
            ]
        ],
        Field(
            min_length=1, max_length=10, description="Tool names; 'tool#variant' for one variant"
        ),
    ]
    include_output: bool = False


class Invoke(_Strict):
    tool: Annotated[str, Field(max_length=100, pattern=TOOL_NAME)]
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Tool arguments; own operation_id (to retry a mutation): 8-128 of A-Za-z0-9_-",
    )
    intent: Annotated[
        str | None,
        Field(max_length=120, description="Why, in the engineer's terms (shown in the NX panel)"),
    ] = None
    detail: Literal["compact", "full"] = "compact"


class InvokeRead(_Strict):
    tool: Annotated[str, Field(max_length=100, pattern=TOOL_NAME)]
    arguments: dict[str, Any] = Field(default_factory=dict)
    detail: Literal["compact", "full"] = "compact"


MODELS: dict[str, type[BaseModel]] = {
    "nx_find_tools": FindTools,
    "nx_get_tool_schemas": GetToolSchemas,
    "nx_invoke": Invoke,
    "nx_invoke_read": InvokeRead,
}
DESCRIPTIONS = {
    "nx_find_tools": (
        "Search NX tools by task (fillet finds blend); empty query: domain overview. Rows: "
        "name, summary, effect, domain, matched. Then nx_get_tool_schemas and nx_invoke "
        "(nx_invoke_read for effect read); not MCP tools."
    ),
    "nx_get_tool_schemas": (
        "Exact input schemas, examples, prerequisites and evidence for 1-10 tools; output "
        "schemas on request. Several variants of one tool ('tool#a', 'tool#b') come as one "
        "entry: input_schema holds what they share; variants.<v>.input_schema adds v's "
        "properties and required."
    ),
    "nx_invoke": (
        "Call any NX tool with its exact arguments; the kernel checks and undoes failures."
    ),
    "nx_invoke_read": (
        "nx_invoke for read-only tools (effect read); others are refused before validation. "
        "Safe to auto-approve."
    ),
}


def tool_schema(name: str) -> dict:
    out = schema.tidy(MODELS[name].model_json_schema())
    for key, prop in list(out.get("properties", {}).items()):
        # An optional field is documented by omission, not by null (as for catalogue tools).
        prop = schema._without_null(prop)
        if "default" in prop and prop["default"] is None:
            prop = {k: v for k, v in prop.items() if k != "default"}
        if key == "offset":  # the model bounds it; no agent pages that far (schema.OFFSET_MAX)
            prop = {k: v for k, v in prop.items() if k != "maximum"}
        out["properties"][key] = prop
    return out
