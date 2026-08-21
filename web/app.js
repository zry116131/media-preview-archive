const state = {
  projects: [], projectId: null, assets: [], filter: '', analysisFilter: '', view: 'library', ai: null,
  storyboards: [], storyboardId: null, storyboard: null, storyboardSaveTimer: null,
  storyboardReadiness: null,
  dashboardStats: {}, assetTotal: 0, assetOffset: 0, assetHasMore: false,
  assetLoading: false, assetRequest: 0, assetPageSize: 120,
};
const textProviderDefaults = {
  local: { endpoint: 'http://127.0.0.1:7061/v1', model: 'Qwen3-4B' },
  deepseek: { endpoint: 'https://api.deepseek.com', model: 'deepseek-v4-flash' },
};
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (data.readiness) {
      state.storyboardReadiness = data.readiness;
      renderStoryboardGate();
    }
    const error = new Error(data.error || `请求失败：${response.status}`);
    error.code = data.code;
    error.readiness = data.readiness;
    throw error;
  }
  return data;
}

function formatBytes(bytes = 0) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index > 2 ? 2 : 1)} ${units[index]}`;
}

function formatDuration(seconds) {
  if (!seconds) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}

function formatTimestamp(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  const h = Math.floor(value / 3600);
  const m = Math.floor((value % 3600) / 60);
  const s = Math.floor(value % 60);
  return h
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`;
}

function segmentTimeLabel(segment) {
  const start = Number(segment.start_time ?? segment.timestamp ?? 0);
  const end = Number(segment.end_time);
  return Number.isFinite(end) && end > start
    ? `${formatTimestamp(start)}–${formatTimestamp(end)}`
    : formatTimestamp(start);
}

function formatRemaining(seconds) {
  if (seconds == null) return '正在估算';
  if (seconds < 60) return '不足 1 分钟';
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `约 ${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `约 ${hours} 小时 ${rest} 分钟` : `约 ${hours} 小时`;
}

function formatUtcTime(value) {
  if (!value) return '';
  const date = new Date(value.includes('T') ? value : `${value.replace(' ', 'T')}Z`);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
}

function taskTiming(task) {
  if (task.status === 'paused') {
    return task.estimated_seconds_remaining == null
      ? '已暂停 · 继续后重新估算完成时间'
      : `已暂停 · 恢复后还需 ${formatRemaining(task.estimated_seconds_remaining)}`;
  }
  if (task.status === 'pausing') return '正在完成当前处理单元，然后暂停';
  if (task.status === 'running' && task.estimated_finished_at) return `预计 ${formatUtcTime(task.estimated_finished_at)} 完成`;
  if (task.status === 'running') return '正在收集速度，稍后给出预计时间';
  if (task.status === 'queued') return '等待执行';
  if (task.finished_at) return `${formatUtcTime(task.finished_at)} 结束`;
  return formatUtcTime(task.created_at);
}

function escapeHtml(value = '') {
  return String(value).replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#039;', '"': '&quot;' }[char]));
}

function assetAnalysisState(asset) {
  if (asset.analysis_exempted) return { key: 'exempted', label: '已排除' };
  return {
    pending: { key: 'pending', label: '待分析' },
    processing: { key: 'processing', label: '分析中' },
    completed: { key: 'completed', label: '已分析' },
    failed: { key: 'failed', label: '分析异常' },
  }[asset.analysis_status] || { key: 'unknown', label: asset.analysis_status || '未知' };
}

function syncAssetFilterButtons() {
  $$('.filter').forEach(button => button.classList.toggle('active', button.dataset.type === state.filter));
  $$('.analysis-filter').forEach(button => button.classList.toggle('active', button.dataset.analysis === state.analysisFilter));
}

function toast(message) {
  const element = $('#toast');
  element.textContent = message;
  element.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove('show'), 2600);
}

async function copyText(value) {
  const text = String(value || '');
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
  } catch (_) {
    // LAN HTTP pages may expose Clipboard API but reject writes; use selection fallback.
  }
  const input = document.createElement('textarea');
  input.value = text;
  input.setAttribute('readonly', '');
  input.style.position = 'fixed';
  input.style.left = '-9999px';
  input.style.opacity = '0';
  document.body.appendChild(input);
  input.focus();
  input.select();
  input.setSelectionRange(0, input.value.length);
  const copied = document.execCommand('copy');
  input.remove();
  if (!copied) throw new Error('浏览器未允许复制，请手动选择路径');
}

async function loadProjects(selectNewest = false) {
  state.projects = await api('/api/projects');
  const select = $('#project-select');
  select.innerHTML = state.projects.length
    ? state.projects.map(p => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join('')
    : '<option value="">未配置项目</option>';
  if (selectNewest && state.projects.length) state.projectId = state.projects[0].id;
  if (!state.projectId && state.projects.length) state.projectId = state.projects[0].id;
  select.value = state.projectId || '';
  await refreshCurrentProject();
}

async function refreshCurrentProject() {
  const empty = $('#empty-state');
  if (!state.projectId) {
    state.storyboardReadiness = null;
    empty.classList.add('show');
    $('#scan-project').disabled = true;
    $('#thumbnail-project').disabled = true;
    $('#analyze-project').disabled = true;
    $('#asset-grid').innerHTML = '';
    $('#asset-load-more').hidden = true;
    renderMetrics(null);
    clearStoryboardWorkspace();
    return;
  }
  const projectId = state.projectId;
  empty.classList.remove('show');
  $('#scan-project').disabled = false;
  $('#thumbnail-project').disabled = false;
  $('#analyze-project').disabled = !state.ai?.enabled;
  const dashboard = await api(`/api/projects/${projectId}/dashboard`);
  if (projectId !== state.projectId) return;
  renderMetrics(dashboard);
  loadStorageHealth().catch(() => {});
  await loadAssets();
  await loadStoryboards();
}

function renderMetrics(data) {
  const stats = data?.stats || {};
  state.dashboardStats = stats;
  state.storyboardReadiness = data?.storyboard_readiness || null;
  $('#metric-size').textContent = formatBytes(stats.bytes || 0);
  $('#metric-total').textContent = stats.active || 0;
  $('#metric-types').textContent = `${stats.videos || 0} / ${stats.images || 0}`;
  $('#metric-pending').textContent = stats.pending || 0;
  $('#project-path').textContent = data?.project?.windows_path || data?.project?.smb_url || '尚未配置项目';
  const storage = $('#storage-status');
  storage.textContent = data ? '正在检查 SMB 与 NAS' : '尚未配置存储';
  storage.dataset.state = data ? 'checking' : 'degraded';
  renderAiMetricNote();
  renderStoryboardGate();
  updateProjectActionState();
}

async function loadStorageHealth() {
  if (!state.projectId) return;
  const projectId = state.projectId;
  const health = await api(`/api/storage/health?project_id=${projectId}`);
  if (projectId !== state.projectId) return;
  const storage = $('#storage-status');
  const smbOnline = Boolean(health.project?.mounted);
  const nasOnline = Boolean(health.nas?.online);
  storage.textContent = `${smbOnline ? 'SMB 在线' : 'SMB 异常'} · ${nasOnline ? 'NAS 直连在线' : 'NAS 直连异常'}`;
  storage.dataset.state = smbOnline && nasOnline ? 'online' : 'degraded';
  storage.title = [health.project?.message, health.nas?.message].filter(Boolean).join('；');
}

function renderAiMetricNote() {
  const failed = Number(state.dashboardStats?.failed || 0);
  const exempted = Number(state.dashboardStats?.exempted || 0);
  const statusNotes = [];
  if (failed) statusNotes.push(`${failed} 条异常`);
  if (exempted) statusNotes.push(`${exempted} 条已排除`);
  const serviceNote = state.ai
    ? (state.ai.enabled ? 'JoyAI 可手动分析' : 'JoyAI 推理服务不可用')
    : '正在检查 JoyAI 状态';
  statusNotes.push(serviceNote);
  $('#ai-metric-note').textContent = statusNotes.join(' · ');
}

function storyboardAuthoringReady() {
  return Boolean(state.projectId && state.storyboardReadiness?.ready);
}

function analysisPrerequisitesReady() {
  const readiness = state.storyboardReadiness;
  const requirements = readiness?.requirements;
  return Boolean(
    state.projectId
    && requirements?.scan_completed
    && requirements?.scan_idle
    && requirements?.has_assets
    && !(readiness?.active_tasks?.analysis || []).length
  );
}

function updateProjectActionState() {
  const analysisRunning = Boolean(state.storyboardReadiness?.active_tasks?.analysis?.length);
  $('#scan-project').disabled = !state.projectId || analysisRunning;
  $('#thumbnail-project').disabled = !state.projectId;
  $('#analyze-project').disabled = !state.ai?.enabled || !analysisPrerequisitesReady();
}

function applyStoryboardControls() {
  const ready = storyboardAuthoringReady();
  [
    '#new-storyboard', '#import-script', '#import-shots', '#script-file',
    '#shots-file', '#storyboard-title', '#script-input', '#split-script',
  ].forEach(selector => {
    const element = $(selector);
    if (element) element.disabled = !ready;
  });
  const versionSelect = $('#storyboard-version-select');
  if (versionSelect) {
    versionSelect.disabled = !ready || !(state.storyboard?.versions || []).length;
  }
  $$('[data-select-shot]').forEach(button => {
    button.disabled = !ready;
    button.title = ready ? '' : '素材扫描与分析完成后才能更换素材';
  });
  const deleteButton = $('#delete-storyboard');
  if (deleteButton) {
    deleteButton.disabled = !state.storyboardId;
    deleteButton.title = state.storyboardId
      ? (ready ? '永久删除当前分镜册' : '历史内容只读，但仍可清理这本分镜册')
      : '请先选择一本分镜册';
  }
  if (!ready) clearTimeout(state.storyboardSaveTimer);
}

function setStoryboardGateStep(id, status, title, detail) {
  const element = $(`#${id}`);
  if (!element) return;
  const item = element.closest('.storyboard-gate-status-item') || element;
  item.dataset.state = {
    done: 'ready', working: 'blocked', pending: 'checking', error: 'error',
  }[status] || status;
  const titleElement = element.matches('strong') ? element : element.querySelector('strong');
  const detailElement = item.querySelector('small');
  if (titleElement) titleElement.textContent = title;
  if (detailElement) detailElement.textContent = detail;
  if (!titleElement && !detailElement) element.textContent = `${title} · ${detail}`;
}

