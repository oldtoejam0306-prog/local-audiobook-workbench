"""Deterministic, offline planning for synthetic character voice designs.

This module creates text prompts only. It never loads a model, writes files, or
calls a network service. Pass an approved plan's ``design_text`` and a short
sample line to voice_design_engine.py with action=design after a human review.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any


_VOICE_FIELDS = ('voice_description', 'voice_traits', 'vocal_traits', 'voice_notes')
_ACOUSTIC_TERMS = ('音高', '音域', '音色', '共鸣', '鼻音', '沙哑', '气声', '咬字', '语速', '节奏', '停顿', '清亮', '低沉', '偏细', '厚实')
_CONTRADICTIONS = (
    (('高音', '高音域', '偏高'), ('低音', '低音域', '偏低')),
    (('语速快', '快速'), ('语速慢', '缓慢')),
    (('清亮',), ('沙哑', '粗粝')),
)
_PROFILE_FIELDS = ('personality', 'description', 'traits', 'role', 'character_profile')


def _clean(value: Any, limit: int = 240) -> str:
    if isinstance(value, (list, tuple)):
        value = '、'.join(str(x).strip() for x in value if str(x).strip())
    if not isinstance(value, str):
        return ''
    return ' '.join(value.split())[:limit]


def _voice_text(profile: dict[str, Any]) -> str:
    """Return only explicitly acoustic profile fields; personality is excluded."""
    fields = []
    for key in _VOICE_FIELDS:
        value = _clean(profile.get(key))
        if value:
            fields.append(value)
    return '；'.join(dict.fromkeys(fields))


def _profile_text(profile: dict[str, Any]) -> str:
    return '；'.join(dict.fromkeys(value for key in _PROFILE_FIELDS
                                   if (value := _clean(profile.get(key), 300))))


def _infer_performance_style(profile_text: str) -> list[dict[str, str]]:
    """Map explicit fictional characterization to labeled performance choices.

    These are design interpretations, never claims about a character's actual
    acoustic properties. The rules deliberately do not infer age, gender, or a
    real-world voice identity.
    """
    text = profile_text.casefold()
    inferred = []
    rules = (
        (('话多', '健谈', '喋喋不休', '絮叨'), '语句衔接更紧、停顿较少', 'talkative_to_dense_rhythm'),
        (('快语速', '语速快', '说话很快', '嘴快'), '采用较快语速', 'explicit_fast_speech'),
        (('喜剧', '滑稽', '搞笑', '幽默', '逗趣'), '采用活泼、轻快的喜剧表演节奏，保留清楚咬字和喜剧停顿', 'comedic_to_playful_timing'),
        (('活泼', '开朗', '热情'), '采用更有活力、起伏较明显的表演方式', 'lively_to_energetic_delivery'),
        (('冷静', '沉着', '谨慎', '内敛'), '采用克制、稳定的表演节奏', 'calm_to_restrained_delivery'),
        (('紧张', '焦虑', '慌张'), '可采用略急促、带犹豫的表演节奏', 'anxious_to_hesitant_delivery'),
        (('威严', '强势', '果断'), '采用稳健、有力度的重音处理', 'assertive_to_firm_emphasis'),
    )
    for terms, output, rule in rules:
        if any(term in text for term in terms):
            inferred.append({'rule': rule, 'style': output})
    return inferred


def _source(profile: dict[str, Any], assumptions: dict[str, Any], name: str) -> tuple[str, str]:
    if name in assumptions:
        item = assumptions[name]
        if isinstance(item, dict):
            text = _voice_text(item)
            return text, 'assumption'
        return _clean(item), 'assumption'
    evidence_status = profile.get('voice_evidence_status')
    kind = 'verified' if evidence_status == 'verified' else ('assumption' if evidence_status == 'assumption' else 'profile_unverified')
    return _voice_text(profile), kind


def _conflicts(text: str) -> list[str]:
    found = []
    for positive, negative in _CONTRADICTIONS:
        if any(word in text for word in positive) and any(word in text for word in negative):
            found.append('声线描述包含互相冲突的特征')
    return list(dict.fromkeys(found))


def _normalize(text: str) -> str:
    text = unicodedata.normalize('NFKC', text).casefold()
    return re.sub(r'[\s，,。；;：:、！!？?（）()\-]', '', text)


def _resolve(name: str, aliases: dict[str, str]) -> str | None:
    return aliases.get(name.strip())


def plan_voice_designs(memory: dict[str, Any], script_rows: list[dict[str, Any]], *,
                       assumptions: dict[str, Any] | None = None,
                       pinned_voices: dict[str, str] | None = None) -> dict[str, Any]:
    """Build an auditable text-only plan from profiles and dialogue occurrence counts.

    ``assumptions`` maps character names to explicitly marked provisional voice
    notes. ``pinned_voices`` can pass a separately verified mapping; a voice
    already present in the profile is also treated as fixed and never replaced.
    Personality fields are retained only as audit metadata and never drive the
    acoustic description.
    """
    if not isinstance(memory, dict) or not isinstance(memory.get('characters'), dict):
        raise ValueError('人物档案必须包含 characters 对象。')
    if not isinstance(script_rows, list):
        raise ValueError('script_rows 必须是话本行列表。')
    characters = memory['characters']
    assumptions = assumptions or {}
    pinned_voices = pinned_voices or {}
    if not isinstance(assumptions, dict) or not isinstance(pinned_voices, dict):
        raise ValueError('assumptions 和 pinned_voices 必须为对象。')

    aliases: dict[str, str] = {}
    for name, profile in characters.items():
        if not isinstance(name, str) or not name or not isinstance(profile, dict):
            raise ValueError('人物档案条目无效。')
        for alias in [name, *profile.get('aliases', [])]:
            if not isinstance(alias, str) or not alias.strip():
                continue
            key = alias.strip()
            if key in aliases and aliases[key] != name:
                raise ValueError(f'人物别名冲突：{key}')
            aliases[key] = name

    counts: Counter[str] = Counter()
    seen_ids: set[str] = set()
    for row in script_rows:
        if not isinstance(row, dict) or row.get('type') != 'dialogue':
            continue
        ident = row.get('id')
        if ident is not None:
            if ident in seen_ids:
                raise ValueError(f'话本对白编号重复：{ident}')
            seen_ids.add(ident)
        name = _resolve(str(row.get('speaker', '')), aliases)
        if name:
            counts[name] += 1

    ranking = sorted(characters, key=lambda name: (-counts[name], name.casefold(), name))
    plans = []
    spec_groups: dict[str, list[str]] = defaultdict(list)
    for rank, name in enumerate(ranking, 1):
        profile = characters[name]
        evidence, evidence_kind = _source(profile, assumptions, name)
        profile_text = _profile_text(profile)
        performance_inference = _infer_performance_style(profile_text)
        pinned = _clean(pinned_voices.get(name) or profile.get('voice'), 120)
        conflicts = _conflicts(evidence)
        warnings = []
        if not evidence:
            if profile_text and performance_inference:
                warnings.append('声音表演风格根据人物画像推断；这是创作选择，不是原文声学事实。')
            else:
                warnings.append('没有可用的声学线索或角色画像；采用不预设性别、年龄或音色的中性描述。')
        elif not any(term in evidence for term in _ACOUSTIC_TERMS):
            warnings.append('现有备注未提供明确声学特征；未将性格描述转成声学事实。')
        if conflicts:
            warnings.extend(conflicts)
        # Treat identity labels as uncertain planning hints, never acoustic evidence.
        gender = _clean(profile.get('gender') or profile.get('gender_identity'), 40)
        if gender and not any(term in evidence for term in _ACOUSTIC_TERMS):
            warnings.append('性别标注不作为声学证据；请试听后决定声线。')
        if evidence_kind == 'assumption':
            warnings.append('声线信息来自标注假设，需人工确认。')
        elif evidence_kind == 'profile_unverified' and evidence:
            warnings.append('声线备注尚未标记为已证实；试听前按待核对线索处理。')

        if evidence or performance_inference:
            sections = ['为中文有声书设计虚构角色的表演声音。']
            if evidence:
                sections.append(f'画像中记录的声学线索（其证据状态为{evidence_kind}）：{evidence}。')
            if performance_inference:
                styles = '；'.join(item['style'] for item in performance_inference)
                sections.append(f'以下是根据人物画像作出的创作性表演推断，不是原文声学事实：{styles}。')
            sections.append('保持自然、清晰并适合长篇对白；不补充画像未提及的年龄或性别，不模仿真人，也不做动物叫声模仿。')
            design_text = ''.join(sections)
        else:
            design_text = '为中文有声书设计自然、中性、清晰的虚构角色表演声音。保持稳定、适合长篇对白；不预设性别、年龄、口音或具体音高，不模仿真人。'
        normalized = _normalize(design_text)
        spec_id = None if pinned else hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]
        if spec_id:
            spec_groups[spec_id].append(name)
        plans.append({
            'character': name,
            'appearance_count': counts[name],
            'priority_rank': rank,
            'profile_evidence': evidence,
            'evidence_kind': evidence_kind if evidence else 'none',
            'personality_audit_only': profile_text,
            'performance_inference': performance_inference,
            'gender_note': gender or None,
            'fixed_voice': pinned or None,
            'preserve_fixed_voice': bool(pinned),
            'status': 'fixed_voice_preserved' if pinned else ('needs_profile_review' if conflicts else 'planned'),
            'design_text': None if pinned else design_text,
            'spec_id': spec_id,
            'warnings': list(dict.fromkeys(warnings)),
        })

    duplicates = {key: names for key, names in spec_groups.items() if len(names) > 1}
    for item in plans:
        shared = duplicates.get(item['spec_id'], [])
        if shared:
            item['warnings'].append('与其他人物生成了相同文字方案：' + '、'.join(shared) + '；需人工区分，规划器不会擅自猜测声学差异。')
    return {
        'version': 1,
        'book_id': memory.get('book_id'),
        'source': 'local_profile_and_script_rows',
        'audio_generated': False,
        'model_called': False,
        'main_character_order': [item['character'] for item in plans],
        'plans': plans,
        'duplicate_specs': duplicates,
        'notes': [
            '出现次数是话本对白条数，不是人物重要性或声学证据。',
            '根据人物性格生成的表演风格属于标记过的创作推断，不是原文中的声学事实。',
            '候选声音应由本地文字声音合成生成，不使用真人参考声音。',
            '已固定声音仅报告并保留，不自动覆盖。',
        ],
    }


def plan_from_json(memory_path: str, script_path: str, **kwargs: Any) -> dict[str, Any]:
    """Read local JSON/JSONL inputs and return a plan without modifying them."""
    from pathlib import Path
    memory = json.loads(Path(memory_path).read_text(encoding='utf-8-sig'))
    rows = [json.loads(line) for line in Path(script_path).read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    return plan_voice_designs(memory, rows, **kwargs)


def plans_from_character_profiles(document: dict[str, Any]) -> dict[str, Any]:
    """Build source-auditable voice cards from a normalized character profile.

    This is a planning-only adapter: it does not invoke a model or write files.
    The normalized document's creative voice design is retained as a proposed
    design, while identity claims remain separately labeled and cited.
    """
    if (not isinstance(document, dict) or document.get('version') != 1
            or document.get('mode') != 'character_profiles_v1'
            or not isinstance(document.get('book_id'), str)
            or not isinstance(document.get('characters'), list)):
        raise ValueError('character_profiles_v1 document required')

    def claim_text(value: Any) -> str:
        if not isinstance(value, dict):
            return ''
        status = value.get('status')
        claim = _clean(value.get('value'), 240)
        return claim if claim and status in ('explicit', 'inferred', 'unknown') else ''

    source_chapters = document.get('source', {}).get('chapters', []) if isinstance(document.get('source'), dict) else []
    source_hashes = {item.get('chapter_id'): item.get('text_sha256') for item in source_chapters
                     if isinstance(item, dict)}

    def anchors(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result = []
        for item in value:
            if not isinstance(item, dict):
                continue
            chapter = item.get('chapter_id')
            quote = item.get('quote')
            source_range = item.get('source_range')
            source_sha = item.get('source_sha256')
            if (isinstance(chapter, str) and isinstance(quote, str) and 0 < len(quote) <= 500 and isinstance(source_range, list)
                    and len(source_range) == 2 and all(type(x) is int for x in source_range)
                    and 0 <= source_range[0] < source_range[1]
                    and source_range[1] - source_range[0] == len(quote)
                    and isinstance(source_sha, str) and re.fullmatch(r'[0-9a-f]{64}', source_sha)
                    and source_hashes.get(chapter) == source_sha):
                result.append({'chapter_id': chapter, 'quote': quote,
                               'source_range': source_range, 'source_sha256': source_sha})
        return result

    plans = []
    seen = set()
    for profile in document['characters']:
        if not isinstance(profile, dict):
            raise ValueError('invalid character profile')
        name = _clean(profile.get('name'), 80)
        if not name or name in seen:
            raise ValueError('missing or duplicate profile name')
        seen.add(name)
        raw_aliases = profile.get('aliases')
        if not isinstance(raw_aliases, list):
            raise ValueError(f'{name}: aliases must be a list')
        aliases = []
        for alias in raw_aliases:
            if (not isinstance(alias, dict) or not isinstance(alias.get('name'), str)
                    or not alias['name'].strip() or not anchors(alias.get('evidence'))
                    or any(name not in ref['quote'] or alias['name'] not in ref['quote']
                           for ref in anchors(alias.get('evidence')))):
                raise ValueError(f'{name}: alias lacks source-bound identity evidence')
            aliases.append({'name': alias['name'].strip(), 'evidence': anchors(alias['evidence'])})
        for key in ('identity', 'gender', 'age'):
            claim = profile.get(key)
            if not isinstance(claim, dict) or claim.get('status') not in ('explicit', 'inferred', 'unknown'):
                raise ValueError(f'{name}: invalid {key} claim')
            if key in ('gender', 'age') and claim['status'] == 'inferred':
                raise ValueError(f'{name}: {key} may only be explicit or unknown')
            if claim['status'] == 'unknown':
                if claim.get('value') not in ('', None) or claim.get('evidence') not in ([], None):
                    raise ValueError(f'{name}: unknown {key} must have empty value and evidence')
            elif not claim_text(claim) or not anchors(claim.get('evidence')):
                raise ValueError(f'{name}: {key} claim lacks source-bound evidence')
        for key in ('personality', 'behavior', 'speech_style'):
            values = profile.get(key)
            if not isinstance(values, list):
                raise ValueError(f'{name}: {key} must be a list')
            for claim in values:
                if not isinstance(claim, dict) or claim.get('status') not in ('explicit', 'inferred', 'unknown'):
                    raise ValueError(f'{name}: invalid or ungrounded {key} claim')
                if claim['status'] == 'unknown':
                    if claim.get('value') not in ('', None) or claim.get('evidence') not in ([], None):
                        raise ValueError(f'{name}: unknown {key} must have empty value and evidence')
                elif not claim_text(claim) or not anchors(claim.get('evidence')):
                    raise ValueError(f'{name}: invalid or ungrounded {key} claim')
        design = profile.get('voice_design')
        if (not isinstance(design, dict) or design.get('status') != 'candidate_needs_review'
                or not isinstance(design.get('description'), str)
                or not isinstance(design.get('rationale'), str)):
            raise ValueError(f'{name}: voice_design is missing or not reviewable')
        description = _clean(design['description'], 600)
        rationale = _clean(design['rationale'], 600)
        choices = design.get('creative_choices')
        if (not isinstance(choices, list) or not choices
                or any(not isinstance(choice, str) or not choice.strip() or len(choice) > 180 for choice in choices)):
            raise ValueError(f'{name}: voice_design requires bounded creative_choices')
        # No URLs, execution-like directives, or real-person imitation prompts.
        unsafe = re.compile(r'(https?://|www\.|忽略.{0,8}(规则|指令)|(?<!不)模仿.{0,12}(真人|本人|某人)|真人参考|上传.{0,8}声音)', re.I)
        if not description or unsafe.search(description) or unsafe.search(rationale) or any(unsafe.search(c) for c in choices):
            raise ValueError(f'{name}: unsafe or empty voice design description')
        identity = {key: profile.get(key) for key in ('identity', 'gender', 'age')}
        facts = {}
        for key, value in identity.items():
            if isinstance(value, dict):
                facts[key] = {'value': claim_text(value), 'status': value.get('status'),
                              'evidence': anchors(value.get('evidence'))}
        traits = {}
        for key in ('personality', 'behavior', 'speech_style'):
            values = profile.get(key, [])
            if not isinstance(values, list):
                raise ValueError(f'{name}: {key} must be a list')
            traits[key] = [{'value': claim_text(c) or '未知', 'status': c.get('status'),
                            'evidence': anchors(c.get('evidence'))}
                           for c in values if isinstance(c, dict) and claim_text(c)]
        design_anchors = []
        for value in [*facts.values(), *(item for group in traits.values() for item in group)]:
            design_anchors.extend(value['evidence'])
        # Keep stable source order while deduplicating identical anchors.
        design_anchors = list({json.dumps(a, ensure_ascii=False, sort_keys=True): a
                               for a in design_anchors}.values())
        profile_sha = hashlib.sha256(json.dumps(profile, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
        design_text = ('为中文有声书设计虚构角色表演声音。声音描述：' + description +
                       ' 创作依据（不是人物事实）：' + rationale +
                       ' 创作选择：' + '；'.join(choices) +
                       ' 以下声线与表演细节均为创作选择，不作为人物事实；保持清晰、自然、适合长篇对白，不模仿真人。')
        spec_id = hashlib.sha256((profile_sha + '\0' + _normalize(design_text)).encode('utf-8')).hexdigest()[:16]
        if not design_anchors:
            design_anchors = [{'chapter_id': item.get('chapter_id'),
                               'source_sha256': item.get('text_sha256'),
                               'quote': None, 'source_range': None}
                              for item in source_chapters if isinstance(item, dict)]
        plans.append({'character': name, 'aliases': aliases,
            'identity_claims': facts, 'personality_behavior_speech': traits,
            'design_text': design_text, 'design_rationale': rationale,
            'creative_choices': choices,
            'profile_evidence': design_anchors, 'profile_sha256': profile_sha,
            'spec_id': spec_id, 'status': 'needs_review',
            'voicing': design.get('mode', 'dedicated')})
    return {'version': 1, 'book_id': document['book_id'],
            'source': document.get('source'), 'audio_generated': False,
            'model_called': False, 'plans': plans,
            'issues': document.get('issues', []), 'scope_summary': document.get('scope_summary')}
