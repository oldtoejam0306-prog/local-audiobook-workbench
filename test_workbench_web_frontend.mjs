import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('./web/workbench.js', import.meta.url), 'utf8');
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const frontend = await import(moduleUrl);

test('startup token is read from #token and removed from the visible URL', () => {
  const writes = [];
  const values = new Map();
  const browser = {
    location: { hash: '#token=local%2Bsecret', pathname: '/', search: '?mode=local' },
    history: { replaceState: (...args) => writes.push(args) },
  };
  const storage = { setItem: (key, value) => values.set(key, value), getItem: (key) => values.get(key) || '' };
  assert.equal(frontend.readSessionToken(browser, storage), 'local+secret');
  assert.equal(values.get('audiobook-workbench-token'), 'local+secret');
  assert.equal(writes[0][2], '/?mode=local');
  browser.location.hash = '';
  assert.equal(frontend.readSessionToken(browser, storage), 'local+secret');
});

test('navigation fragments retain the stored token and valid raw tokens remain supported', () => {
  const writes = [];
  const values = new Map([['audiobook-workbench-token', 'session-from-launcher']]);
  const browser = {
    location: { hash: '#chapters', pathname: '/', search: '' },
    history: { replaceState: (...args) => writes.push(args) },
  };
  const storage = { setItem: (key, value) => values.set(key, value), getItem: (key) => values.get(key) || '' };
  assert.equal(frontend.readSessionToken(browser, storage), 'session-from-launcher');
  assert.equal(values.get('audiobook-workbench-token'), 'session-from-launcher');
  assert.equal(writes.at(-1)[2], '/');
  const rawToken = 'x'.repeat(43);
  browser.location.hash = `#${rawToken}`;
  assert.equal(frontend.readSessionToken(browser, storage), rawToken);
  assert.equal(values.get('audiobook-workbench-token'), rawToken);
});

test('brand activation returns to chapters without navigating to its fragment', () => {
  const calls = [];
  const ui = Object.create(frontend.WorkbenchUI.prototype);
  ui.setPage = (page) => calls.push(page);
  let prevented = false;
  ui.onBrandClick({ preventDefault: () => { prevented = true; } });
  assert.equal(prevented, true);
  assert.deepEqual(calls, ['chapters']);
});

test('state and action requests use the session header and JSON contract', async () => {
  const calls = [];
  let fetchThis = null;
  async function fetchImpl(url, options) {
    fetchThis = this;
    calls.push({ url, options });
    return { ok: true, status: 200, json: async () => ({ books: [] }) };
  }
  const client = new frontend.WorkbenchClient({ token: 'test-session', fetchImpl });
  assert.deepEqual(await client.getState(), { books: [] });
  assert.deepEqual(await client.action('book', { book_id: 'book-a' }), { books: [] });
  assert.equal(calls[0].url, '/api/state');
  assert.equal(calls[0].options.headers['X-Workbench-Token'], 'test-session');
  assert.equal(calls[0].options.method, 'GET');
  assert.equal(calls[0].options.cache, 'no-store');
  assert.equal(fetchThis, globalThis);
  assert.equal(calls[1].url, '/api/action');
  assert.equal(calls[1].options.headers['X-Workbench-Token'], 'test-session');
  assert.equal(calls[1].options.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(calls[1].options.body), { book_id: 'book-a', action: 'book' });
});

test('book preview upload sends raw file bytes with encoded filename and session token', async () => {
  const calls = [];
  const file = { name: '风 起.txt', size: 12 };
  const snapshot = { import_preview: { preview_id: 'preview-a' }, busy: { kind: 'import_preview' } };
  const client = new frontend.WorkbenchClient({
    token: 'test-session',
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return { ok: true, status: 200, json: async () => snapshot };
    },
  });
  assert.deepEqual(await client.previewBook(file), snapshot);
  assert.equal(calls[0].url, '/api/books/preview');
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(calls[0].options.body, file);
  assert.equal(calls[0].options.headers['Content-Type'], 'application/octet-stream');
  assert.equal(calls[0].options.headers['X-Book-Filename'], encodeURIComponent(file.name));
  assert.equal(calls[0].options.headers['X-Workbench-Token'], 'test-session');
  assert.equal(calls[0].options.cache, 'no-store');
});

