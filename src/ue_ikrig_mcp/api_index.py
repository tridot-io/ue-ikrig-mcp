"""Local Unreal Python API catalogue with lexical (BM25) search.

The catalogue is harvested once per engine version from the live editor
(every class/method/property of the `unreal` module with its signature and a
short doc summary) and persisted on disk. Searching it costs no editor
round-trip and no driver tokens beyond the query itself — the swift
alternative to dir(unreal) dumps or hallucinated API names.
"""

import json
import math
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from .ue_scripts import escape_string, wrap_script

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")
_VERSION_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_.+-]")

# Project-layer entry kinds: assets harvested from the asset registry rather
# than the unreal module. For these, 'p' holds the parent class (not a
# containing class) and 's' holds the asset's package path.
PROJECT_ASSET_KINDS = frozenset(
    {"blueprint", "widget", "animbp", "struct", "enum", "dataasset"}
)

# (path, mtime) -> (meta, entries, index) so repeated searches reuse the
# parsed catalogue and its index instead of re-reading tens of MB of JSON.
_INDEX_CACHE: dict[
    tuple[str, float],
    tuple[dict[str, Any], list[dict[str, Any]], "_Bm25Index"],
] = {}


def catalog_dir() -> Path:
    configured = os.environ.get("UE_MCP_CATALOG_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".ue_ikrig_mcp" / "api_catalog"


def catalog_path_for_version(engine_version: str) -> Path:
    safe = _VERSION_SANITIZE_RE.sub("_", engine_version) or "unknown"
    return catalog_dir() / f"{safe}.json"


def latest_catalog_path() -> Optional[Path]:
    directory = catalog_dir()
    if not directory.is_dir():
        return None
    candidates = sorted(
        directory.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


ENGINE_VERSION_SCRIPT = wrap_script(
    "import unreal\n"
    'print("__MCP_RESULT__" + json.dumps('
    '{"engine": str(unreal.SystemLibrary.get_engine_version())}))'
)


def build_harvest_script(target_windows_path: str) -> str:
    """Script that walks the unreal module inside the editor, then the asset
    registry for project-defined types (Blueprints, widgets, user structs and
    enums, data assets), and writes the catalogue JSON to
    `target_windows_path` (kept off the result echo: the payload is tens of
    MB)."""
    target = escape_string(target_windows_path)
    return wrap_script(
        "import unreal\n"
        "import time\n"
        "\n"
        "def _doc_parts(doc):\n"
        "    lines = [l.strip() for l in (doc or '').strip().splitlines() if l.strip()]\n"
        "    sig = lines[0] if lines else ''\n"
        "    rest = ' '.join(lines[1:8])\n"
        "    if rest.startswith('--'):\n"
        "        rest = rest[2:].strip()\n"
        "    return sig[:240], rest[:600]\n"
        "\n"
        "entries = []\n"
        "# UE Python classes report __module__ == 'builtins', so ancestor\n"
        "# filtering must go by membership in the unreal namespace instead.\n"
        "_unreal_names = set(n for n in dir(unreal) if not n.startswith('_'))\n"
        "for name in dir(unreal):\n"
        "    if name.startswith('_'):\n"
        "        continue\n"
        "    try:\n"
        "        obj = getattr(unreal, name)\n"
        "    except Exception:\n"
        "        continue\n"
        "    if isinstance(obj, type):\n"
        "        kind = 'class'\n"
        "    elif callable(obj):\n"
        "        kind = 'function'\n"
        "    else:\n"
        "        kind = 'value'\n"
        "    sig, doc = _doc_parts(getattr(obj, '__doc__', ''))\n"
        "    entry = {'n': name, 'p': '', 'k': kind, 's': sig, 'd': doc}\n"
        "    if isinstance(obj, type):\n"
        "        # Ancestor chain (unreal classes only): UE hierarchies are\n"
        "        # deep, and inherited members live on the defining class.\n"
        "        try:\n"
        "            bases = [c.__name__ for c in type.mro(obj)[1:]\n"
        "                     if c.__name__ in _unreal_names]\n"
        "        except Exception:\n"
        "            bases = []\n"
        "        if bases:\n"
        "            entry['b'] = bases\n"
        "    entries.append(entry)\n"
        "    if not isinstance(obj, type):\n"
        "        continue\n"
        "    # vars() yields only members defined on the class itself, so\n"
        "    # inherited unreal.Object members are not duplicated 3000 times.\n"
        "    try:\n"
        "        own = list(vars(obj).keys())\n"
        "    except TypeError:\n"
        "        own = []\n"
        "    for member in own:\n"
        "        if member.startswith('_'):\n"
        "            continue\n"
        "        try:\n"
        "            attr = getattr(obj, member)\n"
        "        except Exception:\n"
        "            continue\n"
        "        member_kind = 'method' if callable(attr) else 'property'\n"
        "        sig, doc = _doc_parts(getattr(attr, '__doc__', ''))\n"
        "        entries.append({'n': member, 'p': name, 'k': member_kind, 's': sig, 'd': doc})\n"
        "\n"
        "# Project layer: asset-registry walk for types defined in content, not\n"
        "# C++ - Blueprint classes never appear in the unreal module. Parent\n"
        "# classes come from registry tags, so no asset is loaded.\n"
        "def _short_cls(value):\n"
        "    s = str(value or '').strip()\n"
        "    if s.endswith(\"'\"):\n"
        "        s = s[:-1]\n"
        "    if \"'\" in s:\n"
        "        s = s.rsplit(\"'\", 1)[-1]\n"
        "    for sep in ('.', ':', '/'):\n"
        "        if sep in s:\n"
        "            s = s.rsplit(sep, 1)[-1]\n"
        "    return s\n"
        "\n"
        "_ar = unreal.AssetRegistryHelpers.get_asset_registry()\n"
        "_asset_kinds = [\n"
        "    ('/Script/Engine', 'Blueprint', 'blueprint', False),\n"
        "    ('/Script/UMGEditor', 'WidgetBlueprint', 'widget', False),\n"
        "    ('/Script/Engine', 'AnimBlueprint', 'animbp', False),\n"
        "    ('/Script/Engine', 'UserDefinedStruct', 'struct', False),\n"
        "    ('/Script/Engine', 'UserDefinedEnum', 'enum', False),\n"
        "    ('/Script/Engine', 'DataAsset', 'dataasset', True),\n"
        "]\n"
        "_seen_assets = set()\n"
        "for _mod, _cls, _kind, _recursive in _asset_kinds:\n"
        "    try:\n"
        "        _assets = _ar.get_assets(unreal.ARFilter(\n"
        "            class_paths=[unreal.TopLevelAssetPath(_mod, _cls)],\n"
        "            recursive_classes=_recursive,\n"
        "            recursive_paths=True,\n"
        "        ))\n"
        "    except Exception:\n"
        "        _assets = []\n"
        "    for _a in (_assets or []):\n"
        "        _pkg = str(_a.package_name)\n"
        "        # Engine/plugin-shipped content is not project API surface.\n"
        "        if _pkg.startswith('/Engine') or _pkg.startswith('/Script'):\n"
        "            continue\n"
        "        if _pkg in _seen_assets:\n"
        "            continue\n"
        "        _seen_assets.add(_pkg)\n"
        "        try:\n"
        "            _parent = _short_cls(\n"
        "                _a.get_tag_value('ParentClass')\n"
        "                or _a.get_tag_value('NativeParentClass')\n"
        "            )\n"
        "        except Exception:\n"
        "            _parent = ''\n"
        "        _desc = _kind + ' asset' + (', parent ' + _parent if _parent else '')\n"
        "        entries.append({'n': str(_a.asset_name), 'p': _parent,\n"
        "                        'k': _kind, 's': _pkg, 'd': _desc})\n"
        "\n"
        "data = {\n"
        "    'engine': str(unreal.SystemLibrary.get_engine_version()),\n"
        "    'harvested_at': time.time(),\n"
        "    'entries': entries,\n"
        "}\n"
        f'with open("{target}", "w", encoding="utf-8") as f:\n'
        "    json.dump(data, f, ensure_ascii=True)\n"
        "print('__MCP_RESULT__' + json.dumps({\n"
        "    'count': len(entries),\n"
        "    'project_count': len(_seen_assets),\n"
        "    'engine': data['engine'],\n"
        f"    'path': \"{target}\",\n"
        "}))"
    )


def build_asset_describe_script(asset_path: str, asset_name: str, kind: str) -> str:
    """Script that loads one project asset live and reports what stock UE
    Python reflection can see (asset class, parent class, generated class,
    Blueprint variable names). BP-defined function signatures are not
    reflected by UE's Python wrapper, so they are out of reach here."""
    path = escape_string(asset_path)
    name = escape_string(asset_name)
    lines = [
        "import unreal",
        f'asset = unreal.load_asset("{path}")',
        "if asset is None:",
        f'    raise ValueError("Asset not found: " + "{path}")',
        "info = {",
        f'    "asset_path": "{path}",',
        "    'asset_class': type(asset).__name__,",
        "}",
        "try:",
        "    parent = asset.get_editor_property('parent_class')",
        "    if parent is not None:",
        "        # Class references surface as Python types, not wrappers.",
        "        info['parent_class'] = (",
        "            parent.__name__ if isinstance(parent, type) else parent.get_name()",
        "        )",
        "except Exception:",
        "    pass",
    ]
    if kind in ("blueprint", "widget", "animbp"):
        lines += [
            f'gen = unreal.load_object(None, "{path}.{name}_C")',
            "if gen is not None:",
            "    info['generated_class'] = gen.__name__ if isinstance(gen, type) else gen.get_name()",
            "try:",
            "    variables = asset.get_editor_property('new_variables')",
            "    info['variables'] = ["
            "str(v.get_editor_property('var_name')) for v in variables][:80]",
            "except Exception:",
            "    pass",
        ]
    lines.append("print('__MCP_RESULT__' + json.dumps(info, default=str))")
    return wrap_script("\n".join(lines))


def _tokens(text: str) -> list[str]:
    spaced = _CAMEL_RE.sub(" ", text or "")
    return [t for t in _SPLIT_RE.split(spaced.lower()) if len(t) > 1]


def _bigrams(tokens: list[str]) -> list[str]:
    # 'SignUpViewModel' tokenizes to sign/up/view/model; bigrams add
    # signup/upview/viewmodel so compound queries like 'viewmodel' hit.
    return [a + b for a, b in zip(tokens, tokens[1:])]


def _entry_tokens(entry: dict[str, Any]) -> list[str]:
    name = entry.get("n", "")
    tokens: list[str] = []
    name_tokens = _tokens(name)
    tokens.extend(name_tokens * 3)          # name matches dominate
    tokens.extend(_bigrams(name_tokens) * 2)
    if name:
        tokens.append(name.lower())         # whole-name exact token
    parent_tokens = _tokens(entry.get("p", ""))
    tokens.extend(parent_tokens * 2)
    tokens.extend(_bigrams(parent_tokens))
    tokens.extend(_tokens(entry.get("s", "")) * 2)
    tokens.extend(_tokens(entry.get("d", "")))
    for base in entry.get("b", []) or []:
        tokens.extend(_tokens(base))    # ancestor names: subclasses match
    return tokens


class _Bm25Index:
    _K1 = 1.5
    _B = 0.75

    def __init__(self, token_docs: list[list[str]]):
        self._n = len(token_docs)
        self._doc_len = [len(doc) for doc in token_docs]
        self._avg_len = (sum(self._doc_len) / self._n) if self._n else 0.0
        self._postings: dict[str, list[tuple[int, int]]] = {}
        for doc_id, doc in enumerate(token_docs):
            for token, tf in Counter(doc).items():
                self._postings.setdefault(token, []).append((doc_id, tf))

    def search(self, query: str, limit: int) -> list[tuple[float, int]]:
        scores: dict[int, float] = {}
        for token in dict.fromkeys(_tokens(query) + [query.strip().lower()]):
            postings = self._postings.get(token)
            if not postings:
                continue
            df = len(postings)
            idf = math.log(1.0 + (self._n - df + 0.5) / (df + 0.5))
            for doc_id, tf in postings:
                length_norm = 1.0 - self._B + self._B * (
                    self._doc_len[doc_id] / self._avg_len if self._avg_len else 1.0
                )
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * (
                    tf * (self._K1 + 1.0) / (tf + self._K1 * length_norm)
                )
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return [(score, doc_id) for doc_id, score in ranked[:limit]]


def _load_indexed(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], _Bm25Index]:
    mtime = path.stat().st_mtime
    cache_key = (str(path), mtime)
    cached = _INDEX_CACHE.get(cache_key)
    if cached is None:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("entries") or []
        index = _Bm25Index([_entry_tokens(entry) for entry in entries])
        meta = {
            "engine": data.get("engine"),
            "harvested_at": data.get("harvested_at"),
            "entry_count": len(entries),
            "path": str(path),
        }
        _INDEX_CACHE.clear()  # one catalogue in memory at a time
        _INDEX_CACHE[cache_key] = (meta, entries, index)
        cached = _INDEX_CACHE[cache_key]
    return cached


def _qualified(entry: dict[str, Any]) -> str:
    name = entry.get("n", "")
    if entry.get("k") in PROJECT_ASSET_KINDS:
        # 'p' is the parent class here, not a containing class.
        return name
    parent = entry.get("p") or ""
    return f"{parent}.{name}" if parent else name


def _entry_payload(entry: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "symbol": _qualified(entry),
        "kind": entry.get("k"),
        "signature": entry.get("s") or "",
        "doc": entry.get("d") or "",
    }
    if entry.get("k") in PROJECT_ASSET_KINDS:
        payload["asset_path"] = entry.get("s") or ""
        payload["parent"] = entry.get("p") or ""
    return payload


# UE vocabulary differs from common phrasing; expanded only when BM25 finds
# nothing, so a loose synonym can surface results but never distort ranking.
_UE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "rotation": ("rotator",),
    "rotator": ("rotation",),
    "rotate": ("rotation", "rotator"),
    "position": ("location", "translation"),
    "location": ("position",),
    "translation": ("location",),
    "size": ("scale", "bounds", "extent"),
    "delete": ("destroy", "remove"),
    "remove": ("delete", "destroy"),
    "destroy": ("delete",),
    "create": ("spawn", "add", "new", "factory"),
    "spawn": ("create",),
    "duplicate": ("copy", "clone"),
    "copy": ("duplicate", "clone"),
    "opacity": ("alpha", "translucency"),
    "alpha": ("opacity",),
    "image": ("texture",),
    "picture": ("texture", "image"),
    "sound": ("audio", "sound wave"),
    "audio": ("sound",),
    "animation": ("anim", "montage", "sequence"),
    "anim": ("animation",),
    "joint": ("bone",),
    "folder": ("directory", "path"),
    "directory": ("folder", "path"),
    "hide": ("visibility", "visible"),
    "show": ("visibility", "visible"),
}

