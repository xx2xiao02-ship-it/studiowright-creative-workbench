const sync = document.getElementById('swSync');
const statusLine = document.getElementById('swStatus');
const lastResult = document.getElementById('swLast');
const version = document.getElementById('swVersion');

async function refreshStatus() {
    try {
        const state = await chrome.runtime.sendMessage({action:'sw_status'});
        version.textContent = state.version ? 'v' + state.version : '';
        sync.disabled = !state.nativeReady || !state.connected;
        statusLine.textContent = !state.nativeReady ? 'Windows 共享组件未接通，请修复或重新安装 StudioWright 桌面版。'
            : state.connected ? '共享连接已就绪。打开已登录的 Flow 页面后可同步。'
                : '请从本机账号管理发起首次同步，以建立 Ubuntu 连接。';
        lastResult.textContent = state.last?.message || '';
        lastResult.hidden = !lastResult.textContent;
    } catch {
        statusLine.textContent = '插件未响应，请在 Chrome 扩展管理页重新加载。';
        sync.disabled = true;
    }
}

sync.addEventListener('click', async () => {
    sync.disabled = true;
    lastResult.hidden = false;
    lastResult.textContent = '正在读取当前账号，随后交给 Ubuntu 核对…';
    try {
        const result = await chrome.runtime.sendMessage({action:'sw_sync'});
        lastResult.textContent = result.ok ? result.message : result.error;
    } catch {
        lastResult.textContent = '连接中断，请先在 StudioWright 任务队列核对结果。';
    } finally {
        sync.disabled = false;
    }
});
void refreshStatus();
