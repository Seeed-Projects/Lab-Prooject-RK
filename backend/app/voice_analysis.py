from __future__ import annotations

import json
import re
import unicodedata
from typing import Any


DEFAULT_VOICE_ANALYSIS = {
    "summary": "Waiting for conversation",
    "intent": "Available after transcript",
    "recommendation": "Available after transcript",
}

_EMPTY_TEXT = {"", "null", "none", "undefined", "[object object]"}
def _clean_text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    text = str(value).strip()
    if text.lower() in _EMPTY_TEXT:
        return ""
    return text.strip("` \n\r\t")


def _parse_json_text(text: str) -> Any | None:
    candidate = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    candidate = re.sub(r"\s*```$", "", candidate).strip()
    for value in (candidate, candidate[candidate.find("{"):candidate.rfind("}") + 1]):
        if not value:
            continue
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _labeled_sections(text: str) -> dict[str, str]:
    labels = {
        "summary": r"(?:conversation\s+)?summary",
        "intent": r"(?:customer\s+)?intent",
        "recommendation": r"(?:recommendation|recommended\s+action|next\s+step)",
    }
    matches: list[tuple[int, int, str]] = []
    for field, pattern in labels.items():
        for match in re.finditer(rf"(?:^|\n)\s*(?:#+\s*)?{pattern}\s*[:\-]\s*", text, re.IGNORECASE):
            matches.append((match.start(), match.end(), field))
    matches.sort()
    result: dict[str, str] = {}
    for index, (_, content_start, field) in enumerate(matches):
        content_end = matches[index + 1][0] if index + 1 < len(matches) else len(text)
        value = text[content_start:content_end].strip(" \n\r\t-*#")
        if value:
            result[field] = value
    return result


def _field_text(value: Any, field: str, depth: int) -> str:
    direct = _clean_text(value)
    if direct:
        parsed = _parse_json_text(direct)
        if parsed is not None and parsed != value:
            return normalize_voice_analysis(parsed, depth=depth + 1).get(field, "")
        return direct
    if isinstance(value, dict):
        nested = normalize_voice_analysis(value, depth=depth + 1)
        return nested.get(field, "")
    if isinstance(value, (list, tuple)):
        parts = [_clean_text(item) for item in value]
        return " ".join(part for part in parts if part)
    return ""


def normalize_voice_analysis(
    value: Any,
    fallback: dict[str, str] | None = None,
    *,
    depth: int = 0,
) -> dict[str, str]:
    defaults = dict(DEFAULT_VOICE_ANALYSIS if fallback is None else fallback)
    if depth > 6 or value is None:
        return defaults

    if isinstance(value, str):
        text = value.strip()
        parsed = _parse_json_text(text)
        if parsed is not None and parsed != value:
            return normalize_voice_analysis(parsed, defaults, depth=depth + 1)
        sections = _labeled_sections(text)
        if sections:
            value = sections
        else:
            summary = _clean_text(text)
            return {**defaults, **({"summary": summary} if summary else {})}

    if isinstance(value, (list, tuple)):
        for item in value:
            nested = normalize_voice_analysis(item, defaults, depth=depth + 1)
            if nested != defaults:
                return nested
        return defaults

    if not isinstance(value, dict):
        return defaults

    aliases = {
        "summary": ("summary", "conversation_summary", "overview"),
        "intent": ("intent", "customer_intent", "customerIntent"),
        "recommendation": (
            "recommendation", "recommendations", "recommended_action",
            "next_action", "next_step",
        ),
    }
    result: dict[str, str] = {}
    for field, keys in aliases.items():
        for key in keys:
            if key in value:
                text = _field_text(value[key], field, depth)
                if text:
                    result[field] = text
                    break
    if result:
        return {**defaults, **result}

    for key in ("analysis", "result", "data", "output", "response", "choices", "message", "content", "text"):
        if key in value:
            nested = normalize_voice_analysis(value[key], defaults, depth=depth + 1)
            if nested != defaults:
                return nested
    return defaults


def analysis_requires_english_rewrite(analysis: dict[str, str]) -> bool:
    return any(
        unicodedata.category(character).startswith("L") and not character.isascii()
        for value in analysis.values()
        for character in (value or "")
    )
