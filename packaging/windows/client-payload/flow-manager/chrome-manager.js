'use strict';

const $ = id => document.getElementById(id);
const selected = new Set();
const actions = ['open', 'install', 'check', 'sync'];
const instructions = {
  open: '请在对应窗口确认 Flow 登录。',
  install: '请按安装向导在各 Chrome 用户中确认加载或重载。',
  check: '连接检查结果将显示在「插件状态」，此操作不会同步登录信息。',
  sync: '同步结果将逐个更新，请以 Ubuntu 登录核对结果为准。',
};
const syncLabels = {
  waiting: ['等待连接', 'warn'],
  running: ['同步中', 'warn'],
  succeeded: ['同步完成', 'success'],
  failed: ['同步失败', 'error'],
};
const bridgeRestrictions = new Set([
  '未能确认后台版本，请检查连接后再操作。',
  '现有后台需要原版插件；请先使用后台远程登录。',
  '当前账号助手与后台版本不匹配，请核对安装包。',
]);
let profiles = [];
let visibleProfiles = [];
let ubuntuProfiles = null;
let rosterStatus = 'loading';
let restriction = '';
let installReady = false;
let canCheck = false;
let busy = false;
let activeRefresh = null;
let rosterRefresh = null;
let fingerprint = null;
let loadError = '';

