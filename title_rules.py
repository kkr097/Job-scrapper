"""Deterministic negative-title-keyword check (whole-word, case-insensitive).

The LLM must not guess title hits: a local model once capped a job at 2 for a
keyword ("Ownership") that was not in the title. Code decides, the model is told.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

TITLE_PENALTY_MAX_SCORE = 2


def contains_keyword(text: str, keyword: str, match_case: bool = False) -> bool:
    """Whole-word match so short terms like 'it' or 'ai' never hit inside words.

    With ``match_case`` the keyword must appear exactly as given (e.g. uppercase
    "IT"), which stops the pronoun "it" from matching.
    """
    flags = 0 if match_case else re.IGNORECASE
    return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text or "", flags) is not None


def title_penalty_keyword(title: str, keywords: Iterable[str]) -> Optional[str]:
    """Return the first negative keyword found as a whole word in the title, else None."""
    for keyword in keywords or []:
        keyword = str(keyword).strip()
        if keyword and contains_keyword(title, keyword):
            return keyword
    return None
