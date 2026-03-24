"""
agents/validator_agent.py

Validates generated content (emails and articles) against quality rules.

This agent is stateless: receives content string + type, returns a
ValidationResult. Combines fast rule-based checks with an optional
Claude-powered tone/brand check.

Public API:
    validate_content(content, content_type, language) -> ValidationResult

content_type values: "email_1" | "email_2" | "article"
"""

import re
from dataclasses import dataclass, field
from typing import Literal

import structlog

from config import get_settings

log = structlog.get_logger(__name__)

ContentType = Literal["email_1", "email_2", "article"]

# ---------------------------------------------------------------------------
# Illegal claim patterns (applies to all content types)
# ---------------------------------------------------------------------------

_ILLEGAL_CLAIM_PATTERNS = [
    # IT: "cura il/la/lo/i/le <malattia>" — verb "to cure", NOT noun "cura di sé" / "prenditi cura"
    r"\bcura\s+(il|la|lo|i|le|gli)\b",
    r"\bcura\s+(diabete|cancro|obesità|ipertensione|colesterolo|depressione|ansia|artrite)\b",
    r"\bguarisce\b",
    r"\bclinicamente provato\b",
    # EN
    r"\bclinically proven\b",
    r"\bcures?\s+(diabetes|cancer|obesity|hypertension|depression)\b",
    # FR
    r"\bguérit\b",
    r"\btraitement médical\b",
    # DE
    r"\bheilt\b",
    r"\bärztliche Behandlung\b",
    # Generic multi-language
    r"\btrattamento medico\b",
    r"\bmedical treatment\b",
]

# CTA markers accepted in any language
_CTA_PATTERNS = [
    r"http[s]?://",           # any URL counts as CTA
    r"scopri",
    r"acquista",
    r"discover",
    r"buy\s+now",
    r"shop\s+now",
    r"découvre",
    r"achète",
    r"entdecke",
    r"jetzt\s+kaufen",
    r"clicca",
    r"click",
    r"clique",
    r"klick",
]