const esc = value => String(value ?? '').replace(/[&<>"']/g, character =>
  ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'})[character]);

function showStatus(message, tone = '') {
  $('status').textContent = message;
  $('status').dataset.tone = tone;
}

async function request(path, body) {
  const response = await fetch(path, body ? {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Console-Request': '1'},
    body: JSON.stringify(body),
  } : {});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '本机助手暂时无法完成操作。');
  return data;
}

function pendingInstalls() {
  return ubuntuProfiles ? profiles.filter(p => ubuntuProfiles.get(p.profile) === p.email && p.plugin !== 'registered').map(p => p.profile) : [];
}

function renderSelection() {
  const allVisible = visibleProfiles.length > 0 && visibleProfiles.every(item => selected.has(item.profile));
  const hidden = selected.size - visibleProfiles.filter(item => selected.has(item.profile)).length;
  $('selected-count').textContent = selected.size;
  $('hidden-selection').hidden = !hidden;
  $('hidden-selection').textContent = `其中 ${hidden} 个已选账号不在筛选结果中`;
  $('all').disabled = busy || !visibleProfiles.length;
  const filtered = Boolean($('search').value.trim());
  $('all').textContent = allVisible ? (filtered ? '取消选择筛选结果' : '取消全选') : (filtered ? '选中筛选结果' : '全选');
  $('clear').hidden = !selected.size;
  $('clear').disabled = busy;
  for (const action of actions) {
    $(action).disabled = busy || !selected.size || (action === 'sync' && Boolean(restriction))
      || (action === 'install' && !installReady) || (action === 'check' && !canCheck);
  }
  const pending = pendingInstalls().length;
  $('install-missing').disabled = busy || !pending || !installReady;
  $('install-missing').textContent = pending ? `打开 ${pending} 个待安装账号的扩展页` : '没有待安装的 Ubuntu 账号';
  for (const box of $('rows').querySelectorAll('[data-profile]')) {
    box.disabled = busy;
    box.checked = selected.has(box.dataset.profile);
    box.closest('tr').classList.toggle('is-selected', box.checked);
  }
}

function renderAccounts() {
  const query = $('search').value.trim().toLowerCase();
  const registered = ubuntuProfiles ? profiles.filter(p => ubuntuProfiles.get(p.profile) === p.email) : [];
  const unmatched = ubuntuProfiles ? profiles.length - registered.length : 0;
  const restrictedList = ubuntuProfiles !== null && !$('show-all-profiles').checked;
  const scoped = rosterStatus === 'loading' ? []
    : restrictedList ? registered : profiles;
  const allowed = new Set(scoped.map(p => p.profile));
  for (const profile of selected) if (!allowed.has(profile)) selected.delete(profile);
  $('scope-toggle').hidden = !unmatched;
  $('scope-toggle-label').textContent = `显示其他 ${unmatched} 个本机用户`;
  visibleProfiles = scoped.filter(p => [p.name, p.email, p.profile].some(v => String(v || '').toLowerCase().includes(query)));
  const count = restrictedList ? `${scoped.length} 个已接入 Ubuntu 的账号` : `${scoped.length} 个本机账号`;
  const summary = query ? `找到 ${visibleProfiles.length} 个 · ${count}` : count;
  $('list-state').textContent = loadError ? (profiles.length ? '名单刷新失败，正在显示上次结果' : '本机账号读取失败')
    : rosterStatus === 'loading' ? '正在核对 Ubuntu 账号…'
    : rosterStatus === 'unavailable' ? 'Ubuntu 名单暂不可读，显示全部本机用户'
    : rosterStatus === 'stale' ? `${summary} · 使用上次核对名单` : summary;
  const focusedProfile = document.activeElement?.dataset.profile;
  $('rows').innerHTML = visibleProfiles.map(p => {
    const name = String(p.name || p.profile);
    const initial = Array.from(name.trim())[0]?.toUpperCase() || '·';
    const pluginCheck = p.last_plugin_check;
    const [pluginLabel, pluginTone] = p.plugin === 'registered'
      ? ({succeeded:['已安装 · 连接通过', 'success'], failed:['已安装 · 连接检查失败', 'warn'],
          waiting:['已安装 · 正在检查连接', 'warn'], running:['已安装 · 正在检查连接', 'warn']}
          [pluginCheck?.status] || ['已安装', 'success'])
      : ({missing:['未安装', 'warn'], broken:['插件文件缺失', 'error'],
          disabled:['已安装 · 未启用', 'warn']}[p.plugin] || ['状态待核对', '']);
    const [jobText, jobTone] = syncLabels[p.last_job?.status] || ['未发起', ''];
    const pluginNote = p.plugin === 'registered'
      ? pluginCheck?.message || (canCheck ? '连接尚未检查' : '')
      : (p.plugin === 'missing' ? '' : p.plugin_note);
    return `<tr><td><label class="check-target">
      <input type="checkbox" data-profile="${esc(p.profile)}" aria-label="选择 ${esc(name)}（${esc(p.profile)}）">
    </label></td>
    <td><div class="identity"><span class="avatar" aria-hidden="true">${esc(initial)}</span><div>
      <strong>${esc(name)}</strong><small>${esc(p.email || '未登记 Chrome 邮箱')}</small>
      <small class="profile-directory">${esc(p.profile)}</small>
    </div></div></td>
    <td><span class="cell-label">插件状态</span><span class="pill ${pluginTone}">${pluginLabel}</span>
      ${pluginNote ? `<small class="row-detail">${esc(pluginNote)}</small>` : ''}</td>
    <td><span class="cell-label">同步结果</span><span class="pill ${jobTone}">${jobText}</span>
      ${p.last_job?.message ? `<small class="row-detail">${esc(p.last_job.message)}</small>` : ''}</td></tr>`;
  }).join('') || `<tr><td class="empty" colspan="4">${loadError ? '无法读取账号，请点「刷新」重试。'
    : rosterStatus === 'loading' ? '正在核对…'
    : query ? '没有匹配的 Chrome 用户，请试试其他关键词。'
    : restrictedList && unmatched ? '未找到已接入的本机账号，可勾选「显示其他本机用户」。'
    : '未找到本机 Chrome 用户。'}</td></tr>`;
  renderSelection();
  if (focusedProfile && !busy) {
    [...$('rows').querySelectorAll('[data-profile]')].find(box => box.dataset.profile === focusedProfile)?.focus({preventScroll:true});
  }
}

async function readProfiles() {
  try {
    const result = await request('/api/profiles');
    const list = result.profiles;
    if (!Array.isArray(list)) throw new Error('本机账号数据暂不可用。');
    const nextFingerprint = JSON.stringify([list,result.extension,result.extension_folder]);
    $('folder').value = result.extension_folder || '';
    $('copy').disabled = !$('folder').value;
    if (fingerprint === nextFingerprint && !loadError) return true;
    loadError = '';
    fingerprint = nextFingerprint;
    profiles = list;
    const note = list[0]?.plugin_note;
    restriction = result.extension?.note || (bridgeRestrictions.has(note) && list.every(p => p.plugin_note === note) ? note : '');
    installReady = result.extension?.install_ready ?? Boolean(result.extension_folder);
    canCheck = result.extension?.can_check === true;
    $('restriction').textContent = restriction;
    $('restriction').hidden = !restriction;
    const known = new Set(list.map(p => p.profile));
    for (const profile of selected) if (!known.has(profile)) selected.delete(profile);
    renderAccounts();
    return true;
  } catch (error) {
    if (loadError !== error.message) {
      loadError = error.message;
      renderAccounts();
    }
    return false;
  }
}

function refresh() {
  activeRefresh ||= readProfiles().finally(() => { activeRefresh = null; });
  return activeRefresh;
}

function refreshUbuntuProfiles() {
  rosterRefresh ||= (async () => {
    try {
      const result = await request('/api/registered-profiles');
      if (result.available !== true || !Array.isArray(result.profiles)) throw new Error('Ubuntu 名单暂不可读。');
      const next = new Map(result.profiles.map(p => [p.profile, p.email]));
      if (rosterStatus === 'current' && next.size === ubuntuProfiles.size
          && [...next].every(([profile, email]) => ubuntuProfiles.get(profile) === email)) return;
      ubuntuProfiles = next;
      rosterStatus = 'current';
    } catch {
      const status = ubuntuProfiles ? 'stale' : 'unavailable';
      if (rosterStatus === status) return;
      rosterStatus = status;
    }
    renderAccounts();
  })().finally(() => { rosterRefresh = null; });
  return rosterRefresh;
}

async function run(action, targets = [...selected], button = $(action)) {
  if (busy || !targets.length || (action === 'sync' && restriction)
      || (action === 'install' && !installReady) || (action === 'check' && !canCheck)) return;
  busy = true;
  const label = button.textContent;
  button.textContent = '正在打开窗口…';
  renderSelection();
  showStatus('正在打开所选账号的 Chrome 窗口…');
  try {
    const result = await request('/api/open', {action, profiles:targets});
    showStatus(`已打开 ${result.opened.length} 个账号窗口。${instructions[action]}`);
    if (action === 'install') $('plugin-help').open = true;
    await refresh();
  } catch (error) {
    showStatus(error.message, 'error');
    await refresh();
  } finally {
    busy = false;
    button.textContent = label;
    renderSelection();
    if (!button.disabled && document.activeElement === document.body) button.focus({preventScroll:true});
  }
}

$('rows').addEventListener('change', event => {
  const {profile} = event.target.dataset;
  if (!profile || busy) return;
  if (event.target.checked) selected.add(profile);
  else selected.delete(profile);
  renderSelection();
});
$('search').addEventListener('input', () => { if (fingerprint !== null || loadError) renderAccounts(); });
$('show-all-profiles').addEventListener('change', renderAccounts);
$('refresh').onclick = async () => {
  $('refresh').disabled = true;
  try {
    const [success] = await Promise.all([refresh(), refreshUbuntuProfiles()]);
    if (!busy) showStatus(success ? '本机账号列表已刷新。' : loadError, success ? '' : 'error');
  } finally {
    $('refresh').disabled = false;
  }
};
$('all').onclick = () => {
  const allVisible = visibleProfiles.every(p => selected.has(p.profile));
  for (const {profile} of visibleProfiles) {
    if (allVisible) selected.delete(profile);
    else selected.add(profile);
  }
  renderSelection();
};
$('clear').onclick = () => { selected.clear(); renderSelection(); $('all').focus(); };
for (const action of actions) $(action).onclick = () => run(action);
$('install-missing').onclick = () => run('install', pendingInstalls(), $('install-missing'));
$('copy').onclick = async () => {
  try {
    await navigator.clipboard.writeText($('folder').value);
    showStatus('插件文件夹路径已复制。');
  } catch {
    $('folder').focus();
    $('folder').select();
    showStatus('请按 Ctrl+C 复制已选中的文件夹路径。');
  }
};

void Promise.all([refresh(), refreshUbuntuProfiles()]);
setInterval(() => { void refresh(); }, 4000);
setInterval(() => { void refreshUbuntuProfiles(); }, 60000);