function renderStoryboardGate() {
  applyStoryboardControls();
  const gate = $('#storyboard-gate');
  if (!gate) return;

  const readiness = state.storyboardReadiness;
  const ready = storyboardAuthoringReady();
  gate.hidden = ready;
  if (ready) {
    const saveState = $('#storyboard-save-state');
    if (saveState?.dataset.state === 'locked') {
      setSaveState(
        state.storyboard ? `已保存 · ${formatUtcTime(state.storyboard.updated_at)}` : '粘贴内容会自动保存',
        state.storyboard ? 'saved' : '',
      );
    }
    return;
  }

  const requirements = readiness?.requirements || {};
  const counts = readiness?.counts || {
    active: 0, required: 0, completed: 0, exempted: 0,
    pending: 0, processing: 0, failed: 0, remaining: 0,
  };
  const scanRunning = Boolean(readiness?.active_tasks?.scan?.length);
  const analysisRunning = Boolean(readiness?.active_tasks?.analysis?.length);
  const scanFailed = readiness?.state === 'scan_failed';
  const noProject = !state.projectId;

  $('#storyboard-gate-title').textContent = noProject
    ? '先选择一个素材项目'
    : (scanRunning || analysisRunning
      ? '素材准备尚未完成'
      : (counts.failed ? '有异常素材需要处理' : '分镜工作区暂未解锁'));
  $('#storyboard-gate-message').textContent = noProject
    ? '分镜生成依赖项目素材索引与 JoyAI 画面理解结果。'
    : (readiness?.message || '正在读取项目的扫描与分析状态。');

  setStoryboardGateStep(
    'storyboard-gate-scan-status',
    requirements.scan_completed && requirements.scan_idle ? 'done' : (scanFailed ? 'error' : (scanRunning ? 'working' : 'pending')),
    requirements.scan_completed && requirements.scan_idle ? '素材扫描已完成' : (scanFailed ? '最近扫描失败' : (scanRunning ? '正在扫描素材' : '等待素材扫描')),
    readiness?.last_scan_at ? `上次完成于 ${formatUtcTime(readiness.last_scan_at)}` : '先刷新 SMB 项目目录，建立素材索引',
  );
  setStoryboardGateStep(
    'storyboard-gate-asset-status',
    counts.active > 0 ? 'done' : (requirements.scan_completed ? 'error' : 'pending'),
    counts.active > 0 ? `已发现 ${counts.active} 条素材` : '尚无可用素材',
    counts.active > 0 ? '仅统计当前有效素材，已移走素材不参与门槛' : '扫描完成后会在这里显示素材数量',
  );
  const analysisParts = [`${counts.completed} 条已分析`];
  if (counts.exempted) analysisParts.push(`${counts.exempted} 条已排除`);
  if (counts.failed) analysisParts.push(`${counts.failed} 条异常`);
  if (counts.processing) analysisParts.push(`${counts.processing} 条分析中`);
  if (counts.pending) analysisParts.push(`${counts.pending} 条待分析`);
  const analysisTitle = requirements.all_assets_analyzed && requirements.analysis_idle
    ? 'JoyAI 分析已完成'
    : (counts.failed
      ? `${counts.failed} 条素材分析异常`
      : (readiness?.state === 'no_analyzed_assets'
        ? '至少需要 1 条真实分析结果'
        : (analysisRunning ? 'JoyAI 正在分析' : '等待完整分析')));
  setStoryboardGateStep(
    'storyboard-gate-analysis-status',
    requirements.all_assets_analyzed && requirements.analysis_idle ? 'done' : (counts.failed ? 'error' : (analysisRunning ? 'working' : 'pending')),
    analysisTitle,
    analysisParts.join(' · '),
  );

  let action = readiness?.next_action || (noProject ? 'library' : 'tasks');
  let label = {
    scan: '刷新素材', analysis: '启动 JoyAI 分析', tasks: '查看任务中心', library: '返回素材库',
  }[action] || '查看任务中心';
  if (action === 'analysis' && counts.failed) label = '重试失败素材';
  if (readiness?.state === 'no_analyzed_assets') {
    action = 'exempted';
    label = '查看已排除素材';
  }
  if (action === 'analysis' && state.ai && !state.ai.enabled) {
    action = 'ai';
    label = '检查 AI 设置';
  }
  const primary = $('#storyboard-gate-primary');
  primary.dataset.action = action;
  primary.dataset.analysisScope = counts.failed ? 'failed' : 'pending';
  primary.textContent = label;
  primary.disabled = action === 'analysis' && !state.ai?.enabled;
  const tasksButton = $('#storyboard-gate-tasks');
  tasksButton.hidden = noProject || action === 'tasks';
  const exceptionsButton = $('#storyboard-gate-exceptions');
  exceptionsButton.hidden = noProject || !counts.failed;
  exceptionsButton.textContent = counts.failed ? `查看异常素材（${counts.failed}）` : '查看异常素材';

  setSaveState(
    state.storyboardId
      ? '素材准备未完成 · 历史内容只读，可预览、导出或删除分镜册'
      : '完成素材扫描与分析后可创建分镜册',
    'locked',
  );
}

async function loadStoryboardReadiness() {
  if (!state.projectId) {
    state.storyboardReadiness = null;
    renderStoryboardGate();
    updateProjectActionState();
    return null;
  }
  const projectId = state.projectId;
  const readiness = await api(`/api/projects/${projectId}/storyboard-readiness`);
  if (projectId !== state.projectId) return null;
  state.storyboardReadiness = readiness;
  renderStoryboardGate();
  updateProjectActionState();
  return readiness;
}

function storyboardActionReady() {
  if (storyboardAuthoringReady()) return true;
  toast(state.storyboardReadiness?.message || '请先完成素材扫描与 JoyAI 分析');
  renderStoryboardGate();
  return false;
}

async function handleStoryboardGateAction() {
  const primary = $('#storyboard-gate-primary');
  const action = primary.dataset.action;
  try {
    if (action === 'scan') return await startScan();
    if (action === 'analysis') return openAnalysisDialog(primary.dataset.analysisScope || 'pending');
    if (action === 'exempted') return showAssetsByAnalysisStatus('exempted');
    if (action === 'ai') return setView('ai');
    if (action === 'library') return setView('library');
    return setView('tasks');
  } catch (error) {
    toast(error.message);
    await loadStoryboardReadiness().catch(() => {});
  }
}