# H2 usage is forbidden in articles
_H2_PATTERN = re.compile(r"<h2[\s>]", re.IGNORECASE)
# H1 is also forbidden
_H1_PATTERN = re.compile(r"<h1[\s>]", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    passed: bool
    score: int                         # 0–100
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Word count helper
# ---------------------------------------------------------------------------


def _word_count(html: str) -> int:
    """Strip HTML tags and count words."""
    clean = re.sub(r"<[^>]+>", " ", html)
    clean = re.sub(r"\s+", " ", clean).strip()
    if not clean:
        return 0
    return len(clean.split())


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_word_count(
    content: str,
    content_type: ContentType,
) -> tuple[int, list[str], list[str]]:
    """Return (score_penalty, issues, suggestions) for word count."""
    settings = get_settings()
    content_cfg = settings.content

    limits = {
        "email_1": content_cfg.get("email_1", {"min_words": 300, "max_words": 400}),
        "email_2": content_cfg.get("email_2", {"min_words": 350, "max_words": 450}),
        "article": content_cfg.get("article", {"min_words": 1500, "max_words": 1800}),
    }

    cfg = limits[content_type]
    min_w = cfg["min_words"]
    max_w = cfg["max_words"]
    count = _word_count(content)

    if count < min_w:
        penalty = min(40, (min_w - count) // 5 * 5)
        return (
            penalty,
            [f"Content too short: {count} words (minimum {min_w})"],
            [f"Expand content to at least {min_w} words."],
        )

    if count > max_w:
        penalty = min(20, (count - max_w) // 10 * 5)
        return (
            penalty,
            [f"Content too long: {count} words (maximum {max_w})"],
            [f"Trim content to at most {max_w} words."],
        )

    return 0, [], []


def _check_illegal_claims(content: str) -> tuple[int, list[str], list[str]]:
    """Detect unverifiable medical claims."""
    issues = []
    for pattern in _ILLEGAL_CLAIM_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            issues.append(f"Potential illegal medical claim detected (pattern: '{pattern}')")

    if issues:
        # Each illegal claim is a critical violation — penalty must guarantee failure
        return 35, issues, ["Remove all unverifiable medical claims."]
    return 0, [], []


def _check_cta_present(content: str) -> tuple[int, list[str], list[str]]:
    """Verify at least one CTA or link is present."""
    for pattern in _CTA_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return 0, [], []

    return (
        15,
        ["No CTA or link found in content"],
        ["Add a clear call-to-action with a link."],
    )


def _check_html_structure_article(content: str) -> tuple[int, list[str], list[str]]:
    """
    For articles: verify no H1/H2 tags are used,
    and at least one H3 is present.
    """
    issues = []
    suggestions = []
    penalty = 0

    if _H1_PATTERN.search(content):
        issues.append("Article contains <h1> — only H3/H4 are allowed")
        suggestions.append("Replace <h1> with <h3>.")
        penalty += 35  # critical: single violation must guarantee failure

    if _H2_PATTERN.search(content):
        issues.append("Article contains <h2> — only H3/H4 are allowed")
        suggestions.append("Replace <h2> with <h3>.")
        penalty += 35  # critical: single violation must guarantee failure

    if not re.search(r"<h3[\s>]", content, re.IGNORECASE):
        issues.append("Article contains no <h3> headings")
        suggestions.append("Add section headings using <h3> tags.")
        penalty += 5

    return penalty, issues, suggestions


def _check_no_conclusion_heading(content: str) -> tuple[int, list[str], list[str]]:
    """For articles: last section must not be called 'Conclusione'."""
    if re.search(r"conclus", content, re.IGNORECASE):
        return (
            5,
            ["Article uses 'Conclusione' or similar as last heading (forbidden)"],
            ["Rename the final section to 'In sintesi' or a similar alternative."],
        )
    return 0, [], []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_content(
    content: str,
    content_type: ContentType,
    language: str = "it",
) -> ValidationResult:
    """
    Validate generated content against HerbaMarketer quality rules.

    Args:
        content:      HTML body of the email or article.
        content_type: "email_1" | "email_2" | "article"
        language:     ISO language code (for context logging).

    Returns:
        ValidationResult with passed, score (0-100), issues, suggestions.
        Score < 70 means regeneration is required (as per CLAUDE.md spec).
    """
    log.info("validating_content", content_type=content_type, language=language)

    total_penalty = 0
    all_issues: list[str] = []
    all_suggestions: list[str] = []

    # --- 1. Word count ---
    p, i, s = _check_word_count(content, content_type)
    total_penalty += p
    all_issues.extend(i)
    all_suggestions.extend(s)

    # --- 2. Illegal medical claims ---
    p, i, s = _check_illegal_claims(content)
    total_penalty += p
    all_issues.extend(i)
    all_suggestions.extend(s)

    # --- 3. CTA present ---
    p, i, s = _check_cta_present(content)
    total_penalty += p
    all_issues.extend(i)
    all_suggestions.extend(s)

    # --- 4. Article-specific structure checks ---
    if content_type == "article":
        p, i, s = _check_html_structure_article(content)
        total_penalty += p
        all_issues.extend(i)
        all_suggestions.extend(s)

        p, i, s = _check_no_conclusion_heading(content)
        total_penalty += p
        all_issues.extend(i)
        all_suggestions.extend(s)

    score = max(0, 100 - total_penalty)
    min_score = get_settings().validator.get("min_score_to_pass", 70)
    passed = score >= min_score

    result = ValidationResult(
        passed=passed,
        score=score,
        issues=all_issues,
        suggestions=all_suggestions,
    )

    log.info(
        "validation_complete",
        content_type=content_type,
        passed=passed,
        score=score,
        issues_count=len(all_issues),
    )

    return result
