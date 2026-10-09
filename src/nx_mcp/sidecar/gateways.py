"""The four gateway tools (tool-surface 2.1).

- ``nx_find_tools``: ranked search. Every query word must match after simple
  stemming and synonym expansion; a word matching the name outranks keywords,
  which outrank the label/summary, which outrank the description. Legacy tools
  are listed below catalogue tools and marked ``legacy``; an exact name wins.
- ``nx_get_tool_schemas``: input schema, example, prerequisites and evidence for
  1-10 tools or variants (``nx_create_datum_plane#offset``).
- ``nx_invoke``: call any tool; ``intent`` is shown in the NX panel.
- ``nx_invoke_read``: the same for read tools only; others are refused before
  their arguments are validated, so hosts can auto-approve it.
"""

from __future__ import annotations

import difflib
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nx_mcp.core.catalogue import DOMAINS, EFFECTS, first_sentence
from nx_mcp.core.errors import NXError
from nx_mcp.sidecar import schema

NAMES = ("nx_find_tools", "nx_get_tool_schemas", "nx_invoke", "nx_invoke_read")
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
}
WEIGHTS = {"name": 8, "keyword": 4, "label": 2, "description": 1}


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


class Entry:
    """One searchable tool: a catalogue spec or a legacy pseudo-spec."""

    def __init__(self, name, *, effect, domain, label, description, keywords=(), legacy=False):
        self.name = name
        self.effect = effect
        self.domain = domain
        self.legacy = legacy
        self.summary = _clip(first_sentence(description) if legacy else label, SUMMARY_CHARS)
        self.description = description
        self.fields = {
            "name": set(words(name.removeprefix("nx_"))),
            "keyword": {w for k in keywords for w in words(k)},
            "label": set(words(label)),
            "description": set(words(description)),
        }

    def score(self, query: list[str]) -> int | None:
        """Sum of the best field weight per query word; None when a word is missing."""
        total = 0
        for word in query:
            synonyms = [stem(s) for s in SYNONYMS.get(word, ())]
            best = 0
            for field, tokens in self.fields.items():
                if any(_match(word, t) for t in tokens):
                    best = max(best, WEIGHTS[field])
                elif any(_match(s, t) for s in synonyms for t in tokens):
                    best = max(best, WEIGHTS[field] // 2)  # a synonym ranks below the word
            if not best:
                return None
            total += best
        return total


def _match(word: str, token: str) -> bool:
    return token == word or (len(word) >= 3 and token.startswith(word))


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def search(entries, query: str, domain=None, effect=None) -> list:
    query_words = words(query)
    exact = query.strip()
    ranked = []
    for entry in entries:
        if (domain and entry.domain != domain) or (effect and entry.effect != effect):
            continue
        if entry.name == exact:
            ranked.append(((0, 0, 0), entry))
            continue
        score = entry.score(query_words)
        if score is not None:
            ranked.append(((1, int(entry.legacy), -score), entry))
    ranked.sort(key=lambda item: (item[0], item[1].name))
    return [entry for _, entry in ranked]


def close_matches(name: str, known) -> list[str]:
    return difflib.get_close_matches(str(name), list(known), n=3, cutoff=0.6)


def unknown_tool(name: str, known, *, field: str = "/tool") -> NXError:
    matches = close_matches(name, known)
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
        str, Field(max_length=120, description="Task words, e.g. 'fillet edges'; empty lists all")
    ] = ""
    domain: Literal[tuple(sorted(DOMAINS))] | None = None  # type: ignore[valid-type]
    effect: Literal[EFFECTS] | None = None  # type: ignore[valid-type]
    offset: Annotated[int, Field(ge=0, le=10_000)] = 0
    limit: Annotated[int, Field(ge=1, le=20)] = 10


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
    arguments: dict[str, Any] = Field(default_factory=dict)
    intent: Annotated[
        str | None,
        Field(max_length=120, description="Why, in the engineer's terms (shown in the NX panel)"),
    ] = None
    detail: Literal["compact", "full"] = "compact"


class InvokeRead(_Strict):
    tool: Annotated[str, Field(max_length=100, pattern=TOOL_NAME)]
    arguments: dict[str, Any] = Field(default_factory=dict)
    detail: Literal["compact", "full"] = "compact"


MODELS = {
    "nx_find_tools": FindTools,
    "nx_get_tool_schemas": GetToolSchemas,
    "nx_invoke": Invoke,
    "nx_invoke_read": InvokeRead,
}
DESCRIPTIONS = {
    "nx_find_tools": (
        "Search NX tools by task words; every word must match (synonyms count: fillet finds "
        "blend). Rows: name, summary, effect, domain; legacy rows are older tools. Then fetch "
        "schemas with nx_get_tool_schemas and call nx_invoke."
    ),
    "nx_get_tool_schemas": (
        "Exact input schemas, examples, prerequisites and evidence for 1-10 tools; "
        "'tool#variant' narrows a tool to one variant. Output schemas on request."
    ),
    "nx_invoke": (
        "Call any NX tool with its exact arguments; the kernel checks and undoes failures. Put "
        "operation_id inside arguments to retry safely. intent is shown to the engineer in the "
        "NX panel. Compact results keep result_id (expand with nx_result)."
    ),
    "nx_invoke_read": (
        "Call a read-only NX tool (effect read) with its exact arguments; any other tool is "
        "refused before validation. Safe to auto-approve."
    ),
}


def tool_schema(name: str) -> dict:
    return schema.tidy(MODELS[name].model_json_schema())