async function showAssetsByAnalysisStatus(analysisStatus) {
  if (!state.projectId) return setView('library');
  state.filter = '';
  state.analysisFilter = analysisStatus;
  $('#asset-search').value = '';
  syncAssetFilterButtons();
  setView('library');
  try {
    await loadAssets();
  } catch (error) {
    toast(error.message);
  }
}

async function showFailedAssets() {
  return showAssetsByAnalysisStatus('failed');
}

function assetFolder(asset) {
  const parts = asset.relative_path.split('/').filter(Boolean);
  return parts.length > 1 ? parts[0] : '项目根目录';
}

function renderAssetCard(asset, index) {
  const thumbnail = asset.thumb_status === 'failed'
    ? ''
    : `<img src="/api/assets/${asset.id}/thumbnail" alt="" loading="lazy" data-thumbnail-id="${asset.id}">`;
  const analysis = assetAnalysisState(asset);
  return `
    <article class="asset-card" data-id="${asset.id}" tabindex="0">
      <div class="asset-preview">
        <div class="thumb-fallback">${asset.media_type === 'video' ? 'VIDEO' : 'IMAGE'}</div>
        ${thumbnail}
        <span class="asset-index">${String(index + 1).padStart(3, '0')}</span>
        <span class="analysis-badge ${escapeHtml(analysis.key)}">${escapeHtml(analysis.label)}</span>
        <span class="media-badge">${asset.media_type === 'video' ? formatDuration(asset.duration) : 'STILL'}</span>
      </div>
      <div class="asset-meta">
        <h3 title="${escapeHtml(asset.filename)}">${escapeHtml(asset.filename)}</h3>
        <p><span>${escapeHtml(asset.extension.replace('.', '').toUpperCase())}</span><span>${formatBytes(asset.size_bytes)}</span></p>
        ${asset.analysis_summary ? `<p class="asset-summary">${escapeHtml(asset.analysis_summary)}</p>` : ''}
      </div>
    </article>`;
}

function bindAssetCards() {
  $$('.asset-card').forEach(card => {
    const open = () => openAsset(Number(card.dataset.id));
    card.addEventListener('click', open);
    card.addEventListener('keydown', event => { if (event.key === 'Enter') open(); });
  });
  $$('[data-thumbnail-id]').forEach(image => {
    image.addEventListener('error', () => {
      const attempt = Number(image.dataset.retryAttempt || 0);
      if (attempt >= 4) return;
      const delays = [2000, 5000, 12000, 25000];
      image.dataset.retryAttempt = String(attempt + 1);
      setTimeout(() => {
        if (!image.isConnected) return;
        image.src = `/api/assets/${image.dataset.thumbnailId}/thumbnail?retry=${Date.now()}`;
      }, delays[attempt]);
    });
  });
}

function renderAssets() {
  const grid = $('#asset-grid');
  const folders = new Map();
  state.assets.forEach(asset => {
    const folder = assetFolder(asset);
    if (!folders.has(folder)) folders.set(folder, []);
    folders.get(folder).push(asset);
  });
  let cardIndex = 0;
  const groupsMarkup = [...folders.entries()].map(([folder, assets]) => `
    <section class="folder-group">
      <header class="folder-heading">
        <div><span>PROJECT FOLDER</span><h2>${escapeHtml(folder)}</h2></div>
        <strong>${assets.length} 项已加载</strong>
      </header>
      <div class="asset-grid">${assets.map(asset => renderAssetCard(asset, cardIndex++)).join('')}</div>
    </section>`).join('');
  grid.innerHTML = groupsMarkup || `
    <div class="filtered-empty">
      <span>NO MATCH</span>
      <h2>当前筛选下没有素材</h2>
      <p>可以切换素材类型、AI 状态，或清除搜索关键词。</p>
    </div>`;
  bindAssetCards();
  const loadMore = $('#asset-load-more');
  loadMore.hidden = !state.assetHasMore;
  loadMore.disabled = state.assetLoading;
  loadMore.textContent = state.assetLoading
    ? '正在加载…'
    : `加载更多素材（剩余 ${Math.max(0, state.assetTotal - state.assets.length)}）`;
  $('#asset-count').textContent = state.assetTotal
    ? `已加载 ${state.assets.length} / ${state.assetTotal}`
    : '0 项素材';
}

async function loadAssets(reset = true) {
  if (state.assetLoading && !reset) return;
  if (reset) {
    state.assetRequest += 1;
    state.assets = [];
    state.assetOffset = 0;
    state.assetTotal = 0;
    state.assetHasMore = false;
  }
  const requestToken = state.assetRequest;
  const projectId = state.projectId;
  state.assetLoading = true;
  $('#asset-load-more').disabled = true;
  const query = new URLSearchParams();
  if (state.filter) query.set('type', state.filter);
  if (state.analysisFilter) query.set('analysis', state.analysisFilter);
  const search = $('#asset-search').value.trim();
  if (search) query.set('q', search);
  query.set('paged', '1');
  query.set('limit', String(state.assetPageSize));
  query.set('offset', String(state.assetOffset));
  try {
    const page = await api(`/api/projects/${projectId}/assets?${query}`);
    if (requestToken !== state.assetRequest || projectId !== state.projectId) return;
    state.assets.push(...page.items);
    state.assetTotal = page.total;
    state.assetOffset = page.next_offset;
    state.assetHasMore = page.has_more;
  } finally {
    if (requestToken === state.assetRequest && projectId === state.projectId) {
      state.assetLoading = false;
      renderAssets();
    }
  }
}

