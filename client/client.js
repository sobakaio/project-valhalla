'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function loadPreviewWindowSessions() {
  const empty = () => ({ open: false, displayed: null, geometry: null, geometryReady: false });
  const sessions = { studio: empty(), director: empty(), logger: empty() };
  try {
    const saved = JSON.parse(sessionStorage.getItem('valhalla-floating-previews') || '{}');
    for (const owner of Object.keys(sessions)) {
      const source = saved?.[owner];
      if (!source || typeof source !== 'object') continue;
      sessions[owner] = {
        open: Boolean(source.open),
        displayed: source.displayed && typeof source.displayed.image_url === 'string'
          ? source.displayed : null,
        geometry: source.geometry && ['left', 'top', 'width', 'height'].every(
          (key) => Number.isFinite(Number(source.geometry[key])),
        ) ? Object.fromEntries(['left', 'top', 'width', 'height'].map(
          (key) => [key, Number(source.geometry[key])],
        )) : null,
        geometryReady: Boolean(source.geometryReady),
      };
      if (!sessions[owner].displayed) sessions[owner].open = false;
    }
  } catch { /* ignore invalid per-tab UI state */ }
  return sessions;
}

const GALLERY_CARD_DEFAULT = 230;
const GALLERY_CARD_MIN = 120;
const GALLERY_CARD_MAX = 360;
const storedGalleryCardSize = localStorage.getItem('valhalla-gallery-thumbnail-size');

const state = {
  storyboard: null,
  storyboardOpenSet: 0,
  director: null,
  directorShot: 1,
  directorOpenSet: 0,
  directorOpenGroup: null,
  directorCustomField: null,
  previewJob: null,
  previewDisplayed: null,
  previewWindowGeometry: null,
  previewWindowGeometryReady: false,
  previewWindowOwner: null,
  previewJobOwner: null,
  previewWindowSessions: loadPreviewWindowSessions(),
  previewJobTimer: null,
  job: null,
  jobTimer: null,
  loggerInspection: null,
  outputs: [],
  pendingGroups: [],
  galleryBenchmark: false,
  galleryView: sessionStorage.getItem('valhalla-gallery-view') === 'videos'
    ? 'videos'
    : (sessionStorage.getItem('valhalla-gallery-view') === 'flat' ? 'flat' : 'photoshoots'),
  galleryGroup: sessionStorage.getItem('valhalla-gallery-group') || null,
  galleryCardSize: storedGalleryCardSize !== null && Number.isFinite(Number(storedGalleryCardSize))
    ? Math.min(GALLERY_CARD_MAX, Math.max(GALLERY_CARD_MIN, Number(storedGalleryCardSize)))
    : GALLERY_CARD_DEFAULT,
  flatScrollY: 0,
  flatFocusKey: null,
  deletedOutputs: new Set(),
  renderMode: sessionStorage.getItem('valhalla-render-mode') === 'preview'
    ? 'preview'
    : 'production',
  previewIndex: 0,
  previewOutputIndexes: null,
  previewZoom: Number(sessionStorage.getItem('valhalla-preview-zoom')) || 100,
  previewFit: sessionStorage.getItem('valhalla-preview-fit') !== 'false',
  previewPanX: 0,
  previewPanY: 0,
  slideshowTimer: null,
  slideshowActive: false,
  videoControlsTimer: null,
  videoVolume: 1,
  videoLoop: localStorage.getItem('valhalla-video-loop') !== 'false',
  slideshowRandom: sessionStorage.getItem('valhalla-slideshow-random') === 'true',
  slideshowDelay: Math.min(10, Math.max(1, Number(sessionStorage.getItem('valhalla-slideshow-delay')) || 3)),
  fullscreenControlsTimer: null,
  deleteResolver: null,
  promptShot: null,
  promptTab: 'positive',
  seedResolveTimer: null,
  resolveVersion: 0,
  initialAutoResolved: false,
  pendingStructural: false,
  updateResolver: null,
  theme: sessionStorage.getItem('valhalla-theme') || 'system',
  accent: ['lavender', 'azure', 'rose'].includes(sessionStorage.getItem('valhalla-accent'))
    ? sessionStorage.getItem('valhalla-accent') : 'lavender',
  typeSize: ['small', 'normal', 'large'].includes(sessionStorage.getItem('valhalla-type-size'))
    ? sessionStorage.getItem('valhalla-type-size') : 'normal',
  privacyCovered: localStorage.getItem('valhalla-privacy-covered') === 'true',
  privacyShortcut: ['middle', 'shift-x', 'both'].includes(localStorage.getItem('valhalla-privacy-shortcut'))
    ? localStorage.getItem('valhalla-privacy-shortcut') : 'middle',
  privacyIdleMinutes: Math.max(0, Number(localStorage.getItem('valhalla-privacy-idle-minutes')) || 0),
  privacyIdleOptions: [5, 15],
  workflowProfiles: null,
  workflowProfilesByMedia: { image: null, video: null },
  profileMedia: sessionStorage.getItem('valhalla-profile-media') === 'video' ? 'video' : 'image',
  videoSource: null,
  proofsPositions: (() => {
    try { return JSON.parse(sessionStorage.getItem('valhalla-proofs-positions') || '{}'); }
    catch { return {}; }
  })(),
  restoredView: ['studio', 'director', 'outputs', 'logger'].includes(sessionStorage.getItem('valhalla-active-view'))
    ? sessionStorage.getItem('valhalla-active-view') : 'studio',
};

const form = $('#run-form');
const emptyState = $('#empty-state');
const loadingState = $('#loading-state');
const shotGrid = $('#shot-grid');
const storyboardPanel = $('.storyboard-panel');
const storyboardActions = $('#storyboard-actions');
const storyboardMeta = $('#storyboard-meta');
const imageDialog = $('#image-dialog');
const videoDialog = $('#video-dialog');
const deleteDialog = $('#delete-dialog');
const promptDialog = $("#prompt-dialog");
const directorCustomDialog = $("#director-custom-dialog");
const updateStoryboardDialog = $('#update-storyboard-dialog');
const outputGrid = $('#output-grid');
const jobDock = $('#job-dock');
const OUTPUT_OVERSCAN_ROWS = 3;
const OUTPUT_VIRTUALIZATION_THRESHOLD = 100;
let outputLayout = null;
let outputRenderFrame = null;
let outputRenderSignature = '';
let gallerySizeFrame = null;
let pendingGalleryCardSize = null;
let privacyMiddleClickAt = 0;
const PRIVACY_UNLOCK_DOUBLE_CLICK_MS = 500;
let privacyIdleTimer = null;
let privacyLastActivityAt = Date.now();
let statusRefreshTimer = null;
let statusRefreshActive = false;
let statusRefreshSeconds = 10;

