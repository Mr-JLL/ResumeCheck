'use strict';

// Popup 打开时通过 port 保持 Service Worker 活跃，并触发快速轮询
const port = chrome.runtime.connect({ name: 'keepalive' });

const $ = id => document.getElementById(id);

// ── 加载已保存的配置 ──────────────────────────────────────────────
chrome.storage.local.get(['serverUrl', 'deviceName', 'localDownloadFolder', 'deviceId'], d => {
  if (d.serverUrl)  $('serverUrl').value  = d.serverUrl;
  if (d.deviceName) $('deviceName').value = d.deviceName;
  const folder = d.localDownloadFolder || '51job简历下载';
  $('localDownloadFolder').value = folder;
  $('folderPreview').textContent = folder;
  if (d.deviceId) $('deviceIdDisplay').textContent = d.deviceId;
  if (d.serverUrl) checkServer(d.serverUrl);
});

// 文件夹名实时预览
$('localDownloadFolder').addEventListener('input', () => {
  const v = $('localDownloadFolder').value.trim() || '51job简历下载';
  $('folderPreview').textContent = v;
});

// ── 保存设置 ──────────────────────────────────────────────────────
$('btnSave').addEventListener('click', async () => {
  const url    = $('serverUrl').value.trim().replace(/\/$/, '');
  const name   = $('deviceName').value.trim();
  const folder = $('localDownloadFolder').value.trim() || '51job简历下载';
  if (!url) { alert('请输入服务器地址，例如 http://192.168.1.100:5000'); return; }
  await chrome.storage.local.set({ serverUrl: url, deviceName: name, localDownloadFolder: folder });
  $('btnSave').textContent = '✓ 已保存';
  $('btnSave').classList.add('saved-ok');
  setTimeout(() => {
    $('btnSave').textContent = '保存并连接';
    $('btnSave').classList.remove('saved-ok');
  }, 1500);
  setStatus('idle', '连接中…', 'gray');
  checkServer(url);
});

// ── 检测服务器连通性 ──────────────────────────────────────────────
async function checkServer(url) {
  try {
    const r = await fetch(`${url}/api/agent/list`, {
      signal: AbortSignal.timeout(4000),
    });
    if (r.ok) {
      const data = await r.json();
      const count = (data.devices || []).length;
      setStatus('ok', `已连接 · ${count} 台设备在线`, 'green');
    } else {
      setStatus('err', `服务器响应异常 (HTTP ${r.status})`, 'red');
    }
  } catch (e) {
    const msg = e.name === 'TimeoutError' ? '连接超时' : `无法连接 (${e.message})`;
    setStatus('err', msg, 'red');
  }
}

function setStatus(type, text, dotColor) {
  const box  = $('statusBox');
  const dot  = $('statusDot');
  const lbl  = $('statusText');
  box.className = 'status-box ' + { ok: 's-ok', err: 's-err', idle: 's-idle' }[type];
  dot.className = 'dot dot-' + dotColor;
  lbl.textContent = text;
}
