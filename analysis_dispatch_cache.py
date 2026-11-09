"""Reuse one verified analysis receipt for identical chapter inputs and worker code."""
import hashlib
import json
from pathlib import Path
import re

import real_pipeline_analysis as analysis
import real_pipeline_workflow as workflow


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


class AnalysisCacheError(RuntimeError):
    def __init__(self, message, directory):
        super().__init__(message)
        self.status = 'existing_analysis_requires_review'
        self.artifact_path = str(directory)
        self.retry_allowed = False


class CachedAnalysisDispatcher:
    def __init__(self, root, *, dispatch=workflow.dispatch_chapter,
                 preflight=None, worker_sha256=None):
        self.root = Path(root)
        self.dispatch = dispatch
        self.preflight = preflight or (
            (lambda: workflow._preflight_worker('node'))
            if dispatch is workflow.dispatch_chapter else (lambda: None))
        worker = Path(__file__).with_name('real_pipeline_context_worker.mjs')
        self.worker_sha256 = worker_sha256 or _sha(worker.read_bytes())
        if not re.fullmatch(r'[0-9a-f]{64}', self.worker_sha256):
            raise ValueError('invalid analysis worker digest')

    def _verified(self, entry, payload, binding):
        try:
            if (entry / 'binding.json').read_bytes() != workflow._bytes(binding):
                raise ValueError('cached request binding changed')
            task = entry / 'tasks' / (payload['book_id'] + '-' + payload['chapter_id'])
            input_path, receipt = task / 'input.json', task / 'receipt'
            if input_path.read_bytes() != workflow._bytes(payload):
                raise ValueError('cached input changed')
            result = json.loads((receipt / 'result.json').read_bytes())
            if result.get('status') not in ('needs_review','invalid_or_usage_unknown') or result.get('retry_allowed') is not False:
                raise ValueError('cached provider result is incomplete')
            package=analysis.build(input_path, receipt, {'narrator': 'preset:test', 'characters': {}})
            provenance=package.get('provenance',{})
            normalization=provenance.get('local_normalization',{})
            normalization_path=None
            if normalization.get('changes'):
                derivation={'kind':'local_attribution_normalization','human_reviewed':False,
                    'input_sha256':binding['input_sha256'],
                    'response_sha256':provenance['response_sha256'],
                    'normalized_result_sha256':provenance['normalized_result_sha256'],
                    'audit':normalization}
                raw_derivation=workflow._bytes(derivation)
                normalization_path=entry/('local-normalization-'+_sha(raw_derivation)[:20]+'.json')
                if normalization_path.exists():
                    if normalization_path.read_bytes()!=raw_derivation:
                        raise ValueError('local normalization record changed')
                else:
                    workflow._write_new(normalization_path,derivation)
            return {'status': 'needs_review', 'input_path': str(input_path),
                    'receipt_dir': str(receipt), 'reused_analysis': True,
                    'normalization_path':str(normalization_path) if normalization_path else None}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AnalysisCacheError(
                '已有分析请求未能核实完成，请检查回执；不会重新发送。', entry) from exc

    def __call__(self, payload, private_root=None, jobs_root=None):
        book, chapter = payload.get('book_id'), payload.get('chapter_id')
        if (not isinstance(book, str) or not re.fullmatch(r'book-[0-9a-f]{20}', book)
                or not isinstance(chapter, str) or not re.fullmatch(r'c\d+(?:\.p\d+(?:\.\d+)?)?', chapter)):
            raise ValueError('invalid cached analysis identity')
        binding = {'book_id': book, 'chapter_id': chapter,
                   'input_sha256': _sha(workflow._bytes(payload)),
                   'worker_sha256': self.worker_sha256}
        key = _sha(workflow._bytes(binding))
        entry = self.root / 'provider-cache' / book / chapter / key
        if entry.exists():
            return self._verified(entry, payload, binding)
        self.preflight()
        entry.parent.mkdir(parents=True, exist_ok=True)
        try:
            entry.mkdir()
        except FileExistsError:
            return self._verified(entry, payload, binding)
        workflow._write_new(entry / 'binding.json', binding)
        try:
            self.dispatch(payload, entry / 'tasks', entry / 'jobs')
        except Exception as exc:
            if getattr(exc,'operation_not_started',False) is True:
                binding_path=entry/'binding.json'
                if set(entry.iterdir())=={binding_path}:
                    binding_path.unlink()
                    entry.rmdir()
                raise
            # A complete provider reply can fail only the literal-evidence
            # gate. Validate and record the local fallback without another call.
            try:
                return dict(self._verified(entry,payload,binding),reused_analysis=False)
            except AnalysisCacheError:
                pass
            raise AnalysisCacheError(
                '分析调用已保留现场，请检查回执；不会自动重发。', entry) from exc
        return dict(self._verified(entry, payload, binding), reused_analysis=False)
