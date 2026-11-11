const TOKEN_STORAGE_KEY = 'audiobook-workbench-token';
const NAVIGATION_FRAGMENTS = new Set(['chapters', 'chapter', 'editor', 'voices', 'people', 'billing', 'home']);
const POLL_INTERVAL_MS = 800;
const PAID_ACTIONS = new Set(['profiles_analyze', 'execute', 'voice_generate', 'revision_generate']);
const BUSY_SAFE_ACTIONS = new Set(['pause', 'billing_refresh', 'billing_reprice', 'stop', 'playback_started']);
const IDENTITY_EXEMPT_ACTIONS = new Set(['book', 'import_commit', 'import_clear', 'import_heading', 'billing_refresh', 'billing_reprice', 'stop', 'playback_started', 'shutdown']);
const CHAPTER_BOUND_ACTIONS = new Set(['revision_generate', 'revision_play', 'revision_adopt', 'revision_restore']);

export function bindActionIdentity(action, payload = {}, { bookId = '', chapterId = '' } = {}) {
  const bound = { ...payload };
  if (!IDENTITY_EXEMPT_ACTIONS.has(action) && bookId && !Object.hasOwn(bound, 'expected_book_id')) bound.expected_book_id = bookId;
  if (CHAPTER_BOUND_ACTIONS.has(action) && chapterId && !Object.hasOwn(bound, 'expected_chapter_id')) bound.expected_chapter_id = chapterId;
  return bound;
}

export function buildImportCommitPayload(previewId, title, chapterStarts) {
  return {
    preview_id: String(previewId || ''),
    title: String(title || '').trim(),
    chapter_starts: [...new Set([...(chapterStarts || [])].map(Number).filter((value) => Number.isInteger(value) && value >= 0))].sort((a, b) => a - b),
  };
}

export function readSessionToken(browserWindow = window, storage = sessionStorage) {
  let token = '';
  const fragment = String(browserWindow.location.hash || '').replace(/^#/, '');
  if (fragment) {
    const params = new URLSearchParams(fragment);
    const decodedFragment = decodeURIComponent(fragment);
    if (params.has('token')) token = params.get('token') || '';
    else if (!fragment.includes('=') && !NAVIGATION_FRAGMENTS.has(decodedFragment) && /^[A-Za-z0-9_-]{32,}$/.test(decodedFragment)) token = decodedFragment;
    if (!token && NAVIGATION_FRAGMENTS.has(decodedFragment)) {
      browserWindow.history.replaceState(null, '', `${browserWindow.location.pathname}${browserWindow.location.search}`);
    }
  }
  if (token) {
    try { storage.setItem(TOKEN_STORAGE_KEY, token); } catch { /* Session storage may be unavailable in a locked profile. */ }
    browserWindow.history.replaceState(null, '', `${browserWindow.location.pathname}${browserWindow.location.search}`);
    return token;
  }
  try { return storage.getItem(TOKEN_STORAGE_KEY) || ''; } catch { return ''; }
}

export function mediaUrlWithToken(path, token, origin = globalThis.location?.origin || 'http://127.0.0.1') {
  const url = new URL(path, origin);
  if (url.origin !== origin) throw new Error('媒体地址必须来自本机工作台。');
  url.searchParams.set('token', token);
  return `${url.pathname}${url.search}`;
}

export class WorkbenchClient {
  constructor({ token, fetchImpl = globalThis.fetch, baseUrl = '' }) {
    if (typeof token !== 'string' || !token) throw new TypeError('缺少工作台会话令牌。');
    if (typeof fetchImpl !== 'function') throw new TypeError('当前环境不支持请求本机工作台。');
    this.token = token;
    this.fetchImpl = fetchImpl;
    this.baseUrl = baseUrl;
  }

  async request(path, options = {}) {
    const response = await this.fetchImpl.call(globalThis, `${this.baseUrl}${path}`, {
      ...options,
      headers: { ...(options.headers || {}), 'X-Workbench-Token': this.token },
      cache: 'no-store',
    });
    let body;
    try { body = await response.json(); } catch { throw new Error('本机工作台返回了无效响应。'); }
    if (!response.ok) {
      const message = body?.error?.message || `请求失败（${response.status}）。`;
      const error = new Error(message);
      error.status = response.status;
      error.code = body?.error?.code || '';
      throw error;
    }
    if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('本机工作台返回的状态格式无效。');
    return body;
  }

  getState() { return this.request('/api/state', { method: 'GET' }); }

  action(action, payload = {}) {
    const body = { ...payload, action };
    return this.request('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }

  previewBook(file) {
    return this.request('/api/books/preview', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/octet-stream',
        'X-Book-Filename': encodeURIComponent(file.name),
      },
      body: file,
    });
  }
}

function node(tag, className = '', value = null) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (value !== null && value !== undefined) setTextContent(element, value);
  return element;
}

export function setTextContent(element, value) {
  element.textContent = value === null || value === undefined ? '' : String(value);
  return element;
}

function svgIcon(path, viewBox = '0 0 24 24') {
  const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  icon.setAttribute('viewBox', viewBox);
  icon.setAttribute('aria-hidden', 'true');
  const shape = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  shape.setAttribute('d', path);
  icon.append(shape);
  return icon;
}

function actionButton(label, action, { className = 'button', payload = {}, disabled = false, title = '' } = {}) {
  const button = node('button', className, label);
  button.type = 'button';
  button.dataset.action = action;
  button.dataset.payload = JSON.stringify(payload);
  button.disabled = disabled;
  if (title) button.title = title;
  return button;
}

function field(label, control, hint = '') {
  const wrapper = node('label', 'field');
  wrapper.append(node('span', '', label), control);
  if (hint) wrapper.append(node('span', 'small-copy', hint));
  return wrapper;
}

function textArea(value, key, { placeholder = '', rows = 4, className = '' } = {}) {
  const textarea = node('textarea', className);
  textarea.rows = rows;
  textarea.value = value ?? '';
  textarea.dataset.draftKey = key;
  if (placeholder) textarea.placeholder = placeholder;
  return textarea;
}

function selectControl(label, values, current, { dataKey = '', className = '' } = {}) {
  const select = node('select', className);
  select.setAttribute('aria-label', label);
  if (dataKey) select.dataset[dataKey] = 'true';
  for (const item of values) {
    const option = node('option', '', item.label);
    option.value = String(item.value ?? '');
    option.selected = String(item.value ?? '') === String(current ?? '');
    select.append(option);
  }
  return select;
}

function heading(title, subtitle = '') {
  const wrapper = node('div', 'page-heading');
  const copy = node('div', 'stack-tight');
  copy.append(node('h1', '', title));
  if (subtitle) copy.append(node('p', '', subtitle));
  wrapper.append(copy);
  return wrapper;
}

function card(title, { label = '', className = 'surface' } = {}) {
  const section = node('section', className);
  if (title || label) {
    if (label) section.setAttribute('aria-label', label);
    const top = node('div', 'surface-head');
    if (title) top.append(node('h2', '', title));
    section.append(top);
  }
  return section;
}

function emptyState(title, copy, action = null) {
  const empty = node('div', 'empty-state');
  empty.append(svgIcon('M4.5 5.5h15v15h-15zM8 9h8M8 12.5h8M8 16h5'));
  empty.append(node('strong', '', title), node('p', '', copy));
  if (action) empty.append(action);
  return empty;
}

