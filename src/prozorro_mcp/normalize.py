"""Regions and CPV (ДК 021) from whatever the user typed: Ukrainian, Russian, English, cities, typos."""
import difflib
import json
import re
from functools import lru_cache
from importlib import resources


class UnknownInput(ValueError):
    def __init__(self, message: str, suggestions: list[str] | None = None):
        super().__init__(message)
        self.suggestions = suggestions or []


def _load(name: str):
    return json.loads(resources.files("prozorro_mcp").joinpath("data", name).read_text(encoding="utf-8"))


ALL_REGIONS: list[str] = _load("ua_regions.json")

KYIV_CITY, KYIV_OBLAST = "м. Київ", "Київська область"
# tenders whose buyer address is hidden (mostly military units) have no region in Prozorro
NO_REGION = "Без регіону"
_NO_REGION_WORDS = {"без регіону", "без региона", "none", "no region", "hidden", "без адреси"}

# stem (start of a normalized word) -> Prozorro region name; Ukrainian, Russian, English, main city
_STEMS = {
    "Автономна Республіка Крим": ["крим", "крым", "crimea", "krym"],
    "Вінницька область": ["вінниц", "винниц", "vinnyts", "vinnits"],
    "Волинська область": ["волин", "волын", "volyn", "луцьк", "луцк", "lutsk"],
    "Дніпропетровська область": ["дніпр", "днепр", "dnipr", "dnepr"],
    "Донецька область": ["донець", "донецк", "donets"],
    "Житомирська область": ["житомир", "zhytomyr", "zhitomir"],
    "Закарпатська область": ["закарпат", "zakarpat", "ужгород", "uzhhorod", "uzhgorod"],
    "Запорізька область": ["запоріз", "запоріж", "запорож", "zaporiz"],
    "Івано-Франківська область": ["івано", "ивано", "ivano"],
    "Кіровоградська область": ["кіровоград", "кировоград", "kirovohrad", "кропивницьк", "кропивницк",
                               "kropyvnytsk"],
    "Луганська область": ["луган", "luhansk", "lugansk"],
    "Львівська область": ["львів", "львов", "lviv", "lvov"],
    "Миколаївська область": ["микола", "никола", "mykolai", "nikola"],
    "Одеська область": ["одес", "odes"],
    "Полтавська область": ["полтав", "poltav"],
    "Рівненська область": ["рівн", "ровн", "rivn"],
    "Сумська область": ["сумськ", "сумск", "суми", "сумы", "sumy", "sumsk"],
    "Тернопільська область": ["терноп", "ternop"],
    "Харківська область": ["харків", "харьков", "kharkiv", "kharkov"],
    "Херсонська область": ["херсон", "kherson"],
    "Хмельницька область": ["хмельниц", "khmelnyts"],
    "Черкаська область": ["черкас", "cherkas"],
    "Чернівецька область": ["чернівц", "черновц", "chernivts"],
    "Чернігівська область": ["черніг", "черниг", "chernih"],
    "м. Севастополь": ["севастоп", "sevastop"],
}
_KYIV_STEMS = ["київ", "киев", "kyiv", "kiev"]
_OBLAST_MARKERS = re.compile(r"област|обл\b|oblast|region|київськ|киевск")
_NOISE = re.compile(r"\b(область|обл|oblast|region|місто|город|city|м|г)\b\.?")

_STEM_INDEX = sorted(((stem, region) for region, stems in _STEMS.items() for stem in stems),
                     key=lambda p: -len(p[0]))


def _clean(text: str) -> str:
    text = text.casefold().replace("’", "'").replace("ʼ", "'").replace("`", "'")
    return text.strip()


def resolve_region(text: str) -> str:
    raw = _clean(text)
    if not raw:
        raise UnknownInput("Region is empty.", ALL_REGIONS)
    if raw in _NO_REGION_WORDS:
        return NO_REGION
    for region in ALL_REGIONS:
        if raw == region.casefold():
            return region
    is_oblast = bool(_OBLAST_MARKERS.search(raw))
    core = _NOISE.sub(" ", raw).replace(".", " ").strip()
    if any(core.startswith(s) for s in _KYIV_STEMS):
        return KYIV_OBLAST if is_oblast else KYIV_CITY
    for stem, region in _STEM_INDEX:
        if core.startswith(stem):
            return region
    # typos: compare the first word with every stem and city name
    word = core.split()[0] if core.split() else core
    candidates = {stem: region for stem, region in _STEM_INDEX}
    candidates.update({s: KYIV_CITY for s in _KYIV_STEMS})
    close = difflib.get_close_matches(word[: max(len(s) for s in candidates)], list(candidates), n=1,
                                      cutoff=0.75)
    if not close:
        close = difflib.get_close_matches(word[:6], list(candidates), n=1, cutoff=0.75)
    if close:
        return candidates[close[0]]
    loose = difflib.get_close_matches(word, list(candidates), n=3, cutoff=0.5)
    suggestions = list(dict.fromkeys(candidates[c] for c in loose))
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise UnknownInput(f"Unknown region '{text}'.{hint}", suggestions)


@lru_cache(maxsize=1)
def _dk021() -> dict[str, str]:
    return _load("dk021_uk.json")


def _prefix(code: str) -> str:
    digits = code.split("-")[0]
    stripped = digits.rstrip("0")
    return stripped if len(stripped) >= 2 else digits[:2]


def cpv_name(code: str) -> str | None:
    return _dk021().get(code)


def resolve_cpv(text: str) -> list[str]:
    """A code, a code prefix, or words. Returns CPV prefixes for LIKE 'prefix%' matching."""
    raw = _clean(text)
    if re.fullmatch(r"\d{2,8}(-\d)?", raw):
        return [_prefix(raw) if len(raw.split("-")[0]) == 8 else raw]
    words = re.findall(r"[\w'-]+", raw)
    if not words:
        raise UnknownInput(f"Unknown CPV '{text}'. Give a ДК 021 code (e.g. 15 or 09132000-3) or words.")
    stems = [w[:-2] if len(w) > 6 else w for w in words]
    matched = [code for code, name in _dk021().items() if all(s in _clean(name) for s in stems)]
    if not matched:
        raise UnknownInput(f"No ДК 021 category matches '{text}'. Try another word or a code prefix.")
    # names that start with the query rank first ("Бензин" before "Вазелін ... бензин"), then general first
    best: dict[str, int] = {}
    for code in matched:
        rank = 0 if _clean(_dk021()[code]).startswith(stems[0]) else 1
        best[_prefix(code)] = min(rank, best.get(_prefix(code), 1))
    if 0 in best.values():
        named = [p for p, r in best.items() if r == 0]
        keep = set(named)
        exact = any(_clean(_dk021()[c]) == raw for c in matched)
        if not exact:
            # buyers often classify at the parent level ("Продукція борошномельно-..." for flour): add the
            # nearest ancestor that mentions the word, never a wider one
            for q in named:
                parents = [p for p in best if p != q and q.startswith(p)]
                if parents:
                    keep.add(max(parents, key=len))
        best = {p: 0 for p in keep}
    chosen: list[str] = []
    for p in sorted(best, key=lambda p: (best[p], len(p), p)):
        if not any(p.startswith(c) for c in chosen):
            chosen.append(p)
    return chosen[:10]


def prefix_name(prefix: str) -> str | None:
    """Name of the ДК 021 category a prefix stands for (the code whose digits are the prefix + zeros)."""
    for code, name in _dk021().items():
        if _prefix(code) == prefix:
            return name
    return None
