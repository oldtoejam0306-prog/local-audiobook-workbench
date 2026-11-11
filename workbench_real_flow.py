"""Explicit one-shot client for the configured private index bridge."""
import hashlib
import json
import re
import console_voice_ui as console
import workbench_real
import real_pipeline_workflow as workflow


def books():
    entries = workbench_real.registry()
    result = {entry['display_name']: entry['book_id'] for entry in entries}
    if len(result) != len(entries):
        raise ValueError('书籍显示名称必须唯一')
    return result


class _Registry:
    @property
    def BOOKS(self):
        return books()


ui = _Registry()


def index_request(action, book, chapter=None, runner=console.ssh_python):
    from workbench_book_import import BookImporter
    from workbench_real_v2_runtime import ROOT
    importer = BookImporter(ROOT)
    if any(entry['book_id'] == book for entry in importer.list_books()):
        return importer.query(action, book, chapter)
    if book not in books().values() or action not in ('list', 'prepare'):
        raise ValueError('书籍未登记或请求无效')
    if action == 'prepare':
        if not isinstance(chapter, str) or not re.fullmatch(r'c\d+(?:\.p\d+(?:\.\d+)?)?', chapter):
            raise ValueError('章节无效')
    elif chapter is not None:
        raise ValueError('请求无效')
    request = {'action': action, 'book_id': book}
    if chapter is not None:
        request['chapter_id'] = chapter
    process = runner('workbench_real_index_mac.py', request, 90)
    try:
        response = json.loads(process.stdout)
        data = response['data']
        if process.returncode or response.get('ok') is not True or data.get('book_id') != book or data.get('human_reviewed') is not False:
            raise ValueError()
        if not re.fullmatch(r'[0-9a-f]{64}', data.get('index_sha256', '')):
            raise ValueError()
        if action == 'list':
            ids = data['chapters']
            if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids) or not all(isinstance(x, str) and re.fullmatch(r'c\d+(?:\.p\d+(?:\.\d+)?)?', x) for x in ids):
                raise ValueError()
        else:
            payload = data['payload']
            if payload.get('book_id') != book or payload.get('chapter_id') != chapter or hashlib.sha256(workflow._bytes(payload)).hexdigest() != data['payload_sha256']:
                raise ValueError()
        return data
    except (ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeError('索引读取失败或身份校验失败') from None