_COLLAPSE_RE = re.compile(r"[^a-z0-9]+")


def _expand_synonyms(query: str) -> str:
    extra: list[str] = []
    for token in _tokens(query):
        extra.extend(_UE_SYNONYMS.get(token, ()))
    return f"{query} {' '.join(extra)}" if extra else query


def _substring_pass(
    entries: list[dict[str, Any]], query: str, kind_filter: str, limit: int
) -> list[dict[str, Any]]:
    """Token-boundary-free matching: 'retargetercontrol' should still find
    IKRetargeterController even though no BM25 token equals it."""
    tokens = [t for t in _tokens(query) if len(t) >= 3]
    collapsed = _COLLAPSE_RE.sub("", query.lower())
    scored: list[tuple[int, int, int]] = []
    for i, entry in enumerate(entries):
        if kind_filter and entry.get("k") != kind_filter:
            continue
        name = _COLLAPSE_RE.sub("", _qualified(entry).lower())
        hits = sum(1 for t in tokens if t in name)
        if len(collapsed) >= 4 and collapsed in name:
            hits += 2
        if hits:
            scored.append((-hits, len(name), i))
    scored.sort()
    return [entries[i] for _, _, i in scored[:limit]]


def _fuzzy_pass(
    entries: list[dict[str, Any]], query: str, kind_filter: str, limit: int
) -> list[dict[str, Any]]:
    """Last resort: edit-distance match against entry names for typos."""
    import difflib

    collapsed = _COLLAPSE_RE.sub("", query.lower())
    if len(collapsed) < 4:
        return []
    by_name: dict[str, int] = {}
    for i, entry in enumerate(entries):
        if kind_filter and entry.get("k") != kind_filter:
            continue
        by_name.setdefault(_COLLAPSE_RE.sub("", _qualified(entry).lower()), i)
    close = difflib.get_close_matches(collapsed, list(by_name), n=limit, cutoff=0.75)
    return [entries[by_name[name]] for name in close]


