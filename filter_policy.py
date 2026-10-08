"""Versioned job-filter policy: rule semantics and evaluation.

A rule is: scope (title|description) -> pattern -> effect -> reason, evaluated with
a policy version so old decisions can be reconsidered after a rule change.

Effects, strongest first:
  hard_reject   narrow, unambiguous categories (non-jobs, clearly off-field titles)
  score_cap     clearly disqualifying title context; the score is capped
  soft_penalty  weak mismatch evidence shown to the scorer as a signal
  review        ambiguous term shown to the scorer to weigh in context
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from title_rules import TITLE_PENALTY_MAX_SCORE, contains_keyword

POLICY_VERSION = "2026-10-07.1"

HARD, CAP, SOFT, REVIEW, NONE = "hard_reject", "score_cap", "soft_penalty", "review", "none"
_STRENGTH = {NONE: 0, REVIEW: 1, SOFT: 2, CAP: 3, HARD: 4}

# Non-jobs and clearly off-field titles: the only absolute rejects.
DEFAULT_HARD_TITLE = (
    "ausbildung", "ausbild", "praktikant", "praktikum", "werkstudent", "masterarbeit",
    "master thesis", "student", "intern", "sap", "java", ".net", "fullstack", "backend",
    "android", "data scientist",
    # spelling variants of the non-job class (whole-word matching misses these otherwise)
    "pflichtpraktikum", "pflichtpraktikant", "praktikantin", "internship", "bachelorarbeit",
    "abschlussarbeit", "werkstudentin", "working student",
)
# Ambiguous title words: they reach the scorer with a note instead of rejecting.
DEFAULT_REVIEW_TITLE = ("lead", "expert", "specialist", "spezialist", "quality", "manager", "it", "ai", "ki")
# Tokens that are also ordinary words or abbreviations: only count when written exactly
# in uppercase (so the pronoun "it" never matches "IT").
CASE_SENSITIVE_TOKENS = ("it", "ai", "ki", "ml", "ui", "qa")

# Page furniture that must not feed keyword matching (cut the text at the first marker).
BOILERPLATE_MARKERS = (
    "Show more Show less", "Similar jobs", "Ähnliche Jobs", "Weitere Jobs", "Report this job",
    "Visit employer website", "Create search alert",
)


@dataclass(frozen=True)
class Rule:
    scope: str
    pattern: str
    effect: str
    reason: str
    match_case: bool = False


@dataclass
class Decision:
    effect: str = NONE
    rule: str = ""
    reason: str = ""
    cap: Optional[int] = None
    signals: List[str] = field(default_factory=list)


def strip_boilerplate(text: str) -> str:
    """Drop trailing page furniture (related jobs, share/save widgets) from a description."""
    text = text or ""
    cut = len(text)
    for marker in BOILERPLATE_MARKERS:
        idx = text.find(marker)
        if 200 < idx < cut:
            cut = idx
    return text[:cut]


def _clean(values: Optional[Iterable[str]]) -> List[str]:
    return [v.strip() for v in (values or []) if isinstance(v, str) and v.strip()]


class Policy:
    def __init__(self, rules: Sequence[Rule], version: str = POLICY_VERSION):
        self.rules = list(rules)
        self.version = version

    @classmethod
    def from_matching_rules(cls, matching_rules: Optional[dict]) -> "Policy":
        mr = matching_rules or {}
        override = mr.get("filter_policy") or {}
        hard = {k.lower() for k in _clean(override.get("hard_title", DEFAULT_HARD_TITLE))}
        review = {k.lower() for k in _clean(override.get("review_title", DEFAULT_REVIEW_TITLE))}
        rules: List[Rule] = []
        listed = {k.lower() for k in _clean(mr.get("negative_title_keywords"))}
        # The hard class applies to every profile, even when its keyword list lacks a variant
        # (e.g. "Praktikum", "Internship"): a non-job must never reach the scorer as a match.
        for kw in sorted(hard):
            if kw not in listed:
                rules.append(Rule("title", kw, HARD, f"non-job or off-field title: {kw}", kw in CASE_SENSITIVE_TOKENS))
        for kw in _clean(mr.get("negative_title_keywords")):
            low = kw.lower()
            case = low in CASE_SENSITIVE_TOKENS
            pattern = kw.upper() if case else kw
            if low in hard:
                rules.append(Rule("title", pattern, HARD, f"non-job or off-field title: {kw}", case))
            elif low in review:
                rules.append(Rule("title", pattern, REVIEW, f"ambiguous title word: {kw}", case))
            else:
                rules.append(Rule("title", pattern, CAP, f"disqualifying title word: {kw}", case))
        for kw in _clean(mr.get("negative_description_keywords")):
            low = kw.lower()
            case = low in CASE_SENSITIVE_TOKENS
            pattern = kw.upper() if case else kw
            effect = REVIEW if case else SOFT
            rules.append(Rule("description", pattern, effect, f"description keyword: {kw}", case))
        return cls(rules)

    def evaluate(self, job: Dict) -> Decision:
        title = job.get("title") or ""
        description = strip_boilerplate(job.get("description") or "")
        decision = Decision()
        for rule in self.rules:
            text = title if rule.scope == "title" else description
            if not contains_keyword(text, rule.pattern, rule.match_case):
                continue
            if rule.effect == HARD:
                return Decision(HARD, f"title:{rule.pattern}", rule.reason, None, [rule.reason])
            if rule.effect == CAP:
                decision.cap = TITLE_PENALTY_MAX_SCORE
            signal = f"{rule.reason} ({rule.scope}, {rule.effect})"
            decision.signals.append(signal)
            if _STRENGTH[rule.effect] > _STRENGTH[decision.effect]:
                decision.effect, decision.rule, decision.reason = rule.effect, f"{rule.scope}:{rule.pattern}", rule.reason
        return decision


# ---------------------------------------------------------------- rejection log
