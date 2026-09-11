import re


_TITLE_LEVEL_PATTERNS = (
    ("staff", re.compile(r"\bstaff\b", re.IGNORECASE)),
    ("senior", re.compile(r"\b(?:senior|sr\.?)\b", re.IGNORECASE)),
    ("intern", re.compile(r"\b(?:intern|internship|co-?op)\b", re.IGNORECASE)),
    ("new_grad", re.compile(r"\b(?:new grad(?:uate)?|recent grad(?:uate)?)\b", re.IGNORECASE)),
    ("early_career", re.compile(r"\bearly career\b", re.IGNORECASE)),
    ("entry", re.compile(r"\b(?:entry(?: level)?|junior|jr\.?|associate)\b", re.IGNORECASE)),
    ("mid", re.compile(r"\b(?:mid(?:-?level)?|intermediate)\b", re.IGNORECASE)),
)
_YEARS_RE = re.compile(
    r"\b(\d{1,2})\s*(?:\+|plus)?\s*"
    r"(?:(?:-|–|—|to)\s*(\d{1,2})\s*)?"
    r"(?:years?|yrs?)\b",
    re.IGNORECASE,
)
_EXPERIENCE_CONTEXT_RE = re.compile(
    r"experience|experienced|required|requirement|minimum|at least|professional|industry",
    re.IGNORECASE,
)


def early_career_rejection_reason(
    title: str,
    description: str = "",
    max_required_years: int = 4,
    allowed_levels: list[str] | tuple[str, ...] | set[str] | None = None,
) -> str | None:
    selected_levels = set(allowed_levels or ("intern", "new_grad", "early_career", "entry", "mid"))
    if "junior" in selected_levels:
        selected_levels.update(("new_grad", "early_career", "entry"))

    for level, pattern in _TITLE_LEVEL_PATTERNS:
        if pattern.search(title) and level not in selected_levels:
            return f"{level.replace('_', ' ')} level not selected"

    searchable_text = f"{title}\n{description}"
    for match in _YEARS_RE.finditer(searchable_text):
        context_start = max(0, match.start() - 80)
        context_end = min(len(searchable_text), match.end() + 80)
        context = searchable_text[context_start:context_end]
        if not _EXPERIENCE_CONTEXT_RE.search(context):
            continue
        required_years = max(int(value) for value in match.groups() if value)
        if required_years > max_required_years:
            return f"requires {required_years} years (maximum {max_required_years})"

    return None