async function openAsset(id) {
  const asset = await api(`/api/assets/${id}`);
  const analysis = assetAnalysisState(asset);
  const segments = Array.isArray(asset.analysis_segments) ? asset.analysis_segments : [];
  const profile = asset.analysis_profile || null;
  const profileMetrics = [];
  if (profile?.chunk_seconds != null) profileMetrics.push(`处理单元 ${formatDuration(profile.chunk_seconds)}`);
  if (Number(profile?.overlap_seconds || 0) > 0) profileMetrics.push(`重叠 ${Number(profile.overlap_seconds).toFixed(0)} 秒`);
  if (profile?.sample_fps != null) profileMetrics.push(`采样 ${Number(profile.sample_fps).toFixed(2).replace(/\.00$/, '')} FPS`);
  if (profile?.heavy_source) profileMetrics.push('高规格源轻量化');
  if (profile?.preprocess) profileMetrics.push(String(profile.preprocess));
  if (profile?.source_bit_rate) profileMetrics.push(`源码率 ${(Number(profile.source_bit_rate) / 1000000).toFixed(1)} Mbps`);
  const profileNotes = Array.isArray(profile?.notes) ? profile.notes.filter(Boolean) : [];
  const fallbackMarkup = Number(profile?.fallback_segments || 0) > 0
    ? `<strong>${escapeHtml(profile.fallback_segments)} 段已降级为抽帧兼容分析</strong>`
    : profile?.mode === 'video' ? '<span>原生视频理解成功，未触发片段降级</span>' : '';
  const profileMarkup = profile ? `
    <section class="strategy-profile-card">
      <p class="eyebrow">ANALYSIS ROUTE</p>
      <div class="strategy-profile-heading">
        <strong>${escapeHtml(profile.label || asset.analysis_strategy || '自适应分析')}</strong>
        <span>${escapeHtml(profile.reason || '')}</span>
      </div>
      ${profileMetrics.length ? `<div class="strategy-profile-metrics">${profileMetrics.map(item => `<span>${escapeHtml(item)}</span>`).join('')}</div>` : ''}
      ${profileNotes.length ? `<p class="strategy-profile-notes">${profileNotes.map(escapeHtml).join(' · ')}</p>` : ''}
      <p class="strategy-profile-result">
        ${profile.segment_count ? `<span>${asset.media_type === 'image' ? `${escapeHtml(profile.segment_count)} 张图片` : `${escapeHtml(profile.segment_count)} 个处理片段`}</span>` : ''}
        ${fallbackMarkup}
      </p>
    </section>` : '';
  const analysisExceptionMarkup = asset.analysis_exempted ? `
    <section class="analysis-exception-panel exempted">
      <p class="eyebrow">MANUAL EXCLUSION</p>
      <h3>已从当前项目的分析门槛中排除</h3>
      <p class="analysis-exception-reason"><strong>排除原因</strong>${escapeHtml(asset.analysis_exempt_reason || '已确认异常')}</p>
      <p>NAS 原文件没有被删除或修改，这条素材也不会进入分镜匹配。文件内容变化并重新扫描后，系统会自动撤销排除。</p>
      ${asset.analysis_exempted_at ? `<small>排除于 ${escapeHtml(formatUtcTime(asset.analysis_exempted_at))}</small>` : ''}
      <div class="analysis-exception-actions">
        <button type="button" class="button ghost" data-restore-analysis>恢复为待分析</button>
      </div>
    </section>` : asset.analysis_status === 'failed' ? `
    <section class="analysis-exception-panel failed">
      <p class="eyebrow">ANALYSIS EXCEPTION</p>
      <h3>JoyAI 无法识别这条素材</h3>
      <p class="analysis-exception-error">${escapeHtml(asset.analysis_error || '未返回具体错误')}</p>
      <p>先单独重试；若确认文件损坏、无法解码或本项目不用，再填写原因排除。</p>
      <div class="analysis-exception-actions">
        <button type="button" class="button primary" data-retry-analysis>重试该素材</button>
        <button type="button" class="button danger-outline" data-exempt-analysis>标记异常并排除</button>
      </div>
    </section>` : '';
  const mediaHost = $('#drawer-media');
  const media = document.createElement(asset.media_type === 'video' ? 'video' : 'img');
  if (asset.media_type === 'video') {
    media.controls = true;
    media.preload = 'metadata';
    media.poster = `/api/assets/${id}/thumbnail`;
  } else {
    media.alt = asset.filename;
  }
  const mediaStatus = document.createElement('div');
  mediaStatus.className = 'direct-media-status';
  mediaStatus.textContent = asset.direct_play_available
    ? '正在从 NAS 获取临时直连地址…'
    : 'NAS 统一访问账户尚未配置';
  mediaHost.replaceChildren(media, mediaStatus);
  $('#drawer-body').innerHTML = `
    <p class="eyebrow">ASSET ${String(asset.id).padStart(5, '0')}</p>
    <h2>${escapeHtml(asset.filename)}</h2>
    <p class="path">${escapeHtml(asset.relative_path)}</p>
    <div class="detail-grid">
      <div><span>类型</span><strong>${asset.media_type === 'video' ? '视频' : '图片'}</strong></div>
      <div><span>大小</span><strong>${formatBytes(asset.size_bytes)}</strong></div>
      <div><span>时长</span><strong>${formatDuration(asset.duration)}</strong></div>
      <div><span>分辨率</span><strong>${asset.width && asset.height ? `${asset.width} × ${asset.height}` : '—'}</strong></div>
      <div><span>帧率 / 编码</span><strong>${asset.fps ? `${Number(asset.fps).toFixed(2)} FPS` : '—'}${asset.codec ? ` · ${escapeHtml(String(asset.codec).toUpperCase())}` : ''}</strong></div>
      <div><span>索引状态</span><strong>${escapeHtml(asset.status)}</strong></div>
      <div><span>AI 状态</span><strong class="analysis-status-text ${escapeHtml(analysis.key)}">${escapeHtml(analysis.label)}</strong></div>
      <div><span>分析策略</span><strong>${escapeHtml(asset.analysis_strategy || '尚未分析')}</strong></div>
    </div>
    ${profileMarkup}
    ${asset.analysis_summary ? `
      <section class="analysis-block">
        <p class="eyebrow">JOYAI UNDERSTANDING</p>
        <p>${escapeHtml(asset.analysis_summary)}</p>
        <div class="keyword-list">${(asset.analysis_keywords || []).map(item => `<span>${escapeHtml(item)}</span>`).join('')}</div>
        ${segments.length ? `
          <div class="analysis-timeline">
            <p class="timeline-title">内容时间轴 · ${segments.length} 段</p>
            ${segments.map((segment, index) => `
              <button type="button" class="timeline-item" data-seek="${Number(segment.timestamp) || 0}">
                <span class="timeline-index">${String(index + 1).padStart(2, '0')}</span>
                <span class="timeline-time">${segmentTimeLabel(segment)}</span>
                <span class="timeline-copy">
                  <strong>${escapeHtml(segment.summary || '未生成片段描述')}</strong>
                  <small>${(segment.keywords || []).slice(0, 6).map(escapeHtml).join(' · ')}</small>
                </span>
              </button>`).join('')}
          </div>` : ''}
      </section>` : ''}
    ${analysisExceptionMarkup}
    <p class="eyebrow">WINDOWS UNC PATH</p>
    <div class="smb-box">${escapeHtml(asset.smb_path)}</div>
    <button class="button ghost" id="copy-smb" style="margin-top:12px">复制 Windows 路径</button>`;
  $('#copy-smb').addEventListener('click', async () => {
    try {
      await copyText(asset.smb_path);
      toast('Windows 路径已复制，可直接粘贴到资源管理器');
    } catch (error) {
      toast(error.message);
    }
  });
  $('#drawer-body').querySelectorAll('[data-seek]').forEach(button => {
    button.addEventListener('click', () => {
      const video = $('#drawer-media video');
      if (!video) return;
      video.currentTime = Number(button.dataset.seek) || 0;
      video.play().catch(() => {});
    });
  });
  $('#drawer-body [data-retry-analysis]')?.addEventListener('click', event => retryAssetAnalysis(asset.id, event.currentTarget));
  $('#drawer-body [data-exempt-analysis]')?.addEventListener('click', () => openAssetExemptionDialog(asset));
  $('#drawer-body [data-restore-analysis]')?.addEventListener('click', event => restoreAssetAnalysis(asset.id, event.currentTarget));
  const drawer = $('#asset-drawer');
  drawer.classList.add('open');
  drawer.setAttribute('aria-hidden', 'false');
  drawer.inert = false;
  $('#drawer-close').focus();

  if (asset.direct_play_available) {
    try {
      const direct = await api(`/api/assets/${id}/direct-play`);
      const readyEvent = asset.media_type === 'video' ? 'loadedmetadata' : 'load';
      media.addEventListener(readyEvent, () => mediaStatus.classList.add('hidden'), { once: true });
      media.addEventListener('error', () => {
        mediaStatus.textContent = 'NAS 直连加载失败。请确认浏览器已信任 NAS 证书，或复制下方 Windows 路径打开。';
        mediaStatus.classList.add('error');
      }, { once: true });
      media.src = direct.url;
      if (asset.media_type === 'video') media.load();
    } catch (error) {
      mediaStatus.textContent = `NAS 直连失败：${error.message}`;
      mediaStatus.classList.add('error');
    }
  }
}

function closeDrawer() {
  const drawer = $('#asset-drawer');
  drawer.classList.remove('open');
  drawer.setAttribute('aria-hidden', 'true');
  drawer.inert = true;
  $('#drawer-media').innerHTML = '';
  $('#drawer-body').innerHTML = '';
}

async function retryAssetAnalysis(assetId, button) {
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = '正在创建任务…';
  let result;
  try {
    result = await api(`/api/assets/${assetId}/analysis/retry`, {
      method: 'POST', body: '{}',
    });
  } catch (error) {
    toast(error.message);
    button.disabled = false;
    button.textContent = originalLabel;
    await loadStoryboardReadiness().catch(() => {});
    return;
  }
  closeDrawer();
  if (result.readiness) state.storyboardReadiness = result.readiness;
  const refreshError = await refreshCurrentProject().then(() => null).catch(error => error);
  toast(refreshError
    ? `${result.message || '任务已创建'}；页面刷新失败：${refreshError.message}`
    : (result.message || '已创建单条素材重试任务'));
  setView('tasks');
  pollTasks();
}

function openAssetExemptionDialog(asset) {
  const dialog = $('#asset-exemption-dialog');
  const form = $('#asset-exemption-form');
  form.reset();
  dialog.dataset.assetId = String(asset.id);
  $('#asset-exemption-name').textContent = asset.filename;
  $('#asset-exemption-error').textContent = asset.analysis_error || 'JoyAI 无法读取分析画面';
  $('#asset-exemption-form-error').textContent = '';
  dialog.showModal();
  form.elements.reason.focus();
}

function closeAssetExemptionDialog() {
  const dialog = $('#asset-exemption-dialog');
  if (dialog.open) dialog.close();
  delete dialog.dataset.assetId;
  $('#asset-exemption-form-error').textContent = '';
}

