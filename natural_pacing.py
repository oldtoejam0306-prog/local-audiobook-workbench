"""Deterministic, lossless sentence chunking and short inter-clip gaps."""
from dataclasses import dataclass, replace
import math

SENTENCE_END = frozenset("。！？!?．.")
SOFT_END = frozenset("，,、；;：:")
CLOSERS = frozenset('”’\"\'）)]】》」』〉〕〗〙〛')
SENTENCE_DOTS = frozenset('.．')


def _sentence_punctuation(text: str, index: int) -> bool:
    char = text[index]
    if char not in SENTENCE_END:
        return False
    if char in SENTENCE_DOTS and index > 0 and index + 1 < len(text):
        if text[index - 1].isdigit() and text[index + 1].isdigit():
            return False
    return True


def _extend_boundary(text: str, end: int, limit_end: int) -> int:
    while end < limit_end and (text[end] in CLOSERS or text[end].isspace()):
        end += 1
    return end


@dataclass(frozen=True)
class TextChunk:
    text: str
    boundary: str  # sentence, soft, hard, or final


def split_text(text: str, limit: int = 500) -> list[TextChunk]:
    """Split without editing a code point; prefer sentence then soft boundaries.

    Long punctuation-free text uses an explicit hard boundary and is auditable.
    """
    if not isinstance(text, str) or not text:
        raise ValueError("text must be nonempty")
    if limit < 1:
        raise ValueError("limit must be positive")
    result = []
    start = 0
    while len(text) - start > limit:
        end = start + limit
        sentence_options = []
        soft_options = []
        for i in range(start, end):
            if _sentence_punctuation(text, i):
                cut = _extend_boundary(text, i + 1, end)
                if cut > start and text[start:cut].strip() and any(c not in CLOSERS and not c.isspace() for c in text[start:cut]):
                    sentence_options.append(cut)
            elif text[i] in SOFT_END:
                cut = _extend_boundary(text, i + 1, end)
                if cut > start and text[start:cut].strip() and any(c not in CLOSERS and not c.isspace() for c in text[start:cut]):
                    soft_options.append(cut)
        sentence = max(sentence_options, default=0)
        soft = max(soft_options, default=0)
        if sentence:
            cut, kind = sentence, "sentence"
        elif soft:
            cut, kind = soft, "soft"
        else:
            cut, kind = end, "hard"
        result.append(TextChunk(text[start:cut], kind))
        start = cut
    tail = text[start:]
    # A closing quote (or whitespace) must not become a standalone synthesis request.
    if result and tail and all(c in CLOSERS or c.isspace() for c in tail):
        prior = result.pop()
        combined = prior.text + tail
        if len(combined) <= limit:
            result.append(TextChunk(combined, prior.boundary))
            tail = ""
        else:
            trailing = 0
            for c in reversed(combined):
                if c in CLOSERS or c.isspace():
                    trailing += 1
                else:
                    break
            keep = max(len(combined) - limit, trailing + 1)
            result.append(TextChunk(combined[:-keep], "hard"))
            tail = combined[-keep:]
    if tail:
        result.append(TextChunk(tail, "final"))
    if "".join(c.text for c in result) != text or any(not c.text or len(c.text) > limit for c in result):
        raise AssertionError("lossless chunking invariant failed")
    return result


def gap_seconds(previous_text: str, next_text: str, previous_speaker: str,
                next_speaker: str, *, sample_rate: int = 24000,
                sentence: float = 0.20, speaker_change: float = 0.25,
                paragraph: float = 0.35) -> tuple[int, str]:
    """Return PCM frame count and reason; never removes source audio silence."""
    if not previous_text:
        return 0, "start"
    previous_edge = previous_text.rstrip(" \t\f\v")
    next_edge = next_text.lstrip(" \t\f\v")
    if previous_edge.endswith(("\n", "\r")) or next_edge.startswith(("\n", "\r")):
        seconds, reason = paragraph, "paragraph"
    elif previous_speaker != next_speaker:
        seconds, reason = speaker_change, "speaker_change"
    elif next((c for c in reversed(previous_edge) if c not in CLOSERS), "") in SENTENCE_END:
        seconds, reason = sentence, "sentence"
    else:
        return 0, "continuation"
    return round(seconds * sample_rate), reason
