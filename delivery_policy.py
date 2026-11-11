"""Trusted, bounded delivery hints for the experimental native renderer.

Model outputs select only a closed delivery enum. They never supply TTS
instruction text. Explicit user overrides, when supplied separately, take
precedence over the fixed delivery hint.
"""
from __future__ import annotations
from dataclasses import replace

DELIVERIES = frozenset({'neutral', 'calm', 'tense', 'happy', 'angry', 'sad', 'afraid'})

_HINTS = {
    'calm': '语气平静克制，自然清晰。',
    'tense': '语气略带紧张，保持克制，吐字清晰。',
    'happy': '语气轻快愉悦，保持自然，不要夸张。',
    'angry': '语气带有压抑的愤怒，保持克制，吐字清晰。',
    'sad': '语气低沉含蓄，保持自然，吐字清晰。',
    'afraid': '语气略带不安，保持克制，吐字清晰。',
}
_CLOSING = '自然平稳的中文旁白。陈述句末尾自然收束，最后一个字的发音完整、清晰。'


def validate_delivery(value: object) -> str:
    if not isinstance(value, str) or value not in DELIVERIES:
        raise ValueError('unsupported delivery value')
    return value


def validate_override(value: object) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > 500
            or not all(ch.isprintable() for ch in value)):
        raise ValueError('invalid explicit delivery instruction')
    return value


def instruction_for(delivery: str = 'neutral', override: str | None = None,
                    *, terminal_narration: bool = False) -> str | None:
    """Resolve trusted delivery to bounded Chinese TTS instructions.

    Explicit user prose takes precedence. A terminal narration cue is appended
    only when no explicit override is supplied. Neutral delivery adds no hint.
    """
    value = validate_delivery(delivery)
    if override is not None:
        return validate_override(override)
    parts = []
    if terminal_narration:
        parts.append(_CLOSING)
    hint = _HINTS.get(value)
    if hint:
        parts.append(hint)
    return ' '.join(parts) if parts else None


_CLOSING_QUOTES = frozenset('"\'”’」』》〉】〕）)]}»›')
_DECLARATIVE_STOPS = frozenset({'.', '。', '．'})


def _ends_in_declarative_stop(text: object) -> bool:
    if not isinstance(text, str):
        return False
    edge = text.rstrip()
    while edge and edge[-1] in _CLOSING_QUOTES:
        edge = edge[:-1].rstrip()
    return bool(edge) and edge[-1] in _DECLARATIVE_STOPS


def apply_terminal_cadence(plan, narrator_voice: str, explicit_block_ids=()):
    """Copy a Huashuo Plan and add the proven cadence cue to terminal narration.

    Only adjacent body units in the same chapter qualify, and only for a
    narration-to-character transition after declarative terminal punctuation.
    Existing pause values, text, voices, ordering, and block IDs are untouched.
    Explicitly overridden source blocks are left unchanged.
    """
    if not isinstance(narrator_voice, str) or not narrator_voice:
        raise ValueError('narrator_voice must be a nonempty voice reference')
    try:
        explicit = set(explicit_block_ids)
    except TypeError as exc:
        raise ValueError('explicit block IDs must be iterable strings') from exc
    if any(not isinstance(value, str) or not value for value in explicit):
        raise ValueError('explicit block IDs must be nonempty strings')
    units = getattr(plan, 'units', None)
    if not isinstance(units, (list, tuple)):
        raise TypeError('plan.units must be a list or tuple')
    updated = list(units)
    audit = []
    for index in range(len(units) - 1):
        current, following = units[index], units[index + 1]
        if (getattr(current, 'kind', None) != 'body'
                or getattr(following, 'kind', None) != 'body'
                or getattr(current, 'chapter', None) != getattr(following, 'chapter', None)
                or getattr(current, 'voice', None) != narrator_voice
                or getattr(following, 'voice', None) in (None, narrator_voice)):
            continue
        block_ids = getattr(current, 'block_ids', ())
        if not isinstance(block_ids, (list, tuple)) or explicit.intersection(block_ids):
            continue
        page_text = getattr(current, 'page_text', None)
        if not _ends_in_declarative_stop(page_text):
            continue
        existing = getattr(current, 'instruct', None)
        if existing is not None:
            validate_override(existing)
        closing = instruction_for('neutral', terminal_narration=True)
        if existing is None or existing == closing or existing.startswith(closing + ' '):
            instruction = existing or closing
        else:
            instruction = closing + ' ' + existing
        updated[index] = replace(current, instruct=instruction)
        audit.append({'unit_index': index,
                      'chapter_index': getattr(current, 'chapter', None),
                      'block_ids': list(block_ids),
                      'instruction': instruction})
    return replace(plan, units=updated), audit