test('backend error messages survive as actionable client errors', async () => {
  const client = new frontend.WorkbenchClient({
    token: 'test-session',
    fetchImpl: async () => ({ ok: false, status: 409, json: async () => ({ error: { code: 'conflict', message: '请先暂停当前任务。' } }) }),
  });
  await assert.rejects(client.action('execute'), (error) => error.status === 409 && error.code === 'conflict' && error.message === '请先暂停当前任务。');
});

test('media URLs carry the session token only on the same origin', () => {
  const url = frontend.mediaUrlWithToken('/media/audio_1', 'a+b/c', 'http://127.0.0.1:4111');
  assert.equal(url, '/media/audio_1?token=a%2Bb%2Fc');
  assert.throws(() => frontend.mediaUrlWithToken('http://example.invalid/audio', 'secret', 'http://127.0.0.1:4111'), /本机工作台/);
});

test('book and revision actions bind the form identity and revision chapter', () => {
  assert.deepEqual(frontend.bindActionIdentity('voice_generate', { role_id: 'role-a' }, { bookId: 'book-a' }), {
    role_id: 'role-a', expected_book_id: 'book-a',
  });
  assert.deepEqual(frontend.bindActionIdentity('revision_adopt', { candidate_id: 'candidate-a' }, { bookId: 'book-a', chapterId: 'chapter-3' }), {
    candidate_id: 'candidate-a', expected_book_id: 'book-a', expected_chapter_id: 'chapter-3',
  });
  assert.deepEqual(frontend.bindActionIdentity('voice_draft', { role_id: 'old-role', expected_book_id: 'old-book' }, { bookId: 'new-book' }), {
    role_id: 'old-role', expected_book_id: 'old-book',
  });
  assert.deepEqual(frontend.bindActionIdentity('book', { book_id: 'book-b' }, { bookId: 'book-a' }), { book_id: 'book-b' });
  assert.deepEqual(frontend.bindActionIdentity('billing_reprice', { input_price: '2' }, { bookId: 'book-a' }), { input_price: '2' });
  assert.deepEqual(frontend.bindActionIdentity('import_commit', { preview_id: 'p1' }, { bookId: 'old-book' }), { preview_id: 'p1' });
  assert.deepEqual(frontend.bindActionIdentity('import_heading', { preview_id: 'p1', heading: '第十二章' }, { bookId: 'old-book' }), { preview_id: 'p1', heading: '第十二章' });
  assert.deepEqual(frontend.buildImportCommitPayload('p1', ' 新书 ', [22, 4, 22, -1, 'x']), {
    preview_id: 'p1', title: '新书', chapter_starts: [4, 22],
  });
  assert.deepEqual(frontend.buildImportCommitPayload('p1', '书名', new Set([14, 3])), {
    preview_id: 'p1', title: '书名', chapter_starts: [3, 14],
  });
});

test('import preview renders chapter controls and source text as plain text', () => {
  class ElementStub {
    constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.attributes = {}; this._text = ''; this.disabled = false; this.className = ''; }
    set textContent(value) { this._text = String(value); }
    get textContent() { return this._text + this.children.map((child) => child.textContent || '').join(''); }
    append(...items) { this.children.push(...items); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    querySelector(selector) {
      const matches = (item) => selector.startsWith('[data-action=') && item.dataset?.action === selector.match(/"([^"]+)"/)?.[1];
      const visit = (item) => { if (matches(item)) return item; for (const child of item.children || []) { const found = visit(child); if (found) return found; } return null; };
      return visit(this);
    }
  }
  const previousDocument = globalThis.document;
  globalThis.document = { createElement: (tag) => new ElementStub(tag), createElementNS: (_ns, tag) => new ElementStub(tag) };
  try {
    const ui = Object.create(frontend.WorkbenchUI.prototype);
    ui.state = {
      busy: null,
      import_preview: {
        preview_id: 'preview-1', book_id: 'book-new', filename: 'novel.txt', title: '小说', source_sha256: 'a', text_sha256: 'b',
        encoding: 'UTF-8', character_count: 1200, warnings: ['请核对章节边界'],
        chapters: [{ id: 'c001', title: '第一章', start: 0, end: 18, characters: 18, excerpt: '<script>原文</script>' }],
      },
    };
    ui.importFile = { name: 'novel.txt', size: 1200 };
    ui.importTitleDraft = '小说'; ui.importHeadingDraft = ''; ui.importHeadingSearched = false;
    ui.importEnabledStarts = new Set([0]); ui.importSelectedChapter = 'c001'; ui.importError = '';
    ui.importPending = false; ui.pendingActionName = '';
    const page = ui.renderImport();
    assert.match(page.textContent, /在本机解析，点击开始配音后才分析与合成/);
    assert.match(page.textContent, /第一章/);
    assert.match(page.textContent, /18 字/);
    assert.match(page.textContent, /原文/);
    assert.match(page.textContent, /<script>原文<\/script>/);
    assert.equal(page.querySelector('[data-action="import_commit"]').dataset.payload, JSON.stringify({ preview_id: 'preview-1' }));
    assert.equal(page.querySelector('[data-action="import_heading"]').disabled, true);
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
});