function syncPrivacyControls() {
  $$('[data-privacy-shortcut]').forEach((button) => {
    const active = button.dataset.privacyShortcut === state.privacyShortcut;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  $$('[data-privacy-idle]').forEach((button) => {
    const active = Number(button.dataset.privacyIdle) === state.privacyIdleMinutes;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
}

function applyPrivacyIdleOptions(values) {
  if (!Array.isArray(values) || values.length !== 2) return;
  state.privacyIdleOptions = values.map(Number);
  $$('[data-privacy-idle-option]').forEach((button) => {
    const minutes = state.privacyIdleOptions[Number(button.dataset.privacyIdleOption)];
    button.dataset.privacyIdle = String(minutes);
    button.textContent = String(minutes);
    button.title = `After ${minutes} minutes idle`;
    button.setAttribute('aria-label', `Cover after ${minutes} minutes of inactivity`);
  });
  if (state.privacyIdleMinutes && !state.privacyIdleOptions.includes(state.privacyIdleMinutes)) {
    state.privacyIdleMinutes = 0;
    localStorage.setItem('valhalla-privacy-idle-minutes', '0');
  }
  syncPrivacyControls();
  schedulePrivacyIdleCover();
}

function schedulePrivacyIdleCover() {
  clearTimeout(privacyIdleTimer);
  privacyIdleTimer = null;
  if (state.privacyCovered || !state.privacyIdleMinutes) return;
  const delay = Math.max(0, state.privacyIdleMinutes * 60_000 - (Date.now() - privacyLastActivityAt));
  privacyIdleTimer = setTimeout(() => applyPrivacyCover(true), delay);
}

function notePrivacyActivity() {
  if (state.privacyCovered || !state.privacyIdleMinutes) return;
  const now = Date.now();
  if (now - privacyLastActivityAt < 1000) return;
  privacyLastActivityAt = now;
  schedulePrivacyIdleCover();
}

function stripImageResources() {
  $$('img').forEach((image) => {
    image.removeAttribute('srcset');
    image.removeAttribute('sizes');
    image.removeAttribute('src');
  });
  $$('video').forEach((video) => {
    video.pause();
    video.removeAttribute('src');
    video.load();
  });
}

function applyPrivacyCover(covered, { persist = true } = {}) {
  state.privacyCovered = Boolean(covered);
  document.documentElement.classList.toggle('privacy-covered', state.privacyCovered);
  if (persist) localStorage.setItem('valhalla-privacy-covered', String(state.privacyCovered));
  if (state.privacyCovered) {
    clearTimeout(privacyIdleTimer);
    privacyIdleTimer = null;
    stopSlideshow();
    if (promptDialog.open) promptDialog.close();
    if (videoDialog.open) videoDialog.close();
    if (imageDialog.open) $('#image-viewer-title').textContent = 'Preview';
    stripImageResources();
  }
  outputRenderSignature = '';
  renderVirtualOutputs(true);
  if (!state.privacyCovered) {
    privacyLastActivityAt = Date.now();
    schedulePrivacyIdleCover();
    if (imageDialog.open && state.outputs[state.previewIndex]) showPreview(state.previewIndex);
    if (state.previewDisplayed && !$('#shot-preview-window').classList.contains('hidden')) {
      $('#shot-preview-image').src = `${state.previewDisplayed.image_url}?v=${Date.now()}`;
    }
  }
  renderLogger();
  syncPrivacyControls();
}

function togglePrivacyCover() {
  applyPrivacyCover(!state.privacyCovered);
}

function usesMiddlePrivacyShortcut() {
  return state.privacyShortcut === 'middle' || state.privacyShortcut === 'both';
}

function usesKeyboardPrivacyShortcut() {
  return state.privacyShortcut === 'shift-x' || state.privacyShortcut === 'both';
}

window.addEventListener('pointerdown', (event) => {
  if (event.button !== 1 || !usesMiddlePrivacyShortcut()) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  if (!state.privacyCovered) {
    privacyMiddleClickAt = 0;
    applyPrivacyCover(true);
    return;
  }
  const now = performance.now();
  if (privacyMiddleClickAt && now - privacyMiddleClickAt <= PRIVACY_UNLOCK_DOUBLE_CLICK_MS) {
    privacyMiddleClickAt = 0;
    applyPrivacyCover(false);
  } else {
    privacyMiddleClickAt = now;
  }
}, { capture: true, passive: false });

window.addEventListener('auxclick', (event) => {
  if (event.button !== 1 || !usesMiddlePrivacyShortcut()) return;
  event.preventDefault();
  event.stopImmediatePropagation();
}, { capture: true, passive: false });

window.addEventListener('keydown', (event) => {
  if (!usesKeyboardPrivacyShortcut() || !event.shiftKey || event.code !== 'KeyX' || event.repeat) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  togglePrivacyCover();
}, { capture: true });

window.addEventListener('pointermove', notePrivacyActivity, { capture: true, passive: true });
window.addEventListener('pointerdown', notePrivacyActivity, { capture: true, passive: true });
window.addEventListener('keydown', notePrivacyActivity, { capture: true });

const OUTPUT_FILENAME = /^(\d{8}_\d{6}_\d{6})_(photoshoot|random)_(\d+)_(production|preview)_shot_(\d+)_/;

function isVideoOutput(item) {
  return item?.media_type === 'video' || /\.(mp4|webm|mov|mkv|avi)$/i.test(item?.name || '');
}

function galleryMediaMatches(item) {
  if (state.galleryView === 'flat') return true;
  return state.galleryView === 'videos' ? isVideoOutput(item) : !isVideoOutput(item);
}

function galleryPendingGroupMatches(group) {
  if (state.galleryView === 'flat') return true;
  return state.galleryView === 'videos'
    ? group.generation_mode === 'video'
    : group.generation_mode !== 'video';
}

function galleryMediaItems() {
  return state.outputs
    .map((item, outputIndex) => ({ item, outputIndex }))
    .filter(({ item }) => galleryMediaMatches(item));
}

function outputSetKey(item) {
  if (!item) return null;
  if (typeof item.source_set_key === 'string' && item.source_set_key) return item.source_set_key;
  if (typeof item.set_key === 'string' && item.set_key) return item.set_key;
  const sourceName = item.source_image || item.name;
  const match = typeof sourceName === 'string' ? sourceName.match(OUTPUT_FILENAME) : null;
  if (match) return `${match[1]}:${match[2]}:${match[3]}:${match[4]}`;
  return null;
}

function videoSetKey(item) {
  return outputSetKey(item) || item?.source_key || item?.key || `video:${item?.name}`;
}

function videoGroups() {
  const groups = new Map();
  galleryMediaItems().forEach((entry) => {
    const item = entry.item;
    const key = videoSetKey(item);
    if (!groups.has(key)) groups.set(key, {
      key,
      identity: {
        key, kind: 'video', tier: 'production',
        run: item.source_image || 'Source image',
        source_image: item.source_image || 'Source image',
      },
      items: [], firstIndex: entry.outputIndex,
    });
    groups.get(key).items.push(entry);
  });
  state.pendingGroups.filter((group) => group.generation_mode === 'video').forEach((pendingGroup) => {
    const key = pendingGroup.source_set_key || `pending:${pendingGroup.group_key}`;
    if (!groups.has(key)) groups.set(key, {
      key,
      identity: {
        key, kind: 'video', tier: pendingGroup.render_tier || 'production',
        run: pendingGroup.source_image || 'Source image',
        source_image: pendingGroup.source_image || 'Source image',
      },
      items: [], pendingGroups: [], firstIndex: state.outputs.length,
    });
    const group = groups.get(key);
    group.pendingGroups ||= [];
    group.pendingGroups.push(pendingGroup);
    group.pendingGroup ||= pendingGroup;
    group.firstIndex = Math.min(group.firstIndex, state.outputs.length);
  });
  groups.forEach((group) => group.items.sort((left, right) => (
    videoSourceShotSequence(left.item) - videoSourceShotSequence(right.item)
    || left.item.name.localeCompare(right.item.name)
    || outputIdentity(left.item).localeCompare(outputIdentity(right.item))
  )));
  return [...groups.values()].sort((a, b) => a.firstIndex - b.firstIndex);
}

function galleryGroups() {
  if (state.galleryView === 'videos') return videoGroups();
  return state.galleryView === 'photoshoots' ? photoshootGroups() : [];
}

function modeTitle(mode) {
  if (mode === 'video') return 'Video';
  return mode === 'random' ? 'Random' : 'Photoshoot';
}

function tierTitle(tier) {
  return tier === 'preview' ? 'Preview' : 'Production';
}

function outputGroupIdentity(item) {
  if (item.queue_group_key) {
    return {
      key: item.queue_group_key,
      run: `${tierTitle(item.render_tier)} queue`,
      kind: item.generation_mode,
      tier: item.render_tier,
      number: Number(item.group_index),
    };
  }
  const match = item.name.match(OUTPUT_FILENAME);
  if (match) {
    return {
      key: `${match[1]}:${match[2]}_${match[3]}:${match[4]}`,
      run: match[1],
      kind: match[2],
      tier: match[4],
      number: Number(match[3]),
    };
  }
  return null;
}

function outputShotSequence(item) {
  const match = item.name.match(OUTPUT_FILENAME);
  if (match) return Number(match[5]);
  const shot = Number(item.shot);
  return Number.isFinite(shot) ? shot : Number.POSITIVE_INFINITY;
}

function videoSourceShotSequence(item) {
  if (item?.source_shot != null && Number.isFinite(Number(item.source_shot))) {
    return Number(item.source_shot);
  }
  const sourceName = item?.source_image;
  const match = typeof sourceName === 'string' ? sourceName.match(OUTPUT_FILENAME) : null;
  if (match) return Number(match[5]);
  return outputShotSequence(item);
}

function photoshootGroups() {
  const groups = new Map();
  galleryMediaItems().forEach(({ item, outputIndex }) => {
    const identity = outputGroupIdentity(item);
    const key = identity?.key || 'ungrouped';
    if (!groups.has(key)) groups.set(key, { key, identity, items: [], firstIndex: outputIndex });
    groups.get(key).items.push({ item, outputIndex });
  });
  state.pendingGroups.filter((pendingGroup) => galleryPendingGroupMatches(pendingGroup)).forEach((pendingGroup, groupIndex) => {
    const key = pendingGroup.group_key;
    const identity = {
      key,
      run: `${tierTitle(pendingGroup.render_tier)} queue`,
      kind: pendingGroup.generation_mode,
      tier: pendingGroup.render_tier,
      number: pendingGroup.group_index,
    };
    if (!groups.has(key)) {
      groups.set(key, {
        key, identity, items: [], firstIndex: state.outputs.length + groupIndex,
      });
    }
    groups.get(key).pendingGroup = pendingGroup;
  });
  const ordered = [...groups.values()].sort((a, b) => a.firstIndex - b.firstIndex);
  ordered.forEach((group) => {
    group.items.sort((left, right) => {
      return outputShotSequence(left.item) - outputShotSequence(right.item)
        || left.item.name.localeCompare(right.item.name)
        || outputIdentity(left.item).localeCompare(outputIdentity(right.item));
    });
  });
  let photoshootNumber = 0;
  let randomNumber = 0;
  ordered.forEach((group) => {
    if (group.identity?.kind === 'photoshoot') group.displayNumber = ++photoshootNumber;
    if (group.identity?.kind === 'random') group.displayNumber = ++randomNumber;
  });
  return ordered;
}

function pendingGroupCount(group) {
  return Array.isArray(group?.positions) ? group.positions.length : 0;
}

function groupPendingGroups(group) {
  if (Array.isArray(group?.pendingGroups)) return group.pendingGroups;
  return group?.pendingGroup ? [group.pendingGroup] : [];
}

function visiblePendingGroups() {
  return state.pendingGroups.filter((group) => galleryPendingGroupMatches(group));
}

function groupEntryCount(group) {
  return (group?.items?.length || 0) + groupPendingGroups(group)
    .reduce((count, pendingGroup) => count + pendingGroupCount(pendingGroup), 0);
}

function groupEntryAt(group, index) {
  if (index < group.items.length) return group.items[index];
  let offset = index - group.items.length;
  for (const pendingGroup of groupPendingGroups(group)) {
    const count = pendingGroupCount(pendingGroup);
    if (offset < count) {
      return { item: pendingOutput(pendingGroup, offset), outputIndex: null };
    }
    offset -= count;
  }
  return null;
}

function flatEntryCount() {
  return galleryMediaItems().length + state.pendingGroups.filter((group) => galleryPendingGroupMatches(group))
    .reduce((count, group) => count + pendingGroupCount(group), 0);
}

function flatEntryAt(index) {
  const visible = galleryMediaItems();
  if (index < visible.length) return visible[index];
  let offset = index - visible.length;
  for (const group of state.pendingGroups.filter((pendingGroup) => galleryPendingGroupMatches(pendingGroup))) {
    const count = pendingGroupCount(group);
    if (offset < count) {
      return { item: pendingOutput(group, offset), outputIndex: null };
    }
    offset -= count;
  }
  return null;
}

function activePhotoshootGroup() {
  return state.galleryGroup
    ? galleryGroups().find((group) => group.key === state.galleryGroup) || null
    : null;
}

function proofsPositionKey() {
  return `${state.galleryView}:${state.galleryGroup || 'root'}`;
}

function rememberProofsPosition() {
  if (!$('#outputs-view').classList.contains('active')) return;
  state.proofsPositions[proofsPositionKey()] = window.scrollY;
  sessionStorage.setItem('valhalla-proofs-positions', JSON.stringify(state.proofsPositions));
  sessionStorage.setItem('valhalla-gallery-group', state.galleryGroup || '');
}

function restoreProofsPosition({ fallbackToGrid = false } = {}) {
  const saved = Number(state.proofsPositions[proofsPositionKey()]);
  requestAnimationFrame(() => {
    const fallback = fallbackToGrid
      ? Math.max(0, window.scrollY + outputGrid.getBoundingClientRect().top - 16)
      : 0;
    window.scrollTo({ top: Number.isFinite(saved) ? saved : fallback, behavior: 'auto' });
    renderVirtualOutputs(true);
  });
}

function displayedOutputs() {
  const group = activePhotoshootGroup();
  return group ? group.items : galleryMediaItems();
}

function previewOutputs() {
  if (Array.isArray(state.previewOutputIndexes)) {
    return state.previewOutputIndexes
      .map((outputIndex) => ({ item: state.outputs[outputIndex], outputIndex }))
      .filter(({ item }) => item && !item.pending);
  }
  const items = activePhotoshootGroup()?.items || galleryMediaItems();
  return items.filter(({ item }) => !item.pending);
}

function formatOutputRun(run) {
  const match = run?.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_\d{6}$/);
  if (!match) return run || '';
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  return `${Number(match[3])} ${months[Number(match[2]) - 1]} ${match[1]} · ${match[4]}:${match[5]}:${match[6]}`;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#039;');
}

function displayValue(value) {
  const text = String(value ?? '');
  return text.replace(/[A-Za-z]/, (letter) => letter.toUpperCase());
}

function displayCatalogLabel(value) {
  const text = String(value ?? '').replace(/^template[_ ]/i, '').replaceAll('_', ' ');
  return displayValue(text);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  let body;
  try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function toast(title, message = '', type = '') {
  const item = document.createElement('div');
  const compactMessage = String(message).length > 180 ? `${String(message).slice(0, 177)}…` : message;
  item.className = `toast ${type}`;
  item.title = 'Click to dismiss';
  item.innerHTML = `<span class="toast-copy"><strong>${escapeHtml(title)}</strong>${escapeHtml(compactMessage)}</span><i class="toast-clock" aria-hidden="true"></i>`;
  $('#toast-region').append(item);
  const timer = setTimeout(() => item.remove(), 3600);
  item.addEventListener('click', () => {
    clearTimeout(timer);
    item.remove();
  });
}

function setBusy(button, busy, label) {
  if (!button) return;
  if (busy) {
    button.dataset.label = button.innerHTML;
    button.disabled = true;
    if (label) button.textContent = label;
  } else {
    button.disabled = false;
    if (button.dataset.label) button.innerHTML = button.dataset.label;
  }
}

function applyTheme() {
  if (state.theme === 'system') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.dataset.theme = state.theme;
  $$('[data-theme-choice]').forEach((button) => {
    const active = button.dataset.themeChoice === state.theme;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
}

function setTheme(theme) {
  if (!['system', 'light', 'dark'].includes(theme)) return;
  state.theme = theme;
  sessionStorage.setItem('valhalla-theme', state.theme);
  applyTheme();
}

function applyTypeSize() {
  document.documentElement.dataset.typeSize = state.typeSize;
  $$('[data-type-size]').forEach((button) => {
    const active = button.dataset.typeSize === state.typeSize;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
}

function setTypeSize(size) {
  if (!['small', 'normal', 'large'].includes(size)) return;
  state.typeSize = size;
  sessionStorage.setItem('valhalla-type-size', size);
  applyTypeSize();
}

function applyAccent() {
  document.documentElement.dataset.accent = state.accent;
  $$('[data-accent]').forEach((button) => {
    const active = button.dataset.accent === state.accent;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
}

function setAccent(accent) {
  if (!['lavender', 'azure', 'rose'].includes(accent)) return;
  state.accent = accent;
  sessionStorage.setItem('valhalla-accent', accent);
  applyAccent();
}

async function refreshStatus(showToast = false) {
  clearTimeout(statusRefreshTimer);
  statusRefreshTimer = null;
  if (statusRefreshActive) return;
  statusRefreshActive = true;
  const button = $('#refresh-status');
  button.textContent = '…';
  try {
    const status = await api('/api/status');
    $('#comfy-status').textContent = status.comfy.online ? 'Online' : 'Offline';
    statusRefreshSeconds = Number(status.comfy.refresh_seconds) || statusRefreshSeconds;
    $('#comfy-dot').className = `status-dot ${status.comfy.online ? 'online' : 'error'}`;
    const imageWorkflow = status.workflow.image || status.workflow;
    const videoWorkflow = status.workflow.video || {
      source: 'profiles', profiles: [], production: null, ready: false,
    };
    state.workflowProfilesByMedia.video = videoWorkflow;
    const productionProfile = imageWorkflow.profiles.find(
      (profile) => profile.id === imageWorkflow.production,
    );
    const liveWorkflow = imageWorkflow.source === 'live';
    $('#workflow-status').textContent = liveWorkflow
      ? 'Live ComfyUI'
      : imageWorkflow.ready
      ? productionProfile?.name || 'Ready'
      : (imageWorkflow.profiles.length ? 'Select profiles' : 'Missing');
    $('#workflow-status').title = liveWorkflow
      ? 'Latest successful workflow run directly in ComfyUI'
      : productionProfile
      ? `Production: ${productionProfile.file}\nPreview: ${imageWorkflow.preview}`
      : '';
    $('#workflow-dot').className = `status-dot ${imageWorkflow.ready ? 'online' : 'error'}`;
    const videoProduction = videoWorkflow.profiles.find(
      (profile) => profile.id === videoWorkflow.production,
    );
    const liveVideoWorkflow = videoWorkflow.source === 'live';
    $('#video-workflow-status').textContent = liveVideoWorkflow
      ? 'Live ComfyUI'
      : videoWorkflow.ready
      ? videoProduction?.name || 'Ready'
      : (videoWorkflow.profiles.length ? 'Select profile' : 'Missing');
    $('#video-workflow-status').title = liveVideoWorkflow
      ? 'Latest successful external video workflow run directly in ComfyUI'
      : videoProduction
      ? `Production: ${videoProduction.file}`
      : '';
    $('#video-workflow-dot').className = `status-dot ${videoWorkflow.ready ? 'online' : 'error'}`;
    $('#catalog-status').textContent = status.catalog_records.toLocaleString();
    applyPrivacyIdleOptions(status.interface.privacy.auto_cover_minutes);
  } catch (error) {
    $('#comfy-status').textContent = 'Error';
    $('#comfy-dot').className = 'status-dot error';
    if (showToast) toast('Status failed', error.message, 'error');
  } finally {
    statusRefreshActive = false;
    button.textContent = '↻';
    scheduleStatusRefresh();
  }
}

function scheduleStatusRefresh() {
  clearTimeout(statusRefreshTimer);
  statusRefreshTimer = null;
  if (document.hidden) return;
  statusRefreshTimer = setTimeout(() => refreshStatus(), statusRefreshSeconds * 1000);
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearTimeout(statusRefreshTimer);
    statusRefreshTimer = null;
  } else {
    refreshStatus();
  }
});

function syncForm(event) {
  const mode = form.elements.mode.value;
  const content = form.elements.content.value;
  const photoshoots = Math.max(1, Number(form.elements.photoshoots.value) || 1);
  const count = Math.max(1, Number(form.elements.count.value) || 1);
  const total = (mode === 'photoshoot' ? photoshoots : 1) * count;
  $('#photoshoots-field').classList.toggle('hidden', mode === 'random');
  const progressionDisabled = mode === 'random' || content !== 'progressive';
  $('#progression-fields').classList.toggle('hidden', progressionDisabled);
  $('#mode-help').textContent = mode === 'photoshoot'
    ? 'One consistent subject, wardrobe and set per photoshoot.'
    : 'Every image receives an independently assembled production context.';
  $('#content-help').textContent = content === 'sfw'
    ? 'Every frame keeps breasts and genitals fully covered.'
    : (content === 'xxx'
      ? 'Every frame starts at an explicit stage; progression sliders are not used.'
      : (mode === 'photoshoot'
        ? 'Begins clothed and progresses toward the configured NSFW ending.'
        : 'Each independent frame receives a compatible stage selected from the full progression.'));
  const nsfw = Math.max(0, Math.min(100, Number(form.elements.nsfw_percent.value) || 0));
  let plateau = Math.max(0, Math.min(100, Number(form.elements.plateau_percent.value) || 0));
  if (event?.target?.name === 'nsfw_percent' && plateau > nsfw) {
    plateau = nsfw;
    form.elements.plateau_percent.value = String(plateau);
  }
  form.elements.plateau_percent.max = String(nsfw);
  form.elements.plateau_percent.disabled = progressionDisabled || nsfw === 0;
  $('#nsfw-output').textContent = `${nsfw}%`;
  $('#plateau-output').textContent = `${plateau}%`;
  const nsfwFrames = nsfw > 0 ? Math.ceil(count * nsfw / 100) : 0;
  const plateauFrames = plateau > 0 ? Math.min(nsfwFrames, Math.ceil(count * plateau / 100)) : 0;
  const perSet = mode === 'photoshoot' && photoshoots > 1 ? ' per set' : '';
  $('#nsfw-help').textContent = nsfwFrames
    ? `Final ${nsfwFrames} of ${count} frame${nsfwFrames === 1 ? '' : 's'}${perSet} may be topless, nude or explicit.`
    : `0 of ${count} frames${perSet} · Covered and lingerie only.`;
  $('#plateau-help').textContent = nsfw === 0
    ? 'Disabled because the NSFW ending is 0%.'
    : (plateauFrames
      ? `Final ${plateauFrames} of ${count} frame${plateauFrames === 1 ? '' : 's'}${perSet} remain explicit.`
      : 'No repeated explicit ending.');
  $('#planned-total').textContent = `${total} image${total === 1 ? '' : 's'}`;
}

function structuralConfigFromForm() {
  const mode = form.elements.mode.value;
  const contentMode = form.elements.content.value;
  return {
    mode,
    count: Number(form.elements.count.value),
    photoshoots: mode === 'photoshoot' ? Number(form.elements.photoshoots.value) : 1,
    content_mode: contentMode,
    nsfw_percent: mode === 'photoshoot' && contentMode === 'progressive' ? Number(form.elements.nsfw_percent.value) : null,
    plateau_percent: mode === 'photoshoot' && contentMode === 'progressive' ? Number(form.elements.plateau_percent.value) : null,
    prompt_seed: form.elements.prompt_seed.value === '' ? null : String(form.elements.prompt_seed.value),
    use_curated_defaults: form.elements.use_curated_defaults.checked,
  };
}

function structuralConfigFromBoard(board) {
  if (!board) return null;
  const config = board.config;
  return {
    mode: config.mode,
    count: Number(config.count),
    photoshoots: config.mode === 'photoshoot' ? Number(config.photoshoots) : 1,
    content_mode: config.content_mode,
    nsfw_percent: config.mode === 'photoshoot' && config.content_mode === 'progressive' ? Number(config.nsfw_percent) : null,
    plateau_percent: config.mode === 'photoshoot' && config.content_mode === 'progressive' ? Number(config.plateau_percent) : null,
    prompt_seed: config.prompt_seed == null ? null : String(config.prompt_seed),
    use_curated_defaults: config.use_curated_defaults !== false,
  };
}

function configSummary(config) {
  if (!config) return '';
  const mode = config.mode === 'photoshoot' ? `${config.photoshoots} set${Number(config.photoshoots) === 1 ? '' : 's'}` : 'Independent shots';
  const contentMode = config.content_mode;
  const content = contentMode === 'sfw'
    ? 'SFW only'
    : contentMode === 'xxx'
      ? 'Full XXX'
      : config.nsfw_percent == null || config.plateau_percent == null
        ? 'Progressive'
        : `NSFW ${config.nsfw_percent}% · Explicit ${config.plateau_percent}%`;
  const defaults = config.use_curated_defaults === false ? 'Full catalog paths' : 'Curated defaults';
  return `${mode} · ${config.count} shots · ${content} · ${defaults} · Storyboard seed ${config.prompt_seed ?? 'automatic'}`;
}

function syncPendingState() {
  const active = structuralConfigFromBoard(state.storyboard);
  const pending = structuralConfigFromForm();
  state.pendingStructural = Boolean(active && JSON.stringify(active) !== JSON.stringify(pending));
  const changedKeys = active
    ? Object.keys(pending).filter((key) => active[key] !== pending[key])
    : [];
  const changedLabels = {
    mode: 'mode', count: 'shot count', photoshoots: 'set count',
    content_mode: 'content mode', nsfw_percent: 'NSFW ending',
    plateau_percent: 'explicit plateau', prompt_seed: 'Storyboard seed',
    use_curated_defaults: 'curated defaults',
  };
  $('#config-notice-copy').textContent = changedKeys.length
    ? `Changed: ${changedKeys.map((key) => changedLabels[key]).join(', ')}. Update before rendering.`
    : 'Update the storyboard before rendering.';
  const pendingElements = {
    mode: form.elements.mode[0].closest('fieldset'),
    content_mode: form.elements.content[0].closest('fieldset'),
    count: form.elements.count.closest('.field'),
    photoshoots: form.elements.photoshoots.closest('.field'),
    nsfw_percent: $('#progression-fields'),
    plateau_percent: $('#progression-fields'),
    prompt_seed: form.elements.prompt_seed.closest('.field'),
    use_curated_defaults: form.elements.use_curated_defaults.closest('.switch-row'),
  };
  Object.entries(pendingElements).forEach(([key, element]) => {
    element?.classList.toggle('pending-change', changedKeys.includes(key));
  });
  const seedStatus = $('#storyboard-seed-status');
  seedStatus.textContent = !state.storyboard ? 'Used on create' : (state.pendingStructural ? 'Requires update' : 'Active');
  seedStatus.classList.toggle('pending', state.pendingStructural);
  $('#config-notice').classList.toggle('hidden', !state.pendingStructural);
  $('#resolve-button').innerHTML = state.storyboard
    ? (state.pendingStructural ? '<span>↻</span> Update storyboard' : '<span>↻</span> Reroll storyboard')
    : '<span>✦</span> Create storyboard';
  const activeConfig = $('#active-config');
  activeConfig.classList.toggle('hidden', !state.storyboard);
  if (state.storyboard) {
    const variation = state.storyboard.config.inference_strategy === 'random'
      ? 'Fresh random variation per shot'
      : `Variation seed ${state.storyboard.config.inference_seed} · ${state.storyboard.config.inference_strategy}`;
    activeConfig.innerHTML = `<div><span>Active storyboard</span><strong>${escapeHtml(configSummary(active))}</strong></div><div><span>Image rendering</span><strong>${escapeHtml(variation)}</strong></div>${state.pendingStructural ? `<div class="pending"><span>Pending settings</span><strong>${escapeHtml(configSummary(pending))}</strong></div>` : ''}`;
  }
  syncRenderControls();
}

function restoreConfig(config, job) {
  const mode = form.querySelector(`[name="mode"][value="${config.mode}"]`);
  const contentMode = config.content_mode;
  const content = form.querySelector(`[name="content"][value="${contentMode}"]`);
  if (mode) mode.checked = true;
  if (content) content.checked = true;
  form.elements.count.value = config.count;
  form.elements.photoshoots.value = config.photoshoots;
  form.elements.prompt_seed.value = config.prompt_seed ?? '';
  form.elements.use_curated_defaults.checked = config.use_curated_defaults !== false;
  form.elements.inference_seed.value = config.inference_seed ?? '';
  form.elements.inference_strategy.value = config.inference_strategy || 'sequence';
  if (config.nsfw_percent != null) form.elements.nsfw_percent.value = config.nsfw_percent;
  if (config.plateau_percent != null) form.elements.plateau_percent.value = config.plateau_percent;
  const previewMode = job && typeof job.fast === 'boolean'
    ? job.fast
    : Boolean(config.fast);
  state.renderMode = previewMode ? 'preview' : 'production';
  sessionStorage.setItem('valhalla-render-mode', state.renderMode);
  syncRenderControls();
  syncForm();
  syncPendingState();
}

function configPayload() {
  const value = (name) => form.elements[name].value;
  return {
    mode: value('mode'),
    count: Number(value('count')),
    photoshoots: Number(value('photoshoots')),
    content_mode: value('content'),
    nsfw_percent: Number(value('nsfw_percent')),
    plateau_percent: Number(value('plateau_percent')),
    prompt_seed: value('prompt_seed') === '' ? null : value('prompt_seed'),
    inference_seed: value('inference_seed') === '' ? null : value('inference_seed'),
    inference_strategy: value('inference_strategy'),
    use_curated_defaults: form.elements.use_curated_defaults.checked,
    fast: state.renderMode === 'preview',
  };
}

async function resolveStoryboard(event, options = {}) {
  if (event?.preventDefault) event.preventDefault();
  clearTimeout(state.seedResolveTimer);
  const version = ++state.resolveVersion;
  const button = $('#resolve-button');
  setBusy(button, true, 'Resolving…');
  storyboardPanel.classList.remove('resolved');
  emptyState.classList.add('hidden');
  shotGrid.classList.add('hidden');
  storyboardActions.classList.add('hidden');
  storyboardMeta.classList.add('hidden');
  loadingState.classList.remove('hidden');
  try {
    const storyboard = await api('/api/storyboards', { method: 'POST', body: JSON.stringify(configPayload()) });
    if (version !== state.resolveVersion) return;
    state.storyboard = storyboard;
    form.elements.prompt_seed.value = storyboard.config.prompt_seed ?? '';
    form.elements.inference_seed.value = storyboard.config.inference_seed ?? '';
    renderStoryboard();
    return storyboard;
  } catch (error) {
    if (version !== state.resolveVersion) return;
    if (state.storyboard) renderStoryboard();
    else emptyState.classList.remove('hidden');
    toast('Could not resolve storyboard', error.message, 'error');
    return null;
  } finally {
    if (version !== state.resolveVersion) return;
    loadingState.classList.add('hidden');
    setBusy(button, false);
    syncPendingState();
  }
}

function confirmStoryboardUpdate() {
  if (!state.storyboard?.director_edited) return Promise.resolve(true);
  updateStoryboardDialog.showModal();
  return new Promise((resolve) => { state.updateResolver = resolve; });
}

async function requestStoryboardUpdate(options = {}) {
  if (state.storyboard && !(await confirmStoryboardUpdate())) return null;
  return resolveStoryboard(null, options);
}

function scheduleSeedResolve(event) {
  if (!state.storyboard || isRenderActive()) return;
  const name = event.target?.name;
  if (!['inference_seed', 'inference_strategy'].includes(name)) return;
  $('#variation-seed-status').textContent = 'Applying…';
  clearTimeout(state.seedResolveTimer);
  state.seedResolveTimer = setTimeout(
    () => applyVariationSettings(),
    650,
  );
}

function generateUiSeed() {
  const minimum = 100000000000000;
  const span = 900000000000000;
  const words = new Uint32Array(2);
  crypto.getRandomValues(words);
  const safeRandom = (words[0] & 0x1fffff) * 0x100000000 + words[1];
  const seed = minimum + (safeRandom % span);
  return seed % 10 ? seed : seed + 1;
}

function randomizeSeedField(name) {
  const input = form.elements[name];
  const previous = input.value;
  let next;
  do { next = String(generateUiSeed()); } while (next === previous);
  input.value = next;
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

async function applyVariationSettings() {
  if (!state.storyboard || isRenderActive()) return;
  const version = ++state.resolveVersion;
  const seed = form.elements.inference_seed.value;
  try {
    const storyboard = await api(`/api/storyboards/${state.storyboard.id}/seeds`, {
      method: 'POST',
      body: JSON.stringify({
        inference_seed: seed === '' ? null : seed,
        inference_strategy: form.elements.inference_strategy.value,
      }),
    });
    if (version !== state.resolveVersion) return;
    state.storyboard = storyboard;
    state.director = null;
    form.elements.inference_seed.value = storyboard.config.inference_seed ?? '';
    renderStoryboard();
    $('#variation-seed-status').textContent = 'Applied';
  } catch (error) {
    if (version !== state.resolveVersion) return;
    toast('Could not update image variations', error.message, 'error');
    $('#variation-seed-status').textContent = 'Not applied';
  }
}

async function exportStoryboard() {
  if (!state.storyboard) return;
  const button = $('#export-storyboard');
  setBusy(button, true, 'Exporting…');
  try {
    const payload = await api(`/api/storyboards/${state.storyboard.id}/export`);
    if (payload.format !== 'valhalla-storyboard') {
      throw new Error('Server returned a storyboard snapshot instead of an export. Restart the server and try again.');
    }
    const blob = new Blob([JSON.stringify(payload)], { type: 'application/json' });
    const link = document.createElement('a');
    const stamp = new Date().toISOString().replaceAll(':', '-').replace(/\.\d{3}Z$/, 'Z');
    link.href = URL.createObjectURL(blob);
    link.download = `valhalla-storyboard-${stamp}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 0);
    toast('Storyboard exported', `${state.storyboard.total} shots saved in compact JSON.`, 'success');
  } catch (error) {
    toast('Export failed', error.message, 'error');
  } finally {
    setBusy(button, false);
  }
}

async function importStoryboard(event) {
  const file = event.target.files?.[0];
  event.target.value = '';
  if (!file) return;
  const button = $('#import-storyboard');
  setBusy(button, true, 'Importing…');
  try {
    if (file.size > 32_000_000) throw new Error('Storyboard file is larger than 32 MB.');
    const payload = JSON.parse(await file.text());
    state.storyboard = await api('/api/storyboards/import', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    restoreConfig(state.storyboard.config, { fast: state.storyboard.config.fast });
    renderStoryboard();
    switchView('studio');
    toast('Storyboard imported', `${state.storyboard.total} shots are ready to render.`, 'success');
  } catch (error) {
    const message = error instanceof SyntaxError ? 'The selected file is not valid JSON.' : error.message;
    toast('Import failed', message, 'error');
  } finally {
    setBusy(button, false);
  }
}

function shotCard(shot) {
  const explicit = shot.stage.level === 'explicit' ? 'explicit' : '';
  const stage = shot.stage.plateau_kind || shot.stage.level;
  const photoshoot = state.storyboard?.config.mode === 'photoshoot';
  const displayShotNumber = photoshoot ? shot.shot_index + 1 : shot.number;
  const manual = Boolean(shot.stage.manual || shot.manual_fields?.length);
  const manualTitle = shot.manual_fields?.length
    ? `Manual: ${shot.manual_fields.join(', ')}`
    : 'Manual Director edit';
  const statuses = [
    manual ? `<span class="card-status manual" title="${escapeHtml(manualTitle)}">Manual</span>` : '',
    shot.prompt_warnings?.length
      ? `<span class="card-status warning" title="${escapeHtml(shot.prompt_warnings.join('; '))}">Warning</span>`
      : '',
  ].join('');
  return `
    <article class="shot-card" data-shot="${shot.number}">
      <div class="shot-top">
        <div class="shot-number">Shot ${displayShotNumber}</div>
        <div class="shot-top-meta">${statuses}<span class="stage-badge ${explicit} ${shot.stage.manual ? 'manual' : ''}">${escapeHtml(displayValue(stage.replaceAll('_', ' ')))}</span></div>
      </div>
      <div class="shot-body">
        <div class="shot-detail shot-subject" title="${escapeHtml(shot.subject)}"><span>Subject</span><strong>${escapeHtml(displayValue(shot.subject))}</strong></div>
        <div class="shot-detail shot-wardrobe" title="${escapeHtml(displayCatalogLabel(shot.wardrobe))}"><span>Wardrobe</span><strong>${escapeHtml(displayCatalogLabel(shot.wardrobe))}</strong></div>
        <div class="shot-detail"><span>Pose</span><strong title="${escapeHtml(shot.pose.prompt)}">${escapeHtml(displayValue(shot.pose.prompt))}</strong></div>
        <div class="shot-detail"><span>Action</span><strong title="${escapeHtml(shot.action.prompt)}">${escapeHtml(displayValue(shot.action.prompt))}</strong></div>
        <div class="shot-detail"><span>Role</span><strong title="${escapeHtml(shot.editorial_role.prompt)}">${escapeHtml(displayValue(shot.editorial_role.prompt))}</strong></div>
        <div class="shot-detail"><span>Camera</span><strong title="${escapeHtml(shot.camera)}">${escapeHtml(displayValue(shot.camera))}</strong></div>
        <div class="shot-detail"><span>Variation</span><strong title="Inference seed ${shot.inference_seed}">${shot.seed_manual ? 'Custom · ' : ''}${escapeHtml(shot.inference_seed)}</strong></div>
      </div>
      <div class="shot-footer">
        <button type="button" class="direct" data-action="director">Director</button>
        <button type="button" class="reroll" data-action="reroll">Reroll</button>
        <button type="button" data-action="inspect">Prompt</button>
        <button type="button" class="variation" data-action="variation">Variation</button>
        <button type="button" class="preview" data-action="preview">Preview</button>
        <button type="button" class="render-one" data-action="render">Render</button>
      </div>
    </article>`;
}

function storyboardCards(shots) {
  if (state.storyboard?.config.mode !== 'photoshoot') return shots.map(shotCard).join('');
  const sets = new Map();
  shots.forEach((shot) => {
    if (!sets.has(shot.photoshoot_index)) sets.set(shot.photoshoot_index, []);
    sets.get(shot.photoshoot_index).push(shot);
  });
  if (state.storyboardOpenSet !== null && !sets.has(state.storyboardOpenSet)) {
    state.storyboardOpenSet = sets.keys().next().value;
  }
  return [...sets.entries()].map(([setIndex, setShots]) => `
    <details class="director-group storyboard-set" data-storyboard-set="${setIndex}" ${setIndex === state.storyboardOpenSet ? 'open' : ''}>
      <summary><span class="director-group-title">Set ${setIndex + 1}</span><small>${setShots.length} shots · set</small></summary>
      <div class="storyboard-set-shots">${setShots.map(shotCard).join('')}</div>
    </details>
  `).join('');
}

function renderStoryboard() {
  const board = state.storyboard;
  if (!board) return;
  const navigationSetCount = board.config.mode === 'photoshoot'
    ? new Set(board.shots.map((shot) => shot.photoshoot_index)).size
    : (board.shots.length ? 1 : 0);
  updateNavigationCount('studio-count', navigationSetCount, 'set');
  updateNavigationCount('director-count', board.shots.length, 'shot');
  storyboardPanel.classList.add('resolved');
  $('#export-storyboard').disabled = false;
  if (state.director?.storyboard_id !== board.id) {
    state.director = null;
    state.directorOpenGroup = 'identity';
  }
  shotGrid.innerHTML = storyboardCards(board.shots);
  const sets = board.config.mode === 'photoshoot' ? board.config.photoshoots : 'Independent';
  const contentMode = board.config.content_mode;
  const contentLabel = { sfw: 'SFW only', progressive: 'Progressive', xxx: 'Full XXX' }[contentMode];
  storyboardMeta.innerHTML = `<span>Mode <strong>${escapeHtml(board.config.mode)}</strong></span><span>Sets <strong>${sets}</strong></span><span>Shots <strong>${board.total}</strong></span><span>Diversity <strong>${board.diversity}%</strong></span><span>Content <strong>${contentLabel}</strong></span>`;
  emptyState.classList.add('hidden');
  storyboardActions.classList.remove('hidden');
  storyboardMeta.classList.remove('hidden');
  shotGrid.classList.remove('hidden');
  syncPendingState();
}

function updateNavigationCount(id, count, singular, plural = `${singular}s`) {
  const element = $(`#${id}`);
  if (!element) return;
  const label = `${count} ${count === 1 ? singular : plural}`;
  element.textContent = count;
  element.title = label;
  element.setAttribute('aria-label', label);
}

function renderOneShot(shot) {
  const index = state.storyboard.shots.findIndex((item) => item.number === shot.number);
  state.storyboard.shots[index] = shot;
  const current = $(`.shot-card[data-shot="${shot.number}"]`);
  const wrapper = document.createElement('div');
  wrapper.innerHTML = shotCard(shot).trim();
  current.replaceWith(wrapper.firstElementChild);
}

async function rerollShot(number, button) {
  setBusy(button, true, '…');
  try {
    const shot = await api(`/api/storyboards/${state.storyboard.id}/shots/${number}/reroll`, { method: 'POST', body: '{}' });
    state.storyboard.director_edited = true;
    renderOneShot(shot);
  } catch (error) {
    setBusy(button, false);
    toast('Could not reroll shot', error.message, 'error');
  }
}

async function randomizeShotSeed(number, button) {
  if (!state.storyboard || isRenderActive()) return;
  setBusy(button, true, '…');
  try {
    const shot = await api(`/api/storyboards/${state.storyboard.id}/shots/${number}/seed`, {
      method: 'POST', body: '{}',
    });
    state.storyboard.director_edited = true;
    renderOneShot(shot);
    if (state.director && state.directorShot === number) await loadDirector(number);
  } catch (error) {
    toast('Could not change variation', error.message, 'error');
  } finally {
    setBusy(button, false);
  }
}

function openPrompt(shot) {
  if (!shot || !promptDialog) return;
  if (state.privacyCovered) {
    toast('Prompt hidden', 'Disable Privacy Cover to inspect shot prompts.', 'error');
    return;
  }
  try {
    state.promptShot = shot;
    state.promptTab = 'positive';
    $('#dialog-eyebrow').textContent = `Set ${shot.photoshoot_index + 1} · Shot ${shot.shot_index + 1}`;
    const level = shot.stage?.level || 'shot';
    $('#dialog-title').textContent = `${level[0].toUpperCase()}${level.slice(1)} composition`;
    $$('.prompt-tabs button').forEach((button) => button.classList.toggle('active', button.dataset.prompt === 'positive'));
    updatePromptContent();
    if (promptDialog.open) promptDialog.close();
    if (typeof promptDialog.showModal === 'function') promptDialog.showModal();
    else promptDialog.setAttribute('open', '');
  } catch (error) {
    console.error('Could not open prompt dialog', error);
    toast('Could not open prompt', error.message, 'error');
  }
}

function updatePromptContent() {
  if (!state.promptShot) return;
  const selectedIds = state.promptShot.selected_ids;
  const selectedIdText = Array.isArray(selectedIds)
    ? selectedIds.join('\n')
    : Object.values(selectedIds || {}).flat().join('\n');
  const content = {
    positive: state.promptShot.positive_prompt || '',
    negative: state.promptShot.negative_prompt || '',
    ids: selectedIdText,
  }[state.promptTab];
  $('#prompt-content').textContent = content;
}

async function trackQueuedJob(queuedJob, previousActiveId) {
  let session = null;
  try { session = await api('/api/jobs'); } catch { /* regular polling will retry */ }
  if (!session && previousActiveId) return;
  if (session) syncQueuePlaceholders(session.jobs || []);
  const active = session?.active_job || null;
  if (previousActiveId && active?.id === previousActiveId) return;
  const submitted = session?.jobs?.find((job) => job.id === queuedJob.id) || queuedJob;
  if (!session) syncQueuePlaceholders([submitted]);
  state.job = active || submitted;
  showJob();
  pollJob();
}

async function startGeneration() {
  if (!state.storyboard) return;
  const alreadyActive = Boolean(isRenderActive());
  const previousActiveId = alreadyActive ? state.job.id : null;
  if (state.pendingStructural) {
    const updated = await requestStoryboardUpdate();
    if (!updated) return;
  }
  const buttons = $$('[data-render-action]');
  buttons.forEach((button) => {
    if (!button.dataset.idleLabel) button.dataset.idleLabel = button.innerHTML;
    setBusy(button, true, 'Queueing…');
  });
  try {
    const queuedJob = await api('/api/jobs', {
      method: 'POST',
      body: JSON.stringify({ storyboard_id: state.storyboard.id, fast: state.renderMode === 'preview' }),
    });
    await trackQueuedJob(queuedJob, previousActiveId);
    switchView('outputs');
    toast(
      alreadyActive ? 'Added to render queue' : `${modeTitle(queuedJob.generation_mode)} ${tierTitle(queuedJob.render_tier).toLowerCase()} queued`,
      `${queuedJob.total} images queued${alreadyActive ? ` at position ${queuedJob.queue_position}` : ''}.`,
      'success',
    );
  } catch (error) {
    toast('Could not start generation', error.message, 'error');
  } finally {
    buttons.forEach((button) => setBusy(button, false));
    syncRenderControls();
  }
}

async function startShotRender(number, button) {
  if (!state.storyboard) return;
  const alreadyActive = Boolean(isRenderActive());
  const previousActiveId = alreadyActive ? state.job.id : null;
  if (state.pendingStructural) {
    const updated = await requestStoryboardUpdate();
    if (!updated || number > updated.total) return;
  }
  setBusy(button, true, 'Queueing…');
  try {
    const queuedJob = await api(`/api/storyboards/${state.storyboard.id}/shots/${number}/render`, {
      method: 'POST', body: JSON.stringify({ fast: false }),
    });
    await trackQueuedJob(queuedJob, previousActiveId);
    switchView('outputs');
    toast(
      alreadyActive ? 'Shot added to queue' : 'Shot queued',
      `Shot ${number} queued${alreadyActive ? ` at position ${queuedJob.queue_position}` : ''}.`,
      'success',
    );
  } catch (error) {
    toast('Could not render shot', error.message, 'error');
  } finally {
    setBusy(button, false);
  }
}

function formatTime(seconds) {
  if (seconds == null) return 'Calculating ETA';
  if (seconds < 60) return `${Math.round(seconds)} sec remaining`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} min ${Math.round(seconds % 60)} sec remaining`;
}

function formatDuration(seconds) {
  if (seconds == null) return '—';
  const value = Math.max(0, Math.round(seconds));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const rest = value % 60;
  return hours ? `${hours}h ${minutes}m` : (minutes ? `${minutes}m ${rest}s` : `${rest}s`);
}

function formatLoggedPrompt(prompt) {
  return prompt ? String(prompt).replaceAll(', ', ',\n') : 'Waiting for a frame…';
}

function inspectedJobPrompt(job) {
  if (state.loggerInspection?.jobId !== job?.id) return job?.current_prompt || null;
  const entry = job.logs?.[state.loggerInspection.logIndex];
  return entry?.positive != null && entry?.negative != null ? entry : job.current_prompt || null;
}

function displayedLoggerPrompt() {
  const preview = state.previewJob;
  const usePreview = preview && (!state.job || new Date(preview.created_at) >= new Date(state.job.created_at));
  return usePreview ? preview : inspectedJobPrompt(state.job);
}

let loggerImageColumnFrame = null;
function sizeLoggerImageColumn() {
  if (loggerImageColumnFrame != null) cancelAnimationFrame(loggerImageColumnFrame);
  loggerImageColumnFrame = requestAnimationFrame(() => {
    loggerImageColumnFrame = null;
    const grid = $('.logger-prompt-grid');
    const article = $('.logger-rendered');
    const frame = $('.logger-rendered-frame');
    const image = $('#logger-rendered-image');
    const video = $('#logger-rendered-video');
    const media = image.hasAttribute('src') ? image : video;
    const naturalWidth = image.hasAttribute('src') ? image.naturalWidth : video.videoWidth;
    const naturalHeight = image.hasAttribute('src') ? image.naturalHeight : video.videoHeight;
    if (!grid || !article || !frame || !media.getAttribute('src')
        || !naturalWidth || !naturalHeight
        || window.matchMedia('(max-width: 820px)').matches) {
      grid?.style.removeProperty('--logger-image-column');
      return;
    }
    const styles = getComputedStyle(grid);
    const gap = Number.parseFloat(styles.columnGap) || 0;
    const gridWidth = grid.clientWidth;
    const imageHeight = frame.clientHeight;
    if (!gridWidth || !imageHeight) {
      grid.style.removeProperty('--logger-image-column');
      return;
    }
    const panelChrome = Math.max(0, article.offsetWidth - frame.clientWidth);
    const aspectWidth = imageHeight * naturalWidth / naturalHeight + panelChrome;
    const minimumPromptWidth = Math.min(240, Math.max(160, (gridWidth - gap * 2) / 5));
    const maximumImageWidth = Math.max(1, gridWidth - gap * 2 - minimumPromptWidth * 2);
    const columnWidth = Math.min(aspectWidth, maximumImageWidth);
    const currentWidth = Number.parseFloat(grid.style.getPropertyValue('--logger-image-column'));
    if (!Number.isFinite(currentWidth) || Math.abs(currentWidth - columnWidth) > 0.5) {
      grid.style.setProperty('--logger-image-column', `${columnWidth}px`);
    }
  });
}

function renderLoggerImage(prompt) {
  const image = $('#logger-rendered-image');
  const video = $('#logger-rendered-video');
  const empty = $('#logger-rendered-empty');
  const isVideo = prompt?.media_type === 'video' || prompt?.video_url;
  const url = state.privacyCovered ? null : (isVideo ? prompt?.video_url : prompt?.image_url);
  const frame = image.closest('.logger-rendered-frame');
  if (url) {
    if (isVideo) {
      image.removeAttribute('src');
      const output = state.outputs.find((item) => item.url === url);
      video.poster = output?.thumbnail_url || '';
      if (video.getAttribute('src') !== url) {
        video.src = url;
        video.load();
      }
    } else {
      video.pause();
      if (video.hasAttribute('src')) {
        video.removeAttribute('src');
        video.load();
      }
      video.removeAttribute('poster');
      if (image.getAttribute('src') !== url) image.src = url;
      image.alt = `Rendered shot ${prompt.shot || ''}`.trim();
    }
    frame.classList.add('has-media');
    empty.textContent = 'Waiting for rendered media…';
  } else {
    image.removeAttribute('src');
    video.pause();
    video.removeAttribute('src');
    image.alt = '';
    frame.classList.remove('has-media');
    empty.textContent = state.privacyCovered
      ? 'Preview unavailable'
      : 'Waiting for rendered media…';
  }
  const floatingWindow = $('#shot-preview-window');
  const loggerPreview = state.previewWindowSessions.logger;
  if (!state.privacyCovered && loggerPreview.open && loggerPreview.displayed?.persistent) {
    if (prompt?.image_url && !isVideo) {
      const changed = loggerPreview.displayed.image_url !== prompt.image_url
        || loggerPreview.displayed.shot !== prompt.shot;
      loggerPreview.displayed.image_url = prompt.image_url;
      loggerPreview.displayed.shot = prompt.shot;
      if (changed) persistPreviewWindowSessions();
      if (state.previewWindowOwner === 'logger' && !floatingWindow.classList.contains('hidden')) {
        $('#shot-preview-title').textContent = prompt.shot ? `Shot ${prompt.shot}` : 'Rendered image';
        if ($('#shot-preview-image').getAttribute('src') !== prompt.image_url) {
          $('#shot-preview-image').src = prompt.image_url;
        }
      }
    } else if (state.previewWindowOwner === 'logger' && !floatingWindow.classList.contains('hidden')) {
      $('#shot-preview-image').removeAttribute('src');
      $('#shot-preview-title').textContent = 'Rendered image unavailable';
    }
  }
  sizeLoggerImageColumn();
}

function renderLogger() {
  const preview = state.previewJob;
  const usePreview = preview && (!state.job || new Date(preview.created_at) >= new Date(state.job.created_at));
  const job = usePreview ? null : state.job;
  const empty = $('#logger-empty');
  const workspace = $('#logger-workspace');
  if (!job && !preview) {
    empty.classList.remove('hidden');
    workspace.classList.add('hidden');
    updateNavigationCount('log-count', 0, 'log entry', 'log entries');
    $('#clear-logger').disabled = false;
    renderLoggerImage(null);
    return;
  }
  empty.classList.add('hidden');
  workspace.classList.remove('hidden');
  if (usePreview) {
    $('#clear-logger').disabled = ['queued', 'running'].includes(preview.status);
    updateNavigationCount('log-count', 1, 'log entry', 'log entries');
    $('#logger-progress').textContent = 'Preview';
    $('#logger-percent').textContent = preview.status === 'completed' ? 'Ready' : 'Rendering one shot';
    $('#logger-elapsed').textContent = formatDuration(preview.elapsed_seconds);
    $('#logger-eta').textContent = preview.status === 'completed' ? 'Complete' : 'Calculating';
    $('#logger-shot-label').textContent = 'Current shot';
    $('#logger-shot').textContent = `Shot ${preview.shot}`;
    $('#logger-seed').textContent = `Seed ${preview.seed}`;
    $('#logger-positive').textContent = formatLoggedPrompt(preview.positive);
    $('#logger-negative').textContent = formatLoggedPrompt(preview.negative);
    renderLoggerImage(preview);
    $('#logger-job-id').textContent = `${preview.workflow_profile} · Preview ${preview.id.slice(0, 10)}`;
    const time = new Date(preview.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    $('#logger-event-list').innerHTML = `<div class="logger-event ${escapeHtml(preview.status)}"><time>${escapeHtml(time)}</time><i>Preview</i><span>${escapeHtml(`Shot ${preview.shot} preview ${preview.status}`)}</span><em>1/1</em></div>`;
    return;
  }
  const logs = job.logs || [];
  $('#clear-logger').disabled = ['queued', 'running'].includes(job.status);
  updateNavigationCount('log-count', logs.length, 'log entry', 'log entries');
  const visiblePosition = job.current_prompt?.position || job.completed || 0;
  $('#logger-progress').textContent = `${visiblePosition} / ${job.total}`;
  $('#logger-percent').textContent = `${job.progress || 0}% complete`;
  $('#logger-elapsed').textContent = formatDuration(job.elapsed_seconds);
  $('#logger-eta').textContent = job.status === 'completed' ? 'Complete' : formatDuration(job.eta_seconds);
  const inspectedPrompt = inspectedJobPrompt(job);
  const inspectingHistory = state.loggerInspection?.jobId === job.id
    && inspectedPrompt !== job.current_prompt;
  $('#logger-shot-label').textContent = inspectingHistory ? 'Inspected shot' : 'Current shot';
  $('#logger-shot').textContent = inspectedPrompt ? `Shot ${inspectedPrompt.shot}` : '—';
  $('#logger-seed').textContent = inspectedPrompt ? `Seed ${inspectedPrompt.seed}` : 'Seed —';
  $('#logger-positive').textContent = formatLoggedPrompt(inspectedPrompt?.positive);
  $('#logger-negative').textContent = formatLoggedPrompt(inspectedPrompt?.negative);
  renderLoggerImage(inspectedPrompt);
  $('#logger-job-id').textContent = `${modeTitle(job.generation_mode)} · ${tierTitle(job.render_tier)} · ${job.workflow_profile} · Job ${job.id.slice(0, 10)}`;
  $('#logger-event-list').innerHTML = logs.map((entry, logIndex) => ({ entry, logIndex })).reverse().map(({ entry, logIndex }) => {
    const time = new Date(entry.time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const count = entry.position ? `${entry.position}/${entry.total}` : `0/${entry.total}`;
    const liveDuration = Math.max(0, (Date.now() - new Date(entry.time).getTime()) / 1000);
    const detail = entry.type === 'shot_started'
      ? `Rendering · ${formatDuration(liveDuration)}`
      : (entry.type === 'shot_completed'
        ? `Rendered in ${formatDuration(entry.duration_seconds)}`
        : entry.message);
    const inspectable = entry.positive != null && entry.negative != null;
    const eventLabel = entry.shot != null ? `Shot ${entry.shot}` : tierTitle(job.render_tier);
    const selected = state.loggerInspection?.jobId === job.id
      && state.loggerInspection.logIndex === logIndex;
    return `<div class="logger-event ${escapeHtml(entry.type)}${inspectable ? ' inspectable' : ''}${selected ? ' selected' : ''}"${inspectable ? ` data-log-index="${logIndex}" role="button" tabindex="0" aria-label="Inspect prompts for shot ${entry.shot}"` : ''}><time>${escapeHtml(time)}</time><i>${escapeHtml(eventLabel)}</i><span>${escapeHtml(detail)}</span><em>${escapeHtml(count)}</em></div>`;
  }).join('');
}

function showJob() {
  const job = state.job;
  if (!job) return;
  const mediaTitle = job.kind === 'video' || job.generation_mode === 'video' ? 'Video' : 'Image';
  const allImagesRendered = job.total > 0 && job.completed >= job.total;
  const queueSuffix = job.queued_after
    ? ` · ${job.queued_after} job${job.queued_after === 1 ? '' : 's'} queued`
    : '';
  syncRenderControls();
  syncJobDockLayer();
  jobDock.classList.remove('hidden');
  $('#job-percent').textContent = `${job.progress || 0}%`;
  $('#job-progress').style.width = `${job.progress || 0}%`;
  $('#job-detail').textContent = job.cancel_requested
    ? `Cancelling… current ${mediaTitle.toLowerCase()} will finish`
    : (job.status === 'queued'
      ? `Waiting to start${queueSuffix}`
      : (allImagesRendered
        ? `Finalizing ${tierTitle(job.render_tier).toLowerCase()}…${queueSuffix}`
        : `${mediaTitle} ${job.completed} of ${job.total} · ${formatTime(job.eta_seconds)}${queueSuffix}`));
  $('#cancel-job').classList.toggle('hidden', allImagesRendered);
  $('#cancel-job').disabled = Boolean(job.cancel_requested) || allImagesRendered;
  renderLogger();
}

function syncJobDockLayer() {
  const shell = $('#image-viewer-shell');
  if (imageDialog.open) {
    if (jobDock.parentElement !== shell) shell.append(jobDock);
    jobDock.classList.add('in-lightbox');
    return;
  }
  if (jobDock.parentElement !== document.body) document.body.append(jobDock);
  jobDock.classList.remove('in-lightbox');
}

async function pollJob() {
  clearTimeout(state.jobTimer);
  if (!state.job) return;
  try {
    state.job = await api(`/api/jobs/${state.job.id}`);
    syncJobPendingGroups(state.job);
    addOutputs(state.job.outputs || []);
    showJob();
    if (['queued', 'running'].includes(state.job.status)) {
      state.jobTimer = setTimeout(pollJob, 1200);
      return;
    }
    finishJob();
  } catch (error) {
    toast('Lost render status', error.message, 'error');
    state.jobTimer = setTimeout(pollJob, 3000);
  }
}

async function finishJob() {
  const job = state.job;
  syncJobPendingGroups(job);
  syncRenderControls();
  $('#job-dock').classList.add('hidden');
  if (job.status === 'completed') {
    const mediaLabel = job.kind === 'video' || job.generation_mode === 'video' ? 'video' : 'image';
    toast(`${modeTitle(job.generation_mode)} ${tierTitle(job.render_tier).toLowerCase()} complete`, `${job.outputs.length} ${mediaLabel}${job.outputs.length === 1 ? '' : 's'} saved.`, 'success');
    if (mediaLabel !== 'video') switchView('outputs');
  } else if (job.status === 'cancelled') {
    const mediaLabel = job.generation_mode === 'video' ? 'videos' : 'images';
    toast(`${modeTitle(job.generation_mode)} ${tierTitle(job.render_tier).toLowerCase()} cancelled`, `${job.completed} of ${job.total} ${mediaLabel} completed.`);
  } else {
    toast(`${modeTitle(job.generation_mode)} ${tierTitle(job.render_tier).toLowerCase()} failed`, job.error || 'Unknown render error', 'error');
  }
  try {
    const session = await api('/api/jobs');
    syncQueuePlaceholders(session.jobs || []);
    if (session.active_job && session.active_job.id !== job.id) {
      state.job = session.active_job;
      showJob();
      pollJob();
    }
  } catch (error) {
    toast('Could not continue render queue', error.message, 'error');
  }
}

function addOutputs(outputs) {
  const keys = new Set(state.outputs.map(outputIdentity));
  const indexes = new Map(state.outputs.map((output, index) => [outputIdentity(output), index]));
  let added = false;
  let appended = false;
  outputs.forEach((item) => {
    const key = outputIdentity(item);
    const existingIndex = indexes.get(key) ?? -1;
    if (existingIndex >= 0 && item.group_key && !state.outputs[existingIndex].queue_group_key) {
      state.outputs[existingIndex] = {
        ...state.outputs[existingIndex],
        ...item,
        queue_job_id: item.group_key.split(':')[1],
        queue_group_key: item.group_key,
        generation_mode: item.generation_mode,
        render_tier: item.render_tier,
        group_index: item.group_index,
      };
      added = true;
      return;
    }
    if (!keys.has(key) && !state.deletedOutputs.has(key)) {
      state.outputs.push(item.group_key ? {
        ...item,
        queue_job_id: item.group_key.split(':')[1],
        queue_group_key: item.group_key,
      } : item);
      keys.add(key);
      indexes.set(key, state.outputs.length - 1);
      added = true;
      appended = true;
    }
  });
  if (!added) return false;
  if (appended) sortOutputsByFilename();
  renderOutputs();
  return true;
}

function pendingOutput(group, index) {
  const rendering = group.status === 'rendering' && index === 0;
  const position = group.positions?.[index];
  return {
    pending: true,
    key: `${group.group_key}:${position}`,
    job_id: group.job_id,
    position,
    shot: group.shot_numbers[index],
    queue_group_key: group.group_key,
    generation_mode: group.generation_mode,
    render_tier: group.render_tier,
    group_index: group.group_index,
    status: rendering ? 'rendering' : 'queued',
    eta_seconds: rendering ? group.frame_eta_seconds : null,
    observed_at: group.observed_at,
    source_key: group.source_key,
    source_set_key: group.source_set_key,
    source_image: group.source_image,
    name: `pending_${group.job_id}_${String(position).padStart(6, '0')}`,
  };
}

function pendingGroupSignature(group) {
  if (!group) return '';
  return [
    group.group_key, group.positions[0], group.positions.length,
    group.status, group.frame_eta_seconds == null ? 'estimating' : 'estimated',
    group.group_eta_seconds == null ? 'group-estimating' : 'group-estimated',
  ].join(':');
}

function syncJobPendingGroups(job, { render = true } = {}) {
  if (!job) return false;
  const active = ['queued', 'running'].includes(job.status);
  const before = state.pendingGroups.filter((group) => group.job_id === job.id);
  state.pendingGroups = state.pendingGroups.filter((group) => group.job_id !== job.id);
  if (active) state.pendingGroups.push(...(job.pending_groups || []));
  if (!active) {
    state.outputs = state.outputs.map((item) => item.queue_job_id === job.id
      ? Object.fromEntries(Object.entries(item).filter(([key]) => !['queue_job_id', 'queue_group_key'].includes(key)))
      : item);
  }
  const next = active ? (job.pending_groups || []) : [];
  if (before.map(pendingGroupSignature).join('|') === next.map(pendingGroupSignature).join('|')) return false;
  if (render) renderOutputs();
  return true;
}

function syncQueuePlaceholders(jobs) {
  const active = (jobs || []).filter((job) => ['queued', 'running'].includes(job.status));
  const activeIds = new Set(active.map((job) => job.id));
  const beforeGroupCount = state.pendingGroups.length;
  const beforeTaggedCount = state.outputs.filter((item) => item.queue_job_id).length;
  state.pendingGroups = state.pendingGroups.filter((group) => activeIds.has(group.job_id));
  state.outputs = state.outputs.map((item) => item.queue_job_id && !activeIds.has(item.queue_job_id)
    ? Object.fromEntries(Object.entries(item).filter(([key]) => !['queue_job_id', 'queue_group_key'].includes(key)))
    : item);
  let changed = state.pendingGroups.length !== beforeGroupCount
    || state.outputs.filter((item) => item.queue_job_id).length !== beforeTaggedCount;
  active.forEach((job) => {
    changed = syncJobPendingGroups(job, { render: false }) || changed;
  });
  if (changed) renderOutputs();
}

function outputIdentity(item) {
  return item.key || `${item.source || 'output'}:${item.name}`;
}

function sortOutputsByFilename() {
  state.outputs.sort((left, right) => {
    return left.name.localeCompare(right.name)
      || outputIdentity(left).localeCompare(outputIdentity(right));
  });
}

function isRenderActive() {
  return state.job && ['queued', 'running'].includes(state.job.status);
}

function setRenderMode(mode) {
  state.renderMode = mode === 'preview' ? 'preview' : 'production';
  sessionStorage.setItem('valhalla-render-mode', state.renderMode);
  syncRenderControls();
}

function syncRenderControls() {
  const active = Boolean(isRenderActive());
  const preview = state.renderMode === 'preview';
  const baseLabel = preview ? 'Preview storyboard' : 'Render storyboard';
  const idleLabel = state.pendingStructural
    ? (preview ? 'Update & Preview' : 'Update & Render')
    : baseLabel;
  $$('[data-render-control]').forEach((control) => {
    control.classList.toggle('preview', preview);
  });
  $$('[data-render-mode-choice]').forEach((button) => {
    const selected = button.dataset.renderModeChoice === state.renderMode;
    button.classList.toggle('active', selected);
    button.setAttribute('aria-pressed', String(selected));
  });
  $$('[data-render-action]').forEach((button) => {
    button.disabled = false;
    button.textContent = idleLabel;
    button.title = active
      ? 'Add this storyboard after the current render jobs'
      : `${idleLabel} using the ${preview ? 'faster draft' : 'full production'} workflow`;
  });
  form.elements.inference_seed.disabled = active;
  form.elements.inference_strategy.disabled = active;
  $('#randomize-variation-seed').disabled = active;
  if (active) $('#variation-seed-status').textContent = 'Locked while rendering';
  else if ($('#variation-seed-status').textContent === 'Locked while rendering') {
    $('#variation-seed-status').textContent = 'Applied';
  }
}

function syncDeleteControls() {
  const disabled = Boolean(isRenderActive());
  const deleteButton = $('#delete-all-outputs');
  const group = activePhotoshootGroup();
  const hasCompleted = (group?.items.map(({ item }) => item) || galleryMediaItems().map(({ item }) => item))
    .some((item) => !item.pending);
  const mediaGroupList = ['photoshoots', 'videos'].includes(state.galleryView) && !group;
  deleteButton.classList.toggle(
    'hidden', state.outputs.length === 0 || state.galleryBenchmark
      || mediaGroupList || !hasCompleted,
  );
  deleteButton.disabled = disabled || state.galleryBenchmark;
  const groupLabel = group?.identity?.kind === 'random' ? 'random group' : 'photoshoot';
  const mediaGroupLabel = group?.identity?.kind === 'video' ? 'motion group' : groupLabel;
  const deleteLabel = group ? `Delete ${groupLabel}` : 'Delete all';
  deleteButton.textContent = group?.identity?.kind === 'video' ? `Delete ${mediaGroupLabel}` : deleteLabel;
  deleteButton.title = disabled
    ? 'Bulk deletion is unavailable while rendering'
    : (group ? `Delete only the opened ${mediaGroupLabel}` : 'Delete every proof');
  $$('.output-delete, #image-viewer-delete').forEach((button) => {
    button.disabled = false;
    button.title = 'Delete this media item';
  });
}

function confirmDeletion(title, message, confirmLabel) {
  $('#delete-dialog-title').textContent = title;
  $('#delete-dialog-message').textContent = message;
  $('#delete-dialog-confirm').textContent = confirmLabel;
  return new Promise((resolve) => {
    state.deleteResolver = resolve;
    deleteDialog.showModal();
  });
}

function resolveDeletion(value) {
  const resolve = state.deleteResolver;
  state.deleteResolver = null;
  if (deleteDialog.open) deleteDialog.close();
  if (resolve) resolve(value);
}

async function deleteOutput(index) {
  const item = state.outputs[index];
  if (!item || item.pending) return;
  const isolatedPreview = Array.isArray(state.previewOutputIndexes);
  const previewScope = imageDialog.open ? previewOutputs() : [];
  const previewPosition = previewScope.findIndex((entry) => entry.outputIndex === index);
  const confirmed = await confirmDeletion(
    `Delete this ${isVideoOutput(item) ? 'video' : 'image'}?`,
    `${item.name} will be permanently removed from its proof directory.`,
    `Delete ${isVideoOutput(item) ? 'video' : 'image'}`,
  );
  if (!confirmed) return;
  try {
    const key = outputIdentity(item);
    await api(item.url, { method: 'DELETE' });
    state.deletedOutputs.add(key);
    state.outputs = state.outputs.filter((output) => outputIdentity(output) !== key);
    renderOutputs();
    if (imageDialog.open) {
      const remainingScope = previewOutputs();
      if (!remainingScope.length || isolatedPreview || (state.galleryView === 'photoshoots' && !state.galleryGroup)) {
        imageDialog.close();
      } else {
        const next = remainingScope[Math.min(Math.max(0, previewPosition), remainingScope.length - 1)];
        showPreview(next.outputIndex);
      }
    }
  } catch (error) {
    toast(`Could not delete ${isVideoOutput(item) ? 'video' : 'image'}`, error.message, 'error');
  }
}

async function deleteAllOutputs() {
  if (!state.outputs.some((item) => !item.pending)) return;
  if (isRenderActive()) {
    toast('Deletion unavailable', 'Wait for the active render job to finish or cancel it first.', 'error');
    return;
  }
  const group = activePhotoshootGroup();
  const photoshootList = state.galleryView === 'photoshoots' && !group;
  const groupLabel = group?.identity?.kind === 'random' ? 'random group' : 'photoshoot';
  const mediaGroupLabel = group?.identity?.kind === 'video' ? 'motion group' : groupLabel;
  const galleryMediaWord = state.galleryView === 'videos'
    ? 'video' : (state.galleryView === 'flat' ? 'media' : 'image');
  const galleryMediaPlural = state.galleryView === 'videos'
    ? 'videos' : (state.galleryView === 'flat' ? 'items' : 'images');
  const targets = group
    ? group.items.map(({ item }) => item).filter((item) => !item.pending)
    : galleryMediaItems().map(({ item }) => item).filter((item) => !item.pending);
  const count = targets.length;
  const groupDescription = group
    ? `Only the opened ${groupLabel} will be permanently deleted. This cannot be undone.`
    : 'Every image in the configured proof directories will be permanently deleted. This cannot be undone.';
  const mediaDescription = group
    ? `Only the opened ${mediaGroupLabel} will be permanently deleted. This cannot be undone.`
    : (state.galleryView === 'flat'
      ? 'Every image and video in the current gallery will be permanently deleted. This cannot be undone.'
      : `Every ${galleryMediaWord} in the current gallery will be permanently deleted. This cannot be undone.`);
  const countLabel = count === 1 && state.galleryView === 'flat'
    ? '1 media item' : `${count} ${galleryMediaPlural}`;
  const confirmed = await confirmDeletion(
    group ? `Delete this ${mediaGroupLabel} (${count} ${group?.identity?.kind === 'video' ? 'video' : 'image'}${count === 1 ? '' : 's'})?` : `Delete all ${countLabel}?`,
    group?.identity?.kind === 'video' || (!photoshootList && !group)
      ? mediaDescription
      : groupDescription,
    group?.identity?.kind === 'video' ? `Delete ${mediaGroupLabel}` : (group ? `Delete ${groupLabel}` : 'Delete everything'),
  );
  if (!confirmed) return;
  try {
    if (group || state.galleryView !== 'photoshoots') {
      const results = await Promise.allSettled(
        targets.map((item) => api(item.url, { method: 'DELETE' })),
      );
      const deletedKeys = new Set();
      results.forEach((result, index) => {
        if (result.status === 'fulfilled') {
          const key = outputIdentity(targets[index]);
          deletedKeys.add(key);
          state.deletedOutputs.add(key);
        }
      });
      state.outputs = state.outputs.filter(
        (item) => !deletedKeys.has(outputIdentity(item)),
      );
      if (results.some((result) => result.status === 'rejected')) {
        throw new Error(
          `${deletedKeys.size} of ${count} ${state.galleryView === 'videos' ? 'videos' : 'images'} were deleted; ${count - deletedKeys.size} could not be removed.`,
        );
      }
      if (group) {
        state.galleryGroup = null;
        sessionStorage.setItem('valhalla-gallery-group', '');
      }
    } else {
      const result = await api('/api/outputs', { method: 'DELETE' });
      state.outputs = [];
      const deletedLabel = result.deleted === 1 && state.galleryView === 'flat'
        ? '1 media item' : `${result.deleted} ${galleryMediaPlural}`;
      toast('Proofs deleted', `${deletedLabel} permanently removed.`, 'success');
    }
    if (imageDialog.open) imageDialog.close();
    renderOutputs();
    if (group) {
      toast(`${mediaGroupLabel[0].toUpperCase()}${mediaGroupLabel.slice(1)} deleted`, `${count} ${state.galleryView === 'videos' ? 'videos' : 'images'} permanently removed.`, 'success');
    }
  } catch (error) {
    if (imageDialog.open) imageDialog.close();
    renderOutputs();
    toast('Could not delete proofs', error.message, 'error');
  }
}

async function loadOutputs() {
  try {
    const result = await api('/api/outputs');
    state.outputs = result.outputs || [];
    sortOutputsByFilename();
    state.galleryBenchmark = Boolean(result.benchmark);
  } catch (error) {
    toast('Could not load proofs', error.message, 'error');
  }
  renderOutputs();
}

async function restoreApplication() {
  await loadOutputs();
  if (state.galleryBenchmark) {
    switchView('outputs');
    return;
  }
  try {
    const session = await api('/api/jobs');
    state.previewJob = session.latest_preview || null;
    state.job = session.active_job || session.jobs?.[0] || null;
    syncQueuePlaceholders(session.jobs || []);
    (session.jobs || []).forEach((job) => addOutputs(job.outputs || []));
    renderLogger();
    const videoJob = state.job?.kind === 'video' || state.job?.generation_mode === 'video';
    if (state.job && !videoJob) {
      try {
        state.storyboard = await api(`/api/storyboards/${state.job.storyboard_id}`);
        restoreConfig(state.storyboard.config, state.job);
        renderStoryboard();
      } catch (error) {
        toast('Storyboard recovery limited', error.message, 'error');
      }
      showJob();
      if (session.active_job) pollJob();
    }
    if (videoJob && session.active_job) pollJob();
  } catch (error) {
    toast('Could not restore render state', error.message, 'error');
  }
  if (!state.storyboard
    && !(['queued', 'running'].includes(state.job?.status)
      && (state.job?.kind === 'video' || state.job?.generation_mode === 'video'))
    && !state.initialAutoResolved) {
    state.initialAutoResolved = true;
    await resolveStoryboard(null, { initial: true });
  }
  const activeVideoJob = ['queued', 'running'].includes(state.job?.status)
    && (state.job?.kind === 'video' || state.job?.generation_mode === 'video');
  const restoredView = activeVideoJob
    ? 'outputs'
    : (state.restoredView === 'director' && !state.storyboard ? 'studio' : state.restoredView);
  switchView(restoredView);
}

function renderOutputs() {
  const pendingCount = visiblePendingGroups().reduce(
    (count, group) => count + pendingGroupCount(group), 0,
  );
  const completedCount = galleryMediaItems().length;
  const count = completedCount + pendingCount;
  if (state.galleryGroup && !activePhotoshootGroup()) state.galleryGroup = null;
  updateNavigationCount('output-count', state.outputs.length, 'proof');
  $('#outputs-empty').classList.toggle('hidden', count > 0);
  const group = activePhotoshootGroup();
  const groups = galleryGroups();
  const photoshootCount = groups.filter((entry) => entry.identity?.kind === 'photoshoot').length;
  const randomCount = groups.filter((entry) => entry.identity?.kind === 'random').length;
  const videoCount = groups.filter((entry) => entry.identity?.kind === 'video').length;
  const viewMediaPlural = state.galleryView === 'videos'
    ? 'videos' : (state.galleryView === 'flat' ? 'items' : 'images');
  const groupMediaWord = group?.identity?.kind === 'video' ? 'video' : 'image';
  const groupedSummary = [
    videoCount ? `${videoCount} motion group${videoCount === 1 ? '' : 's'}` : '',
    photoshootCount ? `${photoshootCount} photoshoot${photoshootCount === 1 ? '' : 's'}` : '',
    randomCount ? `${randomCount} random group${randomCount === 1 ? '' : 's'}` : '',
    `${completedCount} ${viewMediaPlural}`,
    pendingCount ? `${pendingCount} waiting` : '',
  ].filter(Boolean).join(' · ');
  $$('#gallery-view-toggle button').forEach((button) => {
    button.classList.toggle('active', button.dataset.galleryView === state.galleryView);
    button.setAttribute('aria-pressed', String(button.dataset.galleryView === state.galleryView));
  });
  $('#outputs-summary').textContent = count
    ? (state.galleryBenchmark
      ? `Benchmark: ${count.toLocaleString()} synthetic records.`
      : (group
        ? `${groupEntryCount(group)} ${groupMediaWord}${groupEntryCount(group) === 1 ? '' : 's'} in this group.`
        : (['photoshoots', 'videos'].includes(state.galleryView)
          ? `${groupedSummary}.`
          : `${completedCount === 1 ? '1 generated item' : `${completedCount} generated items`}${pendingCount ? ` · ${pendingCount} waiting` : ''}.`)))
    : (state.galleryView === 'videos'
      ? 'No generated videos.'
      : (state.galleryView === 'flat' ? 'No generated media.' : 'No generated images.'));
  outputRenderSignature = '';
  renderVirtualOutputs(true);
  syncDeleteControls();
}

function measureOutputGrid() {
  const width = outputGrid.clientWidth;
  if (!width) return null;
  const mobile = window.matchMedia('(max-width: 560px)').matches;
  const gap = mobile ? 8 : 14;
  const targetWidth = Math.min(state.galleryCardSize, width);
  const columns = Math.max(1, Math.floor((width + gap) / (targetWidth + gap)));
  const cardWidth = Math.min(targetWidth, (width - gap * (columns - 1)) / columns);
  const cardHeight = cardWidth * 1.25;
  const rowStride = cardHeight + gap;
  const rows = Math.ceil(outputEntryCount() / columns);
  return { width, gap, columns, cardWidth, cardHeight, rowStride, rows };
}

function showingPhotoshootList() {
  return ['photoshoots', 'videos'].includes(state.galleryView) && !state.galleryGroup;
}

function outputEntryCount() {
  if (showingPhotoshootList()) return galleryGroups().length;
  const group = activePhotoshootGroup();
  return group ? groupEntryCount(group) : flatEntryCount();
}

function outputDisplayShot(item, group = null) {
  if (!['photoshoot', 'random'].includes(group?.identity?.kind)) return item.shot;
  const localShot = outputShotSequence(item);
  return Number.isFinite(localShot) ? localShot : item.shot;
}

function outputCardHtml(item, index, layout, position, group = null) {
  const displayShot = outputDisplayShot(item, group);
  const shotLabel = displayShot == null ? 'Output' : `Shot ${displayShot}`;
  if (item.pending) {
    const rendering = item.status === 'rendering';
    const tier = tierTitle(item.render_tier);
    const status = `Shot ${displayShot} (${tier.toLowerCase()})`;
    const deadline = item.eta_seconds != null && Number.isFinite(Number(item.eta_seconds))
      ? new Date(item.observed_at).getTime() + Number(item.eta_seconds) * 1000
      : '';
    return `<article class="output-card pending-output" data-pending-key="${escapeHtml(item.key)}"
      aria-label="${escapeHtml(shotLabel)} ${escapeHtml(status)}">
      <div class="render-placeholder" aria-hidden="true"><span>${escapeHtml(status)}</span><em${rendering ? ` data-pending-deadline="${deadline}"` : ''}>${rendering ? (deadline ? `ETA ${formatDuration(item.eta_seconds)}` : 'ETA estimating') : 'Queued'}</em></div>
    </article>`;
  }
  const visual = state.privacyCovered
    ? '<div class="privacy-placeholder" aria-label="Media hidden by privacy cover"></div>'
    : isVideoOutput(item)
      ? `<div class="video-thumb"><img src="${encodeURI(item.thumbnail_url || item.url)}" alt="Video thumbnail for ${escapeHtml(shotLabel)}" loading="lazy" decoding="async"><span aria-hidden="true">▶</span></div>`
      : `<img src="${encodeURI(item.thumbnail_url || item.url)}" alt="Generated ${escapeHtml(shotLabel)}" loading="lazy" decoding="async">`;
  const mediaLabel = isVideoOutput(item)
    ? `Video ${group?.identity?.kind === 'video' ? position + 1 : ''}`.trim()
    : shotLabel;
  const sourceLabel = item.source_image
    ? `<small class="source-image" title="${escapeHtml(item.source_image)}">from ${escapeHtml(item.source_image)}</small>`
    : '';
  return `<article class="output-card" data-output-index="${index}" tabindex="0" role="button"
    aria-label="Maximize ${escapeHtml(mediaLabel)}" aria-posinset="${position + 1}" aria-setsize="${outputEntryCount()}">
    ${visual}
    <footer><span>${escapeHtml(mediaLabel)}${sourceLabel}</span><span class="output-actions"><a href="${encodeURI(item.url)}" download="${escapeHtml(item.name)}">Download</a>${state.galleryBenchmark ? '' : `<button class="output-delete" data-action="delete-output" aria-label="Delete ${escapeHtml(item.name)}">Delete</button>`}</span></footer>
  </article>`;
}

function refreshPendingCountdowns() {
  $$('[data-pending-deadline]', outputGrid).forEach((node) => {
    const deadline = Number(node.dataset.pendingDeadline);
    if (!Number.isFinite(deadline) || deadline <= 0) {
      node.textContent = 'ETA estimating';
      return;
    }
    const remaining = Math.max(0, (deadline - Date.now()) / 1000);
    node.textContent = remaining > 0 ? `ETA ${formatDuration(remaining)}` : 'Finishing';
  });
}

setInterval(refreshPendingCountdowns, 1000);

function photoshootCardHtml(group, index) {
  const representative = group.items[0]?.item
    || pendingOutput(group.pendingGroup, 0);
  const videoGroup = group.identity?.kind === 'video';
  const title = group.identity?.kind === 'photoshoot'
    ? `Photoshoot ${group.displayNumber}`
    : (group.identity?.kind === 'random'
      ? `Random ${group.displayNumber}`
      : (videoGroup ? 'Motion' : 'Ungrouped'));
  const tier = tierTitle(group.identity?.tier || representative.render_tier);
  const run = videoGroup
    ? `From ${group.identity.source_image || 'source image'}`
    : (group.identity ? formatOutputRun(group.identity.run) : 'Files without photoshoot naming');
  const runTitle = videoGroup
    ? `Source image: ${group.identity.source_image || 'unknown'}`
    : (group.identity ? `Render ID: ${group.identity.run}` : '');
  const pendingGroup = group.pendingGroup;
  const completionDeadline = pendingGroup?.group_eta_seconds != null
    ? new Date(pendingGroup.observed_at).getTime() + Number(pendingGroup.group_eta_seconds) * 1000
    : '';
  const pendingStatus = pendingGroup?.status === 'rendering'
    ? (completionDeadline ? `ETA ${formatDuration(pendingGroup.group_eta_seconds)}` : 'ETA estimating')
    : 'Queued';
  const pendingDeadlineAttribute = pendingGroup?.status === 'rendering'
    ? ` data-pending-deadline="${completionDeadline}"`
    : '';
  const visual = representative.pending
    ? `<div class="render-placeholder"><span>${escapeHtml(title)}</span><em>${escapeHtml(tier)}</em><small${pendingDeadlineAttribute}>${escapeHtml(pendingStatus)}</small></div>`
    : state.privacyCovered
    ? '<div class="privacy-placeholder" aria-label="Media hidden by privacy cover"></div>'
    : videoGroup
      ? `<div class="video-thumb"><img src="${encodeURI(representative.thumbnail_url || representative.url)}" alt="${escapeHtml(title)} thumbnail" loading="lazy" decoding="async"><span aria-hidden="true">▶</span></div>`
      : `<img src="${encodeURI(representative.thumbnail_url || representative.url)}" alt="${escapeHtml(title)} representative frame" loading="lazy" decoding="async">`;
  const footer = representative.pending
    ? `<footer><span>${groupEntryCount(group)} ${videoGroup ? 'videos' : 'frames'}</span></footer>`
    : `<footer><span title="${escapeHtml(runTitle)}"><strong>${escapeHtml(title)}</strong><br>${escapeHtml(tier)} · ${pendingGroup ? `<span${pendingDeadlineAttribute}>${escapeHtml(pendingStatus)}</span>` : escapeHtml(run)}</span><span class="photoshoot-count">${groupEntryCount(group)}</span></footer>`;
  return `<article class="output-card photoshoot-card" data-group-key="${escapeHtml(group.key)}" data-group-index="${index}" tabindex="0" role="button"
    aria-label="Open ${escapeHtml(title)}, ${groupEntryCount(group)} ${group.identity?.kind === 'video' ? 'videos' : 'images'}">
    ${visual}
    ${footer}
  </article>`;
}

function outputEntriesHtml(start, end, layout) {
  if (showingPhotoshootList()) {
    return galleryGroups().slice(start, end)
      .map((group, offset) => photoshootCardHtml(group, start + offset))
      .join('');
  }
  const group = activePhotoshootGroup();
  const entries = [];
  for (let index = start; index < end; index += 1) {
    const entry = group ? groupEntryAt(group, index) : flatEntryAt(index);
    if (entry) entries.push(outputCardHtml(entry.item, entry.outputIndex, layout, index, group));
  }
  return entries.join('');
}

function updateGalleryBenchmarkSummary() {
  if (!state.galleryBenchmark) return;
  $('#outputs-summary').textContent = `Benchmark: ${state.outputs.length.toLocaleString()} records · ${outputGrid.childElementCount} cards in DOM.`;
}

function renderVirtualOutputs(force = false) {
  outputLayout = measureOutputGrid();
  const entryCount = outputEntryCount();
  if (!outputLayout || !entryCount) {
    outputGrid.classList.remove('virtualized');
    outputGrid.style.paddingTop = '';
    outputGrid.style.paddingBottom = '';
    outputGrid.innerHTML = '';
    return;
  }
  outputGrid.style.setProperty('--output-columns', String(outputLayout.columns));
  outputGrid.style.setProperty('--output-card-width', `${outputLayout.cardWidth}px`);
  outputGrid.style.setProperty('--output-gap', `${outputLayout.gap}px`);
  if (entryCount <= OUTPUT_VIRTUALIZATION_THRESHOLD) {
    outputGrid.classList.remove('virtualized');
    outputGrid.style.paddingTop = '';
    outputGrid.style.paddingBottom = '';
    const signature = `native:${state.galleryView}:${state.galleryGroup}:${outputLayout.width}:${entryCount}:${state.outputs.length}`;
    if (!force && signature === outputRenderSignature) return;
    outputRenderSignature = signature;
    outputGrid.innerHTML = outputEntriesHtml(0, entryCount, outputLayout);
    syncDeleteControls();
    updateGalleryBenchmarkSummary();
    return;
  }
  outputGrid.classList.add('virtualized');
  const { rows, rowStride, cardHeight, columns } = outputLayout;
  const rect = outputGrid.getBoundingClientRect();
  const firstVisibleRow = Math.max(0, Math.floor(-rect.top / rowStride));
  const lastVisibleRow = Math.min(
    rows - 1,
    Math.floor((window.innerHeight - rect.top) / rowStride),
  );
  const firstRow = Math.max(0, firstVisibleRow - OUTPUT_OVERSCAN_ROWS);
  const lastRow = Math.min(
    rows - 1,
    Math.max(firstRow, lastVisibleRow) + OUTPUT_OVERSCAN_ROWS,
  );
  const start = firstRow * columns;
  const end = Math.min(entryCount, (lastRow + 1) * columns);
  outputGrid.style.paddingTop = `${firstRow * rowStride}px`;
  outputGrid.style.paddingBottom = `${Math.max(0, rows - lastRow - 1) * rowStride}px`;
  const signature = `${state.galleryView}:${state.galleryGroup}:${start}:${end}:${columns}:${outputLayout.width}:${entryCount}:${state.outputs.length}`;
  if (!force && signature === outputRenderSignature) return;
  outputRenderSignature = signature;
  outputGrid.innerHTML = outputEntriesHtml(start, end, outputLayout);
  syncDeleteControls();
  updateGalleryBenchmarkSummary();
}

function galleryViewportAnchor() {
  const layout = outputLayout || measureOutputGrid();
  const entryCount = outputEntryCount();
  if (!layout || !entryCount) return null;
  const gridTop = window.scrollY + outputGrid.getBoundingClientRect().top;
  if (window.scrollY < gridTop) return null;
  const row = Math.min(layout.rows - 1, Math.max(0, Math.floor((window.scrollY - gridTop) / layout.rowStride)));
  return {
    index: Math.min(entryCount - 1, row * layout.columns),
    offset: window.scrollY - (gridTop + row * layout.rowStride),
  };
}

function syncGallerySizeControls() {
  $('#gallery-size').value = String(state.galleryCardSize);
  $('#gallery-size-output').textContent = `${Math.round(state.galleryCardSize / GALLERY_CARD_DEFAULT * 100)}%`;
}

function setGalleryCardSize(value, { preserveAnchor = true } = {}) {
  const next = Math.min(GALLERY_CARD_MAX, Math.max(GALLERY_CARD_MIN, Math.round(Number(value) / 10) * 10));
  if (!Number.isFinite(next) || next === state.galleryCardSize) return;
  const anchor = preserveAnchor ? galleryViewportAnchor() : null;
  state.galleryCardSize = next;
  localStorage.setItem('valhalla-gallery-thumbnail-size', String(next));
  syncGallerySizeControls();
  outputRenderSignature = '';
  renderVirtualOutputs(true);
  if (!anchor) return;
  requestAnimationFrame(() => {
    const layout = measureOutputGrid();
    if (!layout) return;
    const gridTop = window.scrollY + outputGrid.getBoundingClientRect().top;
    const row = Math.floor(anchor.index / layout.columns);
    window.scrollTo({ top: Math.max(0, gridTop + row * layout.rowStride + anchor.offset), behavior: 'auto' });
    renderVirtualOutputs(true);
  });
}

function scheduleVirtualOutputRender(force = false) {
  if (force) outputRenderSignature = '';
  if (outputRenderFrame != null) return;
  outputRenderFrame = requestAnimationFrame(() => {
    outputRenderFrame = null;
    if ($('#outputs-view').classList.contains('active')) renderVirtualOutputs(force);
  });
}

function focusOutputCard(index, { alignTop = false } = {}) {
  if (!state.outputs[index]) return;
  outputLayout = measureOutputGrid();
  if (!outputLayout) return;
  const displayIndex = displayedOutputs().findIndex((entry) => entry.outputIndex === index);
  if (displayIndex < 0) return;
  const row = Math.floor(displayIndex / outputLayout.columns);
  const gridTop = window.scrollY + outputGrid.getBoundingClientRect().top;
  const cardTop = gridTop + row * outputLayout.rowStride;
  const cardBottom = cardTop + outputLayout.cardHeight;
  if (alignTop || cardTop < window.scrollY || cardBottom > window.scrollY + window.innerHeight) {
    window.scrollTo({ top: cardTop, behavior: 'auto' });
  }
  outputRenderSignature = '';
  requestAnimationFrame(() => {
    renderVirtualOutputs(true);
    requestAnimationFrame(() => {
      const card = $(`.output-card[data-output-index="${index}"]`, outputGrid);
      if (card) card.focus({ preventScroll: true });
    });
  });
}

function persistPreviewScale() {
  sessionStorage.setItem('valhalla-preview-zoom', String(state.previewZoom));
  sessionStorage.setItem('valhalla-preview-fit', String(state.previewFit));
}

function syncPreviewScaleControls() {
  $('#image-fit').checked = state.previewFit;
  $('#image-zoom').value = String(state.previewZoom);
  $('#image-zoom-output').textContent = `${state.previewZoom}%`;
}

function activePreviewMedia() {
  return isVideoOutput(state.outputs[state.previewIndex]) ? $('#image-viewer-video') : $('#image-viewer-image');
}

function syncVideoLoopControl() {
  const control = $('#image-video-loop');
  const video = $('#image-viewer-video');
  control.disabled = !isVideoOutput(state.outputs[state.previewIndex]) || state.privacyCovered;
  control.checked = state.videoLoop;
  video.loop = state.videoLoop && !state.slideshowActive;
}

function formatVideoTime(seconds) {
  const value = Number.isFinite(Number(seconds)) ? Math.max(0, Math.floor(Number(seconds))) : 0;
  const minutes = Math.floor(value / 60);
  const rest = value % 60;
  if (minutes >= 60) {
    const hours = Math.floor(minutes / 60);
    return `${hours}:${String(minutes % 60).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
  }
  return `${minutes}:${String(rest).padStart(2, '0')}`;
}

function syncViewerVideoControls() {
  const video = $('#image-viewer-video');
  const progress = $('#image-viewer-video-progress');
  const duration = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : 0;
  progress.max = String(duration || 1);
  progress.value = String(duration ? Math.min(video.currentTime, duration) : 0);
  progress.disabled = !duration;
  $('#image-viewer-video-time').textContent = `${formatVideoTime(video.currentTime)} / ${formatVideoTime(duration)}`;
  const play = $('#image-viewer-video-play');
  play.textContent = video.paused ? '▶' : '❚❚';
  play.setAttribute('aria-label', video.paused ? 'Play video' : 'Pause video');
  play.title = video.paused ? 'Play video' : 'Pause video';
  const muted = video.muted || video.volume === 0;
  const mute = $('#image-viewer-video-mute');
  mute.textContent = muted ? '🔇' : '🔊';
  mute.setAttribute('aria-label', muted ? 'Unmute video' : 'Mute video');
  mute.title = muted ? 'Unmute video' : 'Mute video';
  $('#image-viewer-video-volume').value = String(muted ? 0 : video.volume);
}

function syncViewerVideoControlPosition() {
  const controls = $('#image-viewer-video-controls');
  const video = $('#image-viewer-video');
  if (!imageStage.classList.contains('video-active') || video.classList.contains('hidden')) {
    controls.style.removeProperty('left');
    controls.style.removeProperty('right');
    controls.style.removeProperty('bottom');
    controls.style.removeProperty('width');
    return;
  }
  const stageRect = imageStage.getBoundingClientRect();
  const videoRect = video.getBoundingClientRect();
  if (!videoRect.width || !videoRect.height) return;
  controls.style.left = `${videoRect.left - stageRect.left}px`;
  controls.style.right = 'auto';
  controls.style.bottom = `${stageRect.bottom - videoRect.bottom}px`;
  controls.style.width = `${videoRect.width}px`;
}

function hideViewerVideoControls() {
  clearTimeout(state.videoControlsTimer);
  state.videoControlsTimer = null;
  imageStage.classList.remove('video-controls-visible');
}

function revealViewerVideoControls({ autoHide = true } = {}) {
  const video = $('#image-viewer-video');
  if (!isVideoOutput(state.outputs[state.previewIndex]) || state.privacyCovered) return;
  imageStage.classList.add('video-controls-visible');
  clearTimeout(state.videoControlsTimer);
  state.videoControlsTimer = null;
  if (autoHide && !video.paused) {
    state.videoControlsTimer = setTimeout(() => {
      state.videoControlsTimer = null;
      if (!video.paused) imageStage.classList.remove('video-controls-visible');
    }, 2000);
  }
}

function toggleViewerVideoPlayback() {
  const video = $('#image-viewer-video');
  if (!isVideoOutput(state.outputs[state.previewIndex]) || state.privacyCovered) return;
  revealViewerVideoControls({ autoHide: true });
  if (video.paused) video.play().catch(() => {});
  else video.pause();
}

function previewPanBounds() {
  const image = activePreviewMedia();
  const stage = $('.image-stage');
  return {
    x: Math.max(0, (image.offsetWidth - stage.clientWidth) / 2),
    y: Math.max(0, (image.offsetHeight - stage.clientHeight) / 2),
  };
}

function applyPreviewPan() {
  const bounds = previewPanBounds();
  state.previewPanX = Math.max(-bounds.x, Math.min(bounds.x, state.previewPanX));
  state.previewPanY = Math.max(-bounds.y, Math.min(bounds.y, state.previewPanY));
  const image = activePreviewMedia();
  image.style.transform = `translate(-50%, -50%) translate(${state.previewPanX}px, ${state.previewPanY}px) scale(var(--preview-pinch-scale, 1))`;
  $('.image-stage').classList.toggle('pannable', bounds.x > 0 || bounds.y > 0);
}

function resetPreviewPan() {
  state.previewPanX = 0;
  state.previewPanY = 0;
  applyPreviewPan();
}

function unloadViewerVideo() {
  const video = $('#image-viewer-video');
  hideViewerVideoControls();
  imageStage.classList.remove('video-active');
  video.pause();
  video.onended = null;
  video.onloadedmetadata = null;
  video.removeAttribute('src');
  video.load();
  video.loop = state.videoLoop;
  video.removeAttribute('data-output-key');
  video.style.removeProperty('width');
  video.style.removeProperty('height');
}

state.previewZoom = Math.min(300, Math.max(25, Number(state.previewZoom) || 100));
function fitPreviewMedia() {
  const image = $('#image-viewer-image');
  const video = $('#image-viewer-video');
  const stage = $('.image-stage');
  const stageWidth = stage.clientWidth;
  const stageHeight = stage.clientHeight;
  const videoActive = !video.classList.contains('hidden') && video.videoWidth && video.videoHeight;
  const mediaWidth = videoActive ? video.videoWidth : image.naturalWidth;
  const mediaHeight = videoActive ? video.videoHeight : image.naturalHeight;
  if (!mediaWidth || !mediaHeight || !stageWidth || !stageHeight) return;
  const mobilePortrait = window.matchMedia('(max-width: 560px) and (orientation: portrait)').matches;
  const scale = state.previewFit
    ? (mobilePortrait
      ? stageHeight / mediaHeight
      : Math.min(stageWidth / mediaWidth, stageHeight / mediaHeight))
    : state.previewZoom / 100;
  const media = videoActive ? video : image;
  media.style.width = `${Math.round(mediaWidth * scale)}px`;
  media.style.height = `${Math.round(mediaHeight * scale)}px`;
  $('#image-zoom-output').textContent = `${Math.round(scale * 100)}%`;
  applyPreviewPan();
  syncViewerVideoControlPosition();
}

function setPreviewZoom(value) {
  state.previewZoom = Math.min(300, Math.max(25, Number(value) || 100));
  state.previewFit = false;
  persistPreviewScale();
  syncPreviewScaleControls();
  fitPreviewMedia();
}

function setPreviewFit(value) {
  state.previewFit = Boolean(value);
  if (!state.previewFit) state.previewZoom = 100;
  persistPreviewScale();
  syncPreviewScaleControls();
  fitPreviewMedia();
}

function showPreview(index) {
  if (!state.outputs.length) return;
  const scope = previewOutputs();
  if (!scope.length) return;
  let position = scope.findIndex((entry) => entry.outputIndex === index);
  if (position < 0) position = 0;
  state.previewIndex = scope[position].outputIndex;
  const item = state.outputs[state.previewIndex];
  const image = $('#image-viewer-image');
  const video = $('#image-viewer-video');
  const videoOutput = isVideoOutput(item);
  const createVideoButton = $('#image-create-video');
  state.videoSource = !videoOutput ? item : null;
  hideViewerVideoControls();
  imageStage.classList.remove('video-active');
  video.onended = null;
  video.onloadedmetadata = null;
  createVideoButton.classList.toggle('hidden', videoOutput || state.privacyCovered);
  createVideoButton.disabled = videoOutput || state.privacyCovered;
  resetPreviewPan();
  if (state.privacyCovered) {
    image.removeAttribute('src');
    video.pause();
    video.removeAttribute('src');
    video.load();
  } else if (videoOutput) {
    image.removeAttribute('src');
    video.src = item.url;
    video.load();
  } else {
    unloadViewerVideo();
    image.src = item.url;
  }
  imageStage.classList.toggle('video-active', videoOutput && !state.privacyCovered);
  image.classList.toggle('hidden', videoOutput);
  video.classList.toggle('hidden', !videoOutput || state.privacyCovered);
  video.controls = false;
  video.tabIndex = videoOutput && !state.privacyCovered ? 0 : -1;
  video.autoplay = videoOutput && !state.privacyCovered;
  video.loop = videoOutput && !state.privacyCovered && state.videoLoop && !state.slideshowActive;
  video.style.display = videoOutput && !state.privacyCovered ? '' : 'none';
  if (videoOutput) video.dataset.outputKey = outputIdentity(item);
  video.onloadedmetadata = () => {
    fitPreviewMedia();
    syncViewerVideoControls();
    if (videoOutput && !state.privacyCovered) video.play().catch(() => {});
  };
  syncViewerVideoControls();
  if (videoOutput && !state.privacyCovered) video.play().catch(() => {});
  image.alt = `Maximized generated output from shot ${outputDisplayShot(item)}`;
  const displayName = videoOutput
    ? (item.source_shot != null ? `Video from shot ${item.source_shot}` : 'Video')
    : (outputDisplayShot(item) != null ? `Shot ${outputDisplayShot(item)}` : 'Image');
  const previewTitle = $('#image-viewer-title');
  previewTitle.textContent = state.privacyCovered ? 'Preview' : displayName;
  previewTitle.title = state.privacyCovered ? '' : item.name;
  $('#image-viewer-count').textContent = `${position + 1} of ${scope.length}`;
  $('#image-viewer-download').href = item.url;
  $('#image-viewer-download').download = item.name;
  const single = scope.length < 2;
  $('#image-previous').disabled = single;
  $('#image-next').disabled = single;
  if (single && state.slideshowActive) stopSlideshow();
  else syncSlideshowControls();
}

function openPreview(index) {
  state.previewOutputIndexes = null;
  showPreview(index);
  if (!imageDialog.open) imageDialog.showModal();
  syncJobDockLayer();
  requestAnimationFrame(fitPreviewMedia);
}

function savedVideoDuration() {
  const value = Number(localStorage.getItem('valhalla-video-duration'));
  return Number.isInteger(value) && value >= 1 && value <= 60 ? value : 5;
}

function savedVideoPromptGuidance() {
  return localStorage.getItem('valhalla-video-prompt-guidance') || '';
}

function openVideoDialog() {
  const source = state.videoSource || state.outputs[state.previewIndex];
  if (!source || source.pending || isVideoOutput(source) || state.privacyCovered) return;
  state.videoSource = source;
  $('#video-source-label').textContent = source.name;
  $('#video-prompt-guidance').value = savedVideoPromptGuidance();
  $('#video-prompt').value = localStorage.getItem('valhalla-video-prompt') || '';
  $('#video-duration').value = String(savedVideoDuration());
  videoDialog.showModal();
  requestAnimationFrame(() => $('#video-prompt-guidance').focus());
}

async function createVideoPrompt() {
  const source = state.videoSource;
  const guidanceInput = $('#video-prompt-guidance');
  const promptInput = $('#video-prompt');
  const durationInput = $('#video-duration');
  const guidance = guidanceInput.value.trim();
  const duration = Math.round(Number(durationInput.value));
  if (!source) return;
  if (!Number.isInteger(duration) || duration < 1 || duration > 60) {
    toast('Invalid duration', 'Choose a whole number of seconds from 1 to 60.', 'error');
    durationInput.focus();
    return;
  }
  const button = $('#video-create-prompt');
  localStorage.setItem('valhalla-video-prompt-guidance', guidance);
  setBusy(button, true, 'Creating…');
  try {
    const result = await api('/api/video-prompts', {
      method: 'POST',
      body: JSON.stringify({
        source: source.source || 'output',
        relative_path: source.relative_path || source.name,
        user_guidance: guidance,
        video_length: duration,
      }),
    });
    promptInput.value = result.prompt || '';
    toast('Video prompt created', 'Review or edit the generated LTX prompt before queueing.', 'success');
    promptInput.focus();
  } catch (error) {
    toast('Could not create video prompt', error.message, 'error');
  } finally {
    setBusy(button, false);
  }
}

async function submitVideo() {
  const source = state.videoSource;
  const promptInput = $('#video-prompt');
  const durationInput = $('#video-duration');
  const prompt = promptInput.value.trim();
  const duration = Math.round(Number(durationInput.value));
  if (!source || !prompt) {
    toast('Video prompt required', 'Describe the motion before queuing the video.', 'error');
    promptInput.focus();
    return;
  }
  if (!Number.isInteger(duration) || duration < 1 || duration > 60) {
    toast('Invalid duration', 'Choose a whole number of seconds from 1 to 60.', 'error');
    durationInput.focus();
    return;
  }
  localStorage.setItem('valhalla-video-prompt', prompt);
  localStorage.setItem('valhalla-video-duration', String(duration));
  const returnGalleryLocation = {
    view: state.galleryView,
    group: state.galleryGroup,
  };
  const button = $('#video-submit');
  const alreadyActive = Boolean(isRenderActive());
  const previousActiveId = alreadyActive ? state.job.id : null;
  setBusy(button, true, 'Queueing…');
  try {
    const queuedJob = await api('/api/videos', {
      method: 'POST',
      body: JSON.stringify({
        source: source.source || 'output',
        relative_path: source.relative_path || source.name,
        prompt,
        duration,
        source_metadata: {
          key: source.key,
          source_key: source.source_key,
          source_image: source.name,
          source_set_key: source.source_set_key || source.set_key,
          generation_mode: source.generation_mode,
          render_tier: source.render_tier,
          group_index: source.group_index,
          shot: source.shot,
        },
      }),
    });
    videoDialog.close();
    if (imageDialog.open) imageDialog.close();
    await trackQueuedJob(queuedJob, previousActiveId);
    restoreGalleryLocation(returnGalleryLocation);
    toast(
      alreadyActive ? 'Video added to queue' : 'Video queued',
      `Video from ${source.name} is waiting${alreadyActive ? ` at position ${queuedJob.queue_position}` : ''}.`,
      'success',
    );
  } catch (error) {
    toast('Could not queue video', error.message, 'error');
  } finally {
    setBusy(button, false);
  }
}

function movePreview(direction) {
  const scope = previewOutputs();
  const position = scope.findIndex((entry) => entry.outputIndex === state.previewIndex);
  const next = scope[(position + direction + scope.length) % scope.length];
  if (next) showPreview(next.outputIndex);
  if (state.slideshowActive) scheduleSlideshow();
}

function movePreviewRandom() {
  const scope = previewOutputs();
  if (scope.length < 2) return;
  const position = scope.findIndex((entry) => entry.outputIndex === state.previewIndex);
  const offset = 1 + Math.floor(Math.random() * (scope.length - 1));
  const next = scope[(position + offset) % scope.length];
  if (next) showPreview(next.outputIndex);
  if (state.slideshowActive) scheduleSlideshow();
}

function movePreviewVideo(direction) {
  const scope = previewOutputs().filter((entry) => isVideoOutput(entry.item));
  if (!scope.length) return;
  const position = scope.findIndex((entry) => entry.outputIndex === state.previewIndex);
  const start = position < 0 ? 0 : position;
  const next = scope[(start + direction + scope.length) % scope.length];
  if (next) showPreview(next.outputIndex);
  if (state.slideshowActive) scheduleSlideshow();
}

function movePreviewVideoRandom() {
  const scope = previewOutputs().filter((entry) => isVideoOutput(entry.item));
  if (!scope.length) return;
  const position = scope.findIndex((entry) => entry.outputIndex === state.previewIndex);
  const start = position < 0 ? 0 : position;
  const offset = scope.length < 2 ? 0 : 1 + Math.floor(Math.random() * (scope.length - 1));
  const next = scope[(start + offset) % scope.length];
  if (next) showPreview(next.outputIndex);
  if (state.slideshowActive) scheduleSlideshow();
}

function syncSlideshowControls() {
  const button = $('#image-slideshow-toggle');
  const active = state.slideshowActive && previewOutputs().length > 1;
  button.classList.toggle('active', active);
  button.disabled = previewOutputs().length < 2;
  button.querySelector('span').textContent = active ? '■' : '▶';
  button.querySelector('strong').textContent = active ? 'Stop slideshow' : 'Slideshow';
  button.setAttribute('aria-label', active ? 'Stop slideshow' : 'Start slideshow');
  $$('[data-slideshow-delay]').forEach((choice) => {
    const selected = Number(choice.dataset.slideshowDelay) === state.slideshowDelay;
    choice.classList.toggle('active', selected);
    choice.setAttribute('aria-pressed', String(selected));
  });
  $('#slideshow-random').checked = state.slideshowRandom;
  syncVideoLoopControl();
}

function scheduleSlideshow() {
  clearTimeout(state.slideshowTimer);
  state.slideshowTimer = null;
  if (!state.slideshowActive || !imageDialog.open || previewOutputs().length < 2) return;
  const current = state.outputs[state.previewIndex];
  const video = $('#image-viewer-video');
  if (isVideoOutput(current)) {
    video.loop = false;
    const advance = () => {
      if (!state.slideshowActive || !imageDialog.open) return;
      if (state.slideshowRandom) movePreviewVideoRandom();
      else movePreviewVideo(1);
    };
    video.onended = advance;
    if (video.ended) {
      state.slideshowTimer = setTimeout(() => {
        state.slideshowTimer = null;
        advance();
      }, 0);
    }
    return;
  }
  state.slideshowTimer = setTimeout(() => {
    if (state.slideshowRandom) movePreviewRandom();
    else movePreview(1);
  }, state.slideshowDelay * 1000);
}

function stopSlideshow() {
  state.slideshowActive = false;
  clearTimeout(state.slideshowTimer);
  state.slideshowTimer = null;
  syncSlideshowControls();
}

function toggleSlideshow() {
  if (state.slideshowActive) {
    stopSlideshow();
    return;
  }
  if (previewOutputs().length < 2) return;
  state.slideshowActive = true;
  syncSlideshowControls();
  scheduleSlideshow();
}

function syncTrueFullscreenControl() {
  const button = $('#image-true-fullscreen');
  const target = $('#image-viewer-shell');
  const active = isViewerFullscreen();
  button.textContent = active ? '⤡' : '⤢';
  button.disabled = false;
  button.setAttribute('aria-label', active ? 'Exit browser fullscreen' : 'Enter browser fullscreen');
  button.title = active ? 'Exit browser fullscreen' : 'Enter browser fullscreen';
  button.classList.toggle('active', active);
}

function isViewerFullscreen() {
  const target = $('#image-viewer-shell');
  return document.fullscreenElement === target || target.classList.contains('fallback-fullscreen');
}

function setFallbackFullscreen(active) {
  const target = $('#image-viewer-shell');
  target.classList.toggle('fallback-fullscreen', active);
  syncTrueFullscreenControl();
  if (active) showFullscreenControls();
  else showFullscreenControls({ autoHide: false });
  requestAnimationFrame(fitPreviewMedia);
}

function hideFullscreenControls() {
  state.fullscreenControlsTimer = null;
  const shell = $('#image-viewer-shell');
  if (isViewerFullscreen()) shell.classList.add('controls-hidden');
}

function showFullscreenControls({ autoHide = true } = {}) {
  const shell = $('#image-viewer-shell');
  shell.classList.remove('controls-hidden');
  clearTimeout(state.fullscreenControlsTimer);
  state.fullscreenControlsTimer = null;
  if (autoHide && isViewerFullscreen()) {
    state.fullscreenControlsTimer = setTimeout(hideFullscreenControls, 2200);
  }
}

async function toggleTrueFullscreen() {
  const target = $('#image-viewer-shell');
  if (document.fullscreenElement === target) {
    await document.exitFullscreen().catch(() => setFallbackFullscreen(false));
    return;
  }
  if (target.classList.contains('fallback-fullscreen')) {
    setFallbackFullscreen(false);
    return;
  }
  if (target.requestFullscreen) {
    try {
      await target.requestFullscreen();
      return;
    } catch { /* Use the viewport fallback below. */ }
  }
  setFallbackFullscreen(true);
}

function syncOutputGridToPreview() {
  if (!state.outputs.length || !$('#outputs-view').classList.contains('active')) return;
  requestAnimationFrame(() => focusOutputCard(state.previewIndex, { alignTop: true }));
}

function rememberFlatGalleryPosition() {
  if (state.galleryView !== 'flat') return;
  state.flatScrollY = window.scrollY;
  const focused = document.activeElement?.closest?.('.output-card[data-output-index]');
  const firstVisible = $$('.output-card[data-output-index]', outputGrid)
    .find((card) => card.getBoundingClientRect().bottom > 0);
  const anchor = focused || firstVisible;
  const item = anchor ? state.outputs[Number(anchor.dataset.outputIndex)] : null;
  state.flatFocusKey = item ? outputIdentity(item) : null;
}

function setGalleryView(view) {
  const next = ['photoshoots', 'flat', 'videos'].includes(view) ? view : 'photoshoots';
  if (next === state.galleryView && !state.galleryGroup) return;
  rememberFlatGalleryPosition();
  rememberProofsPosition();
  state.galleryView = next;
  state.galleryGroup = null;
  sessionStorage.setItem('valhalla-gallery-view', next);
  sessionStorage.setItem('valhalla-gallery-group', '');
  renderOutputs();
  if (next === 'flat') {
    requestAnimationFrame(() => {
      const saved = Number(state.proofsPositions[proofsPositionKey()]);
      window.scrollTo({ top: Number.isFinite(saved) ? saved : state.flatScrollY, behavior: 'auto' });
      renderVirtualOutputs(true);
      if (state.flatFocusKey) {
        const index = state.outputs.findIndex((item) => outputIdentity(item) === state.flatFocusKey);
        const card = $(`.output-card[data-output-index="${index}"]`, outputGrid);
        if (card) card.focus({ preventScroll: true });
      }
    });
  } else {
    restoreProofsPosition({ fallbackToGrid: true });
  }
}

function openPhotoshoot(key) {
  rememberProofsPosition();
  state.galleryGroup = key;
  sessionStorage.setItem('valhalla-gallery-group', key);
  renderOutputs();
  restoreProofsPosition({ fallbackToGrid: true });
}

function restoreGalleryLocation(location) {
  if (!location || !['photoshoots', 'flat', 'videos'].includes(location.view)) return;
  const changed = state.galleryView !== location.view || state.galleryGroup !== location.group;
  state.galleryView = location.view;
  state.galleryGroup = location.group || null;
  sessionStorage.setItem('valhalla-gallery-view', state.galleryView);
  sessionStorage.setItem('valhalla-gallery-group', state.galleryGroup || '');
  if (!changed) return;
  outputRenderSignature = '';
  renderOutputs();
  restoreProofsPosition({ fallbackToGrid: true });
}

function closePhotoshoot() {
  if (!state.galleryGroup) return false;
  rememberProofsPosition();
  state.galleryGroup = null;
  sessionStorage.setItem('valhalla-gallery-group', '');
  renderOutputs();
  restoreProofsPosition({ fallbackToGrid: true });
  return true;
}

window.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape' || !state.galleryGroup) return;
  if (!$('#outputs-view').classList.contains('active')) return;
  if ($('dialog[open]') || !$('#shot-preview-window').classList.contains('hidden')) return;
  if ($('#image-slideshow-delay').open || $('#studio-files-menu').open || $('[data-render-mode][open]')) return;
  if ($('#system-card').classList.contains('mobile-open')) return;
  event.preventDefault();
  event.stopPropagation();
  closePhotoshoot();
}, { capture: true });

outputGrid.addEventListener('click', (event) => {
  const card = event.target.closest('.output-card');
  if (!card) return;
  if (card.dataset.groupKey) {
    openPhotoshoot(card.dataset.groupKey);
    return;
  }
  if (card.classList.contains('pending-output')) return;
  if (event.target.closest('[data-action="delete-output"]')) {
    deleteOutput(Number(card.dataset.outputIndex));
    return;
  }
  if (event.target.closest('a, button')) return;
  openPreview(Number(card.dataset.outputIndex));
});
$('#image-fit').addEventListener('change', (event) => setPreviewFit(event.target.checked));
$('#image-zoom').addEventListener('input', (event) => setPreviewZoom(event.target.value));
$('#image-zoom').addEventListener('dblclick', () => setPreviewZoom(100));
$('#image-video-loop').addEventListener('change', (event) => {
  state.videoLoop = event.currentTarget.checked;
  localStorage.setItem('valhalla-video-loop', String(state.videoLoop));
  syncVideoLoopControl();
});
$('#image-true-fullscreen').addEventListener('click', toggleTrueFullscreen);
$('#image-slideshow-toggle').addEventListener('click', toggleSlideshow);
$$('[data-slideshow-delay]').forEach((button) => button.addEventListener('click', (event) => {
  state.slideshowDelay = Math.min(10, Math.max(1, Number(event.currentTarget.dataset.slideshowDelay) || 3));
  sessionStorage.setItem('valhalla-slideshow-delay', String(state.slideshowDelay));
  syncSlideshowControls();
  $('#image-slideshow-delay').open = false;
  if (state.slideshowActive) scheduleSlideshow();
}));
$('#slideshow-random').addEventListener('change', (event) => {
  state.slideshowRandom = event.currentTarget.checked;
  sessionStorage.setItem('valhalla-slideshow-random', String(state.slideshowRandom));
  if (state.slideshowActive) scheduleSlideshow();
});
document.addEventListener('click', (event) => {
  const menu = $('#image-slideshow-delay');
  if (menu.open && !event.target.closest('#image-slideshow-delay')) menu.open = false;
});
document.addEventListener('keydown', (event) => {
  const menu = $('#image-slideshow-delay');
  if (event.key === 'Escape' && menu.open) {
    event.preventDefault();
    event.stopPropagation();
    menu.open = false;
  }
});
document.addEventListener('fullscreenchange', () => {
  syncTrueFullscreenControl();
  if (document.fullscreenElement === $('#image-viewer-shell')) showFullscreenControls();
  else showFullscreenControls({ autoHide: false });
  if (imageDialog.open) requestAnimationFrame(fitPreviewMedia);
});
syncTrueFullscreenControl();
$('#image-viewer-shell').addEventListener('pointermove', (event) => {
  if (!isViewerFullscreen() || event.clientY > 90) return;
  showFullscreenControls();
});
$('.image-viewer-bar').addEventListener('pointermove', () => {
  if (isViewerFullscreen()) showFullscreenControls();
});
$('#image-viewer-image').addEventListener('load', fitPreviewMedia);
window.addEventListener('resize', () => {
  if (imageDialog.open) fitPreviewMedia();
});

outputGrid.addEventListener('keydown', (event) => {
  if (event.target.closest('a, button')) return;
  const card = event.target.closest('.output-card');
  if (!card) return;
  if (card.dataset.groupKey) {
    if (['Enter', ' '].includes(event.key)) {
      event.preventDefault();
      openPhotoshoot(card.dataset.groupKey);
    }
    return;
  }
  if (card.classList.contains('pending-output')) return;
  const index = Number(card.dataset.outputIndex);
  if (['Enter', ' '].includes(event.key)) {
    event.preventDefault();
    openPreview(index);
    return;
  }
  const columns = outputLayout?.columns || 1;
  const movement = {
    ArrowLeft: -1, ArrowRight: 1, ArrowUp: -columns, ArrowDown: columns,
  }[event.key];
  if (movement == null) return;
  event.preventDefault();
  const entries = displayedOutputs().filter((entry) => !entry.item.pending);
  const position = entries.findIndex((entry) => entry.outputIndex === index);
  if (position < 0) return;
  const next = entries[Math.max(0, Math.min(entries.length - 1, position + movement))];
  if (next) focusOutputCard(next.outputIndex);
});

$$('#gallery-view-toggle button').forEach((button) => {
  button.addEventListener('click', () => setGalleryView(button.dataset.galleryView));
});

$('#gallery-size').addEventListener('input', (event) => setGalleryCardSize(event.currentTarget.value));
$('#gallery-size-smaller').addEventListener('click', () => setGalleryCardSize(state.galleryCardSize - 20));
$('#gallery-size-larger').addEventListener('click', () => setGalleryCardSize(state.galleryCardSize + 20));
$('#gallery-size-reset').addEventListener('click', () => setGalleryCardSize(GALLERY_CARD_DEFAULT));
window.addEventListener('wheel', (event) => {
  if (!(event.ctrlKey || event.metaKey)) return;
  if (!outputGrid.contains(event.target)) return;
  event.preventDefault();
  const direction = event.deltaY < 0 ? 10 : -10;
  pendingGalleryCardSize = (pendingGalleryCardSize ?? state.galleryCardSize) + direction;
  if (gallerySizeFrame != null) return;
  gallerySizeFrame = requestAnimationFrame(() => {
    gallerySizeFrame = null;
    const next = pendingGalleryCardSize;
    pendingGalleryCardSize = null;
    setGalleryCardSize(next);
  });
}, { passive: false });
syncGallerySizeControls();

window.addEventListener('scroll', () => scheduleVirtualOutputRender(), { passive: true });
window.addEventListener('pagehide', rememberProofsPosition);
if ('ResizeObserver' in window) {
  new ResizeObserver(() => scheduleVirtualOutputRender(true)).observe(outputGrid);
}

$('#image-viewer-delete').addEventListener('click', () => deleteOutput(state.previewIndex));
$('#image-previous').addEventListener('click', () => movePreview(-1));
$('#image-next').addEventListener('click', () => movePreview(1));
$('.image-viewer-close').addEventListener('click', () => imageDialog.close());
imageDialog.addEventListener('close', () => {
  stopSlideshow();
  unloadViewerVideo();
  state.videoSource = null;
  state.previewOutputIndexes = null;
  showFullscreenControls({ autoHide: false });
  if (document.fullscreenElement === $('#image-viewer-shell')) document.exitFullscreen().catch(() => {});
  setFallbackFullscreen(false);
  syncJobDockLayer();
  syncOutputGridToPreview();
});
let suppressPreviewStageClick = false;
$('.image-stage').addEventListener('click', (event) => {
  if (suppressPreviewStageClick) {
    suppressPreviewStageClick = false;
    return;
  }
  if (event.target.classList.contains('image-stage')) imageDialog.close();
});
imageDialog.addEventListener('keydown', (event) => {
  const target = event.target;
  const isEditing = target instanceof HTMLElement && (
    target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON'].includes(target.tagName)
  );
  if (isEditing || (target instanceof Element && target.closest('video, a'))) return;
  if (isVideoOutput(state.outputs[state.previewIndex]) && event.code === 'Space') {
    event.preventDefault();
    const video = $('#image-viewer-video');
    revealViewerVideoControls({ autoHide: true });
    if (!state.privacyCovered) video.paused ? video.play().catch(() => {}) : video.pause();
    return;
  }
  if (event.code === 'Space' && !event.repeat && !isEditing) {
    event.preventDefault();
    toggleSlideshow();
  }
  if (['ArrowLeft', 'ArrowUp'].includes(event.key)) { event.preventDefault(); movePreview(-1); }
  if (['ArrowRight', 'ArrowDown'].includes(event.key)) { event.preventDefault(); movePreview(1); }
  if (['Delete', 'Backspace'].includes(event.key) && !event.repeat) {
    event.preventDefault();
    deleteOutput(state.previewIndex);
  }
});

let previewPointer = null;
let previewTouch = null;
let previewPinch = null;
let previewPinchFrame = null;
const imageStage = $('.image-stage');
const viewerVideo = $('#image-viewer-video');
const viewerVideoControls = $('#image-viewer-video-controls');
viewerVideo.addEventListener('pointerenter', (event) => {
  if (event.pointerType === 'mouse') revealViewerVideoControls({ autoHide: true });
});
viewerVideo.addEventListener('pointermove', (event) => {
  if (event.pointerType === 'mouse') revealViewerVideoControls({ autoHide: true });
});
viewerVideoControls.addEventListener('pointermove', (event) => {
  if (event.pointerType === 'mouse') revealViewerVideoControls({ autoHide: true });
});
viewerVideo.addEventListener('loadedmetadata', syncViewerVideoControls);
viewerVideo.addEventListener('durationchange', syncViewerVideoControls);
viewerVideo.addEventListener('timeupdate', syncViewerVideoControls);
viewerVideo.addEventListener('volumechange', syncViewerVideoControls);
viewerVideo.addEventListener('play', () => {
  syncViewerVideoControls();
  if (!imageStage.matches(':hover')) imageStage.classList.remove('video-controls-visible');
});
viewerVideo.addEventListener('pause', () => {
  syncViewerVideoControls();
  if (imageStage.classList.contains('video-active')) revealViewerVideoControls({ autoHide: false });
});
viewerVideo.addEventListener('click', (event) => {
  if (event.target !== viewerVideo) return;
  toggleViewerVideoPlayback();
});
viewerVideo.addEventListener('keydown', (event) => {
  if (event.code !== 'Space' && event.key.toLowerCase() !== 'k') return;
  event.preventDefault();
  toggleViewerVideoPlayback();
});
$('#image-viewer-video-play').addEventListener('click', (event) => {
  event.preventDefault();
  toggleViewerVideoPlayback();
});
$('#image-viewer-video-progress').addEventListener('input', (event) => {
  if (Number.isFinite(viewerVideo.duration) && viewerVideo.duration > 0) {
    viewerVideo.currentTime = Number(event.currentTarget.value);
  }
  revealViewerVideoControls({ autoHide: false });
  syncViewerVideoControls();
});
$('#image-viewer-video-mute').addEventListener('click', (event) => {
  event.preventDefault();
  if (viewerVideo.muted || viewerVideo.volume === 0) {
    viewerVideo.muted = false;
    viewerVideo.volume = state.videoVolume || 1;
  } else {
    state.videoVolume = viewerVideo.volume;
    viewerVideo.muted = true;
  }
  revealViewerVideoControls({ autoHide: true });
  syncViewerVideoControls();
});
$('#image-viewer-video-volume').addEventListener('input', (event) => {
  const volume = Math.min(1, Math.max(0, Number(event.currentTarget.value)));
  if (volume > 0) state.videoVolume = volume;
  viewerVideo.volume = volume;
  viewerVideo.muted = volume === 0;
  revealViewerVideoControls({ autoHide: true });
  syncViewerVideoControls();
});
imageStage.addEventListener('pointerleave', (event) => {
  if (event.pointerType === 'mouse' && !viewerVideo.paused) hideViewerVideoControls();
});
imageStage.addEventListener('pointerdown', (event) => {
  if (event.pointerType === 'touch' && isVideoOutput(state.outputs[state.previewIndex])) {
    revealViewerVideoControls({ autoHide: true });
  }
  if (event.target.closest('.viewer-video-controls')) return;
  if (event.target.closest('video') && event.clientY > event.target.getBoundingClientRect().bottom - 64) return;
  if (event.pointerType === 'touch' || previewPinch || event.button !== 0 || event.target.closest('button')) return;
  const bounds = previewPanBounds();
  previewPointer = {
    id: event.pointerId,
    x: event.clientX,
    y: event.clientY,
    panX: state.previewPanX,
    panY: state.previewPanY,
    pannable: bounds.x > 0 || bounds.y > 0,
    moved: false,
  };
  if (previewPointer.pannable) {
    event.preventDefault();
    imageStage.setPointerCapture(event.pointerId);
  }
  if (previewPointer.pannable) {
    imageStage.classList.add('panning');
  }
});
imageStage.addEventListener('pointermove', (event) => {
  if (!previewPointer || previewPointer.id !== event.pointerId || !previewPointer.pannable) return;
  const dx = event.clientX - previewPointer.x;
  const dy = event.clientY - previewPointer.y;
  previewPointer.moved ||= Math.abs(dx) > 3 || Math.abs(dy) > 3;
  state.previewPanX = previewPointer.panX + dx;
  state.previewPanY = previewPointer.panY + dy;
  applyPreviewPan();
});
function finishPreviewPointer(event) {
  if (!previewPointer || previewPointer.id !== event.pointerId) return;
  const pointer = previewPointer;
  previewPointer = null;
  imageStage.classList.remove('panning');
  const dx = event.clientX - pointer.x;
  const dy = event.clientY - pointer.y;
  const swiped = !pointer.pannable
    && event.type === 'pointerup'
    && Math.abs(dx) > 55
    && Math.abs(dx) > Math.abs(dy) * 1.2;
  suppressPreviewStageClick = event.type === 'pointerup' && (pointer.moved || swiped);
  if (swiped) {
    movePreview(dx > 0 ? -1 : 1);
  }
}
imageStage.addEventListener('pointerup', finishPreviewPointer);
imageStage.addEventListener('pointercancel', finishPreviewPointer);

function touchDistance(touches) {
  return Math.hypot(
    touches[0].clientX - touches[1].clientX,
    touches[0].clientY - touches[1].clientY,
  );
}

imageStage.addEventListener('touchstart', (event) => {
  if (event.target.closest('.viewer-video-controls')) {
    if (isVideoOutput(state.outputs[state.previewIndex])) revealViewerVideoControls({ autoHide: true });
    return;
  }
  if (event.target.closest('video') && event.touches.length === 1 && event.touches[0].clientY > event.target.getBoundingClientRect().bottom - 64) return;
  if (event.touches.length === 1) {
    const touch = event.touches[0];
    const bounds = previewPanBounds();
    previewTouch = {
      x: touch.clientX,
      y: touch.clientY,
      panX: state.previewPanX,
      panY: state.previewPanY,
      pannable: !state.previewFit && (bounds.x > 0 || bounds.y > 0),
      moved: false,
    };
    return;
  }
  if (event.touches.length !== 2) return;
  event.preventDefault();
  previewPointer = null;
  previewTouch = null;
  imageStage.classList.remove('panning');
  const image = activePreviewMedia();
  image.style.removeProperty('--preview-pinch-scale');
  const naturalWidth = image.videoWidth || image.naturalWidth;
  const renderedZoom = naturalWidth
    ? image.offsetWidth / naturalWidth * 100
    : state.previewZoom;
  const baseZoom = Math.min(300, Math.max(25, state.previewFit ? renderedZoom : state.previewZoom));
  previewPinch = {
    distance: Math.max(1, touchDistance(event.touches)),
    zoom: baseZoom,
    pendingZoom: baseZoom,
  };
  state.previewFit = false;
  state.previewZoom = baseZoom;
  syncPreviewScaleControls();
  imageStage.classList.add('pinching');
}, { passive: false });

function renderPreviewPinch() {
  previewPinchFrame = null;
  if (!previewPinch) return;
  const zoom = previewPinch.pendingZoom;
  activePreviewMedia().style.setProperty('--preview-pinch-scale', String(zoom / previewPinch.zoom));
  $('#image-zoom').value = String(Math.round(zoom));
  $('#image-zoom-output').textContent = `${Math.round(zoom)}%`;
}

imageStage.addEventListener('touchmove', (event) => {
  if (previewPinch && event.touches.length === 2) {
    event.preventDefault();
    const scale = touchDistance(event.touches) / previewPinch.distance;
    previewPinch.pendingZoom = Math.min(300, Math.max(25, previewPinch.zoom * scale));
    if (previewPinchFrame == null) previewPinchFrame = requestAnimationFrame(renderPreviewPinch);
    return;
  }
  if (!previewTouch || event.touches.length !== 1) return;
  const touch = event.touches[0];
  const dx = touch.clientX - previewTouch.x;
  const dy = touch.clientY - previewTouch.y;
  previewTouch.moved ||= Math.abs(dx) > 3 || Math.abs(dy) > 3;
  if (previewTouch.pannable) {
    event.preventDefault();
    imageStage.classList.add('panning');
    state.previewPanX = previewTouch.panX + dx;
    state.previewPanY = previewTouch.panY + dy;
    applyPreviewPan();
  } else if (Math.abs(dx) > Math.abs(dy)) {
    event.preventDefault();
  }
}, { passive: false });

function finishPreviewTouch(event) {
  if (previewPinch && event.touches.length < 2) {
    const finalZoom = Math.round(previewPinch.pendingZoom);
    previewPinch = null;
    previewTouch = null;
    if (previewPinchFrame != null) cancelAnimationFrame(previewPinchFrame);
    previewPinchFrame = null;
    imageStage.classList.remove('pinching');
    setPreviewZoom(finalZoom);
    activePreviewMedia().style.removeProperty('--preview-pinch-scale');
    suppressPreviewStageClick = true;
    return;
  }
  if (!previewTouch || event.touches.length) return;
  const touch = event.changedTouches[0];
  const dx = touch.clientX - previewTouch.x;
  const dy = touch.clientY - previewTouch.y;
  const swiped = !previewTouch.pannable
    && event.type === 'touchend'
    && Math.abs(dx) > 55
    && Math.abs(dx) > Math.abs(dy) * 1.2;
  suppressPreviewStageClick = previewTouch.moved || swiped;
  previewTouch = null;
  imageStage.classList.remove('panning');
  if (swiped) movePreview(dx > 0 ? -1 : 1);
}

imageStage.addEventListener('touchend', finishPreviewTouch, { passive: true });
imageStage.addEventListener('touchcancel', finishPreviewTouch, { passive: true });

function directorShotButton(shot) {
  const active = shot.number === state.directorShot ? 'active' : '';
  const stage = shot.stage.plateau_kind || shot.stage.level;
  return `<button class="director-shot ${active}" data-director-shot="${shot.number}">
    <i title="Shot ${shot.shot_index + 1}">${shot.shot_index + 1}</i>
    <span class="director-shot-copy">
      <strong>${escapeHtml(displayValue(stage.replaceAll('_', ' ')))}</strong>
      <span class="director-shot-action" title="${escapeHtml(displayValue(shot.action.prompt))}">${escapeHtml(displayValue(shot.action.prompt))}</span>
    </span>
  </button>`;
}

function directorShotList(shots) {
  const sets = new Map();
  shots.forEach((shot) => {
    if (!sets.has(shot.photoshoot_index)) sets.set(shot.photoshoot_index, []);
    sets.get(shot.photoshoot_index).push(shot);
  });
  if (state.directorOpenSet !== null && !sets.has(state.directorOpenSet)) {
    state.directorOpenSet = sets.keys().next().value;
  }
  return [...sets.entries()].map(([setIndex, setShots]) => `
    <details class="director-set" data-director-set="${setIndex}" ${setIndex === state.directorOpenSet ? 'open' : ''}>
      <summary><span class="director-set-title">Set ${setIndex + 1}</span><small>${setShots.length} shots <i aria-hidden="true">⌄</i></small></summary>
      <div class="director-set-shots">${setShots.map(directorShotButton).join('')}</div>
    </details>
  `).join('');
}

function directorField(field) {
  const customOption = field.custom
    ? `<option value="__director_custom__" selected>${escapeHtml(displayValue(field.custom))}</option>`
    : '';
  const optionHtml = customOption + field.options.map((option) => {
    const suffix = option.default ? ' (default)' : '';
    const label = displayValue(option.label);
    return `<option value="${escapeHtml(option.id)}" ${!field.custom && option.id === field.value ? 'selected' : ''} title="${escapeHtml(option.prompt)}">${escapeHtml(label + suffix)}</option>`;
  }).join('') + '<option value="__director_random__">Random</option>';
  const fieldNote = field.key === 'shot.stage'
    ? `${field.compatibility?.poses ?? 0} poses · ${field.compatibility?.actions ?? 0} actions · ${field.compatibility?.expressions ?? 0} expressions`
    : '';
  const search = [field.label, field.custom, ...field.options.map((option) => `${option.label} ${option.prompt}`)].join(" ").toLowerCase();
  return `<div class="director-field" data-director-search="${escapeHtml(search)}">
    <div class="director-field-head"><label for="director-${escapeHtml(field.key)}">${escapeHtml(field.label)}</label><span class="director-scope">${field.scope === 'set' ? 'This set' : 'This shot'}</span></div>
    <select id="director-${escapeHtml(field.key)}" data-director-field="${escapeHtml(field.key)}">${optionHtml}</select>
    <div class="director-field-footer"><p class="director-field-note">${field.custom ? '' : fieldNote}</p><button type="button" class="director-custom-button ${field.custom ? "active" : ""}" data-director-custom="${escapeHtml(field.key)}">${field.custom ? "Edit custom" : "+ Custom"}</button></div>
  </div>`;
}

function renderDirector() {
  const workspace = $('#director-workspace');
  const empty = $('#director-empty');
  if (!state.storyboard || !state.director) {
    workspace.classList.add('hidden');
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');
  workspace.classList.remove('hidden');
  $('#director-shot-list').innerHTML = directorShotList(state.storyboard.shots);
  const sets = new Set(state.storyboard.shots.map((shot) => shot.photoshoot_index)).size;
  $('#director-set-count').textContent = `${sets} set${sets === 1 ? '' : 's'}`;
  const data = state.director;
  const yoloToggle = $('#director-yolo');
  if (yoloToggle) yoloToggle.checked = Boolean(data.yolo);
  const shot = data.summary;
  $('#director-title').textContent = `Set ${shot.photoshoot_index + 1} · Shot ${shot.shot_index + 1}`;
  $('#director-summary').innerHTML = [
    ['Subject', shot.subject], ['Wardrobe', shot.wardrobe],
    ['Location', shot.location], ['Treatment', shot.photography],
    ['Variation', `${shot.seed_manual ? 'Custom · ' : ''}${shot.inference_seed}`],
  ].map(([label, value]) => {
    const display = label === 'Wardrobe' ? displayCatalogLabel(value) : displayValue(value);
    return `<div class="director-summary-${label.toLowerCase()}"><span>${label}</span><strong title="${escapeHtml(display)}">${escapeHtml(display)}</strong></div>`;
  }).join('');
  $('#director-groups').innerHTML = data.groups.map((group, groupIndex) => `
    <details class="director-group" data-director-group="${escapeHtml(group.id)}" ${state.directorOpenGroup === group.id ? 'open' : ''}>
      <summary><span class="director-group-title"><i>${String(groupIndex + 1).padStart(2, '0')}</i>${escapeHtml(group.label)}</span><small>${group.fields.length} settings · ${['direction', 'camera'].includes(group.id) ? 'shot' : 'set'}</small></summary>
      <div class="director-fields">${group.fields.map(directorField).join('')}</div>
    </details>
  `).join('');
  filterDirector($('#director-search').value);
}

function filterDirector(query, { collapseEmpty = false } = {}) {
  const normalized = String(query || '').trim().toLowerCase();
  if (!normalized && collapseEmpty) {
    state.directorOpenGroup = null;
    $$('[data-director-group]').forEach((group) => { group.open = false; });
  }
  let visible = 0;
  $$('.director-field').forEach((field) => {
    const show = !normalized || field.dataset.directorSearch.includes(normalized);
    field.classList.toggle('hidden', !show);
    if (show) visible += 1;
  });
  $$('[data-director-group]').forEach((group) => {
    const show = Boolean($('.director-field:not(.hidden)', group));
    group.classList.toggle('hidden', !show);
    if (normalized && show) group.open = true;
  });
  let none = $('.director-no-results');
  if (!visible && normalized) {
    if (!none) {
      none = document.createElement('div');
      none.className = 'director-no-results';
      $('#director-groups').append(none);
    }
    none.textContent = `No settings or presets match “${query}”.`;
  } else {
    none?.remove();
  }
}

async function loadDirector(number = state.directorShot) {
  if (!state.storyboard) {
    state.director = null;
    renderDirector();
    return;
  }
  state.directorShot = Math.min(Math.max(1, number), state.storyboard.total);
  $('#director-loading').classList.remove('hidden');
  $('#director-groups').classList.add('hidden');
  try {
    state.director = await api(`/api/storyboards/${state.storyboard.id}/director?shot=${state.directorShot}`);
    renderDirector();
  } catch (error) {
    state.director = null;
    renderDirector();
    toast('Director unavailable', error.message, 'error');
  } finally {
    $('#director-loading').classList.add('hidden');
    $('#director-groups').classList.remove('hidden');
  }
}

async function remixDirector(target, button) {
  if (!state.storyboard || !state.director) return;
  const buttons = $$('[data-director-remix]');
  buttons.forEach((item) => { item.disabled = true; });
  try {
    state.director = await api(`/api/storyboards/${state.storyboard.id}/director`, {
      method: 'POST',
      body: JSON.stringify({ shot: state.directorShot, field: `remix.${target}`, value: '' }),
    });
    state.storyboard = await api(`/api/storyboards/${state.storyboard.id}`);
    renderStoryboard();
    renderDirector();
  } catch (error) {
    toast('Could not remix', error.message, 'error');
  } finally {
    buttons.forEach((item) => { item.disabled = false; });
  }
}

async function toggleDirectorYolo(toggle) {
  if (!state.storyboard || !state.director) return;
  const previous = !toggle.checked;
  toggle.disabled = true;
  try {
    state.director = await api(`/api/storyboards/${state.storyboard.id}/director`, {
      method: 'POST',
      body: JSON.stringify({
        shot: state.directorShot,
        field: 'director.yolo',
        value: String(toggle.checked),
      }),
    });
    state.storyboard = await api(`/api/storyboards/${state.storyboard.id}`);
    renderStoryboard();
    renderDirector();
  } catch (error) {
    toggle.checked = previous;
    toast('Could not change YOLO mode', error.message, 'error');
  } finally {
    toggle.disabled = false;
  }
}

function directorFieldByKey(key) {
  return state.director?.groups.flatMap((group) => group.fields).find((field) => field.key === key);
}

function openDirectorCustom(key) {
  const field = directorFieldByKey(key);
  if (!field) return;
  state.directorCustomField = key;
  $("#director-custom-title").textContent = field.label;
  const propagation = $("#director-custom-propagation");
  const isRandom = state.storyboard?.config?.mode === "random";
  const canPropagate = Number(state.storyboard?.total) > 1;
  const currentSetOption = $("#director-custom-current-set-option");
  $("#director-custom-scope").textContent = "Overrides this field without changing the preset database.";
  propagation.classList.toggle("hidden", !canPropagate);
  $("#director-custom-current-shot-option").classList.toggle("hidden", !canPropagate);
  currentSetOption.classList.toggle("hidden", isRandom || !canPropagate);
  $("#director-custom-all-label").textContent = isRandom ? "All shots" : "All sets";
  const defaultScope = isRandom || field.scope === "shot" ? "current_shot" : "current_set";
  $(`#director-custom-propagation input[value="${defaultScope}"]`).checked = true;
  $("#director-custom-value").value = field.custom || "";
  $("#director-custom-clear").disabled = !field.custom;
  directorCustomDialog.showModal();
  $("#director-custom-value").focus();
}

async function saveDirectorCustom(clear = false) {
  const field = directorFieldByKey(state.directorCustomField);
  if (!field || !state.storyboard) return;
  const value = clear ? "" : $("#director-custom-value").value.trim();
  const propagation = $("#director-custom-propagation");
  const isRandom = state.storyboard.config?.mode === "random";
  const defaultScope = isRandom || field.scope === "shot" ? "current_shot" : "current_set";
  const customScope = !propagation.classList.contains("hidden")
    ? $("#director-custom-propagation input:checked")?.value || defaultScope
    : defaultScope;
  const button = clear ? $("#director-custom-clear") : $("#director-custom-apply");
  setBusy(button, true, clear ? "Clearing…" : "Applying…");
  try {
    state.director = await api(`/api/storyboards/${state.storyboard.id}/director`, {
      method: "POST",
      body: JSON.stringify({ shot: state.directorShot, field: field.key, custom_value: value, custom_scope: customScope }),
    });
    state.storyboard = await api(`/api/storyboards/${state.storyboard.id}`);
    directorCustomDialog.close();
    renderStoryboard();
    renderDirector();
  } catch (error) {
    toast("Could not apply custom value", error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function applyDirectorChange(select) {
  if (!state.storyboard || !state.director) return;
  const field = select.dataset.directorField;
  const card = select.closest('.director-field');
  const activeField = directorFieldByKey(field);
  const previous = activeField?.custom ? '__director_custom__' : activeField?.value;
  if (select.value === '__director_custom__') {
    openDirectorCustom(field);
    return;
  }
  select.disabled = true;
  card.classList.add('changed');
  try {
    state.director = await api(`/api/storyboards/${state.storyboard.id}/director`, {
      method: 'POST',
      body: JSON.stringify({
        shot: state.directorShot,
        field,
        value: select.value,
        clear_custom: Boolean(activeField?.custom),
      }),
    });
    state.storyboard = await api(`/api/storyboards/${state.storyboard.id}`);
    renderStoryboard();
    renderDirector();
  } catch (error) {
    select.value = previous ?? '';
    card.classList.remove('changed');
    select.disabled = false;
    toast('Choice is incompatible', error.message, 'error');
  }
}

function activeViewName() {
  return $('.view.active')?.id?.replace('-view', '') || 'studio';
}

function previewWindowSession(owner = state.previewWindowOwner) {
  return owner ? state.previewWindowSessions[owner] || null : null;
}

function persistPreviewWindowSessions() {
  sessionStorage.setItem('valhalla-floating-previews', JSON.stringify(state.previewWindowSessions));
}

function configureFloatingPreview(displayed) {
  const windowElement = $('#shot-preview-window');
  const persistent = Boolean(displayed?.persistent);
  windowElement.querySelector('.eyebrow').textContent = persistent ? 'Rendered Image' : 'Temporary Preview Render';
  $('#shot-preview-title').textContent = persistent
    ? (displayed.shot ? `Shot ${displayed.shot}` : 'Rendered image')
    : `Shot ${displayed.shot} preview`;
  $('#shot-preview-refresh').classList.toggle('hidden', persistent);
  windowElement.querySelector('footer').textContent = persistent
    ? 'Drag by the header · Resize from the lower-right corner.'
    : 'Drag by the header · Resize from the lower-right corner · Closing discards the preview.';
}

function suspendFloatingPreview() {
  const session = previewWindowSession();
  const windowElement = $('#shot-preview-window');
  if (!session) return;
  rememberShotPreviewGeometry();
  session.open = !windowElement.classList.contains('hidden');
  session.displayed = state.previewDisplayed;
  session.geometry = state.previewWindowGeometry;
  session.geometryReady = state.previewWindowGeometryReady;
  windowElement.classList.add('hidden');
  $('#shot-preview-image').removeAttribute('src');
  state.previewWindowOwner = null;
  state.previewDisplayed = null;
  persistPreviewWindowSessions();
}

function restoreFloatingPreview(owner) {
  const session = state.previewWindowSessions[owner];
  if (!session?.open || !session.displayed) return;
  state.previewWindowOwner = owner;
  state.previewDisplayed = session.displayed;
  state.previewWindowGeometry = session.geometry;
  state.previewWindowGeometryReady = session.geometryReady;
  configureFloatingPreview(session.displayed);
  const windowElement = $('#shot-preview-window');
  if (!session.geometry) resetShotPreviewGeometryStyles();
  windowElement.classList.remove('hidden');
  applyShotPreviewGeometry();
  clampShotPreviewWindow();
  if (!state.privacyCovered) $('#shot-preview-image').src = session.displayed.image_url;
}

function rememberShotPreviewGeometry() {
  const windowElement = $('#shot-preview-window');
  if (!state.previewWindowGeometryReady || windowElement.classList.contains('hidden')) return;
  const rect = windowElement.getBoundingClientRect();
  state.previewWindowGeometry = {
    left: rect.left,
    top: rect.top,
    width: rect.width,
    height: rect.height,
  };
  const session = previewWindowSession();
  if (session) {
    session.geometry = state.previewWindowGeometry;
    session.geometryReady = true;
    persistPreviewWindowSessions();
  }
}

function applyShotPreviewGeometry() {
  const geometry = state.previewWindowGeometry;
  if (!geometry) return false;
  const windowElement = $('#shot-preview-window');
  windowElement.style.left = `${geometry.left}px`;
  windowElement.style.top = `${geometry.top}px`;
  windowElement.style.right = 'auto';
  windowElement.style.bottom = 'auto';
  windowElement.style.width = `${geometry.width}px`;
  windowElement.style.height = `${geometry.height}px`;
  return true;
}

function resetShotPreviewGeometryStyles() {
  const style = $('#shot-preview-window').style;
  for (const property of ['left', 'top', 'right', 'bottom', 'width', 'height']) {
    style.removeProperty(property);
  }
}

async function closeShotPreview() {
  const preview = state.previewDisplayed;
  const session = previewWindowSession();
  if (session) {
    session.open = false;
    session.displayed = null;
    persistPreviewWindowSessions();
  }
  state.previewDisplayed = null;
  $('#shot-preview-window').classList.add('hidden');
  $('#shot-preview-image').removeAttribute('src');
  if (!preview?.id || preview.persistent) return;
  try {
    await api(`/api/previews/${preview.id}`, { method: 'DELETE' });
  } catch (error) {
    toast('Could not discard preview', error.message, 'error');
  }
}

async function openShotPreview(preview, owner = state.previewJobOwner || activeViewName()) {
  const windowElement = $('#shot-preview-window');
  if (state.previewWindowOwner && state.previewWindowOwner !== owner) suspendFloatingPreview();
  const session = state.previewWindowSessions[owner];
  const previous = session.displayed;
  session.displayed = preview;
  session.open = true;
  persistPreviewWindowSessions();
  state.previewWindowOwner = owner;
  state.previewDisplayed = preview;
  state.previewWindowGeometry = session.geometry;
  state.previewWindowGeometryReady = session.geometryReady;
  configureFloatingPreview(preview);
  if (owner !== activeViewName()) {
    state.previewWindowOwner = null;
    state.previewDisplayed = null;
    if (previous?.id && !previous.persistent && previous.id !== preview.id) {
      try { await api(`/api/previews/${previous.id}`, { method: 'DELETE' }); } catch { /* already expired */ }
    }
    return;
  }
  if (!session.geometry) resetShotPreviewGeometryStyles();
  if (state.privacyCovered) $('#shot-preview-image').removeAttribute('src');
  else $('#shot-preview-image').src = `${preview.image_url}?v=${Date.now()}`;
  windowElement.classList.remove('hidden');
  if (!applyShotPreviewGeometry()) {
    const rect = windowElement.getBoundingClientRect();
    windowElement.style.left = `${rect.left}px`;
    windowElement.style.top = `${rect.top}px`;
    windowElement.style.right = 'auto';
    windowElement.style.bottom = 'auto';
  }
  clampShotPreviewWindow();
  if (previous?.id && !previous.persistent && previous.id !== preview.id) {
    try { await api(`/api/previews/${previous.id}`, { method: 'DELETE' }); } catch { /* already expired */ }
  }
}

function openLogbookImagePreview(prompt) {
  const mediaUrl = prompt.video_url || prompt.image_url;
  const outputIndex = state.outputs.findIndex((item) => item.url === mediaUrl);
  if (outputIndex >= 0) {
    state.previewOutputIndexes = [outputIndex];
    showPreview(outputIndex);
    if (!imageDialog.open) imageDialog.showModal();
    syncJobDockLayer();
    requestAnimationFrame(fitPreviewMedia);
    return;
  }
  if (!prompt.image_url) return;
  const windowElement = $('#shot-preview-window');
  if (state.previewWindowOwner && state.previewWindowOwner !== 'logger') suspendFloatingPreview();
  const session = state.previewWindowSessions.logger;
  state.previewDisplayed = {
    image_url: prompt.image_url,
    shot: prompt.shot,
    persistent: true,
  };
  state.previewWindowOwner = 'logger';
  state.previewWindowGeometry = session.geometry;
  state.previewWindowGeometryReady = session.geometryReady;
  session.displayed = state.previewDisplayed;
  session.open = true;
  persistPreviewWindowSessions();
  configureFloatingPreview(state.previewDisplayed);
  if (!session.geometry) resetShotPreviewGeometryStyles();
  $('#shot-preview-image').src = prompt.image_url;
  windowElement.classList.remove('hidden');
  if (!applyShotPreviewGeometry()) {
    const rect = windowElement.getBoundingClientRect();
    windowElement.style.left = `${rect.left}px`;
    windowElement.style.top = `${rect.top}px`;
    windowElement.style.right = 'auto';
    windowElement.style.bottom = 'auto';
  }
  clampShotPreviewWindow();
}

function fitShotPreviewWindowToImage() {
  const windowElement = $('#shot-preview-window');
  const image = $('#shot-preview-image');
  if (windowElement.classList.contains('hidden') || !image.naturalWidth || !image.naturalHeight) return;
  if (applyShotPreviewGeometry()) {
    clampShotPreviewWindow();
    return;
  }
  const headerHeight = windowElement.querySelector('header').offsetHeight;
  const footerHeight = windowElement.querySelector('footer').offsetHeight;
  const frameHeight = headerHeight + footerHeight + 2;
  const maxContentWidth = Math.max(1, window.innerWidth - 34);
  const maxContentHeight = Math.max(1, window.innerHeight - frameHeight - 18);
  const scale = Math.min(
    430 / image.naturalWidth,
    maxContentWidth / image.naturalWidth,
    maxContentHeight / image.naturalHeight,
    1,
  );
  windowElement.style.width = `${Math.round(image.naturalWidth * scale + 2)}px`;
  windowElement.style.height = `${Math.round(image.naturalHeight * scale + frameHeight)}px`;
  clampShotPreviewWindow();
  state.previewWindowGeometryReady = true;
  rememberShotPreviewGeometry();
}

function setPreviewBusy(button, busy) {
  if (button?.id === 'shot-preview-refresh') {
    button.disabled = busy;
    button.classList.toggle('spinning', busy);
    return;
  }
  setBusy(button, busy, busy ? 'Rendering…' : undefined);
}

async function pollShotPreview(button) {
  if (!state.previewJob) return;
  try {
    state.previewJob = await api(`/api/previews/${state.previewJob.id}`);
    renderLogger();
    if (['queued', 'running'].includes(state.previewJob.status)) {
      state.previewJobTimer = setTimeout(() => pollShotPreview(button), 1000);
      return;
    }
    setPreviewBusy(button, false);
    if (state.previewJob.status === 'completed') {
      await openShotPreview(state.previewJob, state.previewJobOwner);
      state.previewJobOwner = null;
      toast('Shot preview ready', 'Temporary preview rendered without adding it to Proofs.', 'success');
    } else {
      const message = state.previewJob.error || 'Preview rendering failed';
      const failedId = state.previewJob.id;
      state.previewJob = null;
      await api(`/api/previews/${failedId}`, { method: 'DELETE' });
      toast('Preview failed', message, 'error');
    }
  } catch (error) {
    toast('Preview status lost', error.message, 'error');
    state.previewJobTimer = setTimeout(() => pollShotPreview(button), 3000);
  }
}

async function startShotPreview(number, button) {
  if (!state.storyboard) return;
  if (isRenderActive()) {
    toast('Preview unavailable', 'Wait for the active storyboard render to finish or cancel it first.', 'error');
    return;
  }
  if (state.previewJob && ['queued', 'running'].includes(state.previewJob.status)) {
    toast('Preview already active', 'Wait for the current shot preview to finish.');
    return;
  }
  if (state.pendingStructural) {
    const updated = await requestStoryboardUpdate();
    if (!updated || number > updated.total) return;
  }
  setPreviewBusy(button, true);
  try {
    state.previewJobOwner = activeViewName();
    state.previewJob = await api('/api/previews', {
      method: 'POST',
      body: JSON.stringify({
        storyboard_id: state.storyboard.id,
        shot: number,
        fast: true,
      }),
    });
    renderLogger();
    pollShotPreview(button);
  } catch (error) {
    setPreviewBusy(button, false);
    toast('Could not start preview', error.message, 'error');
  }
}

function clampShotPreviewWindow() {
  const preview = $('#shot-preview-window');
  if (preview.classList.contains('hidden') || !preview.style.left) return;
  const rect = preview.getBoundingClientRect();
  preview.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - rect.width - 8))}px`;
  preview.style.top = `${Math.max(8, Math.min(rect.top, window.innerHeight - rect.height - 8))}px`;
}

function switchView(name) {
  if (!['studio', 'director', 'outputs', 'logger'].includes(name)) name = 'studio';
  if (name !== 'outputs' && imageDialog.open) imageDialog.close();
  const previousView = $('.view.active')?.id?.replace('-view', '');
  if (previousView === 'outputs' && name !== 'outputs') rememberProofsPosition();
  if (state.previewWindowOwner) suspendFloatingPreview();
  state.restoredView = name;
  sessionStorage.setItem('valhalla-active-view', name);
  $$('.view').forEach((view) => view.classList.toggle('active', view.id === `${name}-view`));
  $$('.nav-item').forEach((item) => item.classList.toggle('active', item.dataset.view === name));
  if (name !== 'outputs') restoreFloatingPreview(name);
  $('#studio-topbar-actions').classList.toggle('hidden', name !== 'studio');
  $('#outputs-topbar-center').classList.toggle('hidden', name !== 'outputs');
  $('#outputs-topbar-actions').classList.toggle('hidden', name !== 'outputs');
  $('#logbook-topbar-actions').classList.toggle('hidden', name !== 'logger');
  $('#view-title').textContent = {
    studio: 'Photo Studio', director: 'Director’s Desk', outputs: 'Proof Gallery', logger: 'Production Logbook',
  }[name] || 'Valhalla Photo Studio';
  $('#view-eyebrow').textContent = {
    studio: 'Creative workspace', director: 'Direction workspace',
    outputs: 'Review workspace', logger: 'Production telemetry',
  }[name] || 'Creative workspace';
  if (name === 'director') loadDirector();
  if (name === 'logger') renderLogger();
  if (name === 'outputs') {
    scheduleVirtualOutputRender(true);
    if (previousView !== 'outputs') restoreProofsPosition();
  }
}

function profileControls(media = state.profileMedia) {
  const video = media === 'video';
  return {
    media: video ? 'video' : 'image',
    source: $(`#${video ? 'live-video-workflow-source' : 'live-workflow-source'}`),
    selectors: $(`#${video ? 'video-profile-selector' : 'workflow-profile-selectors'}`),
    production: $(`#${video ? 'video-production-profile' : 'production-profile'}`),
    preview: video ? null : $('#preview-profile'),
    help: $(`#${video ? 'live-video-workflow-help' : 'live-workflow-help'}`),
    list: $(`#${video ? 'video-workflow-profile-list' : 'workflow-profile-list'}`),
  };
}

function syncProfileControls(media = state.profileMedia) {
  const controls = profileControls(media);
  const profiles = state.workflowProfilesByMedia[controls.media];
  if (!profiles) return;
  const live = profiles.source === 'live';
  const hasProfiles = profiles.profiles.some((profile) => profile.valid);
  controls.source.disabled = false;
  controls.selectors.classList.toggle('disabled', live);
  controls.help.classList.toggle('hidden', !live);
  controls.production.disabled = live || !hasProfiles;
  if (controls.preview) controls.preview.disabled = live || !hasProfiles;
}

function renderWorkflowProfiles(profiles, media = profiles?.media_type || state.profileMedia) {
  if (!profiles) return;
  const controls = profileControls(media);
  state.workflowProfilesByMedia[controls.media] = profiles;
  if (controls.media === state.profileMedia) state.workflowProfiles = profiles;
  const live = profiles.source === 'live';
  const options = profiles.profiles
    .map((profile) => `<option value="${escapeHtml(profile.id)}"${profile.valid ? '' : ' disabled'}>${escapeHtml(profile.name)}${profile.valid ? '' : ' · invalid'}</option>`)
    .join('');
  const selectors = controls.preview
    ? [[controls.production, profiles.production], [controls.preview, profiles.preview]]
    : [[controls.production, profiles.production]];
  selectors.forEach(([select, value]) => {
    select.innerHTML = options || '<option value="">No captured profiles</option>';
    select.value = value || '';
  });
  controls.source.checked = live;
  controls.list.innerHTML = profiles.profiles.length
    ? profiles.profiles.map((profile) => `
      <div class="workflow-profile-item${profile.valid ? '' : ' invalid'}" data-profile-id="${escapeHtml(profile.id)}" data-profile-media-type="${controls.media}">
        <div><strong>${escapeHtml(profile.name)}</strong><small>${escapeHtml(profile.file)}${profile.valid ? ` · ${profile.negative_conditioning ? 'Auxiliary negative connected' : 'Positive-only workflow'}` : ` · ${profile.error}`}</small></div>
        <button type="button" class="text-button" data-profile-action="rename">Rename</button>
        <button type="button" class="text-button danger" data-profile-action="delete">Delete</button>
      </div>`).join('')
    : '<p class="profile-empty">No profiles captured yet.</p>';
  syncProfileControls(controls.media);
}

function setProfileMedia(media) {
  const next = media === 'video' ? 'video' : 'image';
  state.profileMedia = next;
  sessionStorage.setItem('valhalla-profile-media', next);
  $$('.profile-media-tabs [data-profile-media]').forEach((button) => {
    button.classList.toggle('active', button.dataset.profileMedia === next);
    button.setAttribute('aria-pressed', String(button.dataset.profileMedia === next));
  });
  $('#image-profile-settings').classList.toggle('hidden', next !== 'image');
  $('#video-profile-settings').classList.toggle('hidden', next !== 'video');
  const cached = state.workflowProfilesByMedia[next];
  if (cached) {
    renderWorkflowProfiles(cached, next);
    loadWorkflowCaptureCandidate(next);
  }
  else loadWorkflowProfileMedia(next, { candidate: true });
}

const workflowProfileRequest = { image: 0, video: 0 };
async function loadWorkflowProfileMedia(media, { candidate = false } = {}) {
  const next = media === 'video' ? 'video' : 'image';
  const request = ++workflowProfileRequest[next];
  try {
    const profiles = await api(`/api/workflow/profiles?media=${next}`);
    if (request !== workflowProfileRequest[next]) return;
    renderWorkflowProfiles(profiles, next);
  } catch (error) {
    if (request === workflowProfileRequest[next] && next === state.profileMedia) {
      $('#capture-candidate-status').textContent = error.message;
    }
    return;
  }
  if (candidate && request === workflowProfileRequest[next] && next === state.profileMedia) {
    await loadWorkflowCaptureCandidate(next);
  }
}

let captureCandidateRequest = 0;
async function loadWorkflowCaptureCandidate(media = state.profileMedia) {
  const request = ++captureCandidateRequest;
  const next = media === 'video' ? 'video' : 'image';
  const status = $('#capture-candidate-status');
  if (next === state.profileMedia) status.textContent = `Inspecting the latest successful ${next} ComfyUI run…`;
  try {
    const candidate = await api(`/api/workflow/capture-candidate?media=${next}`);
    if (request !== captureCandidateRequest || next !== state.profileMedia) return;
    $('#capture-profile-name').value = candidate.suggested_name;
    status.textContent = `Detected from ComfyUI · ${candidate.suggested_id}.workflow.json`;
  } catch (error) {
    if (request === captureCandidateRequest && next === state.profileMedia) status.textContent = error.message;
  }
}

function requestProfileName(currentName, restoreFocus) {
  return new Promise((resolve) => {
    const dialog = document.createElement('dialog');
    dialog.className = 'small-dialog';
    dialog.innerHTML = '<form method="dialog"><header class="dialog-header"><h2>Rename rendering profile</h2></header><div class="dialog-body"><label class="field"><span>Profile name</span><input name="profileName" required maxlength="100"></label></div><footer class="dialog-footer"><button class="button ghost" value="cancel" formnovalidate>Cancel</button><button class="button primary" value="save">Save name</button></footer></form>';
    const input = dialog.querySelector('input');
    input.value = currentName;
    dialog.addEventListener('close', () => {
      resolve(dialog.returnValue === 'save' ? input.value.trim() : null);
      dialog.remove();
      restoreFocus?.focus();
    }, { once: true });
    document.body.append(dialog);
    dialog.showModal();
    input.select();
  });
}

async function manageWorkflowProfile(button) {
  const item = button.closest('[data-profile-id]');
  const profileId = item?.dataset.profileId;
  if (!profileId) return;
  const media = item.dataset.profileMediaType === 'video' ? 'video' : 'image';
  const action = button.dataset.profileAction;
  let name = '';
  if (action === 'rename') {
    name = await requestProfileName(item.querySelector('strong').textContent, button);
    if (!name?.trim()) return;
  } else if (!await confirmDeletion('Delete rendering profile?', `Remove ${item.querySelector('strong').textContent}? Generated media will remain available.`, 'Delete profile')) return;
  setBusy(button, true, action === 'rename' ? 'Saving…' : 'Deleting…');
  try {
    const profiles = await api(
      `/api/workflow/profiles/${encodeURIComponent(profileId)}${action === 'rename' ? '/rename' : ''}?media=${media}`,
      action === 'rename'
        ? { method: 'POST', body: JSON.stringify({ name }) }
        : { method: 'DELETE' },
    );
    workflowProfileRequest[media] += 1;
    renderWorkflowProfiles(profiles, media);
    refreshStatus();
  } catch (error) {
    toast(`Could not ${action} profile`, error.message, 'error');
    setBusy(button, false);
  }
}

async function openWorkflowProfiles() {
  $('#system-settings').open = false;
  closeMobileSystem();
  $('#capture-dialog').showModal();
  setProfileMedia(state.profileMedia);
  await Promise.allSettled([
    loadWorkflowProfileMedia('image'),
    loadWorkflowProfileMedia('video'),
    loadPromptEnhancerSettings(),
  ]);
  await loadWorkflowCaptureCandidate(state.profileMedia);
}

function renderPromptEnhancerSettings(settings) {
  if (!settings) return;
  $('#prompt-enhancer-production').checked = settings.production === true;
  $('#prompt-enhancer-preview').checked = settings.preview === true;
}

async function loadPromptEnhancerSettings() {
  try {
    renderPromptEnhancerSettings(await api('/api/prompt-enhancer/settings'));
  } catch (error) {
    toast('Could not load prompt enhance settings', error.message, 'error');
  }
}

let promptEnhancerSettingsSaving = false;
async function savePromptEnhancerSettings() {
  if (promptEnhancerSettingsSaving) return;
  promptEnhancerSettingsSaving = true;
  const controls = [$('#prompt-enhancer-production'), $('#prompt-enhancer-preview')];
  controls.forEach((control) => { control.disabled = true; });
  try {
    const settings = await api('/api/prompt-enhancer/settings', {
      method: 'POST',
      body: JSON.stringify({
        production: controls[0].checked,
        preview: controls[1].checked,
      }),
    });
    renderPromptEnhancerSettings(settings);
    refreshStatus();
  } catch (error) {
    toast('Could not save prompt enhance settings', error.message, 'error');
    await loadPromptEnhancerSettings();
  } finally {
    promptEnhancerSettingsSaving = false;
    controls.forEach((control) => { control.disabled = false; });
  }
}

$('#prompt-enhancer-production').addEventListener('change', savePromptEnhancerSettings);
$('#prompt-enhancer-preview').addEventListener('change', savePromptEnhancerSettings);

const workflowSettingsSaving = new Set();

async function saveWorkflowProfileSelection(media = state.profileMedia) {
  const next = typeof media === 'string' ? media : state.profileMedia;
  const controls = profileControls(next);
  if (workflowSettingsSaving.has(next)) return;
  workflowSettingsSaving.add(next);
  [controls.source, controls.production, controls.preview].filter(Boolean)
    .forEach((control) => { control.disabled = true; });
  try {
    const profiles = await api('/api/workflow/profiles/select', {
      method: 'POST',
      body: JSON.stringify(next === 'video' ? {
        production: controls.production.value,
        preview: '',
        source: controls.source.checked ? 'live' : 'profiles',
        media: 'video',
      } : {
        production: $('#production-profile').value,
        preview: $('#preview-profile').value,
        source: $('#live-workflow-source').checked ? 'live' : 'profiles',
        media: 'image',
      }),
    });
    workflowProfileRequest[next] += 1;
    renderWorkflowProfiles(profiles, next);
    refreshStatus();
  } catch (error) {
    toast('Could not select profiles', error.message, 'error');
    try {
      await loadWorkflowProfileMedia(next);
    } catch { /* keep original error */ }
  } finally {
    workflowSettingsSaving.delete(next);
    syncProfileControls(next);
  }
}

$('#live-workflow-source').addEventListener('change', (event) => {
  const live = event.currentTarget.checked;
  $('#workflow-profile-selectors').classList.toggle('disabled', live);
  $('#live-workflow-help').classList.toggle('hidden', !live);
  $$('#workflow-profile-selectors select').forEach((select) => { select.disabled = live; });
  saveWorkflowProfileSelection('image');
});

$('#live-video-workflow-source').addEventListener('change', (event) => {
  const live = event.currentTarget.checked;
  $('#video-profile-selector').classList.toggle('disabled', live);
  $('#live-video-workflow-help').classList.toggle('hidden', !live);
  saveWorkflowProfileSelection('video');
});

$$('#workflow-profile-selectors select, #video-profile-selector select').forEach((select) => {
  select.addEventListener('change', saveWorkflowProfileSelection);
});

async function captureWorkflow() {
  const button = $('#capture-confirm');
  setBusy(button, true, 'Capturing…');
  const media = state.profileMedia;
  try {
    const result = await api('/api/workflow/capture', {
      method: 'POST',
      body: JSON.stringify({
        name: $('#capture-profile-name').value,
        replace: $('#capture-force').checked,
        media,
      }),
    });
    await loadWorkflowProfileMedia(media);
    $('#capture-force').checked = false;
    toast('Workflow profile captured', `${result.profile.file} is ready to select.`, 'success');
    refreshStatus();
  } catch (error) {
    toast('Capture failed', error.message, 'error');
  } finally {
    setBusy(button, false);
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  await requestStoryboardUpdate();
});
form.addEventListener('input', (event) => {
  syncForm(event);
  syncPendingState();
});
form.addEventListener('input', scheduleSeedResolve);
$$('[data-theme-choice]').forEach((button) => button.addEventListener('click', () => setTheme(button.dataset.themeChoice)));
$$('[data-type-size]').forEach((button) => button.addEventListener('click', () => setTypeSize(button.dataset.typeSize)));
$$('[data-accent]').forEach((button) => button.addEventListener('click', () => setAccent(button.dataset.accent)));
$$('[data-privacy-shortcut]').forEach((button) => button.addEventListener('click', () => {
  state.privacyShortcut = button.dataset.privacyShortcut;
  localStorage.setItem('valhalla-privacy-shortcut', state.privacyShortcut);
  syncPrivacyControls();
}));
$$('[data-privacy-idle]').forEach((button) => button.addEventListener('click', () => {
  state.privacyIdleMinutes = Number(button.dataset.privacyIdle);
  localStorage.setItem('valhalla-privacy-idle-minutes', String(state.privacyIdleMinutes));
  privacyLastActivityAt = Date.now();
  schedulePrivacyIdleCover();
  syncPrivacyControls();
}));
$('#refresh-status').addEventListener('click', () => refreshStatus(true));
$('#reset-config').addEventListener('click', () => {
  form.reset();
  setRenderMode('production');
  syncForm({ target: form.elements.nsfw_percent });
  syncPendingState();
});
$('#randomize-storyboard-seed').addEventListener('click', () => randomizeSeedField('prompt_seed'));
$('#randomize-variation-seed').addEventListener('click', () => randomizeSeedField('inference_seed'));
$('#export-storyboard').addEventListener('click', exportStoryboard);
$('#import-storyboard').addEventListener('click', () => $('#storyboard-file').click());
$('#storyboard-file').addEventListener('change', importStoryboard);
const studioFilesMenu = $('#studio-files-menu');
$$('#studio-files-menu button').forEach((button) => {
  button.addEventListener('click', () => { studioFilesMenu.open = false; });
});
document.addEventListener('click', (event) => {
  if (studioFilesMenu.open && !event.target.closest('#studio-files-menu')) {
    studioFilesMenu.open = false;
  }
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') studioFilesMenu.open = false;
});
$$('[data-render-mode-choice]').forEach((button) => button.addEventListener('click', (event) => {
  setRenderMode(event.currentTarget.dataset.renderModeChoice);
  event.currentTarget.closest('[data-render-mode]').open = false;
}));
document.addEventListener('click', (event) => {
  $$('[data-render-mode][open]').forEach((menu) => {
    if (!event.target.closest('[data-render-mode]') || !menu.contains(event.target)) menu.open = false;
  });
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') $$('[data-render-mode][open]').forEach((menu) => { menu.open = false; });
});
$$('[data-render-action]').forEach((button) => button.addEventListener('click', startGeneration));
$('#director-open-studio').addEventListener('click', () => switchView('studio'));
$('#director-search').addEventListener('input', (event) => {
  filterDirector(event.target.value, { collapseEmpty: true });
});
$('.director-quick-actions').addEventListener('click', (event) => {
  const previewButton = event.target.closest('#director-preview-shot');
  if (previewButton) {
    startShotPreview(state.directorShot, previewButton);
    return;
  }
  const variationButton = event.target.closest('#director-randomize-seed');
  if (variationButton) {
    randomizeShotSeed(state.directorShot, variationButton);
    return;
  }
  const button = event.target.closest('[data-director-remix]');
  if (button) remixDirector(button.dataset.directorRemix, button);
});
$('#director-yolo').addEventListener('change', (event) => {
  toggleDirectorYolo(event.currentTarget);
});
$('#director-shot-list').addEventListener('click', (event) => {
  const button = event.target.closest('[data-director-shot]');
  if (button) {
    loadDirector(Number(button.dataset.directorShot));
    return;
  }
  const summary = event.target.closest('details.director-set > summary');
  if (!summary) return;
  const selected = summary.parentElement;
  if (selected.open) {
    state.directorOpenSet = null;
    return;
  }
  state.directorOpenSet = Number(selected.dataset.directorSet);
  $$('.director-set', $('#director-shot-list')).forEach((set) => {
    if (set !== selected) set.open = false;
  });
});
$('#director-shot-list').addEventListener('toggle', (event) => {
  const opened = event.target;
  if (!opened.matches('details.director-set')) return;
  const setIndex = Number(opened.dataset.directorSet);
  if (!opened.open) {
    if (state.directorOpenSet === setIndex) state.directorOpenSet = null;
    return;
  }
  state.directorOpenSet = setIndex;
  $$('.director-set', $('#director-shot-list')).forEach((set) => {
    if (set !== opened) set.open = false;
  });
});
$("#director-groups").addEventListener("click", (event) => {
  const customButton = event.target.closest("[data-director-custom]");
  if (customButton) {
    openDirectorCustom(customButton.dataset.directorCustom);
    return;
  }
  const summary = event.target.closest('.director-group > summary');
  if (!summary) return;
  const selected = summary.parentElement;
  if (selected.open) {
    state.directorOpenGroup = null;
    return;
  }
  state.directorOpenGroup = selected.dataset.directorGroup;
  $$('.director-group', $('#director-groups')).forEach((group) => {
    if (group !== selected) group.open = false;
  });
});
$('#director-groups').addEventListener('change', (event) => {
  const select = event.target.closest('[data-director-field]');
  if (select) applyDirectorChange(select);
});
$('#cancel-job').addEventListener('click', async () => {
  if (!state.job) return;
  try {
    state.job = await api(`/api/jobs/${state.job.id}/cancel`, { method: 'POST', body: '{}' });
    syncJobPendingGroups(state.job);
    showJob();
  } catch (error) { toast('Could not cancel', error.message, 'error'); }
});

shotGrid.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-action]');
  const card = button?.closest('.shot-card');
  if (!button || !card) return;
  event.preventDefault();
  const number = Number(card.dataset.shot);
  const shot = state.storyboard.shots.find((item) => item.number === number);
  if (!shot) return;
  if (button.dataset.action === 'inspect') {
    openPrompt(shot);
    return;
  }
  if (button.dataset.action === 'director') {
    state.directorShot = number;
    switchView('director');
  }
  if (button.dataset.action === 'preview') startShotPreview(number, button);
  if (button.dataset.action === 'render') startShotRender(number, button);
  if (button.dataset.action === 'variation') randomizeShotSeed(number, button);
  if (button.dataset.action === 'reroll') rerollShot(number, button);
}, true);

shotGrid.addEventListener('toggle', (event) => {
  const opened = event.target;
  if (!opened.matches('details.storyboard-set')) return;
  if (!opened.open) {
    state.storyboardOpenSet = null;
    return;
  }
  state.storyboardOpenSet = Number(opened.dataset.storyboardSet);
  $$('.storyboard-set', shotGrid).forEach((set) => {
    if (set !== opened) set.open = false;
  });
}, true);

$('#shot-preview-close').addEventListener('click', closeShotPreview);
$('#shot-preview-refresh').addEventListener('click', (event) => {
  const directorActive = $('#director-view').classList.contains('active');
  const shot = directorActive
    ? state.directorShot
    : (state.previewDisplayed?.shot || state.previewJob?.shot);
  if (shot) startShotPreview(shot, event.currentTarget);
});
$('#shot-preview-image').addEventListener('error', closeShotPreview);
$('#shot-preview-image').addEventListener('load', fitShotPreviewWindowToImage);
let previewDrag = null;
$('#shot-preview-drag-handle').addEventListener('pointerdown', (event) => {
  if (event.target.closest('button')) return;
  const preview = $('#shot-preview-window');
  const rect = preview.getBoundingClientRect();
  preview.style.left = `${rect.left}px`;
  preview.style.top = `${rect.top}px`;
  preview.style.right = 'auto';
  preview.style.bottom = 'auto';
  previewDrag = { x: event.clientX, y: event.clientY, left: rect.left, top: rect.top };
  event.currentTarget.setPointerCapture(event.pointerId);
});
$('#shot-preview-drag-handle').addEventListener('pointermove', (event) => {
  if (!previewDrag) return;
  const preview = $('#shot-preview-window');
  const maxLeft = Math.max(8, window.innerWidth - preview.offsetWidth - 8);
  const maxTop = Math.max(8, window.innerHeight - preview.offsetHeight - 8);
  preview.style.left = `${Math.max(8, Math.min(maxLeft, previewDrag.left + event.clientX - previewDrag.x))}px`;
  preview.style.top = `${Math.max(8, Math.min(maxTop, previewDrag.top + event.clientY - previewDrag.y))}px`;
});
$('#shot-preview-drag-handle').addEventListener('pointerup', () => {
  previewDrag = null;
  rememberShotPreviewGeometry();
});
$('#shot-preview-drag-handle').addEventListener('pointercancel', () => {
  previewDrag = null;
  rememberShotPreviewGeometry();
});
window.addEventListener('resize', clampShotPreviewWindow);
if ('ResizeObserver' in window) {
  new ResizeObserver(() => {
    clampShotPreviewWindow();
    rememberShotPreviewGeometry();
  }).observe($('#shot-preview-window'));
}
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !$('#shot-preview-window').classList.contains('hidden')) {
    closeShotPreview();
  }
});

$$('.prompt-tabs button').forEach((button) => button.addEventListener('click', () => {
  state.promptTab = button.dataset.prompt;
  $$('.prompt-tabs button').forEach((item) => item.classList.toggle('active', item === button));
  updatePromptContent();
}));
$('#delete-all-outputs').addEventListener('click', deleteAllOutputs);
$("#director-custom-apply").addEventListener("click", () => saveDirectorCustom(false));
$("#director-custom-clear").addEventListener("click", () => saveDirectorCustom(true));
$$(".director-custom-close").forEach((button) => button.addEventListener("click", () => directorCustomDialog.close()));
directorCustomDialog.addEventListener("cancel", () => { state.directorCustomField = null; });
directorCustomDialog.addEventListener("close", () => { state.directorCustomField = null; });
$("#delete-dialog-confirm").addEventListener('click', () => resolveDeletion(true));
$$('.delete-dialog-cancel').forEach((button) => button.addEventListener('click', () => resolveDeletion(false)));
deleteDialog.addEventListener('cancel', (event) => {
  event.preventDefault();
  resolveDeletion(false);
});
function resolveStoryboardUpdateConfirmation(value) {
  if (updateStoryboardDialog.open) updateStoryboardDialog.close();
  const resolve = state.updateResolver;
  state.updateResolver = null;
  resolve?.(value);
}
$('#update-storyboard-confirm').addEventListener('click', () => resolveStoryboardUpdateConfirmation(true));
$$('.update-storyboard-cancel').forEach((button) => button.addEventListener('click', () => resolveStoryboardUpdateConfirmation(false)));
updateStoryboardDialog.addEventListener('cancel', (event) => {
  event.preventDefault();
  resolveStoryboardUpdateConfirmation(false);
});
$$('.dialog-close').forEach((button) => button.addEventListener('click', () => promptDialog.close()));
$('#copy-prompt').addEventListener('click', async () => {
  await navigator.clipboard.writeText($('#prompt-content').textContent);
});

$('#capture-button').addEventListener('click', openWorkflowProfiles);
$$('.capture-close').forEach((button) => button.addEventListener('click', () => $('#capture-dialog').close()));
$('#capture-confirm').addEventListener('click', captureWorkflow);
$$('.profile-media-tabs [data-profile-media]').forEach((button) => {
  button.addEventListener('click', () => setProfileMedia(button.dataset.profileMedia));
});
$('#workflow-profile-list').addEventListener('click', (event) => {
  const button = event.target.closest('[data-profile-action]');
  if (button) manageWorkflowProfile(button);
});
$('#video-workflow-profile-list').addEventListener('click', (event) => {
  const button = event.target.closest('[data-profile-action]');
  if (button) manageWorkflowProfile(button);
});
$('#image-create-video').addEventListener('click', openVideoDialog);
$$('.video-dialog-close').forEach((button) => button.addEventListener('click', () => videoDialog.close()));
videoDialog.addEventListener('close', () => { state.videoSource = null; });
videoDialog.addEventListener('cancel', () => { state.videoSource = null; });
$('#video-duration').addEventListener('input', (event) => {
  localStorage.setItem('valhalla-video-duration', event.currentTarget.value);
});
$('#video-create-prompt').addEventListener('click', createVideoPrompt);
$('#video-submit').addEventListener('click', submitVideo);
const mobileSystemToggle = $('#mobile-system-toggle');
const systemCard = $('#system-card');

function closeMobileSystem() {
  systemCard.classList.remove('mobile-open');
  mobileSystemToggle.setAttribute('aria-expanded', 'false');
}

mobileSystemToggle.addEventListener('click', (event) => {
  event.stopPropagation();
  const isOpen = systemCard.classList.toggle('mobile-open');
  mobileSystemToggle.setAttribute('aria-expanded', String(isOpen));
});

$$('.nav-item').forEach((button) => button.addEventListener('click', () => {
  closeMobileSystem();
  switchView(button.dataset.view);
}));

document.addEventListener('click', (event) => {
  if (!systemCard.contains(event.target)) closeMobileSystem();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeMobileSystem();
});

function inspectLoggerEvent(element) {
  if (!state.job || !element?.dataset.logIndex) return;
  const logIndex = Number(element.dataset.logIndex);
  const alreadySelected = state.loggerInspection?.jobId === state.job.id
    && state.loggerInspection.logIndex === logIndex;
  state.loggerInspection = alreadySelected ? null : { jobId: state.job.id, logIndex };
  renderLogger();
}

$('#logger-view').addEventListener('click', async (event) => {
  const timelineEvent = event.target.closest('.logger-event.inspectable');
  if (timelineEvent) {
    inspectLoggerEvent(timelineEvent);
    return;
  }
  const button = event.target.closest('[data-copy-log]');
  const prompt = displayedLoggerPrompt();
  if (!button || !prompt || state.privacyCovered) return;
  const key = button.dataset.copyLog;
  try {
    await navigator.clipboard.writeText(prompt[key] || '');
  } catch (error) {
    toast('Could not copy prompt', error.message, 'error');
  }
});
$('#logger-view').addEventListener('keydown', (event) => {
  if (!['Enter', ' '].includes(event.key)) return;
  const timelineEvent = event.target.closest('.logger-event.inspectable');
  if (!timelineEvent) return;
  event.preventDefault();
  inspectLoggerEvent(timelineEvent);
});
$('#logger-rendered-image').addEventListener('error', (event) => {
  event.currentTarget.removeAttribute('src');
  loggerRenderedFrame.classList.remove('has-media');
  sizeLoggerImageColumn();
  $('#logger-rendered-empty').textContent = 'Rendered media is no longer available.';
});
$('#logger-rendered-image').addEventListener('load', sizeLoggerImageColumn);
$('#logger-rendered-video').addEventListener('loadedmetadata', sizeLoggerImageColumn);
$('#logger-rendered-video').addEventListener('error', (event) => {
  event.currentTarget.removeAttribute('src');
  loggerRenderedFrame.classList.remove('has-media');
  sizeLoggerImageColumn();
  $('#logger-rendered-empty').textContent = 'Rendered media is no longer available.';
});
const loggerRenderedFrame = $('.logger-rendered-frame');
if ('ResizeObserver' in window) {
  new ResizeObserver(sizeLoggerImageColumn).observe($('.logger-prompt-grid'));
}
loggerRenderedFrame.addEventListener('click', () => {
  const prompt = displayedLoggerPrompt();
  if (!(prompt?.image_url || prompt?.video_url) || state.privacyCovered) return;
  openLogbookImagePreview(prompt);
});
$('#clear-logger').addEventListener('click', async () => {
  const button = $('#clear-logger');
  setBusy(button, true, 'Clearing…');
  try {
    await api('/api/logger', { method: 'DELETE' });
    state.job = null;
    state.previewJob = null;
    state.loggerInspection = null;
    renderLogger();
  } catch (error) {
    toast('Could not clear logbook', error.message, 'error');
  } finally {
    setBusy(button, false);
  }
});

applyTheme();
applyTypeSize();
applyAccent();
document.documentElement.classList.toggle('privacy-covered', state.privacyCovered);
syncPrivacyControls();
schedulePrivacyIdleCover();
syncForm();
syncPendingState();
syncRenderControls();
syncPreviewScaleControls();
refreshStatus();
restoreApplication();
