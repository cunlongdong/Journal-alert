"""Keyword scoring: transparent, explainable relevance ranking."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TITLE_MULTIPLIER = 3
ABSTRACT_MULTIPLIER = 1


def _pattern(term: str) -> re.Pattern:
    """Build a case-insensitive, alphanumeric-boundary matcher for ``term``."""
    # Journal APIs often use assorted hyphens (U+2010/U+2011/U+2013) or
    # spaces for the same scientific compound: "spin–orbit torque",
    # "spin-orbit-torque" and "spin orbit torque" must match equally.
    parts = re.split(r"[\s\-\u2010-\u2015\u2212]+", term.strip())
    pieces = [re.escape(p) for p in parts if p]
    escaped = r"[\s\-\u2010-\u2015\u2212]+".join(pieces)
    return re.compile(r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])", re.IGNORECASE)


@dataclass
class Match:
    label: str
    term: str
    field_name: str
    points: int


@dataclass
class Scored:
    score: int = 0
    matches: list[Match] = field(default_factory=list)
    excluded: bool = False
    exclude_term: str = ""

    @property
    def labels(self) -> list[str]:
        seen: list[str] = []
        for match in self.matches:
            if match.label not in seen:
                seen.append(match.label)
        return seen

    @property
    def title_hits(self) -> list[str]:
        return [m.label for m in self.matches if m.field_name == "title"]


class Scorer:
    def __init__(self, keywords: list[dict], exclude_terms: list[str], exclude_doi_prefixes: list[str]):
        self.groups = []
        for entry in keywords:
            label = entry.get("label") or entry.get("term") or "keyword"
            weight = int(entry.get("weight", 3))
            terms = entry.get("terms") or ([entry["term"]] if entry.get("term") else [])
            compiled = [(t, _pattern(t)) for t in terms if t and t.strip()]
            if compiled:
                self.groups.append((label, weight, compiled))
        self.excludes = [(t, _pattern(t)) for t in exclude_terms if t and t.strip()]
        self.exclude_doi_prefixes = [p.lower() for p in exclude_doi_prefixes if p]

    def score(self, item) -> Scored:
        title = item.title or ""
        abstract = item.abstract or ""
        result = Scored()

        for prefix in self.exclude_doi_prefixes:
            if (item.doi or "").lower().startswith(prefix):
                result.excluded = True
                result.exclude_term = f"doi:{prefix}"
                return result

        haystack = f"{title} {abstract}"
        for term, pattern in self.excludes:
            if pattern.search(haystack):
                result.excluded = True
                result.exclude_term = term
                return result

        for label, weight, terms in self.groups:
            hit = None
            for term, pattern in terms:
                if pattern.search(title):
                    hit = (term, "title", weight * TITLE_MULTIPLIER)
                    break
            if hit is None:
                for term, pattern in terms:
                    if pattern.search(abstract):
                        hit = (term, "abstract", weight * ABSTRACT_MULTIPLIER)
                        break
            if hit is not None:
                term, field_name, points = hit
                result.matches.append(Match(label=label, term=term, field_name=field_name, points=points))
                result.score += points
        return result


def tier_of(score: int, tiers: dict) -> str:
    if score >= int(tiers.get("must_read", 12)):
        return "must_read"
    if score >= int(tiers.get("worth_reading", 6)):
        return "worth_reading"
    return "other"


TIER_TITLES = {
    "must_read": "必读",
    "worth_reading": "值得一读",
    "other": "其他相关",
}
