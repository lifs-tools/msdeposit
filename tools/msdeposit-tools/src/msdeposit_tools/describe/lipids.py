"""lipid-annotate: Goslin normalisation and SwissLipids identifiers for the feature names of a study.

Every distinct feature `name` of study.json is annotated. Names of the form 'A/B' where B starts with a class
(e.g. 'PC O-34:2/PC P-34:1') are isomer alternatives and give one row per alternative. '1,2-DG' / '1,3-DG' names are
rewritten for parsing; the regio information is kept in `note`. Names Goslin cannot parse (non-lipids) keep empty
Goslin fields and a note.

Modes: `none` (header only), `goslin` (name, level, sum formula, neutral mass), `goslin+swisslipids` (+ SwissLipids
id by exact name match at the reported level, never a guessed structure). SwissLipids is queried only with
`--allow-network true`; otherwise only the `--cache` TSV (`query`, `swisslipids_id`, `swisslipids_level`; an empty id
records "looked up, not found") is used. New lookups are added to the cache.

Output (CONTRACTS.md §6): lipid_annotation.tsv with `FIELDS`.
"""

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from ..io import InputError, as_bool, read_json, read_table, write_table

FIELDS = ["name", "alternative", "goslin_name", "goslin_level", "formula", "neutral_mass", "swisslipids_name",
          "swisslipids_id", "swisslipids_level", "note"]
CACHE_FIELDS = ["query", "swisslipids_id", "swisslipids_level"]
MODES = ("none", "goslin", "goslin+swisslipids")
SWISSLIPIDS_SEARCH = "https://www.swisslipids.org/api/index.php/search?term={}&type=lipid"
SWISSLIPIDS_ENTITY = "https://www.swisslipids.org/#/entity/{}"
_parser = None


def add_arguments(p):
    p.add_argument("--study-json", required=True, type=Path)
    p.add_argument("--mode", choices=MODES, default="goslin")
    p.add_argument("--cache", type=Path, help="SwissLipids cache TSV (read, and updated after network lookups)")
    p.add_argument("--allow-network", default="false", help="query the SwissLipids API for names not in the cache")
    p.add_argument("--out", type=Path, default=Path("lipid_annotation.tsv"))


def alternatives(name: str) -> list[str]:
    """'PC O-34:2/PC P-34:1' -> both; 'Cer 18:1;O2/24:0' stays one name (the '/' is followed by a digit)."""
    return [p.strip() for p in re.split(r"/(?=[A-Z])", name) if p.strip()]


def regio(name: str) -> tuple[str, str]:
    """'1,2-DG 34:1' -> ('DG 34:1', note); Goslin has no notation for the glycerol regio isomers."""
    m = re.match(r"(1,[23])-(DG .*)$", name)
    return (m[2], f"{m[1]}-diacylglycerol (regio isomer as reported)") if m else (name, "")


def swisslipids_name(name: str) -> str:
    """Goslin shorthand -> SwissLipids name: 'Cer 18:1;O2/24:0' -> 'Cer(d18:1/24:0)', 'PC O-34:2' -> 'PC(O-34:2)'."""
    cls, _, chains = name.partition(" ")
    chains = re.sub(r"(\d+:\d+);O2", r"d\1", chains)
    chains = re.sub(r"(\d+:\d+);O3", r"t\1", chains)
    return f"{cls}({chains})" if chains else cls


def parser():
    global _parser
    if _parser is None:
        from pygoslin.parser.Parser import LipidParser
        _parser = LipidParser()
    return _parser


def goslin(alternative: str) -> dict:
    """Goslin fields of one alternative (empty with a note when the name is not a parsable lipid)."""
    parsable, note = regio(alternative)
    try:
        lip = parser().parse(parsable)
        return dict(goslin_name=lip.get_lipid_string(), goslin_level=lip.lipid.info.level.name,
                    formula=lip.get_sum_formula(), neutral_mass=f"{lip.get_mass():.4f}", note=note)
    except Exception as e:
        return dict(goslin_name="", goslin_level="", formula="", neutral_mass="",
                    note="; ".join(x for x in (note, f"Goslin could not parse ({e.__class__.__name__})") if x))