async function submitAssetExemption(event) {
  event.preventDefault();
  const dialog = $('#asset-exemption-dialog');
  const form = $('#asset-exemption-form');
  const assetId = Number(dialog.dataset.assetId);
  const values = Object.fromEntries(new FormData(form));
  const selectedReason = String(values.reason || '').trim();
  const details = String(values.details || '').trim();
  const errorElement = $('#asset-exemption-form-error');
  if (!assetId || !selectedReason) {
    errorElement.textContent = '请选择排除原因。';
    return;
  }
  const button = $('#confirm-asset-exemption');
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = '正在排除…';
  errorElement.textContent = '';
  let result;
  try {
    result = await api(`/api/assets/${assetId}/analysis-exemption`, {
      method: 'POST',
      body: JSON.stringify({ exempt: true, reason: details ? `${selectedReason}：${details}` : selectedReason }),
    });
  } catch (error) {
    errorElement.textContent = error.message;
    button.disabled = false;
    button.textContent = originalLabel;
    return;
  }
  closeAssetExemptionDialog();
  closeDrawer();
  button.disabled = false;
  button.textContent = originalLabel;
  if (result.readiness) state.storyboardReadiness = result.readiness;
  const refreshError = await refreshCurrentProject().then(() => null).catch(error => error);
  toast(refreshError
    ? `${result.message || '排除已保存'}；页面刷新失败：${refreshError.message}`
    : (result.message || '异常素材已排除'));
}

async function restoreAssetAnalysis(assetId, button) {
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = '正在恢复…';
  let result;
  try {
    result = await api(`/api/assets/${assetId}/analysis-exemption`, {
      method: 'POST', body: JSON.stringify({ exempt: false }),
    });
  } catch (error) {
    toast(error.message);
    button.disabled = false;
    button.textContent = originalLabel;
    await loadStoryboardReadiness().catch(() => {});
    return;
  }
  closeDrawer();
  if (result.readiness) state.storyboardReadiness = result.readiness;
  const refreshError = await refreshCurrentProject().then(() => null).catch(error => error);
  toast(refreshError
    ? `${result.message || '恢复已保存'}；页面刷新失败：${refreshError.message}`
    : (result.message || '素材已恢复为待分析'));
}

async function loadTasks(prefetchedTasks = null) {
  const tasks = prefetchedTasks || await api('/api/tasks');
  const taskTypeLabel = task => ({ scan: '素材扫描', analysis: 'AI 分析', thumbnail: '预览图缓存' }[task.task_type] || task.task_type);
  $('#task-list').innerHTML = tasks.length ? tasks.map(task => `
    <article class="task-item ${escapeHtml(task.status)}">
      <div class="task-type">${escapeHtml(taskTypeLabel(task))}<br><small>${escapeHtml(task.project_name || '系统')}</small></div>
      <div class="task-message"><strong>${escapeHtml(task.message || '等待处理')}</strong><small class="task-eta">${escapeHtml(taskTiming(task))}</small></div>
      <div class="task-control">
        <div class="progress-track"><div class="progress-fill" style="width:${Math.round(task.progress * 100)}%"></div></div>
        <div class="task-status">${escapeHtml(task.status)} · ${Math.round(task.progress * 100)}%</div>
        ${['queued', 'running'].includes(task.status) ? `<button class="task-action" data-task-id="${task.id}" data-action="pause">暂停</button>` : ''}
        ${task.status === 'pausing' ? '<button class="task-action" disabled>暂停中</button>' : ''}
        ${task.status === 'paused' ? `<button class="task-action resume" data-task-id="${task.id}" data-action="resume">继续</button>` : ''}
      </div>
    </article>`).join('') : '<p class="muted-note">暂无后台任务。</p>';
  $$('.task-action[data-action]').forEach(button => button.addEventListener('click', () => controlTask(button)));
  return tasks;
}

async function controlTask(button) {
  const taskId = Number(button.dataset.taskId);
  const action = button.dataset.action;
  button.disabled = true;
  try {
    const result = await api(`/api/tasks/${taskId}/${action}`, { method: 'POST', body: '{}' });
    toast(result.message);
    await loadTasks();
    if (action === 'resume') pollTasks();
  } catch (error) {
    toast(error.message);
    await loadTasks();
  }
}

async function loadAiStatus(showToast = false) {
  const status = await api('/api/ai/status');
  state.ai = status;
  $('#ai-state-label').textContent = status.status === 'online' ? '服务在线' : '服务不可用';
  $('#ai-status-message').textContent = status.message;
  $('#ai-state-dot').classList.toggle('online', status.enabled);
  $('#ai-model-select').value = status.model;
  $('#ai-endpoint').value = status.endpoint || '尚未部署';
  const textPlanner = status.text_planner || {};
  renderTextModelStatus(textPlanner, true);
  renderAiMetricNote();
  updateProjectActionState();
  renderStoryboardGate();
  if (showToast) toast(status.message);
}

function providerLabel(provider) {
  return { local: '本地模型', deepseek: 'DeepSeek', custom: '自定义 API' }[provider] || '未配置';
}

function renderTextModelStatus(config, populateForm = false) {
  const enabled = Boolean(config?.enabled);
  $('#text-state-label').textContent = enabled ? '服务在线' : '服务不可用';
  $('#text-status-message').textContent = config?.message || '文案规划服务尚未配置。';
  $('#text-state-dot').classList.toggle('online', enabled);
  $('#text-provider-summary').textContent = providerLabel(config?.provider).toUpperCase();
  $('#text-model-summary').textContent = config?.model || '—';
  if (!populateForm) return;
  $('#text-provider').value = config?.provider || 'local';
  $('#text-endpoint').value = config?.endpoint || textProviderDefaults.local.endpoint;
  $('#text-model-name').value = config?.model || textProviderDefaults.local.model;
  $('#text-model-form').dataset.keyConfigured = config?.api_key_configured ? 'true' : 'false';
  $('#text-api-key').value = '';
  configureTextProviderFields(false);
}

function configureTextProviderFields(resetDefaults = false) {
  const provider = $('#text-provider').value;
  const defaults = textProviderDefaults[provider];
  if (resetDefaults && defaults) {
    $('#text-endpoint').value = defaults.endpoint;
    $('#text-model-name').value = defaults.model;
  } else if (resetDefaults && provider === 'custom') {
    $('#text-endpoint').value = '';
    $('#text-model-name').value = '';
  }
  $('#text-endpoint').readOnly = provider !== 'custom';
  const keyInput = $('#text-api-key');
  const keyHelp = $('#text-key-help');
  keyInput.disabled = provider === 'local';
  if (provider === 'local') {
    keyInput.value = '';
    keyInput.placeholder = '本地模型无需填写';
    keyHelp.textContent = '本地 Qwen 服务在内部节点运行，不需要 API Key。';
  } else {
    const configured = $('#text-model-form').dataset.keyConfigured === 'true';
    keyInput.placeholder = configured ? '已配置，留空表示继续使用' : '输入服务商 API Key';
    keyHelp.textContent = configured
      ? '后端已有密钥；留空保存或测试时会保留原值。'
      : '密钥只提交给网站后端，页面不会再次读取明文。';
  }
  $('#text-config-message').textContent = '';
}

function textModelPayload() {
  const payload = {
    provider: $('#text-provider').value,
    endpoint: $('#text-endpoint').value.trim(),
    model: $('#text-model-name').value.trim(),
  };
  const apiKey = $('#text-api-key').value.trim();
  if (apiKey) payload.api_key = apiKey;
  return payload;
}

function setTextConfigMessage(message, kind = '') {
  const element = $('#text-config-message');
  element.textContent = message;
  element.dataset.kind = kind;
}

async function testTextModel() {
  const button = $('#test-text-model');
  button.disabled = true;
  setTextConfigMessage('正在连接模型服务…', 'working');
  try {
    const result = await api('/api/ai/settings/text-planner/test', {
      method: 'POST', body: JSON.stringify(textModelPayload()),
    });
    renderTextModelStatus(result, false);
    setTextConfigMessage(result.enabled ? '连接成功，可以保存并启用。' : result.message, result.enabled ? 'success' : 'error');
  } catch (error) {
    setTextConfigMessage(error.message, 'error');
  } finally {
    button.disabled = false;
  }
}

async function saveTextModel(event) {
  event.preventDefault();
  const button = $('#save-text-model');
  button.disabled = true;
  setTextConfigMessage('正在保存并检测服务…', 'working');
  try {
    const result = await api('/api/ai/settings/text-planner', {
      method: 'POST', body: JSON.stringify(textModelPayload()),
    });
    renderTextModelStatus(result, true);
    setTextConfigMessage(result.enabled ? '配置已保存，文案分镜将使用此模型。' : `配置已保存，但当前不可用：${result.message}`, result.enabled ? 'success' : 'error');
    toast(result.enabled ? '文案分镜模型已启用' : '配置已保存，请检查连接');
  } catch (error) {
    setTextConfigMessage(error.message, 'error');
  } finally {
    button.disabled = false;
  }
}

