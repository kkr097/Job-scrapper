"""Conservative language-requirement filtering for job descriptions."""

from __future__ import annotations

import re
from typing import Optional


_LEVEL_RANK = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6}
_GERMAN_TERM = r"(?:german(?:\s+language)?|deutsch(?:kenntnisse)?|deutsche\s+sprache)"

_OPTIONAL_GERMAN_PATTERNS = [
    re.compile(rf"\b{_GERMAN_TERM}\b.{{0,35}}\b(?:a\s+plus|plus|preferred|optional|desirable|nice\s+to\s+have|an\s+advantage|beneficial|not\s+required)\b", re.I),
    re.compile(rf"\b(?:a\s+plus|preferred|optional|desirable|nice\s+to\s+have|an\s+advantage|beneficial)\b.{{0,35}}\b{_GERMAN_TERM}\b", re.I),
    re.compile(r"\b(?:deutsch(?:kenntnisse)?|deutsche\s+sprache)\b.{0,35}\b(?:wünschenswert|von\s+vorteil|optional|kein\s+muss|keine\s+voraussetzung|nicht\s+erforderlich)\b", re.I),
    re.compile(r"\b(?:idealerweise|wünschenswert|von\s+vorteil|optional)\b.{0,35}\b(?:deutsch(?:kenntnisse)?|deutsche\s+sprache)\b", re.I),
]
_LEVEL_PATTERNS = [
    re.compile(rf"\b{_GERMAN_TERM}\b.{{0,40}}\b(?:level|niveau|cefr|ger)?\s*(?:at\s+least|minimum|min\.?|mindestens|ab)?\s*(A1|A2|B1|B2|C1|C2)\b", re.I),
    re.compile(rf"\b(?:at\s+least|minimum|min\.?|mindestens|ab)?\s*(A1|A2|B1|B2|C1|C2)\b.{{0,40}}\b{_GERMAN_TERM}\b", re.I),
]
_MANDATORY_PATTERNS = [
    re.compile(r"\bgerman(?:\s+(?:language|language\s+skills|skills|proficiency|fluency))?\s+(?:is\s+|are\s+)?(?:required|mandatory|essential|necessary|a\s+prerequisite|non-negotiable)\b", re.I),
    re.compile(r"\b(?:required|mandatory|essential|necessary|prerequisite)\s*:?\s*(?:german(?:\s+(?:language|language\s+skills|skills|proficiency|fluency))?)(?=\s*(?:[,;/&]|\band\b|\bor\b|$))", re.I),
    re.compile(r"\bgerman\s+(?:and|&)\s+english\s+(?:language\s+)?(?:skills|proficiency)\s+(?:is\s+|are\s+)?(?:required|mandatory|essential)\b", re.I),
    re.compile(r"\bmust\s+(?:speak|write|understand|communicate\s+in|be\s+fluent\s+in)\s+german\b", re.I),
    re.compile(r"\b(?:fluent|business[-\s]fluent|business[-\s]level|native|full\s+professional|professional\s+working)(?:\s+(?:level|proficiency))?\s+(?:in\s+)?german\b", re.I),
    re.compile(r"\bgerman\s+(?:at\s+)?(?:business[-\s]fluent|business[-\s]level|native[-\s]level|native\s+speaker)\b", re.I),
    re.compile(r"\bfluency\s+in\b.{0,35}\bgerman\b", re.I),
    re.compile(r"\b(?:excellent|very\s+good|strong)\s+(?:command\s+of|knowledge\s+of|proficiency\s+in|written\s+and\s+spoken)?\s*german(?:\s+(?:language|skills?|proficiency))?\b", re.I),
    re.compile(r"\b(?:deutsch(?:kenntnisse)?|deutsche\s+sprache)\b.{0,55}\b(?:erforderlich|zwingend|notwendig|vorausgesetzt|voraussetzung|pflicht|muss)\b", re.I),
    re.compile(r"\b(?:erforderlich|zwingend|notwendig|vorausgesetzt|voraussetzung|pflicht)\b.{0,55}\b(?:deutsch(?:kenntnisse)?|deutsche\s+sprache)\b", re.I),
    re.compile(r"\b(?:gute(?:\s+bis\s+sehr\s+gute)?|sehr\s+gute|fließende|fliessende|verhandlungssichere|ausgezeichnete|hervorragende)\s+(?:deutschkenntnisse|deutsch(?:-\s*und\s*englisch)?kenntnisse|deutsche\s+sprachkenntnisse|kenntnisse\s+der\s+deutschen\s+sprache)\b", re.I),
    re.compile(r"\b(?:sehr\s+gutes|fließend(?:es)?|fliessend(?:es)?|verhandlungssicher(?:es)?|muttersprachlich(?:es)?)\s+deutsch\b|\bdeutsch\s+in\s+wort\s+und\s+schrift\b", re.I),
    re.compile(r"\bdeutschkenntnisse\s+auf\s+muttersprachlichem\s+niveau\b", re.I),
]