def lookup_swisslipids(query: str, attempts: int = 3) -> tuple[str, str] | None:
    """Exact-name search; (id, level), ('', '') when not found, None when the service could not be reached."""
    url = SWISSLIPIDS_SEARCH.format(urllib.parse.quote(query))
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                hits = json.loads(r.read() or b"[]")
            time.sleep(0.2)   # be gentle with the public API
            break
        except Exception:
            time.sleep(2 * (attempt + 1))
    else:
        return None
    for h in hits if isinstance(hits, list) else []:
        if h.get("entity_name") == query:
            return h.get("entity_id", ""), h.get("classification_level", "")
    return "", ""


def read_cache(path: Path | None) -> dict[str, tuple[str, str]]:
    if not path or not path.exists():
        return {}
    return {r["query"]: (r.get("swisslipids_id", ""), r.get("swisslipids_level", "")) for r in read_table(path, "\t")
            if r.get("query")}


def annotate(names: list[str], mode: str, cache: dict[str, tuple[str, str]], allow_network: bool,
             lookup=None) -> tuple[list[dict], int]:
    """Rows for all names (in the given order); `cache` is updated in place. Returns (rows, new lookups)."""
    if mode == "none":
        return [], 0
    lookup = lookup or lookup_swisslipids
    rows, looked_up = [], 0
    for name in dict.fromkeys(n for n in names if n):
        for alt in alternatives(name):
            row = dict.fromkeys(FIELDS, "") | dict(name=name, alternative=alt) | goslin(alt)
            if mode == "goslin+swisslipids":
                query = swisslipids_name(row["goslin_name"] or regio(alt)[0])
                hit = cache.get(query)
                if hit is None and allow_network:
                    hit = lookup(query)
                    looked_up += 1
                    if hit is not None:
                        cache[query] = hit
                if hit is None:
                    status = "SwissLipids lookup failed" if allow_network else \
                        "SwissLipids not looked up (not in cache, network not allowed)"
                    row["note"] = "; ".join(x for x in (row["note"], status) if x)
                elif hit[0]:
                    row.update(swisslipids_name=query, swisslipids_id=hit[0], swisslipids_level=hit[1])
            rows.append(row)
    return rows, looked_up


def load_annotations(path: Path | None) -> dict[str, list[dict]]:
    """lipid_annotation.tsv -> {name: [alternative rows]} (empty without a file)."""
    out: dict[str, list[dict]] = {}
    if path:
        for r in read_table(path, "\t"):
            out.setdefault(r["name"], []).append(r)
    return out


def run(args) -> int:
    study = read_json(args.study_json, "msdeposit/study/1")
    allow_network = as_bool(args.allow_network)
    if args.mode != "goslin+swisslipids" and allow_network:
        print("note: --allow-network has no effect without --mode goslin+swisslipids")
    try:
        cache = read_cache(args.cache)
    except KeyError as e:
        raise InputError(f"SwissLipids cache: missing column {e}") from None
    names = [f.get("name") or "" for f in study.get("features", [])]
    n_cached = len(cache)
    rows, looked_up = annotate(names, args.mode, cache, allow_network)
    write_table(args.out, rows, FIELDS)
    if args.cache and len(cache) != n_cached:
        write_table(args.cache, [dict(query=q, swisslipids_id=i, swisslipids_level=lv)
                                 for q, (i, lv) in sorted(cache.items())], CACHE_FIELDS)
    print(f"{len(set(r['name'] for r in rows))} names, {len(rows)} alternatives: Goslin parsed "
          f"{sum(1 for r in rows if r['formula'])}, SwissLipids ids {sum(1 for r in rows if r['swisslipids_id'])}, "
          f"{looked_up} network lookups")
    return 0
