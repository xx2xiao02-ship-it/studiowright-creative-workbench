"use strict";

let apiKeyRead = 0, apiKeySignature = null, apiKeyOperations = 0;
async function loadApiKeys({background = false} = {}) {
  if (background && (apiKeyOperations || $('apiKeyDialog').open)) return;
  const read = ++apiKeyRead;
  const status = $('apiKeyStatus');
  try {
    const data = await getJson('/api/access-keys', {timeoutMs:8000});
    if (!Array.isArray(data.items)) throw new Error('unavailable');
    if (read !== apiKeyRead || (background && (apiKeyOperations || $('apiKeyDialog').open))) return;
    const signature = JSON.stringify(data.items.map(({id, name, suffix, copyable, created}) => [id, name, suffix, copyable, created]));
    if (background && apiKeySignature === signature) return;
    apiKeySignature = signature;
    const list = $('apiKeyList'), focused = document.activeElement?.closest('#apiKeyList button')?.dataset;
    list.replaceChildren();
    for (const key of data.items) {
      if (!/^[0-9a-f]{32}$/.test(key.id)) continue;
      const row = document.createElement('div'); row.className = 'api-key-row';
      const detail = document.createElement('div');
      const title = document.createElement('strong'); title.textContent = key.name;
      const meta = document.createElement('small');
      meta.textContent = '•••• ' + key.suffix + ' · 使用中 · 创建于 ' + new Date(key.created * 1000).toLocaleString('zh-CN');
      detail.append(title, meta); row.appendChild(detail);
      const actions = document.createElement('div'); actions.className = 'api-key-actions';
      const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'api-key-copy';
      copy.textContent = '复制'; copy.dataset.keyId = key.id; copy.dataset.keyAction = 'copy';
      copy.setAttribute('aria-label', '复制密钥 ' + key.name);
      if (!key.copyable) { copy.disabled = true; copy.title = '旧密钥未保存原文，无法复制，请新建密钥。'; }
      const revoke = document.createElement('button'); revoke.type = 'button'; revoke.className = 'api-key-revoke';
      revoke.textContent = '撤销'; revoke.dataset.keyId = key.id; revoke.dataset.keyName = key.name; revoke.dataset.keyAction = 'revoke';
      revoke.setAttribute('aria-label', '撤销密钥 ' + key.name);
      actions.append(copy, revoke); row.appendChild(actions);
      list.appendChild(row);
      if (focused?.keyId === key.id) (focused.keyAction === 'copy' ? copy : revoke).focus({preventScroll:true});
    }
    const count = $('apiKeyCount');
    if (count) count.textContent = `${list.childElementCount} 把密钥`;
    status.textContent = data.items.length ? '新建密钥可重复复制；撤销后将删除，已提交的请求可能继续执行。' : '还没有密钥，创建后即可接入。';
  } catch (_) {
    if (read !== apiKeyRead || (background && apiKeySignature !== null)) return;
    $('apiKeyList').replaceChildren();
    const count = $('apiKeyCount');
    if (count) count.textContent = '列表暂不可读取';
    status.textContent = '密钥管理暂不可用，请确认算力中心已连接。';
  }
}

function completeApiDocument() {
  const root = $('apiView');
  const clean = (node) => (node?.textContent || '').replace(/\s+/g, ' ').trim();
  const fence = String.fromCharCode(96).repeat(3);
  const out = ['# 算力中心 API 调用说明', clean(root.querySelector('.api-intro p')),
    '算力中心地址：' + clean(root.querySelector('.api-base code'))];
  for (const panel of root.querySelectorAll('.api-main > section.api-panel')) {
    for (const child of panel.children) {
      if (child.classList.contains('api-heading')) {
        out.push('## ' + clean(child.querySelector('h2')));
        const note = clean(child.querySelector('p')); if (note) out.push(note);
      } else if (child.classList.contains('api-callout')) {
        out.push('**' + clean(child.querySelector('strong')) + '**');
        child.querySelectorAll('p').forEach(p => out.push(clean(p)));
      } else if (child.classList.contains('api-endpoint')) {
        out.push('**' + clean(child.querySelector('b')) + ' ' + clean(child.querySelector('code')) + '** · ' + clean(child.querySelector('span')));
      } else if (child.tagName === 'H3') {
        out.push('### ' + clean(child));
      } else if (child.tagName === 'P') {
        out.push(clean(child));
      } else if (child.classList.contains('api-code-head')) {
        out.push('**' + clean(child.querySelector('span')) + '**');
      } else if (child.tagName === 'PRE') {
        out.push(fence + 'python\n' + child.querySelector('code').textContent.trim() + '\n' + fence);
      } else if (child.classList.contains('api-params')) {
        out.push([...child.children].map(row => '- **' + clean(row.querySelector('strong')) + '**：' + clean(row.querySelector('span'))).join('\n'));
      } else if (child.classList.contains('api-table-wrap')) {
        const rows = [...child.querySelectorAll('tr')].map(row => [...row.querySelectorAll('th,td')].map(cell => clean(cell).replace(/\|/g, '\\|')));
        if (rows.length) out.push(['| ' + rows[0].join(' | ') + ' |', '| ' + rows[0].map(() => '---').join(' | ') + ' |',
          ...rows.slice(1).map(row => '| ' + row.join(' | ') + ' |')].join('\n'));
      } else if (child.classList.contains('api-links')) {
        out.push([...child.querySelectorAll('a')].map(a => '- [' + clean(a.querySelector('strong')) + '](' + a.getAttribute('href') + ')：' + clean(a.querySelector('small'))).join('\n'));
      } else if (child.classList.contains('api-legacy')) {
        const link = child.querySelector('a');
        out.push('### ' + clean(child.querySelector('summary')), clean(child.querySelector('p')),
          '- [' + clean(link) + '](' + link.getAttribute('href') + ')');
      }
    }
  }
  return out.filter(Boolean).join('\n\n') + '\n';
}