_SEARCH_MISS_HINT = (
    "No catalogue match even after synonym, substring, and fuzzy passes. "
    "Before concluding the API does not exist: (1) try the UE term for the "
    "concept (e.g. 'rotator' not 'rotation', 'spawn' not 'create'); "
    "(2) if it belongs to a plugin enabled after the last harvest, rebuild "
    "with build_api_catalog(force=true); (3) as a last resort probe live: "
    "print([n for n in dir(unreal) if 'keyword' in n.lower()])."
)


def search_catalog(
    query: str,
    *,
    limit: int = 12,
    kind: str = "",
    path: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """Search the newest catalogue. Returns None when no catalogue exists.

    Recall ladder: BM25 first; on zero hits, retry with UE-domain synonyms,
    then substring matching, then fuzzy (typo) matching - `match_mode` in the
    response says which pass produced the matches."""
    target = path or latest_catalog_path()
    if target is None or not target.exists():
        return None
    meta, entries, index = _load_indexed(target)
    kind_filter = kind.strip().lower()

    def bm25(q: str) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        # With a kind filter, rank ALL scored docs: rare kinds (e.g. 37
        # animbps) never survive a fixed over-fetch window against common
        # tokens like 'anim' that thousands of native entries also carry.
        fetch = len(entries) if kind_filter else limit
        for score, doc_id in index.search(q, max(fetch, limit)):
            entry = entries[doc_id]
            if kind_filter and entry.get("k") != kind_filter:
                continue
            collected.append({**_entry_payload(entry), "score": round(score, 2)})
            if len(collected) >= limit:
                break
        return collected

    match_mode = "bm25"
    matches = bm25(query)
    if not matches:
        expanded = _expand_synonyms(query)
        if expanded != query:
            matches = bm25(expanded)
            match_mode = "synonyms"
    if not matches:
        matches = [_entry_payload(e) for e in _substring_pass(entries, query, kind_filter, limit)]
        match_mode = "substring"
    if not matches:
        matches = [_entry_payload(e) for e in _fuzzy_pass(entries, query, kind_filter, limit)]
        match_mode = "fuzzy"
    result: dict[str, Any] = {"catalog": meta, "matches": matches, "match_mode": match_mode}
    if not matches:
        result["match_mode"] = "none"
        result["hint"] = _SEARCH_MISS_HINT
    return result


def describe_from_catalog(
    symbol: str,
    path: Optional[Path] = None,
    *,
    include_inherited: bool = False,
) -> Optional[dict[str, Any]]:
    """Return catalogue entries for a (possibly dotted) symbol name.

    For class symbols the response carries the ancestor chain; with
    `include_inherited` it also maps each ancestor to the member names it
    contributes (names only - describe Base.member for full signatures)."""
    target = path or latest_catalog_path()
    if target is None or not target.exists():
        return None
    meta, entries, _index = _load_indexed(target)
    wanted = symbol.strip()
    # Drivers see Blueprint classes as B_Foo_C at runtime; the catalogue
    # stores the asset name B_Foo - accept both spellings.
    names = {wanted}
    if wanted.endswith("_C") and len(wanted) > 2:
        names.add(wanted[:-2])
    found = []
    ancestors: list[str] = []
    for entry in entries:
        is_project = entry.get("k") in PROJECT_ASSET_KINDS
        # For project entries 'p' is a parent class: matching it would dump
        # every Blueprint derived from a class into that class's describe.
        if (
            _qualified(entry) in names
            or entry.get("n") in names
            or (not is_project and entry.get("p") in names)
        ):
            found.append(_entry_payload(entry))
            if entry.get("k") == "class" and entry.get("n") in names:
                ancestors = list(entry.get("b") or [])
    # Project entries first: they must survive the cap even when the asset
    # name collides with a member name defined on 60+ native classes.
    found.sort(key=lambda e: 0 if e.get("asset_path") else 1)
    described: dict[str, Any] = {"catalog": meta, "entries": found[:60]}
    if ancestors:
        described["ancestors"] = ancestors
        if include_inherited:
            chain = set(ancestors)
            inherited: dict[str, list[str]] = {}
            for entry in entries:
                parent = entry.get("p")
                if parent in chain and entry.get("k") in ("method", "property"):
                    inherited.setdefault(parent, []).append(entry.get("n", ""))
            # Nearest ancestor first, mirroring resolution order.
            described["inherited"] = {
                base: sorted(inherited[base])
                for base in ancestors
                if base in inherited
            }
    return described


def save_catalog_marker(engine_version: str, harvested_at: Optional[float] = None) -> Path:
    """Used by tests: write a minimal valid catalogue for an engine version."""
    path = catalog_path_for_version(engine_version)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "engine": engine_version,
        "harvested_at": harvested_at or time.time(),
        "entries": [],
    }), encoding="utf-8")
    return path
