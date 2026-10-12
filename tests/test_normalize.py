import pytest

from prozorro_mcp.normalize import ALL_REGIONS, UnknownInput, cpv_name, resolve_cpv, resolve_region

SPELLINGS = [
    ("Львівська область", "Львівська область"),
    ("Львівська", "Львівська область"),
    ("львівська обл.", "Львівська область"),
    ("Львів", "Львівська область"),
    ("львовская", "Львівська область"),
    ("Lviv", "Львівська область"),
    ("Lviv oblast", "Львівська область"),
    ("лвів", "Львівська область"),
    ("Київ", "м. Київ"),
    ("м. Київ", "м. Київ"),
    ("Kyiv", "м. Київ"),
    ("Kyiv city", "м. Київ"),
    ("киев", "м. Київ"),
    ("Київська", "Київська область"),
    ("Kyiv region", "Київська область"),
    ("Kyiv oblast", "Київська область"),
    ("киевская область", "Київська область"),
    ("Дніпропетровська", "Дніпропетровська область"),
    ("Днепр", "Дніпропетровська область"),
    ("Dnipro", "Дніпропетровська область"),
    ("Одеса", "Одеська область"),
    ("одесская", "Одеська область"),
    ("Odesa", "Одеська область"),
    ("Харків", "Харківська область"),
    ("Kharkiv", "Харківська область"),
    ("Івано-Франківськ", "Івано-Франківська область"),
    ("ivano-frankivsk", "Івано-Франківська область"),
    ("Запоріжжя", "Запорізька область"),
    ("Крим", "Автономна Республіка Крим"),
    ("Crimea", "Автономна Республіка Крим"),
    ("Севастополь", "м. Севастополь"),
    ("Хмельницький", "Хмельницька область"),
    ("Черкаси", "Черкаська область"),
    ("Чернігів", "Чернігівська область"),
    ("Чернівці", "Чернівецька область"),
    ("Ужгород", "Закарпатська область"),
    ("Луцьк", "Волинська область"),
    ("Рівне", "Рівненська область"),
    ("Тернопіль", "Тернопільська область"),
    ("Вінниця", "Вінницька область"),
    ("Житомир", "Житомирська область"),
    ("Кропивницький", "Кіровоградська область"),
    ("Полтава", "Полтавська область"),
    ("Суми", "Сумська область"),
    ("Миколаїв", "Миколаївська область"),
    ("Херсон", "Херсонська область"),
    ("Донецька", "Донецька область"),
    ("Луганськ", "Луганська область"),
]


@pytest.mark.parametrize("text,expected", SPELLINGS)
def test_region_spellings(text, expected):
    assert resolve_region(text) == expected


def test_all_regions_are_the_prozorro_list():
    assert len(ALL_REGIONS) == 27 and "м. Київ" in ALL_REGIONS


def test_unknown_region_suggests():
    with pytest.raises(UnknownInput) as exc:
        resolve_region("Атлантида")
    assert "Атлантида" in str(exc.value)


def test_typo_in_a_long_name_still_resolves():
    assert resolve_region("Закарпатьська облазть") == "Закарпатська область"


def test_cpv_code_prefix_passes_through():
    assert resolve_cpv("15") == ["15"]
    assert resolve_cpv("331") == ["331"]


def test_full_cpv_code_becomes_its_prefix():
    assert resolve_cpv("15610000-7") == ["1561"]
    assert resolve_cpv("15612100") == ["156121"]


def test_cpv_words_find_code_families_most_general_first():
    assert resolve_cpv("реактиви")[0] == "33696"
    assert resolve_cpv("бензин")[0] == "09132"
    assert "1561" in resolve_cpv("борошно") or "156" in resolve_cpv("борошно")


def test_cpv_words_collapse_children_under_parent():
    codes = resolve_cpv("бензин")
    assert not any(c != "09132" and c.startswith("09132") for c in codes)


def test_cpv_words_apostrophe_variants():
    assert resolve_cpv("борошномельно-круп'яної") == resolve_cpv("борошномельно-круп’яної")


def test_cpv_unknown_word_raises():
    with pytest.raises(UnknownInput):
        resolve_cpv("ноутбукиииии")


def test_cpv_name():
    assert cpv_name("09132000-3") == "Бензин"
    assert cpv_name("00000000-0") is None