let apiCopyInvoker;
async function copyCompleteApiDocument(status = $('apiCopyAllStatus'), invoker = $('apiCopyAll')) {
  const content = completeApiDocument();
  apiCopyInvoker = invoker;
  let copied = false;
  try {
    if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(content); copied = true; }
  } catch (_) { /* Try the local fallback below. */ }
  if (!copied) {
    const scratch = document.createElement('textarea');
    scratch.className = 'api-copy-scratch'; scratch.value = content;
    document.body.appendChild(scratch); scratch.focus(); scratch.select();
    try { copied = document.execCommand('copy'); } catch (_) { copied = false; }
    scratch.remove();
  }
  if (copied) {
    status.textContent = '完整文档已复制，可以直接粘贴。';
    invoker.focus({preventScroll: true});
  } else {
    const dialog = $('apiCopyDialog'); const fallback = $('apiCopyFallback');
    fallback.value = content; dialog.showModal(); fallback.focus(); fallback.select();
    status.textContent = '请在弹窗中复制完整文档。';
  }
}

function initApiDocumentation() {
  apiCopyInvoker = $('apiCopyAll');
  document.body.appendChild($('apiCopyDialog'));
  $('apiKeyForm').addEventListener('submit', async event => {
    event.preventDefault();
    const button = $('apiKeyCreate'); if (button.disabled) return;
    ++apiKeyOperations; ++apiKeyRead;
    button.disabled = true; $('apiKeyStatus').textContent = '正在创建密钥…';
    try {
      const response = await fetch('/api/access-keys', {method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-Workbench-Request':'1'}, body:JSON.stringify({name:$('apiKeyName').value.trim()})});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '创建失败。');
      if (!/^swu_[A-Za-z0-9_-]{43}$/.test(data.key)) throw new Error('密钥返回格式不正确。');
      $('apiKeyName').value = ''; $('apiKeySecret').value = data.key; $('apiKeyCopyStatus').textContent = '';
      $('apiKeyDialogHeading').textContent = '密钥创建成功'; $('apiKeyDialogHelp').textContent = '密钥已加密保存，以后可在列表中再次复制。';
      $('apiKeyDialog').showModal(); $('apiKeyCopy').focus();
      await loadApiKeys();
    } catch (error) {
      $('apiKeyStatus').textContent = '创建未确认，请先刷新页面核对密钥列表。' + (error.message || '');
    } finally { --apiKeyOperations; button.disabled = false; }
  });
  $('apiKeyList').addEventListener('click', async event => {
    const button = event.target.closest('button[data-key-id][data-key-action]'); if (!button || button.disabled) return;
    const action = button.dataset.keyAction;
    if (action === 'revoke' && !window.confirm('撤销并删除后，“' + button.dataset.keyName + '”的新请求会被拒绝，已提交的请求可能继续执行。确定撤销？')) return;
    if (!['copy', 'revoke'].includes(action)) return;
    ++apiKeyOperations; ++apiKeyRead;
    button.disabled = true;
    try {
      const response = await fetch('/api/access-keys/' + button.dataset.keyId + '/' + action, {method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-Workbench-Request':'1'}, body:'{}'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '操作未完成。');
      if (action === 'copy') {
        if (!/^swu_[A-Za-z0-9_-]{43}$/.test(data.key)) throw new Error('密钥不可复制。');
        try {
          if (!navigator.clipboard?.writeText) throw new Error('unavailable');
          await navigator.clipboard.writeText(data.key);
          $('apiKeyStatus').textContent = '密钥已复制，请妥善保管。';
        } catch (_) {
          $('apiKeyDialogHeading').textContent = '复制密钥'; $('apiKeyDialogHelp').textContent = '自动复制不可用，密钥已选中，请按 Ctrl+C。';
          $('apiKeySecret').value = data.key; $('apiKeyCopyStatus').textContent = '';
          $('apiKeyDialog').showModal(); $('apiKeySecret').focus(); $('apiKeySecret').select();
        }
        button.disabled = false;
        return;
      }
      if (!data.revoked) throw new Error('撤销结果未确认，请刷新页面核对。');
      await loadApiKeys();
    } catch (error) { $('apiKeyStatus').textContent = error.message || '操作未完成，请刷新页面核对。'; button.disabled = false; }
    finally { --apiKeyOperations; }
  });
  $('apiKeyCopy').addEventListener('click', async () => {
    const input = $('apiKeySecret');
    try {
      if (!navigator.clipboard?.writeText) throw new Error('unavailable');
      await navigator.clipboard.writeText(input.value);
      $('apiKeyCopyStatus').textContent = '密钥已复制，请妥善保存。';
    } catch (_) {
      input.focus(); input.select(); $('apiKeyCopyStatus').textContent = '密钥已选中，请按 Ctrl+C 复制。';
    }
  });
  $('apiKeyDone').addEventListener('click', () => $('apiKeyDialog').close());
  $('apiKeyDialog').addEventListener('close', () => { $('apiKeySecret').value = ''; $('apiKeyCreate').focus(); });
  $('apiCopyAll').addEventListener('click', () => copyCompleteApiDocument());
  $('overviewApiCopy').addEventListener('click', () => copyCompleteApiDocument($('overviewCopyStatus'), $('overviewApiCopy')));
  $('apiCopyClose').addEventListener('click', () => $('apiCopyDialog').close());
  $('apiCopyDialog').addEventListener('close', () => apiCopyInvoker.focus({preventScroll: true}));
}