def _clauses(text: str) -> list[str]:
    normalized = re.sub(r"[\t\r]+", " ", text or "")
    return [re.sub(r"\s+", " ", clause).strip(" -–—•●▪\t") for clause in re.split(r"\n+|(?<=[.!?;])\s+", normalized) if clause.strip(" -–—•●▪\t")]


def _optional_german(clause: str) -> bool:
    return any(pattern.search(clause) for pattern in _OPTIONAL_GERMAN_PATTERNS)


def _required_level(clause: str) -> Optional[str]:
    for pattern in _LEVEL_PATTERNS:
        match = pattern.search(clause)
        if match:
            return match.group(1).upper()
    return None


def mandatory_german_requirement(text: str, candidate_level: str = "A2", reject_any_mandatory: bool = True) -> Optional[str]:
    candidate_rank = _LEVEL_RANK.get(str(candidate_level).upper(), 0)
    for clause in _clauses(text):
        if _optional_german(clause):
            continue
        required_level = _required_level(clause)
        if required_level:
            if reject_any_mandatory:
                return f"Mandatory German {required_level} requirement: {clause[:180]}"
            if _LEVEL_RANK[required_level] > candidate_rank:
                return f"German {required_level} required above candidate level {str(candidate_level).upper()}: {clause[:180]}"
            continue
        if any(pattern.search(clause) for pattern in _MANDATORY_PATTERNS):
            return f"Mandatory German-language requirement: {clause[:180]}"
    return None


def language_requirement_excerpts(text: str, max_excerpts: int = 4, max_chars: int = 1200) -> list[str]:
    excerpts = []
    for clause in _clauses(text):
        if not re.search(r"\bgerman\b|\bdeutsch(?!land)\w*", clause, re.I):
            continue
        if clause not in excerpts:
            excerpts.append(clause[:350])
        if len(excerpts) >= max_excerpts:
            break
    result, used = [], 0
    for excerpt in excerpts:
        remaining = max_chars - used
        if remaining <= 0:
            break
        result.append(excerpt[:remaining])
        used += len(result[-1])
    return result


def llm_language_rejection(assessment: dict, candidate_level: str = "A2", reject_any_mandatory: bool = True) -> Optional[str]:
    assessment = normalize_language_assessment(assessment)
    if assessment["german_requirement"] != "mandatory":
        return None
    level = assessment["required_level"]
    if not reject_any_mandatory and level in _LEVEL_RANK and _LEVEL_RANK[level] <= _LEVEL_RANK.get(str(candidate_level).upper(), 0):
        return None
    level_text = "" if level in {"", "UNSPECIFIED", "NONE", "UNKNOWN"} else f" ({level})"
    evidence_text = f": {assessment['evidence'][:220]}" if assessment["evidence"] else ""
    return f"LLM classified German as mandatory{level_text}{evidence_text}"


def normalize_language_assessment(assessment: dict) -> dict:
    if not isinstance(assessment, dict):
        assessment = {}
    status = str(assessment.get("german_requirement", "unclear")).strip().lower()
    status = {"required": "mandatory", "must_have": "mandatory", "must-have": "mandatory", "preferred": "optional", "nice_to_have": "optional", "none": "not_mentioned"}.get(status, status)
    if status not in {"mandatory", "optional", "not_mentioned", "unclear"}:
        status = "unclear"
    level = str(assessment.get("required_level", "unspecified")).strip().upper()
    if level not in {*_LEVEL_RANK, "UNSPECIFIED"}:
        level = "UNSPECIFIED"
    evidence = re.sub(r"\s+", " ", str(assessment.get("evidence", ""))).strip()
    return {"german_requirement": status, "required_level": level, "evidence": evidence[:350]}


def evidence_appears_in_text(evidence: str, text: str) -> bool:
    normalized_evidence = re.sub(r"\s+", " ", str(evidence or "")).strip().casefold()
    normalized_text = re.sub(r"\s+", " ", str(text or "")).strip().casefold()
    return bool(normalized_evidence) and normalized_evidence in normalized_text