function valueText(value, fallback = '—') {
  if (value === null || value === undefined || value === '') return fallback;
  if (Array.isArray(value)) return value.filter(Boolean).join('、') || fallback;
  return String(value);
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
  const whole = Math.floor(seconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const remaining = whole % 60;
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${String(remaining).padStart(2, '0')}` : `${minutes}:${String(remaining).padStart(2, '0')}`;
}

function stageEntries(stages) {
  const labels = { analyze: '对白分析', memory: '人物与记忆', stage: '配音计划', render: '语音生成' };
  if (Array.isArray(stages)) return stages.map((stage) => {
    if (typeof stage === 'string') return { id: stage, label: labels[stage] || stage, status: 'complete' };
    const id = stage?.id || stage?.name || stage?.stage || '';
    return { id, label: stage?.label || labels[id] || id || '阶段', status: stage?.status || '' };
  });
  if (stages && typeof stages === 'object') return Object.entries(stages).map(([id, status]) => ({ id, label: labels[id] || id, status: typeof status === 'string' ? status : status?.status || '' }));
  return [];
}

function deliveryLabel(value) {
  const labels = { neutral: '平静', calm: '舒缓', tense: '紧张', happy: '开心', angry: '愤怒', sad: '难过', afraid: '害怕' };
  const raw = typeof value === 'string' ? value : value?.label || value?.hint || '';
  return labels[raw] || raw;
}

function claimEntries(claims) {
  if (!claims || typeof claims !== 'object' || Array.isArray(claims)) return [];
  const labels = { identity: '身份', gender: '性别', age: '年龄', personality: '性格', behavior: '行为', speech_style: '说话方式' };
  const output = [];
  for (const [fieldName, raw] of Object.entries(claims)) {
    const list = Array.isArray(raw) ? raw : [raw];
    for (const item of list) {
      const value = typeof item === 'string' ? item : item?.value ?? item?.text ?? item?.claim ?? '';
      if (!value) continue;
      const state = String(item?.status || item?.basis || item?.confidence || (item?.inferred ? 'inferred' : item?.explicit ? 'explicit' : '')).toLowerCase();
      const qualifier = state.includes('inferred') || state.includes('推断') ? '推断' :
        state.includes('explicit') || state.includes('明确') ? '原文明确' :
        state.includes('unknown') || state.includes('未知') ? '未知' : '待核对';
      output.push({ label: labels[fieldName] || fieldName, value: String(value), qualifier });
    }
  }
  return output;
}

function stageClass(status) {
  const value = String(status || '').toLowerCase();
  if (/done|complete|success|完成|通过/.test(value)) return ' is-done';
  if (/error|fail|blocked|失败|阻断/.test(value)) return ' is-error';
  if (/warn|review|pending|待|候选/.test(value)) return ' is-warning';
  return '';
}

function statusLabel(status, fallback = '待处理') {
  const value = String(status || '').toLowerCase();
  if (/done|complete|success|完成|通过/.test(value)) return '已完成';
  if (/error|fail|blocked|失败|阻断/.test(value)) return '失败';
  if (/warn|review|pending|待|候选/.test(value)) return '待确认';
  return value ? String(status) : fallback;
}

export class WorkbenchUI {
  constructor({ client, documentRef = document, windowRef = window, pollInterval = POLL_INTERVAL_MS } = {}) {
    this.client = client;
    this.document = documentRef;
    this.window = windowRef;
    this.pollInterval = pollInterval;
    this.state = null;
    this.page = 'chapters';
    this.selectedRole = '';
    this.selectedChapter = '';
    this.selectedParagraph = '';
    this.profileOpen = false;
    this.billingOpen = false;
    this.drafts = new Map();
    this.bookSignature = '';
    this.lastPageSignature = '';
    this.lastBillingSignature = '';
    this.pendingRender = false;
    this.pollTimer = null;
    this.polling = false;
    this.stateEpoch = 0;
    this.latestActionId = 0;
    this.pendingActionId = 0;
    this.pendingActionName = '';
    this.destroyed = false;
    this.lastPlaybackCommand = null;
    this.playbackSourceId = '';
    this.playbackStartedId = '';
    this.playbackAutoplayPending = false;
    this.playbackSeekPending = null;
    this.activePlaybackSpeed = 1;
    this.lastPlaybackLabel = '';
    this.priceDraft = null;
    this.importPending = false;
    this.importFile = null;
    this.importTitleDraft = '';
    this.importEnabledStarts = new Set();
    this.importSelectedChapter = '';
    this.importPreviewId = '';
    this.importHeadingDraft = '';
    this.importHeadingSearched = false;
    this.importHeadingSearchedFor = '';
    this.importHeadingAwaiting = '';
    this.importCommitAwaiting = null;
    this.importError = '';
    this.importPreviewSuppressed = false;
    this.importPriorPreviewId = '';
    this.shutdownRequested = false;
    this.root = documentRef.getElementById('workbench');
    this.content = documentRef.getElementById('page-content');
    this.liveStatus = documentRef.getElementById('live-status');
    this.audio = documentRef.getElementById('audio-player');
    this.bindEvents();
  }

  bindEvents() {
    this.root.addEventListener('click', (event) => this.onClick(event));
    this.root.addEventListener('change', (event) => this.onChange(event));
    this.root.addEventListener('input', (event) => this.onInput(event));
    this.root.addEventListener('focusout', () => {
      if (this.pendingRender && !this.renderTimer) {
        this.renderTimer = this.window.setTimeout(() => {
          this.renderTimer = null;
          if (this.pendingRender && !this.shouldDeferRender()) this.renderPage();
        }, 80);
      }
    });
    this.audio.addEventListener('timeupdate', () => this.updateProgress());
    this.audio.addEventListener('durationchange', () => this.updateProgress());
    this.audio.addEventListener('loadedmetadata', () => this.onAudioMetadata());
    this.audio.addEventListener('playing', () => this.onAudioPlaying());
    this.audio.addEventListener('ended', () => this.stopPlayback('音频播放完成。'));
    this.audio.addEventListener('error', () => this.stopPlayback('音频无法读取，请刷新状态后重试。'));
    this.document.getElementById('audio-seek').addEventListener('input', (event) => {
      if (Number.isFinite(this.audio.duration) && this.audio.duration > 0) {
        this.audio.currentTime = (Number(event.target.value) / 1000) * this.audio.duration;
      }
    });
    this.document.getElementById('player-play').addEventListener('click', () => this.playReadyAudio());
    this.document.getElementById('player-stop').addEventListener('click', () => this.stopPlayback());
    this.document.getElementById('playback-speed').addEventListener('change', (event) => this.changePlaybackSpeed(Number(event.target.value)));
    this.document.getElementById('theme-toggle').addEventListener('click', () => this.toggleTheme());
    this.document.getElementById('billing-toggle').addEventListener('click', () => this.toggleBilling());
    this.document.getElementById('billing-close').addEventListener('click', () => this.toggleBilling(false));
    this.document.getElementById('book-select').addEventListener('change', (event) => {
      if (event.target.value) this.runAction('book', { book_id: event.target.value });
    });
    this.document.getElementById('import-book-button').addEventListener('click', () => this.openImport());
    this.document.getElementById('import-file-input').addEventListener('change', (event) => {
      this.importFile = event.target.files?.[0] || null;
      this.importError = '';
      this.importPreviewSuppressed = Boolean(this.importFile);
      if (this.importFile) {
        this.importPriorPreviewId = this.state?.import_preview?.preview_id || '';
      }
      event.target.value = '';
      if (this.page === 'import') this.renderPage();
    });
    this.root.querySelector('.brand').addEventListener('click', (event) => this.onBrandClick(event));
    this.root.querySelectorAll('[data-page]').forEach((button) => button.addEventListener('click', () => this.setPage(button.dataset.page)));
    this.restoreTheme();
  }

  start() {
    return this.refreshState({ initial: true }).finally(() => this.schedulePoll());
  }

  destroy() {
    this.destroyed = true;
    if (this.pollTimer) this.window.clearTimeout(this.pollTimer);
    this.audio.pause();
    this.audio.removeAttribute('src');
  }

  schedulePoll() {
    if (this.destroyed || this.shutdownRequested) return;
    if (this.pollTimer) this.window.clearTimeout(this.pollTimer);
    this.pollTimer = this.window.setTimeout(async () => {
      this.pollTimer = null;
      if (!this.polling) await this.refreshState();
      this.schedulePoll();
    }, this.pollInterval);
  }

  async refreshState({ initial = false } = {}) {
    if (this.polling || this.destroyed) return;
    this.polling = true;
    const capturedEpoch = this.stateEpoch;
    const capturedActionId = this.latestActionId;
    try {
      const state = await this.client.getState();
      if (capturedEpoch !== this.stateEpoch || capturedActionId !== this.latestActionId) return;
      this.applyState(state);
      if (initial) this.setLiveStatus('工作台已连接。');
    } catch (error) {
      this.setLiveStatus(error.message || '读取本机状态失败。', true);
    } finally {
      this.polling = false;
    }
  }

  applyState(state) {
    if (!state || typeof state !== 'object') return;
    const previousBook = this.state?.book_id || '';
    const previousChapter = this.state?.editor?.chapter_id || this.selectedChapter || '';
    const previousTaskState = this.state ? JSON.stringify([this.state.status, this.state.error, this.state.busy]) : '';
    const previousProfiles = new Map((this.state?.profiles || []).map((profile) => [String(profile.id), profile.profile_revision]));
    const wasBusy = Boolean(this.state?.busy);
    const previousPreviewId = this.state?.import_preview?.preview_id || '';
    this.state = state;
    this.observeImportCommitState(state);
    this.observeImportHeadingState(state);
    if (this.importPreviewSuppressed && state.import_preview?.preview_id
      && state.import_preview.preview_id !== this.importPriorPreviewId
      && state.busy?.kind !== 'import_preview') this.importPreviewSuppressed = false;
    const preview = this.importPreviewSuppressed ? null : state.import_preview;
    if (preview?.preview_id && preview.preview_id !== previousPreviewId && preview.preview_id !== this.importPreviewId) {
      this.importPreviewId = preview.preview_id;
      this.importTitleDraft = preview.existing_name || preview.title || preview.filename?.replace(/\.[^.]+$/, '') || '';
      this.importEnabledStarts = new Set((preview.chapters || []).map((chapter) => Number(chapter.start)));
      this.importSelectedChapter = String(preview.chapters?.[0]?.id || '');
      this.importHeadingDraft = '';
      this.importHeadingSearched = false;
      this.importHeadingSearchedFor = '';
      this.importError = '';
    }
    if (previousBook === (state.book_id || '')) {
      for (const profile of state.profiles || []) {
        const roleId = String(profile.id);
        if (previousProfiles.has(roleId) && previousProfiles.get(roleId) !== profile.profile_revision) {
          const key = this.voiceDraftKey(roleId);
          this.drafts.delete(key);
          const timer = this.draftTimers?.get(key);
          if (timer) { this.window.clearTimeout(timer); this.draftTimers.delete(key); }
        }
      }
    }
    const nextTaskState = JSON.stringify([state.status, state.error, state.busy]);
    if (previousTaskState && previousTaskState !== nextTaskState) this.clientError = '';
    if (!wasBusy && state.busy) this.cancelVoiceDraftTimers();
    if (previousBook !== (state.book_id || '')) {
      this.cancelVoiceDraftTimers();
      this.selectedRole = '';
      this.selectedParagraph = '';
      this.selectedChapter = '';
      this.selectedSpeaker = null;
      this.profileOpen = false;
      this.lastPlaybackCommand = null;
      this.lastPlaybackLabel = '';
      this.playbackAutoplayPending = false;
      this.playbackSeekPending = null;
      this.activePlaybackSpeed = 1;
    }
    if (previousChapter !== (state.editor?.chapter_id || '')) {
      this.selectedParagraph = '';
      this.selectedSpeaker = null;
    }
    this.selectedChapter = state.editor?.chapter_id || this.selectedChapter || '';
    this.syncBooks(state);
    this.updateNavigation();
    const pageSignature = this.pageStateSignature();
    if (pageSignature !== this.lastPageSignature) {
      if (this.page === 'import') this.renderPage();
      else if (this.shouldDeferRender()) {
        this.pendingRender = true;
        this.syncBusyControls();
      } else this.renderPage();
    }
    this.renderBilling();
    this.syncPlayback(state);
    if (state.error) this.setLiveStatus(String(state.error), true);
    else if (this.clientError) this.setLiveStatus(this.clientError, true);
    else if (state.status) this.setLiveStatus(String(state.status));
  }

  syncBooks(state) {
    const select = this.document.getElementById('book-select');
    const books = Array.isArray(state.books) ? state.books : [];
    const signature = JSON.stringify(books.map((book) => [book.id, book.name]));
    if (signature !== this.bookSignature) {
      this.bookSignature = signature;
      select.replaceChildren();
      const placeholder = node('option', '', books.length ? '选择书籍' : '暂无可用书籍');
      placeholder.value = '';
      select.append(placeholder);
      for (const book of books) {
        const option = node('option', '', book.name || book.id);
        option.value = String(book.id ?? '');
        select.append(option);
      }
    }
    if (this.document.activeElement !== select) select.value = String(state.book_id || '');
    select.disabled = !books.length || this.isBusy();
    const importButton = this.document.getElementById('import-book-button');
    importButton.disabled = this.isBusy();
    this.document.getElementById('shutdown-button').disabled = this.isBusy();
  }

  updateNavigation() {
    this.root.querySelectorAll('[data-page]').forEach((button) => {
      const active = button.dataset.page === this.page;
      button.classList.toggle('is-active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
  }

  setPage(page) {
    if (!['chapters', 'editor', 'voices', 'import'].includes(page)) return;
    this.page = page;
    this.updateNavigation();
    this.renderPage();
  }

  openImport() {
    if (this.isBusy()) {
      this.setLiveStatus('当前任务运行中，完成后再导入新书。', true);
      return;
    }
    this.importError = '';
    this.setPage('import');
  }

  onBrandClick(event) {
    event.preventDefault();
    this.setPage('chapters');
  }

  renderPage() {
    if (!this.state) return;
    const focus = this.captureFocus();
    const scrollTop = this.window.scrollY || 0;
    this.content.replaceChildren();
    this.content.dataset.bookId = String(this.state.book_id || '');
    this.content.dataset.chapterId = this.page === 'editor' ? String(this.state.editor?.chapter_id || '') : '';
    if (this.page === 'import') this.content.append(this.renderImport());
    else if (this.page === 'chapters') this.content.append(this.renderChapters());
    else if (this.page === 'editor') this.content.append(this.renderEditor());
    else this.content.append(this.renderVoices());
    this.restoreFocus(focus);
    this.lastPageSignature = this.pageStateSignature();
    this.pendingRender = false;
    this.window.scrollTo?.(0, scrollTop);
  }

  pageStateSignature() {
    const state = this.state || {};
    const busy = state.busy ? [state.busy.kind, state.busy.label] : null;
    if (this.page === 'import') return JSON.stringify([state.import_preview, busy, this.importPending, this.importError, this.importFile?.name, this.importFile?.size]);
    if (this.page === 'chapters') return JSON.stringify([state.books, state.book_id, state.book_name, state.range, state.chapters, state.diagnostics, busy, this.selectedChapter]);
    if (this.page === 'editor') return JSON.stringify([state.book_id, state.book_name, state.chapters, state.editor, state.diagnostics, busy]);
    return JSON.stringify([state.book_id, state.book_name, state.selected_role, state.profiles, busy]);
  }

  shouldDeferRender() {
    const active = this.document.activeElement;
    if (!active || !this.content.contains(active)) return false;
    if (active.matches?.('input, textarea, select')) {
      try { if (active.matches('select:open')) return true; } catch { /* Older engines do not support the open-state pseudo-class. */ }
      return true;
    }
    return false;
  }

  syncBusyControls() {
    if (!this.isBusy()) return;
    this.content.querySelectorAll('textarea[data-draft-key], [data-range-start], [data-range-end], [data-speaker-select], [data-import-title], [data-import-heading], [data-import-start], [data-local="import_chapter"], [data-local="add_import_heading"]').forEach((control) => { control.disabled = true; });
    this.content.querySelectorAll('button[data-action]').forEach((button) => {
      if (!BUSY_SAFE_ACTIONS.has(button.dataset.action)) button.disabled = true;
    });
    this.updatePlaybackControls();
  }

  captureFocus() {
    const active = this.document.activeElement;
    if (!active || !this.content.contains(active)) return null;
    const key = active.dataset?.draftKey || active.dataset?.focusKey;
    if (!key) return null;
    return { key, start: active.selectionStart, end: active.selectionEnd };
  }

  restoreFocus(bookmark) {
    if (!bookmark) return;
    const target = [...this.content.querySelectorAll('[data-draft-key], [data-focus-key]')]
      .find((item) => item.dataset.draftKey === bookmark.key || item.dataset.focusKey === bookmark.key);
    if (!target) return;
    target.focus({ preventScroll: true });
    if (Number.isInteger(bookmark.start) && typeof target.setSelectionRange === 'function') {
      target.setSelectionRange(bookmark.start, bookmark.end);
    }
  }

  renderChapters() {
    const state = this.state;
    const busy = state.busy;
    const wrapper = node('section', 'page-section');
    wrapper.append(heading('章节制作', state.book_name || '选择一本书，查看已登记章节与制作状态。'));
    const range = state.range || {};
    const toolbar = node('div', 'book-toolbar');
    const chapterRows = Array.isArray(state.chapters) ? state.chapters : [];
    const startIndex = chapterRows.findIndex((item) => String(item.id) === String(range.start || ''));
    const endIndex = chapterRows.findIndex((item) => String(item.id) === String(range.end || ''));
    const visibleRows = startIndex >= 0 && endIndex >= startIndex ? chapterRows.slice(startIndex, endIndex + 1) : [];
    const rangeLabel = (item) => item.title ? `${item.id} · ${item.title}` : item.name || item.id;
    const startSelect = selectControl('起始章节', chapterRows.map((item) => ({ value: item.id, label: rangeLabel(item) })), range.start, { className: 'range-field', dataKey: 'rangeStart' });
    const endSelect = selectControl('结束章节', chapterRows.map((item) => ({ value: item.id, label: rangeLabel(item) })), range.end, { className: 'range-field', dataKey: 'rangeEnd' });
    startSelect.dataset.focusKey = 'range-start'; endSelect.dataset.focusKey = 'range-end';
    startSelect.disabled = this.isBusy(); endSelect.disabled = this.isBusy();
    toolbar.append(field('范围起点', startSelect), field('范围终点', endSelect));
    const toolbarActions = node('div', 'actions');
    toolbarActions.append(actionButton('读取章节目录', 'index', { className: 'button', disabled: this.isBusy() || !state.book_id }));
    toolbarActions.append(actionButton('读取进度', 'progress', { className: 'button button-quiet', disabled: this.isBusy() || !range.start || !range.end }));
    toolbarActions.append(actionButton('分析人物', 'profiles_analyze', { className: 'button', disabled: this.isBusy() || !state.book_id || !range.start || !range.end }));
    toolbarActions.append(actionButton('开始配音', 'execute', { className: 'button button-primary', disabled: this.isBusy() || !state.book_id || !range.start || !range.end }));
    if (busy?.kind === 'execute') toolbarActions.append(actionButton('暂停任务', 'pause', { className: 'button button-quiet button-danger' }));
    toolbar.append(toolbarActions);
    wrapper.append(toolbar);

    const grid = node('div', 'page-grid');
    const list = card('章节列表');
    if (!state.book_id) list.append(emptyState('先选择一本书', '选择书籍后可以读取本机章节目录、查看制作进度和试听已完成音频。'));
    else if (!chapterRows.length) list.append(emptyState('尚未读取章节目录', '读取本机索引后，这里会列出可制作章节。', actionButton('读取章节目录', 'index', { className: 'button button-primary', disabled: this.isBusy() })));
    else if (!visibleRows.length) list.append(emptyState('选择显示范围', '使用起始与结束章节下拉框定位要查看的章节。'));
    else {
      const table = node('div', 'chapter-table');
      table.tabIndex = 0;
      table.setAttribute('aria-label', `当前范围内 ${visibleRows.length} 个章节`);
      for (const chapter of visibleRows) table.append(this.renderChapterRow(chapter));
      list.append(table);
      list.querySelector('.surface-head')?.append(node('span', 'small-copy', `${visibleRows.length} 章`));
    }
    const side = node('aside', 'stack');
    const task = node('section', 'task-panel');
    task.append(node('h2', '', busy?.label || (state.book_name ? '本机制作' : '开始使用')));
    task.append(node('p', 'muted', busy ? '任务运行中；章节与书籍选择暂时锁定。' : '选择范围后读取目录、分析人物或开始配音。'));
    if (busy?.kind === 'execute') task.append(actionButton('暂停当前任务', 'pause', { className: 'button button-warm' }));
    side.append(task);
    const stages = node('section', 'surface');
    const stagesHead = node('div', 'surface-head'); stagesHead.append(node('h2', '', '制作阶段')); stages.append(stagesHead);
    const stageList = node('div', 'stage-list');
    const selected = visibleRows.find((item) => String(item.id) === String(this.selectedChapter)) || visibleRows[0];
    const completedStages = new Map(stageEntries(selected?.stages).map((stage) => [stage.id || stage.label, stage]));
    const stageNames = [['analyze', '对白分析'], ['memory', '人物与记忆'], ['stage', '配音计划'], ['render', '语音生成']];
    for (const [id, label] of stageNames) {
      const stage = completedStages.get(id) || completedStages.get(label);
      const done = Boolean(stage);
      const row = node('div', 'stage-row');
      const mark = node('span', `stage-mark${done ? ' is-done' : ' is-pending'}`); mark.append(done ? svgIcon('m5 12 4 4L19 6') : svgIcon('M12 5v14M5 12h14'));
      row.append(mark, node('span', '', label), node('span', 'small-copy', done ? '已完成' : '待处理'));
      stageList.append(row);
    }
    stages.append(stageList); side.append(stages);
    if (state.diagnostics) side.append(this.renderDiagnostics(state.diagnostics));
    else if (selected?.diagnostics) side.append(this.renderDiagnostics(selected.diagnostics));
    grid.append(list, side); wrapper.append(grid);
    return wrapper;
  }

  renderImport() {
    const state = this.state;
    const preview = this.importPreviewSuppressed ? null : state.import_preview;
    const wrapper = node('section', 'page-section import-page');
    wrapper.append(heading('导入新书', '选择 TXT 或 DOCX，核对章节边界后登记到本机工作台。'));
    wrapper.append(node('p', 'import-local-note', '在本机解析，点击开始配音后才分析与合成。'));
    const form = node('section', 'import-workspace');
    const choose = node('div', 'import-file-row');
    const fileLabel = this.importFile ? `${this.importFile.name} · ${Math.ceil(this.importFile.size / 1024)} KB` : (preview?.filename || '尚未选择文件');
    choose.append(node('div', 'import-file-copy', fileLabel));
    choose.append(actionButton(this.importFile ? '更换文件' : '选择 TXT / DOCX', 'local_choose_import_file', { className: 'button' }));
    form.append(choose);
    if (this.importFile && this.importFile.size > 64 * 1024 * 1024) {
      form.append(node('p', 'error-copy', '文件超过 64 MiB，请选择较小的 TXT 或 DOCX。'));
    }
    const previewButton = actionButton(this.importPending ? '正在读取并预览…' : '读取并预览', 'local_import_preview', {
      className: 'button button-primary', disabled: this.isBusy() || !this.importFile || this.importFile.size > 64 * 1024 * 1024,
    });
    form.append(previewButton);
    if (this.importError) form.append(node('p', 'error-copy', this.importError));
    if (!preview && this.importPreviewSuppressed && state.import_preview) {
      form.append(actionButton('恢复上次预览', 'local_restore_import_preview', { className: 'button button-quiet', disabled: this.isBusy() }));
    }
    if (state.busy?.kind === 'import_preview' || this.importPending) {
      form.append(node('p', 'small-copy import-progress', state.busy?.label || '正在本机解析文件…'));
    }
    wrapper.append(form);

    if (preview) {
      const existingBook = Boolean(preview.existing_book);
      const details = node('section', 'import-preview');
      details.append(node('h2', '', '书名与章节预览'));
      details.append(field('书名', this.importTitleInput({ disabled: existingBook || this.isBusy() }), existingBook ? '这份文件对应的书目已在本机登记。' : '登记时若名称重复，工作台会自动添加区分后缀。'));
      if (existingBook) details.append(node('p', 'import-existing-note', `这份文件已登记，可直接使用已有书目${preview.existing_name ? `「${preview.existing_name}」` : ''}。`));
      details.append(node('p', 'import-meta', `${preview.character_count ?? 0} 字 · ${preview.chapters?.length || 0} 个章节候选 · ${preview.encoding || 'DOCX 正文'}`));
      if (preview.warnings?.length) {
        const warnings = node('ul', 'import-warnings');
        for (const warning of preview.warnings) warnings.append(node('li', '', warning));
        details.append(warnings);
      }
      const preface = node('p', 'import-preface');
      preface.append(node('span', '', '合并误识别标题只调整章节边界，原文与开头前言都会保留。'));
      details.append(preface);
      const layout = node('div', 'import-chapter-layout');
      const list = node('div', 'import-chapter-list');
      const chapters = this.importChapters(preview);
      if (!chapters.length) list.append(emptyState('没有识别到章节标题', '你仍可登记文件；后续请先核对章节索引再开始制作。'));
      for (const chapter of chapters) {
        const row = node('article', `import-chapter${String(chapter.id) === this.importSelectedChapter ? ' is-selected' : ''}`);
        const checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.checked = this.importEnabledStarts.has(Number(chapter.start));
        checkbox.dataset.importStart = String(chapter.start); checkbox.disabled = existingBook || this.isBusy() || Number(chapter.start) === Number(chapters[0]?.start); checkbox.setAttribute('aria-label', `将“${chapter.title || chapter.id}”作为章节起点`);
        const select = node('button', 'import-chapter-title', chapter.title || chapter.id || '未命名章节');
        select.type = 'button'; select.disabled = this.isBusy(); select.dataset.local = 'import_chapter'; select.dataset.chapterId = String(chapter.id || '');
        const meta = node('span', 'import-chapter-meta', `${Number(chapter.characters || 0).toLocaleString()} 字`);
        row.append(checkbox, select, meta); list.append(row);
      }
      const excerpt = node('section', 'import-excerpt');
      excerpt.append(node('h3', '', '章节摘录'));
      const active = chapters.find((chapter) => String(chapter.id) === this.importSelectedChapter) || chapters[0];
      excerpt.append(node('p', 'import-excerpt-copy', active?.excerpt || '选择左侧章节查看摘录。'));
      layout.append(list, excerpt); details.append(layout);
      const headingSearch = node('div', 'import-heading-search');
      const headingField = this.importHeadingInput();
      if (!existingBook) {
        headingSearch.append(field('新增章节标题', headingField, '输入原文中完整、独占一行的标题；查找按去除首尾空白后的整行精确匹配。'));
        headingSearch.append(actionButton('查找原文标题', 'import_heading', {
          className: 'button', disabled: this.isBusy() || !this.importHeadingDraft.trim(),
        }));
      }
      const showMatches = this.importHeadingSearched && this.importHeadingSearchedFor === this.importHeadingDraft.trim();
      const matches = showMatches && Array.isArray(preview.heading_matches) ? preview.heading_matches : [];
      if (matches.length) {
        const matchList = node('div', 'import-match-list');
        for (const match of matches) {
          const matchRow = node('article', 'import-match-row');
          matchRow.append(node('div', '', match.title || '未命名标题'), node('span', 'import-chapter-meta', `${Number(match.characters || 0).toLocaleString()} 字`));
          const added = this.importEnabledStarts.has(Number(match.start));
          const add = actionButton(added ? '已加入章界' : '加入章节起点', 'noop', {
            className: added ? 'button button-quiet' : 'button', disabled: added || this.isBusy(),
            payload: { start: Number(match.start), chapter_id: String(match.id || ''), title: match.title || '', excerpt: match.excerpt || '', characters: Number(match.characters || 0) },
          });
          add.dataset.local = 'add_import_heading';
          matchRow.append(add);
          matchList.append(matchRow);
        }
        headingSearch.append(matchList);
      } else if (showMatches) {
        headingSearch.append(node('p', 'small-copy', '未找到独立成行的完整标题。当前章界草稿已保留。'));
      }
      details.append(headingSearch);
      details.append(actionButton(existingBook ? '进入已有书籍' : '登记并进入制作', 'import_commit', {
        className: 'button button-primary', disabled: this.isBusy() || !String(this.importTitleDraft || '').trim(),
        payload: { preview_id: preview.preview_id },
      }));
      wrapper.append(details);
    }
    wrapper.append(actionButton('返回章节', 'local_page', { className: 'button button-quiet', payload: { page: 'chapters' } }));
    return wrapper;
  }

  importTitleInput({ disabled = false } = {}) {
    const input = node('input', 'text-input'); input.type = 'text'; input.value = this.importTitleDraft;
    input.maxLength = 160; input.dataset.importTitle = 'true'; input.dataset.focusKey = 'import-title'; input.disabled = disabled; input.setAttribute('aria-label', '书名');
    return input;
  }

  importHeadingInput() {
    const input = node('input', 'text-input'); input.type = 'text'; input.value = this.importHeadingDraft;
    input.maxLength = 240; input.disabled = this.isBusy(); input.dataset.importHeading = 'true'; input.dataset.focusKey = 'import-heading'; input.setAttribute('aria-label', '新增章节标题');
    return input;
  }

  importChapters(preview) {
    const chapters = Array.isArray(preview?.chapters) ? [...preview.chapters] : [];
    const known = new Set(chapters.map((chapter) => Number(chapter.start)));
    for (const match of preview?.heading_matches || []) {
      const start = Number(match.start);
      if (this.importEnabledStarts.has(start) && !known.has(start)) chapters.push(match);
    }
    return chapters.sort((a, b) => Number(a.start) - Number(b.start));
  }

  renderChapterRow(chapter) {
    const row = node('div', `chapter-row${String(chapter.id) === String(this.selectedChapter) ? ' is-selected' : ''}`);
    const chapterLabel = chapter.title ? `${chapter.id} · ${chapter.title}` : chapter.name || chapter.id || '未命名章节';
    const open = node('button', 'button button-quiet chapter-title', chapterLabel);
    open.type = 'button'; open.disabled = this.isBusy(); open.dataset.action = 'chapter'; open.dataset.payload = JSON.stringify({ chapter_id: chapter.id });
    row.append(open);
    const stages = node('div', 'chapter-stages');
    for (const stage of stageEntries(chapter.stages)) {
      const tag = node('span', `stage-tag${stageClass(stage.status)}`, stage.label);
      tag.title = statusLabel(stage.status, stage.label);
      stages.append(tag);
    }
    row.append(stages);
    const meta = node('div', 'chapter-meta');
    const chapterStatus = node('span', '', valueText(chapter.status, '未开始'));
    if (chapter.history === true) chapterStatus.append(node('span', 'small-copy', ' · 历史成品'));
    meta.append(chapterStatus);
    const actions = node('div', 'actions');
    if (chapter.audio) actions.append(actionButton('试听', 'chapter_play', { className: 'button button-quiet', disabled: this.isBusy(), payload: { chapter_id: chapter.id, speed: this.currentSpeed() } }));
    actions.append(actionButton('校听', 'chapter', { className: 'button button-quiet', disabled: this.isBusy(), payload: { chapter_id: chapter.id } }));
    actions.append(actionButton('诊断', 'diagnostics', { className: 'button button-quiet', disabled: this.isBusy(), payload: { chapter_id: chapter.id } }));
    meta.append(actions); row.append(meta);
    row.addEventListener('focusin', () => { this.selectedChapter = String(chapter.id); });
    return row;
  }

  renderDiagnostics(diagnostics) {
    const section = card('诊断记录');
    const list = node('div', 'diagnostic-list');
    list.tabIndex = 0; list.setAttribute('aria-label', '诊断记录');
    const expectedBook = String(this.state?.book_id || '');
    const expectedChapter = String(this.page === 'editor' ? this.state?.editor?.chapter_id || '' : this.selectedChapter || '');
    if ((diagnostics?.book_id && String(diagnostics.book_id) !== expectedBook) ||
        (diagnostics?.chapter_id && String(diagnostics.chapter_id) !== expectedChapter)) {
      list.append(node('p', 'small-copy', '这份诊断属于其他书籍或章节，当前已隐藏。'));
      section.append(list);
      return section;
    }
    const sourceStatus = diagnostics?.status === 'verified' ? '来源核验状态：已核验' :
      diagnostics?.status === 'not_started' ? '来源核验状态：尚未开始' :
      diagnostics?.status === 'missing_auto_pointer' ? '来源核验状态：缺少 auto 指针' :
      `来源核验状态：${valueText(diagnostics?.status, '未知')}`;
    list.append(node('p', 'diagnostic-status', sourceStatus));
    if (diagnostics?.profile_registry_status) list.append(node('p', 'diagnostic-status', `人物档案快照：${diagnostics.profile_registry_status}`));
    if (diagnostics?.profile_blocked !== undefined) list.append(node('p', 'diagnostic-status', `人物档案阻断：${diagnostics.profile_blocked ? '是' : '否'}`));
    const groups = [
      ['blocking_conflicts', '阻断冲突'], ['unresolved', '未解决事项'], ['narrator_only', '仅旁白项'],
      ['literal_evidence_downgrades', '字面证据降级'], ['profile_conflicts', '人物档案冲突'],
      ['alias_conflicts', '别名冲突'], ['unresolved_profiles', '待确认人物'],
    ];
    for (const [key, label] of groups) {
      const rows = Array.isArray(diagnostics?.[key]) ? diagnostics[key] : [];
      const group = node('section', 'diagnostic-group');
      group.append(node('h3', '', `${label} · ${rows.length}`));
      if (!rows.length) group.append(node('p', 'small-copy', '无记录'));
      for (const item of rows) {
        if (typeof item === 'string') { group.append(node('p', 'diagnostic-row', item)); continue; }
        const parts = [];
        for (const fieldName of ['reason', 'block_id', 'entity_id', 'character', 'alias', 'profile', 'type', 'message', 'quote', 'source_range']) {
          const value = item?.[fieldName];
          if (value !== null && value !== undefined && value !== '') parts.push(`${fieldName}：${Array.isArray(value) ? value.join('、') : value}`);
        }
        group.append(node('p', 'diagnostic-row', parts.join(' · ') || '记录详情不可显示'));
      }
      list.append(group);
    }
    section.append(list);
    return section;
  }

  renderEditor() {
    const state = this.state;
    const editor = state.editor;
    const section = node('section', 'page-section');
    const chapter = (state.chapters || []).find((item) => String(item.id) === String(editor?.chapter_id || this.selectedChapter));
    const title = chapter?.name || chapter?.title || editor?.chapter_id || '校听';
    const headingNode = heading(`${title} · 校听`, '选择段落并调整说话人或朗读指令。');
    const headingActions = node('div', 'actions');
    if (editor?.chapter_id) {
      headingActions.append(actionButton('试听当前章节', 'chapter_play', { disabled: this.isBusy(), payload: { chapter_id: editor.chapter_id, speed: this.currentSpeed() } }));
    }
    if (editor?.adopted || editor?.adopted_revision_id) headingActions.append(actionButton('恢复原章', 'revision_restore', { className: 'button', disabled: this.isBusy() }));
    headingNode.append(headingActions); section.append(headingNode);
    section.append(node('p', 'editor-banner', '修订基于原章，每章选用一个版本；试听为整章。'));
    if (editor?.candidate_error) section.append(node('p', 'editor-banner', `修订候选已失效：${editor.candidate_error}`));
    if (!state.book_id) {
      section.append(emptyState('先选择一本书', '选择书籍后读取章节，再进入校听与段落修订。'));
      return section;
    }
    if (!editor) {
      section.append(emptyState('选择要校听的章节', '从章节页选择一章，工作台会读取该章段落和已验证音频。', actionButton('返回章节', 'local_page', { className: 'button button-primary', payload: { page: 'chapters' } })));
      return section;
    }
    if (state.diagnostics) section.append(this.renderDiagnostics(state.diagnostics));
    const layout = node('div', 'editor-layout');
    const chapterNav = node('nav', 'editor-chapter-nav');
    chapterNav.tabIndex = 0; chapterNav.setAttribute('aria-label', '章节导航');
    for (const item of state.chapters || []) {
      const navButton = node('button', `button${String(item.id) === String(editor.chapter_id) ? ' is-active' : ''}`, item.name || item.title || item.id);
      navButton.type = 'button'; navButton.dataset.action = 'chapter'; navButton.disabled = this.isBusy();
      navButton.dataset.payload = JSON.stringify({ chapter_id: item.id });
      chapterNav.append(navButton);
    }
    const primary = node('div', 'editor-content');
    const paragraphs = Array.isArray(editor.paragraphs) ? editor.paragraphs : [];
    const transcript = card('段落');
    if (!paragraphs.length) transcript.append(emptyState('本章没有可展示段落', '刷新章节状态或重新选择章节。'));
    else {
      const list = node('div', 'paragraph-list');
      list.tabIndex = 0; list.setAttribute('aria-label', '章节段落');
      for (const paragraph of paragraphs) list.append(this.renderParagraph(paragraph));
      transcript.append(list);
    }
    primary.append(transcript);
    if (editor.candidate) primary.append(this.renderRevisionCandidate(editor));
    const aside = node('aside', 'surface');
    const form = node('div', 'editor-form');
    const current = paragraphs.find((item) => String(item.id) === String(this.selectedParagraph)) || paragraphs[0];
    if (current) this.selectedParagraph = String(current.id);
    const speakers = (editor.speakers || []).map((item) => ({ value: item.id, label: item.name || item.id }));
      const candidateSpeaker = String(editor.candidate?.paragraph_id || '') === String(current?.id || '')
        ? (editor.candidate?.speaker_id || editor.candidate?.speaker_override_id || '') : '';
    const chosenSpeaker = this.selectedSpeaker?.paragraphId === String(current?.id || '')
      ? this.selectedSpeaker.value : (candidateSpeaker || current?.speaker_id || '');
    const speakerSelect = selectControl('说话人', speakers, chosenSpeaker, { dataKey: 'speakerSelect' });
    speakerSelect.dataset.paragraphId = String(current?.id || '');
    speakerSelect.dataset.focusKey = 'speaker-select';
    speakerSelect.disabled = this.isBusy();
    form.append(field('说话人', speakerSelect));
    const draftKey = current ? this.revisionDraftKey(editor.chapter_id, current.id) : '';
    const instruct = textArea(this.getDraft(draftKey, current?.instruct || ''), draftKey, { placeholder: '为所选段落补充朗读指令', rows: 4 });
    instruct.disabled = this.isBusy();
    form.append(field('朗读指令', instruct, '应用于当前选中的段落。'));
    form.append(actionButton('生成候选', 'revision_generate', { className: 'button button-primary', disabled: !current || this.isBusy(), payload: { paragraph_id: current?.id, instruction: instruct.value, speaker_id: current?.speaker_id || '' } }));
    const candidateAdopted = Boolean(editor.candidate?.adopted || editor.adopted_revision_id === editor.candidate?.id);
    if (editor.candidate && !candidateAdopted) {
      form.append(actionButton('采用候选', 'revision_adopt', { className: 'button button-warm', disabled: !editor.candidate.auditioned, payload: { candidate_id: editor.candidate.id } }));
      if (!editor.candidate.auditioned) form.append(node('p', 'small-copy', '试听候选整章后才可采用。'));
    }
    if (editor.adopted || editor.adopted_revision_id) form.append(actionButton('恢复原章', 'revision_restore', { className: 'button button-quiet', disabled: this.isBusy() }));
    if (editor.chapter_id) form.append(actionButton('查看本章诊断', 'diagnostics', { className: 'button button-quiet', disabled: this.isBusy(), payload: { chapter_id: editor.chapter_id } }));
    form.append(node('p', 'small-copy', '可从底部播放进度拖到所选段落附近。'));
    aside.append(form);
    layout.append(chapterNav, primary, aside); section.append(layout);
    return section;
  }

  renderParagraph(paragraph) {
    const selected = String(paragraph.id) === String(this.selectedParagraph);
    const article = node('article', `paragraph${selected ? ' is-selected' : ''}`);
    article.dataset.paragraphId = String(paragraph.id ?? '');
    const head = node('div', 'paragraph-head');
    head.append(node('strong', '', paragraph.speaker_name || paragraph.speaker_id || '旁白'));
    const delivery = deliveryLabel(paragraph.delivery);
    head.append(node('span', 'small-copy', [delivery, `段落 ${valueText(paragraph.index, '')}`].filter(Boolean).join(' · ')));
    const copy = node('p', 'paragraph-copy', paragraph.text || '');
    article.append(head, copy);
    article.addEventListener('click', () => {
      this.selectedParagraph = String(paragraph.id);
      this.renderPage();
    });
    return article;
  }

  renderRevisionCandidate(editor) {
    const candidate = editor.candidate;
    const adopted = Boolean(candidate.adopted || editor.adopted_revision_id === candidate.id);
    const box = node('section', `candidate-box${adopted ? ' is-current' : ''}`);
    const top = node('div', 'section-row');
    top.append(node('h2', '', adopted ? '正在使用的修订版本' : '修订候选'));
    top.append(node('span', 'small-copy', candidate.auditioned ? '已试听' : '待试听'));
    box.append(top);
    if (candidate.instruction) box.append(node('p', 'candidate-copy', candidate.instruction));
    if (candidate.statistics && typeof candidate.statistics === 'object') {
      const summary = Object.entries(candidate.statistics).map(([key, value]) => `${key}：${value}`).join(' · ');
      if (summary) box.append(node('p', 'candidate-copy', summary));
    }
    const controls = node('div', 'editor-actions');
    controls.append(actionButton('试听候选', 'revision_play', { className: 'button', disabled: this.isBusy(), payload: { candidate_id: candidate.id, speed: this.currentSpeed() } }));
    if (!adopted) controls.append(actionButton('采用此候选', 'revision_adopt', { className: 'button button-warm', disabled: this.isBusy() || !candidate.auditioned, payload: { candidate_id: candidate.id } }));
    box.append(controls);
    return box;
  }

  renderVoices() {
    const state = this.state;
    const profiles = Array.isArray(state.profiles) ? state.profiles : [];
    const section = node('section', 'page-section');
    section.append(heading('人物与声音', state.book_name || '选择一本书，查看人物档案与按书保存的声音绑定。'));
    const layout = node('div', 'voice-layout');
    const listCard = card('人物列表');
    const list = node('div', 'profile-list');
    list.tabIndex = 0; list.setAttribute('aria-label', '本书人物列表');
    const selected = profiles.find((profile) => String(profile.id) === String(this.selectedRole || state.selected_role)) || profiles[0];
    if (selected && !this.selectedRole) this.selectedRole = String(selected.id);
    for (const profile of profiles) {
      const item = node('button', `profile-item${selected && String(profile.id) === String(selected.id) ? ' is-active' : ''}`);
      item.type = 'button'; item.dataset.roleId = String(profile.id ?? '');
      const mark = node('span', 'profile-mark', String(profile.name || profile.id || '人').slice(0, 1));
      item.append(mark, node('span', 'profile-name', profile.name || profile.id || '未命名人物'));
      list.append(item);
    }
    if (!profiles.length) list.append(emptyState(state.book_id ? '尚无人物档案' : '先选择一本书', state.book_id ? '分析已登记章节后，人物档案会显示在这里。' : '选择书籍后才能读取本书的人物档案。'));
    listCard.append(list);
    const main = node('section', 'surface voice-main');
    if (!selected) {
      if (state.book_id && profiles.length === 0) main.append(actionButton('分析人物档案', 'profiles_analyze', { className: 'button button-primary', disabled: this.isBusy() }));
      main.append(emptyState('选择人物查看声音', '人物信息、来源引文、待确认内容和声音候选会显示在这里。'));
    } else {
      main.append(this.renderVoiceProfile(selected, state));
    }
    layout.append(listCard, main); section.append(layout);
    return section;
  }

  renderVoiceProfile(profile, state) {
    const wrapper = node('div', 'stack');
    const header = node('div', 'voice-heading');
    header.append(node('span', 'voice-mark', String(profile.name || profile.id || '人').slice(0, 2)));
    const copy = node('div', 'voice-heading-copy');
    copy.append(node('h2', '', `${profile.name || profile.id}的声音`));
    copy.append(node('p', '', [profile.gender, profile.age, profile.identity].filter(Boolean).join(' · ') || '人物档案'));
    const aliases = Array.isArray(profile.aliases) ? profile.aliases : [];
    if (aliases.length) copy.append(node('p', 'small-copy', `别名：${aliases.join('、')}`));
    const recordLink = node('button', 'button button-quiet profile-toggle', this.profileOpen ? '收起人物档案' : '查看人物档案');
    recordLink.type = 'button'; recordLink.dataset.local = 'profile-toggle';
    copy.append(recordLink);
    header.append(copy); wrapper.append(header);
    if (profile.id !== 'narrator') {
      const voiceFacts = node('dl', 'profile-facts');
      for (const [label, key] of [['性格', 'personality'], ['说话方式', 'speech_style']]) {
        const fact = node('div', 'fact'); fact.append(node('dt', '', label), node('dd', '', valueText(profile[key]))); voiceFacts.append(fact);
      }
      wrapper.append(voiceFacts);
    }
    const selectedVoice = profile.selected;
    const candidate = profile.candidate;
    const descriptionKey = this.voiceDraftKey(profile.id);
    const currentDescription = this.getDraft(descriptionKey, profile.description || '');
    const editor = node('div', 'voice-editor');
    editor.append(node('h3', '', '声音设计'));
    const description = textArea(currentDescription, descriptionKey, { placeholder: '为人物描述希望的声音特点', rows: 4 });
    description.disabled = this.isBusy();
    editor.append(field('声音描述', description, '修改后会自动保存为本机草稿。'));
    editor.append(actionButton('生成声音候选', 'voice_generate', { className: 'button button-primary', disabled: this.isBusy(), payload: { role_id: profile.id, description: currentDescription } }));
    const voiceOptions = node('div', 'voice-options');
    if (selectedVoice) voiceOptions.append(this.renderVoiceOption(profile, 'selected', selectedVoice, descriptionKey));
    if (candidate) voiceOptions.append(this.renderVoiceOption(profile, 'candidate', candidate, descriptionKey));
    if (voiceOptions.childElementCount) editor.append(voiceOptions);
    wrapper.append(editor);

    const claimsList = claimEntries(profile.claims);
    if (profile.selection_error) wrapper.append(node('p', 'uncertainty', `当前声音读取失败：${profile.selection_error}`));
    const openIssues = [...(Array.isArray(profile.uncertainties) ? profile.uncertainties : []), ...(Array.isArray(profile.issues) ? profile.issues : [])];
    if (openIssues.length) {
      const pending = node('section', 'stack-tight open-issues'); pending.tabIndex = 0; pending.append(node('h3', '', '待确认'));
      for (const item of openIssues) pending.append(node('p', 'uncertainty', typeof item === 'string' ? item : item?.message || item?.label || item?.text || '待确认内容'));
      wrapper.append(pending);
    }
    if (this.profileOpen) {
      const facts = node('section', 'stack-tight'); facts.append(node('h3', '', '人物档案'));
      if (profile.id !== 'narrator') {
        for (const claim of claimsList) facts.append(node('p', 'evidence-quote', `${claim.label}：${claim.value}${claim.qualifier ? `（${claim.qualifier}）` : ''}`));
      }
      const evidenceRows = Array.isArray(profile.evidence) ? profile.evidence : [];
      const seenEvidence = new Set();
      const uniqueEvidence = evidenceRows.filter((item) => {
        const quote = typeof item === 'string' ? item : item?.quote || item?.text || '';
        const key = `${item?.chapter_id || ''}\u0000${item?.source_range || ''}\u0000${quote}`;
        if (!quote || seenEvidence.has(key)) return false;
        seenEvidence.add(key); return true;
      });
      if (uniqueEvidence.length) {
        const evidence = node('section', 'evidence-block'); evidence.tabIndex = 0; evidence.setAttribute('aria-label', '来源引文');
        evidence.append(node('h3', '', '来源引文'));
        for (const item of uniqueEvidence) {
          const quote = typeof item === 'string' ? item : item?.quote || item?.text || '';
          evidence.append(node('p', 'evidence-quote', `“${quote}”`));
          const source = typeof item === 'object' ? [item.chapter_id, item.source_range].filter(Boolean).join(' · ') : '';
          if (source) evidence.append(node('span', 'small-copy', source));
        }
        facts.append(evidence);
      }
      wrapper.append(facts);
    }
    const correctionKey = this.correctionDraftKey(profile.id);
    const correction = node('div', 'correction-form');
    const correctionText = textArea(this.getDraft(correctionKey, ''), correctionKey, { placeholder: '补充需要更正的人物信息', rows: 3 });
    correctionText.disabled = this.isBusy();
    correction.append(field('纠错说明', correctionText));
    const currentChapter = state.editor?.chapter_id || this.selectedChapter || '';
    correction.append(actionButton('保存说明', 'correction_save', { className: 'button button-quiet', disabled: !currentChapter || this.isBusy(), payload: { chapter_id: currentChapter, role_id: profile.id, text: correctionText.value } }));
    wrapper.append(correction);
    return wrapper;
  }

  renderVoiceOption(profile, kind, voice, descriptionKey) {
    const isSelected = kind === 'selected';
    const box = node('section', `voice-candidate${isSelected ? ' is-selected' : ' is-proposed'}`);
    const header = node('div', 'voice-candidate-head');
    header.append(node('h3', '', isSelected ? '当前声音' : '声音候选'));
    header.append(node('span', 'small-copy', isSelected ? '已绑定' : (voice.adopted ? '当前使用' : (voice.auditioned ? '已试听' : '待试听'))));
    box.append(header);
    box.append(node('p', 'candidate-copy', isSelected ? '固定声线已绑定。' : (voice.description || '')));
    const controls = node('div', 'actions');
    controls.append(actionButton(isSelected ? '试听当前声音' : '试听候选声音', 'voice_play', {
      className: 'button', disabled: this.isBusy(), payload: { role_id: profile.id, kind, speed: this.currentSpeed() },
    }));
    if (!isSelected && !voice.adopted) controls.append(actionButton('采用此声音', 'voice_adopt', {
      className: 'button button-warm', disabled: this.isBusy() || !voice.auditioned,
      payload: { role_id: profile.id, candidate_id: voice.id },
    }));
    box.append(controls);
    return box;
  }

  renderBilling() {
    if (!this.state) return;
    const panel = this.document.getElementById('billing-panel');
    panel.hidden = !this.billingOpen;
    this.document.getElementById('billing-toggle').setAttribute('aria-expanded', String(this.billingOpen));
    if (!this.billingOpen) return;
    const billing = this.state.billing || {};
    const signature = JSON.stringify(billing);
    if (signature === this.lastBillingSignature) return;
    const controls = this.document.getElementById('billing-controls');
    const activePrice = this.document.activeElement?.dataset?.priceField || '';
    const activePriceValue = activePrice ? this.document.activeElement.value : '';
    const selectionStart = activePrice ? this.document.activeElement.selectionStart : null;
    const selectionEnd = activePrice ? this.document.activeElement.selectionEnd : null;
    controls.replaceChildren();
    const prices = billing.prices || {};
    for (const [key, label] of [['input', '输入单价'], ['output', '输出单价']]) {
      const input = node('input', 'text-input'); input.type = 'text'; input.inputMode = 'decimal';
      input.dataset.priceField = key; input.setAttribute('aria-label', label);
      input.value = this.priceDraft?.[key] ?? (activePrice === key ? activePriceValue : String(prices[key] ?? ''));
      controls.append(field(`${label}（元/百万 token）`, input));
    }
    controls.append(actionButton('重算费用', 'billing_reprice', { className: 'button button-quiet' }));
    controls.append(actionButton(billing.loading ? '正在刷新…' : '刷新用量', 'billing_refresh', { className: 'button', disabled: Boolean(billing.loading) }));
    const summary = billing.summary || {};
    const summaryText = billing.loading ? '正在扫描本机用量回执…' :
      `已确认 ${valueText(summary.known_count, '0')} 次 · 未知 ${valueText(summary.unknown_count, '0')} 次 · 重复 ${valueText(summary.duplicate_count, '0')} 次\n输入 ${valueText(summary.input, '0')} / 输出 ${valueText(summary.output, '0')} tokens · 估算 ¥${valueText(summary.estimated_cost, '0')}`;
    setTextContent(this.document.getElementById('billing-summary'), summaryText);
    const list = this.document.getElementById('billing-list'); list.replaceChildren();
    for (const row of billing.rows || []) {
      const line = node('article', 'billing-row');
      line.append(node('span', 'billing-row-label', row.label || '用量记录'), node('span', '', row.cost === null || row.cost === undefined ? '—' : `¥${row.cost}`));
      line.append(node('span', 'billing-row-meta', `输入 ${valueText(row.input)} · 输出 ${valueText(row.output)} · ${valueText(row.status)}`));
      list.append(line);
    }
    const error = this.document.getElementById('billing-error');
    setTextContent(error, billing.error || '');
    error.hidden = !billing.error;
    this.lastBillingSignature = signature;
    if (activePrice) {
      const input = controls.querySelector(`[data-price-field="${activePrice}"]`);
      if (input) {
        input.focus({ preventScroll: true });
        if (Number.isInteger(selectionStart)) input.setSelectionRange(selectionStart, selectionEnd);
      }
    }
  }

  onClick(event) {
    const local = event.target.closest('[data-local]');
    if (local?.dataset.local === 'profile-toggle') {
      this.profileOpen = !this.profileOpen; this.renderPage(); return;
    }
    if (local?.dataset.local === 'import_chapter') {
      this.importSelectedChapter = String(local.dataset.chapterId || '');
      this.renderPage(); return;
    }
    if (local?.dataset.local === 'add_import_heading') {
      let match = {};
      try { match = JSON.parse(local.dataset.payload || '{}'); } catch { match = {}; }
      const start = Number(match.start);
      if (Number.isInteger(start) && start >= 0) {
        this.importEnabledStarts.add(start);
        if (match.chapter_id) this.importSelectedChapter = String(match.chapter_id);
        this.renderPage();
      }
      return;
    }
    const profile = event.target.closest('[data-role-id]');
    if (profile) { this.selectedRole = profile.dataset.roleId; this.profileOpen = false; this.renderPage(); return; }
    const pageButton = event.target.closest('[data-page]');
    if (pageButton) { this.setPage(pageButton.dataset.page); return; }
    const button = event.target.closest('[data-action]');
    if (!button || button.disabled) return;
    let payload = {};
    try { payload = JSON.parse(button.dataset.payload || '{}'); } catch { payload = {}; }
    const action = button.dataset.action;
    if (action === 'local_page') { this.setPage(payload.page); return; }
    if (action === 'local_choose_import_file') {
      this.document.getElementById('import-file-input').click(); return;
    }
    if (action === 'local_restore_import_preview') {
      this.importFile = null;
      this.importPreviewSuppressed = false;
      this.importError = '';
      this.renderPage(); return;
    }
    if (action === 'local_import_preview') { this.runImportPreview(); return; }
    if (action === 'import_heading') {
      payload.preview_id = this.state?.import_preview?.preview_id || '';
      payload.heading = String(this.importHeadingDraft || '').trim();
      this.importHeadingSearched = false;
    }
    if (action === 'import_commit') {
      const preview = this.state?.import_preview;
      Object.assign(payload, buildImportCommitPayload(
        preview?.preview_id || payload.preview_id,
        preview?.existing_book ? (preview.existing_name || preview.title || this.importTitleDraft) : this.importTitleDraft,
        preview?.existing_book ? [] : this.importEnabledStarts,
      ));
    }
    if (action === 'chapter') {
      this.selectedChapter = String(payload.chapter_id || '');
      this.selectedParagraph = '';
      this.page = 'editor'; this.updateNavigation();
    }
    if (action === 'revision_generate') {
      const textarea = this.content.querySelector('[data-draft-key^="revision:"]');
      const speaker = this.content.querySelector('[data-speaker-select]');
      payload.instruction = textarea?.value || '';
      payload.speaker_id = speaker?.value || payload.speaker_id || '';
      if (this.state.editor?.chapter_id) this.selectedChapter = this.state.editor.chapter_id;
    }
    if (action === 'voice_generate') {
      const profile = (this.state.profiles || []).find((item) => String(item.id) === String(payload.role_id));
      payload.description = this.drafts.get(this.voiceDraftKey(payload.role_id)) ?? profile?.description ?? profile?.selected?.description ?? '';
    }
    if (action === 'correction_save') {
      const role = payload.role_id || this.selectedRole || this.state.selected_role || '';
      const key = this.correctionDraftKey(role);
      payload.text = this.drafts.get(key) ?? '';
    }
    if (action === 'billing_reprice') {
      payload.input_price = this.priceDraft?.input ?? this.document.querySelector('[data-price-field="input"]')?.value ?? '';
      payload.output_price = this.priceDraft?.output ?? this.document.querySelector('[data-price-field="output"]')?.value ?? '';
    }
    if (action === 'shutdown') {
      if (this.isBusy()) { this.setLiveStatus('任务运行中，完成或暂停后再退出工作台。', true); return; }
      this.shutdownRequested = true;
      this.setLiveStatus('正在关闭本机工作台…');
      if (this.pollTimer) this.window.clearTimeout(this.pollTimer);
    }
    const identity = {
      bookId: this.content.dataset.bookId || '',
      chapterId: this.content.dataset.chapterId || '',
    };
    if (['voice_play', 'revision_play', 'chapter_play'].includes(action)) {
      this.lastPlaybackCommand = { action, payload: bindActionIdentity(action, payload, identity) };
      this.playbackAutoplayPending = true;
      this.setLiveStatus('正在准备整章音频…');
    }
    this.runAction(action, payload, { identity });
  }

  onChange(event) {
    const target = event.target;
    if (target.matches('[data-import-start]')) {
      const start = Number(target.dataset.importStart);
      if (target.checked) this.importEnabledStarts.add(start);
      else this.importEnabledStarts.delete(start);
      return;
    }
    if (target.matches('[data-range-start], [data-range-end]')) {
      const start = this.content.querySelector('[data-range-start]')?.value || '';
      const end = this.content.querySelector('[data-range-end]')?.value || '';
      if (start && end) this.runAction('range', { start, end });
      return;
    }
    if (target.matches('[data-speaker-select]')) {
      const chapterId = this.state?.editor?.chapter_id;
      const paragraphId = target.dataset.paragraphId;
      this.selectedSpeaker = { paragraphId: String(paragraphId || ''), value: target.value };
      const selected = (this.state?.editor?.paragraphs || []).find((item) => String(item.id) === String(paragraphId));
      if (selected) this.drafts.set(this.revisionDraftKey(chapterId, paragraphId), selected.instruct || '');
      return;
    }
  }

  onInput(event) {
    const target = event.target;
    if (target.matches('[data-import-title]')) {
      this.importTitleDraft = target.value;
      return;
    }
    if (target.matches('[data-import-heading]')) {
      this.importHeadingDraft = target.value;
      if (this.importHeadingDraft.trim() !== this.importHeadingSearchedFor) this.importHeadingSearched = false;
      this.renderPage();
      return;
    }
    if (target.dataset?.draftKey) {
      const key = target.dataset.draftKey;
      const value = target.value;
      this.drafts.set(key, value);
      if (key.startsWith('voice:') && !this.isBusy()) {
        const profile = (this.state?.profiles || []).find((item) => this.voiceDraftKey(item.id) === key);
        if (profile) this.debounceDraft('voice_draft', {
          role_id: profile.id, description: value, expected_book_id: this.state?.book_id || '',
        }, key);
      }
      return;
    }
    if (target.dataset?.priceField) {
      const prices = this.state?.billing?.prices || {};
      const existing = this.priceDraft || { input: String(prices.input ?? ''), output: String(prices.output ?? '') };
      existing[target.dataset.priceField] = target.value;
      this.priceDraft = existing;
    }
  }

  debounceDraft(action, payload, key) {
    const timers = this.draftTimers || (this.draftTimers = new Map());
    if (timers.has(key)) this.window.clearTimeout(timers.get(key));
    const timer = this.window.setTimeout(() => {
      timers.delete(key);
      this.runAction(action, payload, { quiet: true });
    }, 450);
    timers.set(key, timer);
  }

  cancelVoiceDraftTimers() {
    if (!this.draftTimers) return;
    for (const [key, timer] of this.draftTimers) {
      if (key.startsWith('voice:')) {
        this.window.clearTimeout(timer);
        this.draftTimers.delete(key);
      }
    }
  }

  async runAction(action, payload = {}, { quiet = false, identity = null } = {}) {
    if (!this.state && action !== 'book') return;
    if (!quiet) this.clientError = '';
    if (this.isImportBusy()) {
      if (!quiet) this.setLiveStatus('新书正在处理，请等待导入操作完成。', true);
      return;
    }
    if (this.isBusy() && !BUSY_SAFE_ACTIONS.has(action)) {
      if (!quiet) this.setLiveStatus('当前任务运行中，请等待或先暂停任务。', true);
      return;
    }
    const actionId = ++this.latestActionId;
    this.stateEpoch += 1;
    this.pendingActionId = actionId;
    this.pendingActionName = action;
    if (action === 'import_commit') {
      this.importCommitAwaiting = {
        previewId: payload.preview_id,
        bookId: String(this.state?.import_preview?.book_id || ''),
      };
    } else if (action === 'import_heading') {
      this.importHeadingAwaiting = String(payload.heading || '');
      this.importHeadingSearched = false;
      this.importHeadingSearchedFor = '';
      this.importError = '';
    }
    this.updatePlaybackControls();
    if (['import_commit', 'import_heading'].includes(action) && this.page === 'import') {
      this.syncBusyControls();
      if (this.document?.getElementById) this.syncBooks(this.state || {});
    }
    try {
      const context = identity || {
        bookId: this.content.dataset.bookId || this.state?.book_id || '',
        chapterId: this.content.dataset.chapterId || this.state?.editor?.chapter_id || '',
      };
      const boundPayload = bindActionIdentity(action, payload, context);
      const state = await this.client.action(action, boundPayload);
      if (actionId !== this.latestActionId) { this.schedulePoll(); return; }
      this.stateEpoch += 1;
      if (action === 'billing_reprice' || action === 'billing_refresh') this.priceDraft = null;
      this.applyState(state);
      this.pendingActionId = 0;
      this.pendingActionName = '';
      this.updatePlaybackControls();
      if ((action === 'import_commit' || action === 'import_heading') && this.document?.getElementById) this.syncBooks(this.state || {});
      if (action === 'import_commit' && this.page === 'import' && !this.importCommitAwaiting && !this.isBusy()) this.renderPage();
      if (action === 'import_heading' && this.page === 'import') this.renderPage();
      if (action === 'shutdown') {
        try { this.window?.chrome?.webview?.postMessage?.('audiobook-close-window'); } catch { /* Native close bridge is optional. */ }
      }
    } catch (error) {
      if (actionId !== this.latestActionId) { this.schedulePoll(); return; }
      this.stateEpoch += 1;
      this.pendingActionId = 0;
      this.pendingActionName = '';
      if (action === 'import_commit') this.importCommitAwaiting = null;
      if (action === 'import_heading') this.importHeadingAwaiting = '';
      if (action === 'shutdown') {
        this.shutdownRequested = false;
        this.schedulePoll();
      }
      this.clientError = error.message || (quiet ? '草稿保存失败。' : '操作失败。');
      if (action === 'import_commit' || action === 'import_heading') this.importError = this.clientError;
      this.setLiveStatus(this.clientError, true);
      this.updatePlaybackControls();
      if (this.page === 'import') this.renderPage();
      if ((action === 'import_commit' || action === 'import_heading') && this.document?.getElementById) this.syncBooks(this.state || {});
    }
  }

  async runImportPreview() {
    if (this.isBusy()) {
      this.setLiveStatus('当前任务运行中，完成后再读取新书。', true);
      return;
    }
    const file = this.importFile;
    if (!file) {
      this.importError = '请先选择 TXT 或 DOCX 文件。';
      this.renderPage();
      return;
    }
    if (!file.size) {
      this.importError = '文件为空，请选择有正文内容的 TXT 或 DOCX。';
      this.renderPage();
      return;
    }
    if (file.size > 64 * 1024 * 1024) {
      this.importError = '文件超过 64 MiB，请选择较小的文件。';
      this.renderPage();
      return;
    }
    if (!/\.(txt|docx)$/i.test(file.name)) {
      this.importError = '文件格式需为 TXT 或 DOCX。';
      this.renderPage();
      return;
    }
    const actionId = ++this.latestActionId;
    this.stateEpoch += 1;
    this.pendingActionId = actionId;
    this.pendingActionName = 'import_preview';
    this.importPending = true;
    this.importError = '';
    this.renderPage();
    this.syncBooks(this.state || {});
    try {
      const state = await this.client.previewBook(file);
      if (actionId !== this.latestActionId) { this.schedulePoll(); return; }
      this.stateEpoch += 1;
      this.applyState(state);
      this.importPending = false;
      this.pendingActionId = 0;
      this.pendingActionName = '';
      this.importError = state.error || '';
      this.renderPage();
      this.syncBooks(this.state || {});
      this.schedulePoll();
    } catch (error) {
      if (actionId !== this.latestActionId) { this.schedulePoll(); return; }
      this.stateEpoch += 1;
      this.importPending = false;
      this.pendingActionId = 0;
      this.pendingActionName = '';
      this.importError = error.message || '读取文件失败，请检查文件后重试。';
      this.setLiveStatus(this.importError, true);
      this.renderPage();
      this.syncBooks(this.state || {});
    }
  }

  observeImportCommitState(state) {
    const pending = this.importCommitAwaiting;
    if (!pending) return '';
    if (state.busy) return 'pending';
    if (state.error) {
      this.importCommitAwaiting = null;
      this.importError = String(state.error);
      return 'failed';
    }
    if (!state.import_preview && pending.bookId && state.book_id === pending.bookId) {
      this.importCommitAwaiting = null;
      this.importFile = null;
      this.importTitleDraft = '';
      this.importEnabledStarts = new Set();
      this.importPreviewId = '';
      this.importHeadingDraft = '';
      this.importHeadingSearched = false;
      this.importHeadingSearchedFor = '';
      this.importHeadingAwaiting = '';
      this.importPreviewSuppressed = false;
      this.importError = '';
      if (this.page === 'import') this.setPage('chapters');
      return 'complete';
    }
    return 'pending';
  }

  observeImportHeadingState(state) {
    if (!this.importHeadingAwaiting || state.busy) return '';
    if (state.error) {
      this.importHeadingAwaiting = '';
      this.importHeadingSearched = false;
      this.importError = String(state.error);
      return 'failed';
    }
    this.importHeadingSearched = true;
    this.importHeadingSearchedFor = this.importHeadingAwaiting;
    this.importHeadingAwaiting = '';
    return 'complete';
  }

  isImportBusy() { return Boolean(this.importPending || this.importCommitAwaiting || this.importHeadingAwaiting || ['import_preview', 'import_commit', 'import_heading'].includes(this.state?.busy?.kind)); }
  isBusy() { return Boolean(this.state?.busy || this.importPending || this.importCommitAwaiting || this.importHeadingAwaiting || ['import_commit', 'import_heading'].includes(this.pendingActionName)); }
  isMediaLocked() {
    return this.isBusy() || this.pendingActionName === 'stop' ||
      ['chapter_play', 'voice_play', 'revision_play'].includes(this.pendingActionName);
  }
  updatePlaybackControls() {
    const locked = this.isMediaLocked();
    const play = this.document.getElementById('player-play');
    play.disabled = locked || (!this.audio.src && !this.lastPlaybackCommand);
    this.document.getElementById('playback-speed').disabled = locked;
    const seek = this.document.getElementById('audio-seek');
    const duration = Number.isFinite(this.audio.duration) ? this.audio.duration : 0;
    seek.disabled = locked || duration <= 0;
  }
  currentSpeed() { return Number(this.document.getElementById('playback-speed').value || '1'); }
  voiceDraftKey(roleId) { return `voice:${this.state?.book_id || ''}:${roleId || ''}`; }
  correctionDraftKey(roleId) { return `correction:${this.state?.book_id || ''}:${roleId || ''}`; }
  revisionDraftKey(chapterId, paragraphId) { return `revision:${this.state?.book_id || ''}:${chapterId || ''}:${paragraphId || ''}`; }
  getDraft(key, fallback) { return key && this.drafts.has(key) ? this.drafts.get(key) : fallback; }

  setLiveStatus(message, isError = false) {
    setTextContent(this.liveStatus, message || '');
    this.liveStatus.classList.toggle('is-error', Boolean(isError));
  }

  toggleBilling(force = null) {
    this.billingOpen = force === null ? !this.billingOpen : Boolean(force);
    this.renderBilling();
    if (this.billingOpen && this.state && !this.state.billing) this.runAction('billing_refresh');
  }

  toggleTheme() {
    const root = this.document.documentElement;
    const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
    root.dataset.theme = next;
    try { this.window.localStorage.setItem('audiobook-workbench-theme', next); } catch { /* Theme is still active for this session. */ }
  }

  restoreTheme() {
    try {
      const theme = this.window.localStorage.getItem('audiobook-workbench-theme');
      if (theme === 'dark' || theme === 'light') this.document.documentElement.dataset.theme = theme;
    } catch { /* Keep the warm light theme. */ }
  }

  syncPlayback(state) {
    const playback = state.playback;
    const title = this.document.getElementById('player-title');
    const subtitle = this.document.getElementById('player-subtitle');
    const playButton = this.document.getElementById('player-play');
    if (!playback) {
      const preparing = state.busy?.kind === 'playback';
      if (this.playbackSourceId) {
        this.audio.pause(); this.audio.removeAttribute('src'); this.audio.load();
        this.playbackSourceId = ''; this.playbackStartedId = '';
      }
      if (!preparing) {
        this.playbackAutoplayPending = false;
        this.playbackSeekPending = null;
        this.activePlaybackSpeed = 1;
      }
      setTextContent(title, preparing ? '正在准备音频' : (this.lastPlaybackLabel || '尚未选择音频'));
      setTextContent(subtitle, preparing ? '正在核验本机音频…' : (this.lastPlaybackCommand ? '停止了播放；按播放可重新准备。' : '试听和校听会显示在这里'));
      playButton.disabled = preparing || !this.lastPlaybackCommand;
      this.document.getElementById('player-state').textContent = preparing ? '正在准备' : (this.lastPlaybackCommand ? '已停止' : '待播放');
      this.document.getElementById('playing-indicator').classList.remove('is-playing');
      this.updateProgress();
      this.updatePlaybackControls();
      return;
    }
    this.lastPlaybackLabel = playback.label || this.lastPlaybackLabel;
    setTextContent(title, playback.label || '本机音频');
    setTextContent(subtitle, '速度处理保留原音高');
    playButton.disabled = false;
    if (String(playback.id) !== String(this.playbackSourceId)) {
      this.playbackSourceId = String(playback.id);
      this.playbackStartedId = '';
      this.audio.pause();
      this.audio.src = mediaUrlWithToken(playback.url, this.client.token, this.window.location.origin);
      this.audio.playbackRate = 1;
      this.activePlaybackSpeed = Number(playback.speed || 1);
      this.audio.load();
      this.document.getElementById('player-state').textContent = '音频已就绪';
      const speed = this.document.getElementById('playback-speed');
      if (playback.speed !== undefined && [...speed.options].some((option) => Number(option.value) === Number(playback.speed))) speed.value = String(playback.speed);
    }
    if (this.playbackAutoplayPending && !state.busy) {
      if (this.playbackSeekPending !== null) this.onAudioMetadata();
      else {
        this.playbackAutoplayPending = false;
        this.tryAutoplay();
      }
    }
    this.updatePlaybackControls();
  }

  onAudioMetadata() {
    if (this.playbackSeekPending !== null && Number.isFinite(this.audio.duration) && this.audio.duration > 0) {
      this.audio.currentTime = Math.min(this.playbackSeekPending, this.audio.duration);
      this.playbackSeekPending = null;
      this.updateProgress();
    }
    if (this.playbackAutoplayPending && this.playbackSeekPending === null && !this.state?.busy && this.state?.playback) {
      this.playbackAutoplayPending = false;
      this.tryAutoplay();
    }
  }

  async tryAutoplay() {
    try { await this.audio.play(); }
    catch {
      this.document.getElementById('player-state').textContent = '音频已就绪，按播放开始';
      this.setLiveStatus('音频已准备好；若浏览器未自动播放，请按底部播放键。');
    }
  }

  async playReadyAudio() {
    if (this.isMediaLocked()) return;
    if (!this.audio.src) {
      if (!this.lastPlaybackCommand || this.isBusy()) return;
      this.playbackAutoplayPending = true;
      this.setLiveStatus('正在重新准备上次选择的音频…');
      await this.runAction(this.lastPlaybackCommand.action, this.lastPlaybackCommand.payload);
      return;
    }
    try { await this.audio.play(); }
    catch (error) { this.setLiveStatus(error.message || '浏览器无法播放这段音频。', true); }
  }

  onAudioPlaying() {
    const playbackId = String(this.state?.playback?.id || '');
    if (!playbackId || playbackId === this.playbackStartedId) return;
    this.playbackStartedId = playbackId;
    this.audio.playbackRate = 1;
    this.document.getElementById('player-state').textContent = '正在播放';
    this.document.getElementById('playing-indicator').classList.add('is-playing');
    this.runAction('playback_started', { media_id: playbackId }, { quiet: true });
  }

  async stopPlayback(message = '') {
    const hadPlayback = Boolean(this.playbackSourceId);
    const preparingPlayback = this.state?.busy?.kind === 'playback' ||
      ['chapter_play', 'voice_play', 'revision_play'].includes(this.pendingActionName);
    this.audio.pause();
    try { this.audio.currentTime = 0; } catch { /* Metadata may not have loaded yet. */ }
    this.playbackStartedId = '';
    this.playbackAutoplayPending = false;
    this.playbackSeekPending = null;
    this.document.getElementById('playing-indicator').classList.remove('is-playing');
    this.document.getElementById('player-state').textContent = message ? '播放结束' : '已停止';
    if (hadPlayback || preparingPlayback) await this.runAction('stop', {}, { quiet: !message });
    if (message) this.setLiveStatus(message, message.includes('无法') || message.includes('请刷新'));
    this.updateProgress();
  }

  changePlaybackSpeed(speed) {
    this.audio.playbackRate = 1;
    if (this.lastPlaybackCommand) this.lastPlaybackCommand.payload.speed = speed;
    if (this.isMediaLocked()) return;
    if (!this.lastPlaybackCommand || !this.state?.playback || this.audio.paused) return;
    const oldSpeed = Number(this.activePlaybackSpeed || this.state.playback.speed || 1);
    this.playbackSeekPending = this.audio.currentTime * oldSpeed / speed;
    this.playbackAutoplayPending = true;
    this.audio.pause();
    const payload = { ...this.lastPlaybackCommand.payload, speed };
    this.lastPlaybackCommand.payload = payload;
    this.runAction(this.lastPlaybackCommand.action, payload);
  }

  updateProgress() {
    const seek = this.document.getElementById('audio-seek');
    const duration = Number.isFinite(this.audio.duration) ? this.audio.duration : 0;
    const current = Number.isFinite(this.audio.currentTime) ? this.audio.currentTime : 0;
    seek.disabled = duration <= 0 || this.isMediaLocked();
    if (duration > 0 && !seek.matches(':active')) seek.value = String(Math.round((current / duration) * 1000));
    setTextContent(this.document.getElementById('audio-current'), formatDuration(current));
    setTextContent(this.document.getElementById('audio-duration'), formatDuration(duration));
  }

}

function initialize() {
  const browserWindow = window;
  let token = '';
  try { token = readSessionToken(browserWindow, browserWindow.sessionStorage); }
  catch (error) {
    const live = document.getElementById('live-status');
    if (live) { live.textContent = error.message || '工作台令牌读取失败。'; live.classList.add('is-error'); }
    return null;
  }
  if (!token) {
    const live = document.getElementById('live-status');
    if (live) { live.textContent = '工作台会话令牌缺失，请从本机工作台启动入口重新打开。'; live.classList.add('is-error'); }
    return null;
  }
  const client = new WorkbenchClient({ token });
  const ui = new WorkbenchUI({ client });
  ui.start();
  return ui;
}

if (typeof document !== 'undefined' && typeof window !== 'undefined') initialize();