function setAiCategory(category) {
  $$('.ai-category-tab').forEach(button => {
    const active = button.dataset.aiCategory === category;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
  });
  $$('.ai-category-panel').forEach(panel => panel.classList.toggle('active', panel.id === `ai-${category}-category`));
}

function setView(view) {
  closeDrawer();
  state.view = view;
  $$('.nav-item').forEach(item => item.classList.toggle('active', item.dataset.view === view));
  $$('.view').forEach(item => item.classList.remove('active'));
  $(`#${view}-view`).classList.add('active');
  $('#view-title').textContent = { library: '素材索引', script: '文案分镜', tasks: '任务中心', ai: 'AI 设置' }[view];
  $('.view-intro').textContent = {
    library: '让散落的画面，重新进入创作流程。',
    script: '从一句旁白，到每一个准确的镜头。',
    tasks: '所有后台工作，都在这里有迹可循。',
    ai: '模型状态、启用条件和推理入口。',
  }[view];
  if (view === 'tasks') {
    loadTasks().then(tasks => {
      if (tasks.some(task => ['queued', 'running', 'pausing'].includes(task.status))) pollTasks();
    }).catch(error => toast(error.message));
  }
  if (view === 'script') {
    loadStoryboardReadiness().catch(error => toast(error.message));
  }
  if (view === 'ai') loadAiStatus();
}

function openProjectDialog() {
  $('#project-form').reset();
  $('#form-error').textContent = '';
  $('#project-dialog').showModal();
}

function closeProjectDialog() {
  $('#project-dialog').close();
  $('#form-error').textContent = '';
}

async function saveProject(event) {
  event.preventDefault();
  const form = new FormData($('#project-form'));
  try {
    await api('/api/projects', { method: 'POST', body: JSON.stringify(Object.fromEntries(form)) });
    $('#project-dialog').close();
    toast('项目已创建');
    await loadProjects(true);
  } catch (error) {
    $('#form-error').textContent = error.message;
  }
}

async function startScan() {
  if (!state.projectId) return openProjectDialog();
  const result = await api(`/api/projects/${state.projectId}/scan`, { method: 'POST', body: '{}' });
  await loadStoryboardReadiness();
  toast(result.message);
  setView('tasks');
  pollTasks();
}

async function startThumbnailWarmup() {
  if (!state.projectId) return openProjectDialog();
  const result = await api(`/api/projects/${state.projectId}/thumbnails`, { method: 'POST', body: '{}' });
  toast(result.message);
  setView('tasks');
  pollTasks();
}

function openAnalysisDialog(scope = 'pending') {
  if (!state.projectId) return openProjectDialog();
  if (!analysisPrerequisitesReady()) {
    if (state.storyboardReadiness?.active_tasks?.analysis?.length) {
      toast('JoyAI 分析任务尚未完成');
      return setView('tasks');
    }
    toast(state.storyboardReadiness?.message || '请先完成素材扫描');
    return;
  }
  $('#analysis-form').reset();
  const scopeField = $('#analysis-form').elements.scope;
  if (['pending', 'failed', 'all'].includes(scope)) scopeField.value = scope;
  $('#analysis-form-error').textContent = '';
  $('#analysis-dialog').showModal();
}

function closeAnalysisDialog() {
  $('#analysis-dialog').close();
  $('#analysis-form-error').textContent = '';
}

async function startAnalysis(event) {
  event.preventDefault();
  if (!state.projectId) return openProjectDialog();
  const values = Object.fromEntries(new FormData($('#analysis-form')));
  const payload = {
    scope: values.scope,
    media_type: values.media_type,
    folder_prefix: values.folder_prefix.trim(),
    limit: Number(values.limit || 0),
  };
  if (values.min_duration !== '') payload.min_duration = Number(values.min_duration);
  if (values.max_duration !== '') payload.max_duration = Number(values.max_duration);
  try {
    const result = await api(`/api/projects/${state.projectId}/analysis`, {
      method: 'POST', body: JSON.stringify(payload),
    });
    $('#analysis-dialog').close();
    await loadStoryboardReadiness();
    toast(result.message);
    setView('tasks');
    pollTasks();
  } catch (error) {
    $('#analysis-form-error').textContent = error.message;
  }
}

async function pollTasks() {
  clearInterval(pollTasks.timer);
  pollTasks.timer = setInterval(async () => {
    const tasks = await api('/api/tasks');
    if (state.view === 'tasks') await loadTasks(tasks);
    const running = tasks.some(task => ['queued', 'running', 'pausing'].includes(task.status));
    if (!running) {
      clearInterval(pollTasks.timer);
      await loadProjects();
    }
  }, 1200);
}

function setSaveState(message, kind = '') {
  const element = $('#storyboard-save-state');
  element.textContent = message;
  element.dataset.state = kind;
}

function clearStoryboardWorkspace() {
  state.storyboards = [];
  state.storyboardId = null;
  state.storyboard = null;
  $('#storyboard-select').innerHTML = '<option value="">尚未创建</option>';
  $('#storyboard-title').value = '';
  $('#script-input').value = '';
  renderStoryboardVersions(null);
  $('#shot-list').innerHTML = '<p class="muted-note">请先选择一个素材项目。</p>';
  setSaveState('选择项目后可创建分镜册');
  renderStoryboardGate();
}

async function loadStoryboards(selectId = null) {
  if (!state.projectId) return clearStoryboardWorkspace();
  state.storyboards = await api(`/api/projects/${state.projectId}/storyboards`);
  const select = $('#storyboard-select');
  select.innerHTML = state.storyboards.length
    ? state.storyboards.map(board => `<option value="${board.id}">${escapeHtml(board.title)} · ${board.shot_count} 镜</option>`).join('')
    : '<option value="">尚未创建</option>';
  const available = new Set(state.storyboards.map(board => board.id));
  const target = selectId || (available.has(state.storyboardId) ? state.storyboardId : state.storyboards[0]?.id);
  if (!target) {
    state.storyboardId = null;
    state.storyboard = null;
    $('#storyboard-title').value = '';
    $('#script-input').value = '';
    renderStoryboardVersions(null);
    $('#shot-list').innerHTML = '<p class="muted-note">新建分镜册，直接粘贴或导入文案。</p>';
    setSaveState('粘贴内容会自动保存');
    renderStoryboardGate();
    return;
  }
  select.value = String(target);
  await loadStoryboard(target);
}

async function loadStoryboard(id) {
  const board = await api(`/api/storyboards/${id}`);
  state.storyboardId = board.id;
  state.storyboard = board;
  $('#storyboard-select').value = String(board.id);
  $('#storyboard-title').value = board.title || '';
  $('#script-input').value = board.source_text || '';
  renderStoryboardVersions(board);
  renderShotList(board.shots || []);
  setSaveState(`已保存 · ${formatUtcTime(board.updated_at)}`, 'saved');
  renderStoryboardGate();
}

function renderStoryboardVersions(board) {
  const select = $('#storyboard-version-select');
  const meta = $('#storyboard-version-meta');
  const versions = board?.versions || [];
  if (!versions.length) {
    select.innerHTML = '<option value="">尚无版本</option>';
    select.disabled = true;
    meta.textContent = '每次成功生成都会保留';
    applyStoryboardControls();
    return;
  }
  select.disabled = false;
  select.innerHTML = versions.map(version => `
    <option value="${version.id}">V${String(version.version_number).padStart(2, '0')} · ${escapeHtml(version.planner || '未知模型')} · ${escapeHtml(formatUtcTime(version.created_at))}</option>
  `).join('');
  select.value = String(board.active_version_id || versions[0].id);
  const active = versions.find(version => version.id === Number(select.value));
  meta.textContent = active
    ? `当前 V${String(active.version_number).padStart(2, '0')} · ${active.warning || '生成结果已保存'}`
    : `${versions.length} 个历史版本`;
  applyStoryboardControls();
}