# Huashuo native Plan pause policy. This function is intentionally pure: it
# changes only generated layout pause overrides, never synthesis inputs or audio.
_TRANSITION_EXCLUDED_AFTER = frozenset({'end', 'chapter', 'chapter_end', 'heading', 'title', 'break'})
_CUE_END = frozenset(':：,，')
_OPENERS = frozenset('“‘「『（([{〈《【')


def _ends_with_continuation_cue(text: str) -> bool:
    edge = text.rstrip()
    while edge and edge[-1] in CLOSERS:
        edge = edge[:-1].rstrip()
    while edge and edge[-1] in _OPENERS:
        edge = edge[:-1].rstrip()
    return bool(edge) and edge[-1] in _CUE_END


def adjust_narration_dialogue_pauses(plan, narrator_voice: str, resolved_pauses: dict,
                                     *, full_handoff: float = 0.50,
                                     continuation_cue: float = 0.35):
    """Return a copied Huashuo Plan with minimum pauses at narration/dialogue turns.

    Only adjacent body units in the same chapter are eligible. Existing
    ``pause_override`` values are explicit author/layout decisions and are
    preserved exactly. For other eligible boundaries, the preceding unit's
    pause override becomes max(resolved default, policy floor), so silence is
    not stacked. The returned audit rows contain only unit indices, durations,
    chapter indices, and boundary type; no source text or voice identifiers.
    """
    if not isinstance(narrator_voice, str) or not narrator_voice:
        raise ValueError('narrator_voice must be a nonempty voice reference')
    if not isinstance(resolved_pauses, dict):
        raise TypeError('resolved_pauses must be a mapping')
    floors = {'full_handoff': float(full_handoff),
              'continuation_cue': float(continuation_cue)}
    if any(not math.isfinite(value) or value < 0 for value in floors.values()):
        raise ValueError('pause floors must be finite nonnegative seconds')
    units = getattr(plan, 'units', None)
    if not isinstance(units, (list, tuple)):
        raise TypeError('plan.units must be a list or tuple')
    updated = list(units)
    changes = []
    for index in range(len(units) - 1):
        current, following = units[index], units[index + 1]
        if (getattr(current, 'kind', None) != 'body'
                or getattr(following, 'kind', None) != 'body'
                or getattr(current, 'chapter', None) != getattr(following, 'chapter', None)):
            continue
        current_voice = getattr(current, 'voice', None)
        following_voice = getattr(following, 'voice', None)
        current_is_narrator = current_voice == narrator_voice
        following_is_narrator = following_voice == narrator_voice
        if current_is_narrator == following_is_narrator:
            continue
        after = getattr(current, 'after', None)
        if not isinstance(after, str) or after in _TRANSITION_EXCLUDED_AFTER:
            continue
        if getattr(current, 'pause_override', None) is not None:
            continue
        if after not in resolved_pauses:
            continue
        before = float(resolved_pauses[after])
        if not math.isfinite(before) or before < 0:
            raise ValueError('resolved pause must be finite nonnegative seconds')
        cue = _ends_with_continuation_cue(getattr(current, 'page_text', ''))
        boundary_type = 'continuation_cue' if cue else 'full_handoff'
        floor = floors[boundary_type]
        after_seconds = max(before, floor)
        if after_seconds <= before:
            continue
        updated[index] = replace(current, pause_override=after_seconds)
        changes.append({'unit_index': index,
                        'chapter_index': getattr(current, 'chapter', None),
                        'before_seconds': before,
                        'after_seconds': after_seconds,
                        'type': boundary_type,
                        'direction': ('narration_to_dialogue' if current_is_narrator
                                      else 'dialogue_to_narration')})
    return replace(plan, units=updated), changes


