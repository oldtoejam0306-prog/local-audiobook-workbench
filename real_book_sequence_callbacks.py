"""Private sequential bridge; no GUI wiring or global lock.

Inject a synchronous renderer and verifier; verifier must validate output and actual
voice assets. The default dispatcher calls the existing one-shot context worker.
The caller owns index/snapshot trust, cross-process exclusion, consent and budgets.
"""
import hashlib
import json
from pathlib import Path

import real_book_sequence as seq
import real_pipeline_workflow as workflow
import real_pipeline_analysis as analysis
from context_compaction import compact_prior_context


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _read(path, sha256):
    raw = Path(path).read_bytes()
    if _hash(raw) != sha256:
        raise ValueError('private artifact changed')
    return json.loads(raw)


def _save(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    workflow._write_new(path, obj)
    return {'path': str(path), 'sha256': _hash(path.read_bytes())}


class Bridge:
    """Callbacks for seq.run; all paths are private, isolated per book/chapter.

    index_path and trusted_snapshot are pinned inputs accepted by the existing
    verified index restore. Source hash in the plan is SHA256 of UTF-8 text.
    Renderer receives (render_json_path, voice_bindings), returns a receipt;
    verify_renderer(receipt, render_json_path, voice_bindings) must return True.
    """
    def __init__(self, root, index_path, trusted_snapshot, voice_bindings,
                 renderer, verify_renderer, *, dispatch=workflow.dispatch_chapter,
                 restore=workflow.prepare_index_chapter):
        self.root = Path(root).resolve()
        self.index = index_path
        self.snapshot = trusted_snapshot
        self.voices = voice_bindings
        self.renderer = renderer
        self.verify_renderer = verify_renderer
        self.dispatch = dispatch
        self.restore = restore
        self.correction_root = None
        if not callable(renderer) or not callable(verify_renderer):
            raise ValueError('verified renderer required')

    def callbacks(self):
        return {s: getattr(self, s) for s in seq.STAGES}

    def _dir(self, chapter):
        return self.root / chapter['book_id'] / chapter['id']

    def analyze(self, chapter, prior, artifacts):
        if seq.fingerprint(self.voices) != chapter['voice_bindings_sha256']:
            raise ValueError('voice binding changed')
        payload = self.restore(self.index, chapter['id'], self.snapshot, chapter['index_sha256'])
        if (payload.get('book_id') != chapter['book_id'] or payload.get('chapter_id') != chapter['id']
                or _hash(payload['text'].encode('utf8')) != chapter['source_sha256']):
            raise ValueError('source identity mismatch')
        records = []
        corrections = []
        for item in prior:
            a = item['artifacts']['memory']
            record = _read(a['path'], a['sha256'])
            prior_chapter = item.get('chapter', {})
            expected_source = prior_chapter.get('source_sha256')
            if record.get('book_id') != chapter['book_id'] or record.get('chapter_id') != prior_chapter.get('id'):
                raise ValueError('prior memory belongs to another book')
            if (not isinstance(expected_source, str)
                    or (record.get('source_sha256') is not None and record['source_sha256'] != expected_source)):
                raise ValueError('prior memory source hash mismatch')
            records.append({'book_id': record['book_id'], 'chapter_id': record['chapter_id'],
                'record_sha256': a['sha256'], 'response_sha256': record['response_sha256'],
                'source_sha256': expected_source, 'characters': record['characters'],
                'chapter_summary': record.get('chapter_summary'), 'unresolved': record.get('unresolved', []),
                'human_reviewed': record.get('human_reviewed')})
            corrections.extend(record.get('corrections', []))
        if self.correction_root is not None:
            from correction_memory import read_notes
            notes=read_notes(self.correction_root,chapter['book_id'],chapter['id'],include_current=True)
            expected_sources={r['chapter_id']:r['source_sha256'] for r in records}
            expected_sources[chapter['id']]=chapter['source_sha256']
            for note in notes:
                expected_source=expected_sources.get(note['chapter_id'])
                if expected_source is None:
                    note_payload=self.restore(self.index,note['chapter_id'],self.snapshot,chapter['index_sha256'])
                    if (note_payload.get('book_id')!=chapter['book_id']
                            or note_payload.get('chapter_id')!=note['chapter_id']
                            or not isinstance(note_payload.get('text'),str)):
                        raise ValueError('cannot verify correction note source')
                    expected_source=_hash(note_payload['text'].encode('utf8'))
                if note['source_sha256']!=expected_source:
                    raise ValueError('correction note source differs from frozen chapter')
            corrections.extend(notes)
        payload['prior_context'] = compact_prior_context(chapter['book_id'], chapter['id'],
            records, corrections=corrections, max_bytes=12000,
            allow_current_corrections=self.correction_root is not None)
        base = self._dir(chapter)
        # dispatch_chapter exclusively reserves and retains raw intent/input/response.
        result = self.dispatch(payload, base / 'tasks', base / 'jobs')
        input_path, receipt = Path(result['input_path']), Path(result['receipt_dir'])
        if json.loads(input_path.read_bytes()) != payload:
            raise ValueError('dispatch input mismatch')
        # Record verified usage even if later model-content validation fails.
        raw_body = (receipt / 'response.body').read_bytes()
        intent = json.loads((receipt / 'intent.json').read_bytes())
        meta = json.loads((receipt / 'response.meta.json').read_bytes())
        if (intent.get('book_id') != chapter['book_id'] or intent.get('chapter_id') != chapter['id']
                or intent.get('input_sha256') != _hash(input_path.read_bytes())
                or meta.get('body_sha256') != _hash(raw_body) or meta.get('http_status') != 200):
            raise ValueError('receipt identity mismatch; usage unknown')
        body = json.loads(raw_body)
        usage = body.get('usage', {})
        if not all(type(usage.get(k)) is int and usage[k] >= 0 for k in ('prompt_tokens', 'completion_tokens')):
            raise ValueError('usage unknown')
        billing = _save(base / 'billing.json', {'book_id': chapter['book_id'], 'chapter_id': chapter['id'],
            'response_sha256': _hash(raw_body), 'usage': usage,
            'status': 'reported_usage_not_account_bill', 'human_reviewed': False})
        # build checks input, response, usage, coverage and dialogue evidence.
        package = analysis.build(input_path, receipt, {'narrator': self.voices['narrator']['voice'], 'characters': {}})
        return {'status': 'ok', 'artifact': {'input_path': str(input_path), 'input_sha256': _hash(input_path.read_bytes()),
            'receipt_dir': str(receipt), 'response_sha256': package['provenance']['response_sha256'], 'billing': billing,
            'local_normalization':package['provenance'].get('local_normalization')}}

    def memory(self, chapter, prior, artifacts):
        a = artifacts['analyze']; x = _read(a['input_path'], a['input_sha256'])
        body = (Path(a['receipt_dir']) / 'response.body').read_bytes()
        if _hash(body) != a['response_sha256']:
            raise ValueError('response changed')
        parsed = json.loads(json.loads(body)['choices'][0]['message']['content'])
        if not isinstance(parsed.get('characters'), list):
            raise ValueError('invalid candidates')
        accepted, rejected, seen = [], 0, set()
        for c in parsed['characters']:
            if not isinstance(c, dict):
                rejected += 1; continue
            n, aliases, evidence = c.get('name'), c.get('aliases'), c.get('evidence')
            if (not isinstance(n, str) or not n or n in seen or not isinstance(aliases, list)
                or not all(isinstance(v, str) and v for v in aliases) or not isinstance(evidence, str)
                or not evidence or evidence not in x['text'] or n not in evidence):
                rejected += 1; continue
            seen.add(n)
            accepted.append({'name': n, 'aliases': aliases, 'evidence': evidence,
                             'chapter_id': chapter['id'], 'identity_status': 'unreviewed_model_candidate'})
        summary = parsed.get('chapter_summary')
        if not isinstance(summary, str) or len(summary) > 1500:
            summary = None
        record = {'book_id': chapter['book_id'], 'chapter_id': chapter['id'], 'characters': accepted,
                  'chapter_summary': summary, 'response_sha256': a['response_sha256'],
                  'source_sha256': chapter['source_sha256'],
                  'unresolved': [{'type': 'rejected_character_evidence', 'count': rejected}] if rejected else [],
                  'human_reviewed': False}
        normalization=a.get('local_normalization') or {}
        if normalization.get('changes'):
            record['unresolved'].append({'type':'local_attribution_normalization',
                                         'changes':normalization['changes']})
        if a.get('recovered') is True:
            record.update({'execution_kind': 'derived_locally',
                           'original_response_sha256': a['original_response_sha256'],
                           'original_meta_sha256': a['original_meta_sha256'],
                           'recovery_binding_sha256': a['recovery_binding_sha256'],
                           'usage_source': 'original_saved_provider_receipt'})
        return {'status': 'ok', 'artifact': _save(self._dir(chapter) / 'memory.json', record)}

    def stage(self, chapter, prior, artifacts):
        if seq.fingerprint(self.voices) != chapter['voice_bindings_sha256']:
            raise ValueError('voice binding changed')
        m = _read(**artifacts['memory'])
        names = {c['name'] for c in m['characters']}
        chosen = {n: v for n, v in self.voices['characters'].items() if n in names}
        voices = {'narrator': self.voices['narrator']['voice'], 'characters': {n: v['voice'] for n, v in chosen.items()}}
        a = artifacts['analyze']
        _read(a['input_path'], a['input_sha256'])
        if _hash((Path(a['receipt_dir']) / 'response.body').read_bytes()) != a['response_sha256']:
            raise ValueError('response changed')
        out = self._dir(chapter) / 'stage'
        result = workflow.stage_receipt(a['input_path'], a['receipt_dir'], voices,
                    {n: v['id'] for n, v in chosen.items()}, out, narrator_id=self.voices['narrator']['id'])
        p = Path(result['path'])
        artifact = {'path': str(p), 'sha256': _hash(p.read_bytes())}
        if a.get('recovered') is True:
            artifact.update({'execution_kind': 'derived_locally',
                             'original_response_sha256': a['original_response_sha256'],
                             'original_meta_sha256': a['original_meta_sha256'],
                             'recovery_binding_sha256': a['recovery_binding_sha256'],
                             'usage_source': 'original_saved_provider_receipt'})
        return {'status': 'ok', 'artifact': artifact}

    def render(self, chapter, prior, artifacts):
        stage = artifacts['stage']; _read(stage['path'], stage['sha256'])
        if seq.fingerprint(self.voices) != chapter['voice_bindings_sha256']:
            raise ValueError('voice binding changed')
        completed_path = self._dir(chapter) / 'render-receipt.json'
        if completed_path.is_file():
            completed = json.loads(completed_path.read_bytes())
            if (completed.get('human_reviewed') is not False
                    or self.verify_renderer(completed.get('receipt'), stage['path'], self.voices) is not True):
                raise ValueError('existing render receipt does not match frozen inputs')
            return {'status': 'ok', 'artifact': {'path': str(completed_path),
                    'sha256': _hash(completed_path.read_bytes())}}
        receipt = self.renderer(stage['path'], self.voices)
        if self.verify_renderer(receipt, stage['path'], self.voices) is not True:
            raise ValueError('render not verified')
        record = {'receipt': receipt, 'human_reviewed': False}
        if stage.get('execution_kind') == 'derived_locally':
            for key in ('execution_kind', 'original_response_sha256', 'original_meta_sha256',
                        'recovery_binding_sha256', 'usage_source'):
                record[key] = stage[key]
        return {'status': 'ok', 'artifact': _save(self._dir(chapter) / 'render-receipt.json', record)}