async function activateStoryboardVersion(versionId) {
  if (!state.storyboardId || !versionId) return;
  if (!storyboardActionReady()) return renderStoryboardVersions(state.storyboard);
  const select = $('#storyboard-version-select');
  select.disabled = true;
  try {
    const board = await api(`/api/storyboards/${state.storyboardId}/versions/${versionId}/activate`, {
      method: 'POST', body: '{}',
    });
    state.storyboard = board;
    renderStoryboardVersions(board);
    renderShotList(board.shots || []);
    setSaveState(`已切换到 V${String(board.versions.find(item => item.id === Number(versionId))?.version_number || '').padStart(2, '0')}`, 'saved');
    toast('已切换分镜版本');
  } catch (error) {
    toast(error.message);
    if (state.storyboard) renderStoryboardVersions(state.storyboard);
  }
}

async function createStoryboard() {
  if (!state.projectId) return openProjectDialog();
  if (!storyboardActionReady()) return null;
  const board = await api(`/api/projects/${state.projectId}/storyboards`, {
    method: 'POST', body: JSON.stringify({ title: '未命名分镜', source_text: '' }),
  });
  await loadStoryboards(board.id);
  $('#storyboard-title').select();
  toast('已新建分镜册');
  return board;
}

function openDeleteStoryboardDialog() {
  const board = state.storyboard;
  if (!board || !state.storyboardId) return toast('请先选择一本分镜册');
  const dialog = $('#delete-storyboard-dialog');
  dialog.dataset.storyboardId = String(board.id);
  dialog.dataset.storyboardTitle = board.title || '未命名分镜';
  $('#delete-storyboard-title').textContent = board.title || '未命名分镜';
  $('#delete-storyboard-shot-count').textContent = String((board.shots || []).length);
  $('#delete-storyboard-version-count').textContent = String((board.versions || []).length);
  $('#delete-storyboard-summary').textContent = `“${board.title || '未命名分镜'}”及其中的分镜、生成版本和素材选择记录将永久删除。`;
  $('#delete-storyboard-error').textContent = '';
  const confirmButton = $('#confirm-delete-storyboard');
  confirmButton.disabled = false;
  confirmButton.textContent = '永久删除';
  dialog.showModal();
}

function closeDeleteStoryboardDialog() {
  const dialog = $('#delete-storyboard-dialog');
  if (dialog.open) dialog.close();
}

async function deleteSelectedStoryboard(event) {
  event.preventDefault();
  const dialog = $('#delete-storyboard-dialog');
  const storyboardId = Number(dialog.dataset.storyboardId);
  const storyboardTitle = dialog.dataset.storyboardTitle || '';
  if (!storyboardId) return closeDeleteStoryboardDialog();

  const currentIndex = state.storyboards.findIndex(item => item.id === storyboardId);
  const fallbackId = state.storyboards[currentIndex + 1]?.id
    || state.storyboards[currentIndex - 1]?.id
    || null;
  const confirmButton = $('#confirm-delete-storyboard');
  clearTimeout(state.storyboardSaveTimer);
  confirmButton.disabled = true;
  confirmButton.textContent = '正在删除…';
  $('#delete-storyboard-error').textContent = '';
  try {
    const result = await api(`/api/storyboards/${storyboardId}`, {
      method: 'DELETE',
      body: JSON.stringify({ confirm_title: storyboardTitle }),
    });
    closeDeleteStoryboardDialog();
    if (state.storyboardId === storyboardId) {
      state.storyboardId = null;
      state.storyboard = null;
    }
    await loadStoryboards(fallbackId);
    toast(`已删除“${result.title}”`);
  } catch (error) {
    $('#delete-storyboard-error').textContent = error.message;
    confirmButton.disabled = false;
    confirmButton.textContent = '永久删除';
  }
}

async function ensureStoryboard() {
  if (state.storyboardId) return state.storyboard;
  if (!state.projectId) return openProjectDialog();
  if (!storyboardActionReady()) return null;
  const board = await api(`/api/projects/${state.projectId}/storyboards`, {
    method: 'POST',
    body: JSON.stringify({
      title: $('#storyboard-title').value.trim() || '未命名分镜',
      source_text: $('#script-input').value,
    }),
  });
  state.storyboardId = board.id;
  state.storyboard = board;
  await loadStoryboards(board.id);
  return board;
}

async function saveStoryboard() {
  clearTimeout(state.storyboardSaveTimer);
  if (!storyboardActionReady()) return null;
  await ensureStoryboard();
  if (!state.storyboardId) return null;
  setSaveState('正在保存…', 'saving');
  try {
    const board = await api(`/api/storyboards/${state.storyboardId}`, {
      method: 'POST',
      body: JSON.stringify({
        title: $('#storyboard-title').value.trim() || '未命名分镜',
        source_text: $('#script-input').value,
      }),
    });
    state.storyboard = board;
    const listItem = state.storyboards.find(item => item.id === board.id);
    if (listItem) listItem.title = board.title;
    const option = $(`#storyboard-select option[value="${board.id}"]`);
    if (option) option.textContent = `${board.title} · ${(board.shots || []).length} 镜`;
    setSaveState(`已保存 · ${formatUtcTime(board.updated_at)}`, 'saved');
    return board;
  } catch (error) {
    if (error.code === 'storyboard_not_ready') renderStoryboardGate();
    else setSaveState(`保存失败：${error.message}`, 'error');
    throw error;
  }
}

function scheduleStoryboardSave() {
  if (!storyboardActionReady()) return;
  setSaveState('有未保存修改', 'dirty');
  clearTimeout(state.storyboardSaveTimer);
  state.storyboardSaveTimer = setTimeout(() => saveStoryboard().catch(error => toast(error.message)), 550);
}

function renderCandidate(candidate, shot) {
  const selected = shot.selected_asset_id === candidate.asset_id;
  return `<article class="shot-candidate ${selected ? 'selected' : ''}">
    <button class="candidate-preview" data-preview-asset="${candidate.asset_id}" title="预览素材">
      <img src="/api/assets/${candidate.asset_id}/thumbnail" alt="" loading="lazy">
      <span>${candidate.media_type === 'video' ? 'VIDEO' : 'STILL'}</span>
    </button>
    <div class="candidate-info">
      <strong title="${escapeHtml(candidate.filename)}">${escapeHtml(candidate.filename)}</strong>
      <small>${escapeHtml((candidate.match_reasons || []).join(' / ') || '候选素材')}</small>
      <p>${escapeHtml(candidate.summary || '尚无画面摘要')}</p>
      <div class="candidate-actions">
        <button data-select-shot="${shot.id}" data-asset-id="${candidate.asset_id}" ${storyboardAuthoringReady() ? '' : 'disabled'}>${selected ? '已选用' : '选用'}</button>
        <button data-copy-smb="${escapeHtml(candidate.smb_path)}">复制 Windows 路径</button>
      </div>
    </div>
  </article>`;
}

function renderShotList(shots = []) {
  if (!shots.length) {
    $('#shot-list').innerHTML = '<p class="muted-note">文案已保存。点击“生成并匹配素材”创建分镜清单。</p>';
    applyStoryboardControls();
    return;
  }
  $('#shot-list').innerHTML = shots.map(shot => `
    <article class="shot-row">
      <div class="shot-number">${String(shot.position).padStart(2, '0')}</div>
      <div class="shot-copy">
        <strong>${escapeHtml(shot.narration)}</strong>
        <small>${escapeHtml(shot.visual_request || '待补充画面需求')} · ${escapeHtml(shot.shot_type || '待确认')}</small>
        ${(shot.keywords || []).length ? `<div class="shot-keywords">${shot.keywords.map(word => `<span>${escapeHtml(word)}</span>`).join('')}</div>` : ''}
      </div>
      <div class="shot-match">
        ${(shot.candidates || []).length
          ? shot.candidates.map(candidate => renderCandidate(candidate, shot)).join('')
          : '<p class="no-candidate">当前已分析素材中暂无文字命中的候选</p>'}
      </div>
    </article>`).join('');
  applyStoryboardControls();
}

async function generateStoryboard() {
  if (!storyboardActionReady()) return;
  if (!$('#script-input').value.trim()) return toast('请先粘贴或导入文案');
  const button = $('#split-script');
  button.disabled = true;
  button.textContent = '正在生成…';
  try {
    await saveStoryboard();
    const board = await api(`/api/storyboards/${state.storyboardId}/generate`, { method: 'POST', body: '{}' });
    state.storyboard = board;
    renderShotList(board.shots || []);
    await loadStoryboards(board.id);
    toast(board.planner_warning || `已由 ${board.planner} 生成并匹配`);
  } catch (error) {
    toast(error.message);
  } finally {
    button.textContent = '生成并匹配素材';
    renderStoryboardGate();
  }
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1]);
    reader.onerror = () => reject(new Error('文件读取失败'));
    reader.readAsDataURL(file);
  });
}