test('already registered book preview offers direct selection without editable boundaries', () => {
  class ElementStub {
    constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.attributes = {}; this._text = ''; this.disabled = false; this.className = ''; }
    set textContent(value) { this._text = String(value); }
    get textContent() { return this._text + this.children.map((child) => child.textContent || '').join(''); }
    append(...items) { this.children.push(...items); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    querySelector(selector) {
      const matches = (item) => selector.startsWith('[data-action=') && item.dataset?.action === selector.match(/"([^"]+)"/)?.[1];
      const visit = (item) => { if (matches(item)) return item; for (const child of item.children || []) { const found = visit(child); if (found) return found; } return null; };
      return visit(this);
    }
  }
  const previousDocument = globalThis.document;
  globalThis.document = { createElement: (tag) => new ElementStub(tag), createElementNS: (_ns, tag) => new ElementStub(tag) };
  try {
    const ui = Object.create(frontend.WorkbenchUI.prototype);
    ui.state = { busy: null, import_preview: { preview_id: 'preview-existing', existing_book: true, existing_name: '已有书', filename: 'book.txt', title: '已有书', chapters: [] } };
    ui.importTitleDraft = '已有书'; ui.importHeadingDraft = ''; ui.importHeadingSearched = false;
    ui.importEnabledStarts = new Set(); ui.importSelectedChapter = ''; ui.importError = ''; ui.importPending = false; ui.pendingActionName = '';
    const page = ui.renderImport();
    assert.match(page.textContent, /这份文件已登记，可直接使用已有书目/);
    assert.match(page.textContent, /进入已有书籍/);
    assert.equal(page.querySelector('[data-action="import_heading"]'), null);
    assert.equal(page.querySelector('[data-action="import_commit"]').disabled, false);
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
});

test('a found independent heading joins enabled boundaries in source offset order', () => {
  const ui = Object.create(frontend.WorkbenchUI.prototype);
  ui.importEnabledStarts = new Set([0, 42]);
  const chapters = ui.importChapters({
    chapters: [{ id: 'c1', title: '第一章', start: 0, end: 41, characters: 41 }],
    heading_matches: [{ id: 'c2', title: '第二章', start: 42, end: 80, characters: 38 }],
  });
  assert.deepEqual(chapters.map((chapter) => [chapter.title, chapter.start]), [['第一章', 0], ['第二章', 42]]);
});

test('accepted async import failure preserves the preview title and boundary draft for retry', () => {
  const ui = Object.create(frontend.WorkbenchUI.prototype);
  const starts = new Set([0, 81]);
  ui.importCommitAwaiting = { previewId: 'preview-a', bookId: 'book-new' };
  ui.importTitleDraft = '已编辑书名'; ui.importEnabledStarts = starts; ui.importFile = { name: 'book.txt' };
  ui.importError = ''; ui.page = 'import'; ui.setPage = (page) => { ui.page = page; };
  assert.equal(ui.observeImportCommitState({ busy: { kind: 'import_commit' }, import_preview: { preview_id: 'preview-a' } }), 'pending');
  assert.deepEqual([...ui.importEnabledStarts], [0, 81]);
  const result = ui.observeImportCommitState({
    busy: null, error: '登记失败，请检查目录权限。', book_id: 'old-book',
    import_preview: { preview_id: 'preview-a' },
  });
  assert.equal(result, 'failed');
  assert.equal(ui.page, 'import');
  assert.equal(ui.importCommitAwaiting, null);
  assert.equal(ui.importTitleDraft, '已编辑书名');
  assert.equal(ui.importEnabledStarts, starts);
  assert.equal(ui.importError, '登记失败，请检查目录权限。');
});

