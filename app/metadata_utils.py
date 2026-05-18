import re
from datetime import date


def extract_year_from_text(text: str) -> int | None:
    if not text:
        return None

    match = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    if match:
        return int(match.group(1))

    return None


def extract_date_from_filename(filename: str) -> tuple[date | None, str | None]:
    """
    Returns:
    (detected_date, date_source)
    """

    if not filename:
        return None, None

    name = filename.lower()

    # Example: IMG_20151224_1930.jpg
    match = re.search(r"(20\d{2})([01]\d)([0-3]\d)", name)
    if match:
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3))

        try:
            return date(year, month, day), "filename"
        except ValueError:
            pass

    # Example: 2015-12-24
    match = re.search(r"(20\d{2})[-_\.]([01]\d)[-_\.]([0-3]\d)", name)
    if match:
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3))

        try:
            return date(year, month, day), "filename"
        except ValueError:
            pass

    # Example: christmas_2015.jpg
    match = re.search(r"\b(19\d{2}|20\d{2})\b", name)
    if match:
        year = int(match.group(1))
        return date(year, 1, 1), "filename"

    return None, None


def detect_event(text: str) -> str | None:
    if not text:
        return None

    value = text.lower()

    event_keywords = {
        "christmas": [
            "christmas",
            "xmas",
            "christmas eve",
            "christmas day"
        ],
        "new_year": [
            "new year",
            "nye",
            "new year's eve"
        ],
        "birthday": [
            "birthday"
        ],
        "charter": [
            "charter"
        ],
        "maintenance": [
            "maintenance",
            "repair",
            "service",
            "engine",
            "refit"
        ],
        "party": [
            "party",
            "event",
            "celebration"
        ],
        "dinner": [
            "dinner",
            "menu",
            "table setting"
        ]
    }

    for event, keywords in event_keywords.items():
        for keyword in keywords:
            if keyword in value:
                return event

    return None


def generate_basic_tags(text: str) -> list[str]:
    if not text:
        return []

    value = text.lower()

    keywords = [
        "christmas",
        "xmas",
        "decoration",
        "decorations",
        "tree",
        "garland",
        "garlands",
        "candles",
        "salon",
        "deck",
        "interior",
        "exterior",
        "dinner",
        "menu",
        "invoice",
        "maintenance",
        "engine",
        "crew",
        "guest",
        "charter",
        "new year",
        "birthday",
        "party",
        "table",
        "flowers",
        "lights",
        "ornaments"
    ]

    tags = []

    for keyword in keywords:
        if keyword in value:
            tags.append(keyword)

    return sorted(list(set(tags)))


def extract_query_filters(query: str) -> dict:
    filters = {}

    if not query:
        return filters

    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", query)
    if year_match:
        filters["year"] = int(year_match.group(1))

    lowered = query.lower()

    if "christmas" in lowered or "xmas" in lowered:
        filters["event"] = "christmas"

    if "new year" in lowered or "nye" in lowered:
        filters["event"] = "new_year"

    if "decoration" in lowered or "decorations" in lowered:
        filters["topic"] = "decoration"

    return filters