async function importStoryboardFile(file, mode) {
  if (!file) return;
  if (!state.projectId) return openProjectDialog();
  if (!storyboardActionReady()) return;
  if (file.size > 25 * 1024 * 1024) return toast('导入文件不能超过 25MB');
  try {
    const contentBase64 = await fileToBase64(file);
    const board = await api(`/api/projects/${state.projectId}/storyboards/import`, {
      method: 'POST',
      body: JSON.stringify({ filename: file.name, content_base64: contentBase64, mode }),
    });
    await loadStoryboards(board.id);
    toast(mode === 'shots' ? '分镜清单已导入' : '文案已导入');
  } catch (error) {
    toast(error.message);
  }
}

async function selectShotCandidate(shotId, assetId) {
  if (!storyboardActionReady()) return;
  const board = await api(`/api/storyboards/${state.storyboardId}/shots/${shotId}/select`, {
    method: 'POST', body: JSON.stringify({ asset_id: assetId }),
  });
  state.storyboard = board;
  renderShotList(board.shots || []);
  toast('已保存素材选择');
}

function selectedCandidate(shot) {
  return shot.selected_candidate || (shot.candidates || [])[0] || null;
}

function previewStoryboard() {
  const board = state.storyboard;
  if (!board) return toast('请先新建或选择分镜册');
  $('#preview-title').textContent = board.title;
  $('#preview-content').innerHTML = (board.shots || []).length ? board.shots.map(shot => {
    const candidate = selectedCandidate(shot);
    return `<article class="preview-shot">
      <div class="preview-shot-number">${String(shot.position).padStart(2, '0')}</div>
      <div class="preview-shot-copy"><strong>${escapeHtml(shot.narration)}</strong><p>${escapeHtml(shot.visual_request || '')}</p></div>
      <div class="preview-shot-media">${candidate ? `
        <button data-preview-asset="${candidate.asset_id}" aria-label="预览素材 ${escapeHtml(candidate.filename)}"><img src="/api/assets/${candidate.asset_id}/thumbnail" alt=""></button>
        <div><strong>${escapeHtml(candidate.filename)}</strong><small>${escapeHtml(candidate.smb_path)}</small></div>`
        : '<span>尚未匹配素材</span>'}</div>
    </article>`;
  }).join('') : '<p class="muted-note">还没有生成分镜清单。</p>';
  $('#storyboard-preview').showModal();
}

function exportStoryboard(format) {
  if (!state.storyboardId) return toast('请先新建或选择分镜册');
  const link = document.createElement('a');
  link.href = `/api/storyboards/${state.storyboardId}/export?format=${format}`;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function bindEvents() {
  $$('.nav-item').forEach(item => item.addEventListener('click', () => setView(item.dataset.view)));
  $('#add-project').addEventListener('click', openProjectDialog);
  $('#empty-add-project').addEventListener('click', openProjectDialog);
  $('#project-form').addEventListener('submit', saveProject);
  $('#cancel-project').addEventListener('click', closeProjectDialog);
  $('#scan-project').addEventListener('click', startScan);
  $('#thumbnail-project').addEventListener('click', startThumbnailWarmup);
  $('#analyze-project').addEventListener('click', () => openAnalysisDialog());
  $('#analysis-form').addEventListener('submit', startAnalysis);
  $('#cancel-analysis').addEventListener('click', closeAnalysisDialog);
  $('#storyboard-gate-primary').addEventListener('click', handleStoryboardGateAction);
  $('#storyboard-gate-exceptions').addEventListener('click', showFailedAssets);
  $('#storyboard-gate-tasks').addEventListener('click', () => setView('tasks'));
  $('#asset-load-more').addEventListener('click', () => loadAssets(false));
  $('#project-select').addEventListener('change', async event => {
    state.projectId = Number(event.target.value) || null;
    state.storyboardId = null;
    state.storyboard = null;
    state.storyboardReadiness = null;
    state.filter = '';
    state.analysisFilter = '';
    $('#asset-search').value = '';
    syncAssetFilterButtons();
    renderStoryboardGate();
    await refreshCurrentProject();
  });
  $$('.filter').forEach(button => button.addEventListener('click', async () => {
    $$('.filter').forEach(item => item.classList.remove('active'));
    button.classList.add('active');
    state.filter = button.dataset.type;
    if (state.projectId) await loadAssets();
  }));
  $$('.analysis-filter').forEach(button => button.addEventListener('click', async () => {
    $$('.analysis-filter').forEach(item => item.classList.remove('active'));
    button.classList.add('active');
    state.analysisFilter = button.dataset.analysis;
    if (state.projectId) await loadAssets();
  }));
  let searchTimer;
  $('#asset-search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => state.projectId && loadAssets(), 280);
  });
  $('#drawer-close').addEventListener('click', closeDrawer);
  $('#cancel-asset-exemption').addEventListener('click', closeAssetExemptionDialog);
  $('#asset-exemption-form').addEventListener('submit', submitAssetExemption);
  $('#new-storyboard').addEventListener('click', createStoryboard);
  $('#delete-storyboard').addEventListener('click', openDeleteStoryboardDialog);
  $('#cancel-delete-storyboard').addEventListener('click', closeDeleteStoryboardDialog);
  $('#delete-storyboard-form').addEventListener('submit', deleteSelectedStoryboard);
  $('#storyboard-select').addEventListener('change', event => loadStoryboard(Number(event.target.value)));
  $('#storyboard-version-select').addEventListener('change', event => activateStoryboardVersion(Number(event.target.value)));
  $('#storyboard-title').addEventListener('input', scheduleStoryboardSave);
  $('#script-input').addEventListener('input', scheduleStoryboardSave);
  $('#split-script').addEventListener('click', generateStoryboard);
  $('#import-script').addEventListener('click', () => $('#script-file').click());
  $('#import-shots').addEventListener('click', () => $('#shots-file').click());
  $('#script-file').addEventListener('change', async event => {
    await importStoryboardFile(event.target.files[0], 'script');
    event.target.value = '';
  });
  $('#shots-file').addEventListener('change', async event => {
    await importStoryboardFile(event.target.files[0], 'shots');
    event.target.value = '';
  });
  $('#preview-storyboard').addEventListener('click', previewStoryboard);
  $('#close-storyboard-preview').addEventListener('click', () => $('#storyboard-preview').close());
  $$('.export-storyboard').forEach(button => button.addEventListener('click', () => exportStoryboard(button.dataset.format)));
  $('#shot-list').addEventListener('click', async event => {
    const preview = event.target.closest('[data-preview-asset]');
    const select = event.target.closest('[data-select-shot]');
    const copy = event.target.closest('[data-copy-smb]');
    if (preview) return openAsset(Number(preview.dataset.previewAsset));
    if (select) return selectShotCandidate(Number(select.dataset.selectShot), Number(select.dataset.assetId));
    if (copy) {
      try {
        await copyText(copy.dataset.copySmb);
        return toast('Windows 路径已复制');
      } catch (error) {
        return toast(error.message);
      }
    }
  });
  $('#preview-content').addEventListener('click', event => {
    const preview = event.target.closest('[data-preview-asset]');
    if (preview) {
      $('#storyboard-preview').close();
      openAsset(Number(preview.dataset.previewAsset));
    }
  });
  $('#check-ai').addEventListener('click', () => loadAiStatus(true));
  $$('.ai-category-tab').forEach(button => button.addEventListener('click', () => setAiCategory(button.dataset.aiCategory)));
  $('#text-provider').addEventListener('change', () => configureTextProviderFields(true));
  $('#test-text-model').addEventListener('click', testTextModel);
  $('#text-model-form').addEventListener('submit', saveTextModel);
  document.addEventListener('keydown', event => { if (event.key === 'Escape') closeDrawer(); });
}

async function init() {
  bindEvents();
  try {
    await api('/api/health');
    const aiStatusPromise = loadAiStatus().catch(error => {
      state.ai = { enabled: false };
      renderAiMetricNote();
      updateProjectActionState();
      renderStoryboardGate();
      toast(`AI 状态暂不可用：${error.message}`);
    });
    const [tasks] = await Promise.all([api('/api/tasks'), loadProjects()]);
    if (tasks.some(task => ['queued', 'running', 'pausing'].includes(task.status))) pollTasks();
    await aiStatusPromise;
  } catch (error) {
    toast(error.message);
  }
}

document.addEventListener('DOMContentLoaded', init);
