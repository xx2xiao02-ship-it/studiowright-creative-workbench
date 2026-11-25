import RFB from '/remote-vendor/core/rfb.js';

const $ = id => document.getElementById(id);
const profilePattern = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const sessionPattern = /^[a-f0-9]{48}$/;
const profile = new URLSearchParams(location.hash.slice(1)).get('profile');
const session = new URLSearchParams(location.search).get('session');
const startButton = $('startLogin');
const finishButton = $('finishLogin');
const screen = $('remoteScreen');
let rfb, timer, finishing = false;

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Console-Request': '1'},
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '连接失败');
  return data;
}

function status(text) {
  $('remoteState').textContent = text;
}

async function checkWindow() {
  try {
    await api('/api/remote/' + session);
  } catch (error) {
    clearInterval(timer);
    finishButton.disabled = true;
    rfb?.disconnect();
    status(error.message);
  }
}

async function connectWindow() {
  status('正在核对远程连接…');
  try {
    await api('/api/remote/' + session);
    screen.hidden = false;
    finishButton.hidden = false;
    $('remoteHint').textContent = '正在连接此账号的 Ubuntu 浏览器…';
    const socket = new URL('/api/remote/' + session + '/ws', location.href);
    socket.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    rfb = new RFB(screen, socket.href, {shared: false});
    rfb.scaleViewport = true;
    rfb.resizeSession = false;
    rfb.addEventListener('connect', () => {
      $('remoteHint').textContent = '已连接此账号的 Ubuntu 浏览器';
      status('登录后请点击「完成登录并检查」。');
      finishButton.disabled = false;
    });
    rfb.addEventListener('disconnect', event => {
      clearInterval(timer);
      finishButton.disabled = true;
      if (!finishing) status(event.detail.clean ? '远程窗口已关闭。' : '远程画面连接中断，请返回账号管理重新打开。');
    });
    rfb.addEventListener('securityfailure', () => {
      status('远程画面连接未通过验证，请返回账号管理重新打开。');
    });
    timer = setInterval(checkWindow, 15000);
  } catch (error) {
    status(error.message);
  }
}

startButton.onclick = async () => {
  startButton.disabled = true;
  status('正在打开此账号的 Ubuntu 登录窗口…');
  try {
    const opened = await api('/api/accounts/' + encodeURIComponent(profile) + '/remote', {});
    if (opened.profile !== profile || !sessionPattern.test(opened.id || '')) {
      throw new Error('无法确认远程窗口所属账号，请返回账号管理重新打开。');
    }
    location.replace('/remote?session=' + opened.id);
  } catch (error) {
    status(error.message);
    startButton.disabled = false;
  }
};

finishButton.onclick = async () => {
  if (finishing) return;
  finishing = true;
  finishButton.disabled = true;
  clearInterval(timer);
  try {
    const task = await api('/api/remote/' + session + '/finish', {});
    rfb?.disconnect();
    status('已保存登录资料，正在核对 Flow 登录状态…');
    try {
      for (let i = 0; i < 90; i++) {
        await new Promise(resolve => setTimeout(resolve, 1500));
        const result = await api('/api/tasks/' + encodeURIComponent(task.id));
        if (['queued', 'running'].includes(result.status)) continue;
        status(result.status === 'succeeded' ? 'Flow 登录核对成功。'
          : '登录核对未成功：' + (result.failure_reason || '请查看账号检查记录。'));
        return;
      }
    } catch {
      status('已提交核对，请在算力中心查看账号检查记录。');
      return;
    }
    status('后台仍在核对，请在算力中心查看账号检查记录。');
  } catch (error) {
    status(error.message);
    finishing = false;
    finishButton.disabled = false;
    timer = setInterval(checkWindow, 15000);
  }
};

window.addEventListener('beforeunload', () => {
  clearInterval(timer);
  rfb?.disconnect();
});

if (sessionPattern.test(session || '')) {
  void connectWindow();
} else if (window.top === window && !session && profilePattern.test(profile || '')) {
  startButton.hidden = false;
  $('remoteHint').textContent = '打开此 Flow 账号的专用浏览器';
} else {
  status('请从算力中心的 Flow 账号卡片打开远程登录。');
}
