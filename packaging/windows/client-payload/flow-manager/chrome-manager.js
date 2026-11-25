'use strict';

const $ = id => document.getElementById(id);
const selected = new Set();
const actions = ['open', 'install', 'sync'];
const instructions = {
  open: '请在对应窗口确认 Flow 登录。',
  install: '请按「插件安装指引」安装或重新加载插件。',
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
let restriction = '';
let busy = false;
let activeRefresh = null;
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
    $(action).disabled = busy || !selected.size || (action === 'sync' && Boolean(restriction));
  }
  for (const box of $('rows').querySelectorAll('[data-profile]')) {
    box.disabled = busy;
    box.checked = selected.has(box.dataset.profile);
    box.closest('tr').classList.toggle('is-selected', box.checked);
  }
}

function renderAccounts() {
  const query = $('search').value.trim().toLowerCase();
  visibleProfiles = profiles.filter(p => [p.name, p.email, p.profile].some(v => String(v || '').toLowerCase().includes(query)));
  const summary = query ? `找到 ${visibleProfiles.length} 个，共 ${profiles.length} 个本机账号` : `${profiles.length} 个本机账号`;
  $('list-state').textContent = loadError ? (profiles.length ? '名单刷新失败，正在显示上次结果' : '本机账号读取失败') : summary;
  const focusedProfile = document.activeElement?.dataset.profile;
  $('rows').innerHTML = visibleProfiles.map(p => {
    const name = String(p.name || p.profile);
    const initial = Array.from(name.trim())[0]?.toUpperCase() || '·';
    const plugin = p.plugin === 'registered';
    const [jobText, jobTone] = syncLabels[p.last_job?.status] || ['未发起', ''];
    const pluginNote = p.plugin_note === restriction || p.plugin_note === '未找到插件登记' ? ''
      : p.plugin_note === '已登记，运行状态待连接确认' ? '运行状态待确认' : p.plugin_note;
    return `<tr><td><label class="check-target">
      <input type="checkbox" data-profile="${esc(p.profile)}" aria-label="选择 ${esc(name)}（${esc(p.profile)}）">
    </label></td>
    <td><div class="identity"><span class="avatar" aria-hidden="true">${esc(initial)}</span><div>
      <strong>${esc(name)}</strong><small>${esc(p.email || '未登记 Chrome 邮箱')}</small>
      <small class="profile-directory">${esc(p.profile)}</small>
    </div></div></td>
    <td><span class="cell-label">插件状态</span><span class="pill ${plugin ? 'success' : 'warn'}">${plugin ? '已登记' : '未登记'}</span>
      ${pluginNote ? `<small class="row-detail">${esc(pluginNote)}</small>` : ''}</td>
    <td><span class="cell-label">同步结果</span><span class="pill ${jobTone}">${jobText}</span>
      ${p.last_job?.message ? `<small class="row-detail">${esc(p.last_job.message)}</small>` : ''}</td></tr>`;
  }).join('') || `<tr><td class="empty" colspan="4">${loadError ? '无法读取账号，请点「刷新」重试。'
    : query ? '没有匹配的 Chrome 用户，请试试其他关键词。' : '未找到本机 Chrome 用户。'}</td></tr>`;
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
    const nextFingerprint = JSON.stringify(list);
    $('folder').value = result.extension_folder || '';
    $('copy').disabled = !$('folder').value;
    if (fingerprint === nextFingerprint && !loadError) return true;
    loadError = '';
    fingerprint = nextFingerprint;
    profiles = list;
    const note = list[0]?.plugin_note;
    restriction = bridgeRestrictions.has(note) && list.every(p => p.plugin_note === note) ? note : '';
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

async function run(action) {
  if (busy || !selected.size || (action === 'sync' && restriction)) return;
  busy = true;
  const button = $(action);
  const label = button.textContent;
  button.textContent = '正在打开窗口…';
  renderSelection();
  showStatus('正在打开所选账号的 Chrome 窗口…');
  try {
    const result = await request('/api/open', {action, profiles:[...selected]});
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
$('refresh').onclick = async () => {
  $('refresh').disabled = true;
  try {
    const success = await refresh();
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

void refresh();
setInterval(() => { void refresh(); }, 4000);