test('accepted async import completion clears the draft and returns to chapters only for its book', () => {
  const ui = Object.create(frontend.WorkbenchUI.prototype);
  const pages = [];
  ui.importCommitAwaiting = { previewId: 'preview-b', bookId: 'book-new' };
  ui.importTitleDraft = '新书'; ui.importEnabledStarts = new Set([0, 24]); ui.importFile = { name: 'novel.txt' };
  ui.importPreviewId = 'preview-b'; ui.importHeadingDraft = '第十章'; ui.importHeadingSearched = true;
  ui.importHeadingSearchedFor = '第十章'; ui.importError = ''; ui.importPreviewSuppressed = false;
  ui.page = 'import'; ui.setPage = (page) => { pages.push(page); ui.page = page; };
  assert.equal(ui.observeImportCommitState({ busy: { kind: 'import_commit' }, import_preview: { preview_id: 'preview-b' } }), 'pending');
  assert.equal(ui.observeImportCommitState({ busy: null, error: '', book_id: 'book-new', import_preview: null }), 'complete');
  assert.deepEqual(pages, ['chapters']);
  assert.equal(ui.importTitleDraft, '');
  assert.deepEqual([...ui.importEnabledStarts], []);
  assert.equal(ui.importFile, null);
  assert.equal(ui.importCommitAwaiting, null);
});

test('heading lookup waits for completed snapshot matches before showing results', () => {
  const ui = Object.create(frontend.WorkbenchUI.prototype);
  ui.importHeadingAwaiting = '第十二章'; ui.importHeadingSearched = false; ui.importHeadingSearchedFor = '';
  assert.equal(ui.observeImportHeadingState({ busy: { kind: 'import_heading' }, import_preview: { heading_matches: [] } }), '');
  assert.equal(ui.importHeadingSearched, false);
  assert.equal(ui.observeImportHeadingState({ busy: null, error: '', import_preview: { heading_matches: [] } }), 'complete');
  assert.equal(ui.importHeadingSearched, true);
  assert.equal(ui.importHeadingSearchedFor, '第十二章');
  assert.equal(ui.importHeadingAwaiting, '');
});

test('import busy state blocks other actions including otherwise safe billing refresh', async () => {
  const calls = [];
  const ui = Object.create(frontend.WorkbenchUI.prototype);
  ui.state = { busy: { kind: 'import_preview', label: '正在解析' } };
  ui.importPending = false; ui.importCommitAwaiting = null; ui.importHeadingAwaiting = '';
  ui.latestActionId = 0; ui.clientError = '';
  ui.client = { action: async (...args) => { calls.push(args); return {}; } };
  ui.setLiveStatus = () => {};
  await ui.runAction('billing_refresh');
  assert.deepEqual(calls, []);
  assert.equal(ui.latestActionId, 0);
});

