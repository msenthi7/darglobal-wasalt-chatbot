"""Detect AI-directed instructions and invisible-character tricks in untrusted text.

Used by the scraper (quarantine + trap report) and by the app's corpus loader
(defence in depth). Patterns are phrase-level with word boundaries: substrings
such as "Installment" or "callMethod" must not match "llm" (a false positive
seen during recon).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

HIGH = "high"  # instruction-like text aimed at an AI: quarantined from the corpus
INFO = "info"  # AI-related wording worth reporting, not removed

_AI = (
    r"(?:an?\s+)?(?:ai|a\.i\.|llm|large\s+language\s+model|language\s+model|bot|chat\s?bot|"
    r"assistant|gpt|chatgpt|claude|gemini|copilot|agent|automated\s+(?:system|tool|agent)|"
    r"machine|crawler|scraper)"
)

_RAW_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("ai_conditional", HIGH, rf"\bif\s+you(?:\s+are|\s*['’]re|\s+re)\s+{_AI}\b"),
    (
        "ignore_instructions",
        HIGH,
        r"\b(?:ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}?"
        r"\b(?:previous|prior|above|earlier|all|any|your|system|original)\b[^.\n]{0,25}?"
        r"\b(?:instructions?|prompts?|rules|guidelines|directives)\b",
    ),
    ("addressed_to_ai", HIGH, rf"\b(?:note|message|instructions?|attention|reminder)\s+(?:to|for)\s+(?:the\s+|any\s+|all\s+)?{_AI}s?\b"),
    ("role_markers", HIGH, r"<\|im_start\|>|<\|im_end\|>|<\|system\|>|\[/?INST\]|<<SYS>>|^\s*#{2,}\s*(?:system|instructions?)\s*:?\s*$"),
    ("you_are_now", HIGH, r"\byou\s+are\s+now\s+(?:a|an|in|the|my)\b"),
    ("prompt_terms", HIGH, r"\b(?:system\s+prompt|developer\s+(?:message|mode)|hidden\s+(?:prompt|instructions?)|jailbreak)\b"),
    ("arabic_ai_conditional", HIGH, r"إذا\s+كنت\s+(?:نموذج|ذكاء\s*اصطناعي|روبوت|مساعد|بوت)"),
    ("arabic_ignore_instructions", HIGH, r"تجاهل\s+(?:جميع\s+|كل\s+)?(?:التعليمات|الأوامر)"),
    ("embed_request", INFO, r"\b(?:embed|insert|include|mention|append)\s+(?:this|the\s+(?:word|phrase|string|code|token|keyword|secret))\b"),
    ("secret_token", INFO, r"\b(?:secret|code|magic)\s+(?:word|phrase)\b|\bcanary\s+(?:token|string)\b"),
    ("as_an_ai", INFO, r"\bas\s+an?\s+(?:ai|llm|language\s+model)\b"),
    ("ai_directive", INFO, r"\b(?:ai|llm)s?\s+(?:models?|assistants?|agents?|systems?|crawlers?)\s+(?:must|should|need\s+to|are\s+required\s+to)\b"),
    (
        "ai_crawler_name",
        INFO,
        r"\b(?:gptbot|chatgpt-user|oai-searchbot|anthropic-ai|claudebot|claude-web|claude-user|"
        r"perplexitybot|perplexity-user|ccbot|google-extended|bytespider)\b",
    ),
    # "AI integration" is ordinary smart-home marketing on DarGlobal's blog, so it is deliberately not matched.
    ("ai_policy", INFO, r"\bllms?(?:-full)?\.txt\b|\bllm[-\s]?policy\b|\ballowing\s+ai\b|\bai\s+(?:policy|access|crawlers?|bots?)\b"),
)

PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (pid, sev, re.compile(rx, re.IGNORECASE | re.MULTILINE)) for pid, sev, rx in _RAW_PATTERNS
)

_TAG_CHARS = re.compile("[\U000e0000-\U000e007f]")  # Unicode "tag" block: invisible ASCII smuggling
_INVISIBLE = re.compile("[​-‏⁠-⁤﻿᠎­‪-‮⁦-⁩]")
_INVISIBLE_RUN = re.compile("[​-‏⁠-⁤﻿᠎­‪-‮⁦-⁩]{4,}")
_WS = re.compile(r"\s+")

MAX_PER_PATTERN = 3


@dataclass(frozen=True)
class Finding:
    pattern: str
    severity: str
    excerpt: str


def decode_tag_chars(text: str) -> str:
    """Decode Unicode tag characters (U+E0020..U+E007E) into the ASCII they hide."""
    return "".join(chr(ord(c) - 0xE0000) for c in text if 0xE0020 <= ord(c) <= 0xE007E)


def strip_invisible(text: str) -> str:
    """Remove invisible/format characters and normalise compatibility forms (NFKC)."""
    text = _TAG_CHARS.sub("", text)
    text = _INVISIBLE.sub("", text)
    return unicodedata.normalize("NFKC", text)


def _visible_repr(text: str) -> str:
    return "".join(f"\\u{ord(c):04x}" if _INVISIBLE.match(c) or _TAG_CHARS.match(c) else c for c in text)


def _excerpt(text: str, start: int, end: int, width: int = 70) -> str:
    snippet = _WS.sub(" ", text[max(0, start - width) : end + width]).strip()
    return _visible_repr(snippet)[:240]


def scan_text(text: str) -> list[Finding]:
    """Return trap findings for one block of untrusted text (deterministic order)."""
    if not text:
        return []
    findings: list[Finding] = []
    tags = _TAG_CHARS.findall(text)
    if tags:
        hidden = decode_tag_chars("".join(tags))
        findings.append(Finding("unicode_tag_smuggling", HIGH, (hidden or "<undecodable tag characters>")[:240]))
        text = f"{text}\n{hidden}"  # scan the decoded payload as well
    run = _INVISIBLE_RUN.search(text)
    if run:
        findings.append(Finding("invisible_char_run", INFO, _excerpt(text, run.start(), run.end(), 30)))
    clean = strip_invisible(text)
    for pid, severity, rx in PATTERNS:
        for count, match in enumerate(rx.finditer(clean)):
            if count >= MAX_PER_PATTERN:
                break
            findings.append(Finding(pid, severity, _excerpt(clean, match.start(), match.end())))
    return findings


def has_high(findings: list[Finding]) -> bool:
    return any(f.severity == HIGH for f in findings)


def line_is_suspicious(line: str) -> bool:
    """True when a single line carries a high-severity pattern (used for line-level quarantine)."""
    return has_high(scan_text(line))