test('dynamic text assignment uses textContent and never parses book text as markup', () => {
  const assigned = [];
  const element = {};
  Object.defineProperty(element, 'textContent', { set(value) { assigned.push(value); }, get() { return assigned.at(-1); } });
  frontend.setTextContent(element, '<img src=x onerror=alert(1)>');
  assert.equal(element.textContent, '<img src=x onerror=alert(1)>');
  assert.match(source, /element\.textContent\s*=/);
  assert.doesNotMatch(source, /\.innerHTML\s*=|insertAdjacentHTML\s*\(|document\.write\s*\(/);
});

test('chapter page renders a non-empty chapter range without browser globals', () => {
  class ElementStub {
    constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.attributes = {}; this._text = ''; this.disabled = false; this.className = ''; }
    set textContent(value) { this._text = String(value); }
    get textContent() { return this._text + this.children.map((child) => child.textContent || '').join(''); }
    get childElementCount() { return this.children.length; }
    append(...items) { this.children.push(...items); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    addEventListener() {}
    querySelector(selector) {
      const matches = (item) => selector.startsWith('.')
        ? String(item.className || '').split(/\s+/).includes(selector.slice(1))
        : selector.startsWith('[data-action=') && item.dataset?.action === selector.match(/"([^"]+)"/)?.[1];
      const visit = (item) => { if (matches(item)) return item; for (const child of item.children || []) { const found = visit(child); if (found) return found; } return null; };
      return visit(this);
    }
  }
  const previousDocument = globalThis.document;
  globalThis.document = {
    createElement: (tag) => new ElementStub(tag),
    createElementNS: (_ns, tag) => new ElementStub(tag),
  };
  try {
    const ui = Object.create(frontend.WorkbenchUI.prototype);
    ui.state = {
      book_id: 'book-a', book_name: '本机书稿', busy: null,
      range: { start: 'c1', end: 'c2' },
      chapters: [
        { id: 'c1', title: '原始章节标题', status: '可试听', stages: ['analyze', 'render'], audio: false, history: true },
        { id: 'c2', status: '待读取进度', stages: [], audio: false, history: false },
        { id: 'c3', status: '范围外', stages: [], audio: false },
      ],
    };
    ui.selectedChapter = 'c1';
    const page = ui.renderChapters();
    assert.match(page.textContent, /章节制作/);
    assert.match(page.textContent, /读取进度/);
    assert.equal((page.textContent.match(/历史成品/g) || []).length, 1);
    assert.match(page.textContent, /原始章节标题/);
    assert.match(page.textContent, /对白分析/);
    assert.match(page.textContent, /语音生成/);
    assert.doesNotMatch(page.textContent, /范围外/);
    assert.equal(page.querySelector('.chapter-table').attributes['aria-label'], '当前范围内 2 个章节');
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
});

test('late GET snapshot is discarded after a newer playback action response', async () => {
  const ui = Object.create(frontend.WorkbenchUI.prototype);
  const applied = [];
  let releasePoll;
  ui.state = { book_id: 'book-a', busy: null, playback: null };
  ui.stateEpoch = 0; ui.latestActionId = 0; ui.pendingActionId = 0; ui.pendingActionName = '';
  ui.polling = false; ui.destroyed = false; ui.pollInterval = 800; ui.pollTimer = null;
  ui.content = { dataset: { bookId: 'book-a', chapterId: '' } };
  ui.client = {
    getState: () => new Promise((resolve) => { releasePoll = resolve; }),
    action: async () => ({ book_id: 'book-a', busy: null, playback: { id: 'replay-media', url: '/media/replay-media', speed: 1 } }),
  };
  ui.applyState = (state) => { ui.state = state; applied.push(state.playback?.id || null); };
  ui.updatePlaybackControls = () => {};
  ui.setLiveStatus = () => {};
  ui.schedulePoll = () => {};
  const staleGet = ui.refreshState();
  await ui.runAction('chapter_play', { chapter_id: 'c001', speed: 1 });
  releasePoll({ book_id: 'book-a', busy: null, playback: null });
  await staleGet;
  assert.deepEqual(applied, ['replay-media']);
  assert.equal(ui.state.playback.id, 'replay-media');
});

test('book scoped chapter actions are gated by active service busy state', async () => {
  const ui = Object.create(frontend.WorkbenchUI.prototype);
  const calls = [];
  ui.state = { book_id: 'book-a', busy: { kind: 'index', label: '读取索引' } };
  ui.content = { dataset: { bookId: 'book-a', chapterId: '' } };
  ui.client = { action: async (...args) => { calls.push(args); return {}; } };
  ui.updatePlaybackControls = () => {};
  ui.setLiveStatus = () => {};
  for (const action of ['index', 'range', 'chapter', 'progress', 'diagnostics']) await ui.runAction(action, {});
  assert.deepEqual(calls, []);
});

function makeShutdownUI(actionImpl, windowRef = {}) {
  const ui = Object.create(frontend.WorkbenchUI.prototype);
  ui.state = { book_id: 'book-a', busy: null };
  ui.content = { dataset: { bookId: 'book-a', chapterId: '' } };
  ui.window = windowRef;
  ui.client = { action: actionImpl };
  ui.latestActionId = 0; ui.stateEpoch = 0; ui.pendingActionId = 0; ui.pendingActionName = '';
  ui.updatePlaybackControls = () => {};
  ui.applyState = (state) => { ui.state = state; };
  ui.setLiveStatus = () => {};
  ui.schedulePoll = () => {};
  ui.shutdownRequested = true;
  return ui;
}

test('accepted shutdown notifies the native desktop host once', async () => {
  const messages = [];
  const ui = makeShutdownUI(async () => ({ book_id: 'book-a', busy: null }), { chrome: { webview: { postMessage: (message) => messages.push(message) } } });
  await ui.runAction('shutdown');
  assert.deepEqual(messages, ['audiobook-close-window']);
});

test('accepted shutdown works normally without a WebView bridge', async () => {
  const ui = makeShutdownUI(async () => ({ book_id: 'book-a', busy: null }));
  await assert.doesNotReject(ui.runAction('shutdown'));
  assert.equal(ui.state.book_id, 'book-a');
});

test('failed shutdown does not notify the native desktop host', async () => {
  const messages = [];
  const ui = makeShutdownUI(async () => { throw new Error('shutdown rejected'); }, { chrome: { webview: { postMessage: (message) => messages.push(message) } } });
  await ui.runAction('shutdown');
  assert.deepEqual(messages, []);
  assert.equal(ui.shutdownRequested, false);
});

test('stale shutdown response does not notify the native desktop host', async () => {
  const messages = [];
  let resolveAction;
  const ui = makeShutdownUI(() => new Promise((resolve) => { resolveAction = resolve; }), { chrome: { webview: { postMessage: (message) => messages.push(message) } } });
  const pending = ui.runAction('shutdown');
  ui.latestActionId += 1;
  resolveAction({ book_id: 'book-a', busy: null });
  await pending;
  assert.deepEqual(messages, []);
});

test('stop and media preparation keep play, speed, and seek locked until the action settles', () => {
  const ui = Object.create(frontend.WorkbenchUI.prototype);
  const controls = {
    'player-play': { disabled: false },
    'playback-speed': { disabled: false },
    'audio-seek': { disabled: false },
  };
  ui.state = { busy: null };
  ui.pendingActionName = 'stop';
  ui.lastPlaybackCommand = { action: 'chapter_play', payload: {} };
  ui.audio = { src: 'http://127.0.0.1/media/current', duration: 20 };
  ui.document = { getElementById: (id) => controls[id] };
  ui.updatePlaybackControls();
  assert.deepEqual(Object.fromEntries(Object.entries(controls).map(([key, value]) => [key, value.disabled])), {
    'player-play': true, 'playback-speed': true, 'audio-seek': true,
  });
  ui.pendingActionName = '';
  ui.updatePlaybackControls();
  assert.equal(controls['player-play'].disabled, false);
  assert.equal(controls['playback-speed'].disabled, false);
});

test('frontend stays local and implements real playback progress semantics', async () => {
  const html = await readFile(new URL('./web/index.html', import.meta.url), 'utf8');
  const css = await readFile(new URL('./web/workbench.css', import.meta.url), 'utf8');
  assert.match(html, /href="\/workbench\.css"/);
  assert.match(html, /src="\/workbench\.js"/);
  assert.doesNotMatch(html, /https?:\/\//i);
  assert.doesNotMatch(css, /@import\s+url\s*\(/i);
  assert.match(source, /this\.audio\.playbackRate\s*=\s*1/);
  assert.match(source, /this\.audio\.currentTime\s*=.*this\.audio\.duration/);
  assert.match(source, /playback\.url.*this\.client\.token/);
  assert.match(source, /playback_started/);
  assert.match(source, /修订基于原章，每章选用一个版本/);
  assert.match(source, /editor\?\.candidate_error/);
  assert.match(source, /actionButton\('读取进度', 'progress'/);
  assert.match(source, /candidate\?\.speaker_id/);
  assert.match(source, /previousProfiles\.get\(roleId\) !== profile\.profile_revision/);
  assert.match(source, /const groups = \[/);
  assert.match(source, /profile_registry_status/);
  assert.match(source, /item\.source_range/);
  assert.match(source, /playbackSeekPending = this\.audio\.currentTime \* oldSpeed \/ speed/);
  assert.match(css, /\.chapter-table\s*\{[^}]*max-height:[^}]*overflow:\s*auto/s);
  assert.match(css, /\.paragraph-list\s*\{[^}]*max-height:[^}]*overflow:\s*auto/s);
  assert.match(css, /\.profile-list\s*\{[^}]*max-height:[^}]*overflow:\s*auto/s);
  assert.match(source, /node\('div', 'voice-options'\)/);
  assert.match(css, /\.voice-options\s*\{[^}]*grid-template-columns:\s*repeat\(2/s);
  assert.match(css, /@media \(max-width: 780px\)[\s\S]*?\.voice-options\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(css, /select\s*\{[^}]*min-height:\s*40px/s);
  assert.match(css, /select\s*\{[^}]*min-height:\s*40px[^}]*font-size:\s*14px/s);
  assert.match(css, /\.field select\s*\{[^}]*min-height:\s*40px/s);
  assert.match(css, /\.speed-control select\s*\{[^}]*min-height:\s*40px[^}]*font-size:\s*14px/s);
  assert.match(css, /--radius:\s*12px/);
  assert.match(css, /"Source Sans 3"/);
  for (const speed of ['1.08', '1.1', '1.25']) assert.match(html, new RegExp(`value="${speed}"`));
});
