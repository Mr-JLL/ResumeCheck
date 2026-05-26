'use strict';
// ── 51job 抓取助手 · Service Worker ─────────────────────────────
// 与 node_agent.py 使用完全相同的服务器 API，无需修改服务器代码。
// MV3 Service Worker 注意事项：
//   - 全局变量在 SW 被系统挂起后会丢失，持久状态存 chrome.storage.session
//   - 抓取期间用 keep-alive 定时器防止 SW 被挂起
//   - popup 连接时通过 port 保持 SW 活跃

// ── 工具函数 ─────────────────────────────────────────────────────

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function getConfig() {
  const d = await chrome.storage.local.get(['serverUrl', 'deviceId', 'deviceName']);
  let deviceId = d.deviceId;
  if (!deviceId) {
    deviceId = 'ext-' + Math.random().toString(36).slice(2, 10);
    chrome.storage.local.set({ deviceId });
  }
  return {
    url: (d.serverUrl || 'http://localhost:5000').replace(/\/$/, ''),
    id: deviceId,
    name: d.deviceName || ('扩展-' + deviceId.slice(-4)),
  };
}

async function getSessionState() {
  const d = await chrome.storage.session.get(['isScraping', 'stopFlag', 'job51TabId', 'currentJob']);
  return {
    isScraping:  d.isScraping  || false,
    stopFlag:    d.stopFlag    || false,
    job51TabId:  d.job51TabId  || null,
    currentJob:  d.currentJob  || null,
  };
}

async function setSS(updates) {
  await chrome.storage.session.set(updates);
}

async function isTabAlive(tabId) {
  if (!tabId) return false;
  try { await chrome.tabs.get(tabId); return true; } catch { return false; }
}

// ── 网络请求 ─────────────────────────────────────────────────────

async function apiFetch(url, method, body, timeoutMs = 7000) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    });
    return res.ok;
  } catch { return false; } finally { clearTimeout(t); }
}

async function sendProgress(cfg, jobName, phase, current, total, msg) {
  const st = await getSessionState();
  await apiFetch(`${cfg.url}/api/agent/progress`, 'POST', {
    device_id:   cfg.id,
    device_name: cfg.name,
    job_name:    jobName,
    phase, current, total, message: msg,
    browser_open: await isTabAlive(st.job51TabId),
  });
}

// ── Keep-alive（防止 SW 在抓取中被挂起）──────────────────────────

let _kaTimer = null;
let _cdpSeq  = 0;  // waitForPageDownload 每次调用的唯一序号，用于生成不冲突的 alarm name

function startKeepAlive() {
  if (_kaTimer) clearInterval(_kaTimer);
  _kaTimer = setInterval(() => {
    // 调用轻量 Chrome API，使 SW 保持活跃状态
    chrome.storage.session.get('isScraping');
  }, 20_000);
}

function stopKeepAlive() {
  if (_kaTimer) { clearInterval(_kaTimer); _kaTimer = null; }
}

// ── Tab 导航与等待 ───────────────────────────────────────────────

async function tabNavigateAndWait(tabId, url, maxMs = 14000) {
  await chrome.tabs.update(tabId, { url });
  await sleep(900); // 等待导航启动
  const deadline = Date.now() + maxMs;
  while (Date.now() < deadline) {
    await sleep(600);
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) throw new Error('标签页已关闭');
    if (tab.status === 'complete') return;
  }
  // 超时后继续（页面可能仍可用）
}

async function execInTab(tabId, func, args = []) {
  const result = await chrome.scripting.executeScript({
    target: { tabId },
    func,
    args,
  });
  return result[0]?.result;
}

// 在页面主世界（Main World）注入脚本——拦截器必须跑在主世界才能影响页面的原型链
async function execInTabMain(tabId, func, args = []) {
  const result = await chrome.scripting.executeScript({
    target: { tabId },
    world: 'MAIN',
    func,
    args,
  });
  return result[0]?.result;
}

// ── 从列表页提取候选人 ID ─────────────────────────────────────────

async function getResumeCards(tabId) {
  return await execInTab(tabId, () => {
    const cards = [...document.querySelectorAll('[class*="resume-card"]')];
    return cards.map(card => {
      const m = card.innerHTML.match(/no_interested_(\d+)/);
      if (!m) return null;
      let name = '未知';
      const ne = card.querySelector('.name');
      if (ne) {
        name = ne.textContent.split('\n')[0]
          .replace(/(先生|女士|活跃|沟通|电话|拨打|离职|在职|刚刚|1周内|1小时|3日内|1个月内|\s)/g, '')
          || '未知';
      }
      return { id: m[1], name };
    }).filter(Boolean);
  }) || [];
}

async function scrollToLastCard(tabId) {
  await execInTab(tabId, () => {
    const cards = document.querySelectorAll('[class*="resume-card"]');
    if (cards.length) cards[cards.length - 1].scrollIntoView({ behavior: 'smooth' });
  });
}

// ── 抓取主流程 ───────────────────────────────────────────────────

async function doScrape(cfg, jobName, targetCount, sessionId) {
  const st = await getSessionState();
  if (!await isTabAlive(st.job51TabId)) {
    await sendProgress(cfg, jobName, 'error', 0, 0,
      '51job 标签页未打开，请先点击「打开浏览器」');
    return;
  }
  const tabId = st.job51TabId;

  await setSS({ isScraping: true, stopFlag: false, currentJob: jobName });
  startKeepAlive();

  try {
    // ── 第一阶段：扫描列表页，收集候选人 ID ──────────────────────
    await sendProgress(cfg, jobName, 'scraping', 0, targetCount,
      `开始扫描，目标 ${targetCount} 份`);

    const taskPool = [];
    const seenIds  = new Set();
    let noProgress = 0;

    while (taskPool.length < targetCount && noProgress < 4) {
      if ((await chrome.storage.session.get('stopFlag')).stopFlag) break;

      const cards = await getResumeCards(tabId).catch(() => []);
      let foundNew = false;
      for (const { id, name } of cards) {
        if (seenIds.has(id)) continue;
        seenIds.add(id);
        taskPool.push({ id, name });
        foundNew = true;
        if (taskPool.length >= targetCount) break;
      }

      await sendProgress(cfg, jobName, 'scraping', taskPool.length, targetCount,
        `扫描到 ${taskPool.length}/${targetCount} 份候选人`);

      if (taskPool.length >= targetCount) break;

      await scrollToLastCard(tabId);
      await sleep(2500);
      noProgress = foundNew ? 0 : noProgress + 1;
    }

    if (!taskPool.length) {
      await sendProgress(cfg, jobName, 'error', 0, 0,
        '未找到候选人，请确认已在 51job 搜索结果页并已有候选人列表');
      return;
    }

    // ── 第二阶段：逐个访问详情页并上传 ──────────────────────────
    let uploaded = 0;
    for (let i = 0; i < taskPool.length; i++) {
      if ((await chrome.storage.session.get('stopFlag')).stopFlag) {
        await sendProgress(cfg, jobName, 'stopped', i, taskPool.length, '已停止');
        break;
      }

      const { id, name } = taskPool[i];
      await sendProgress(cfg, jobName, 'scraping', i + 1, taskPool.length,
        `保存 (${i + 1}/${taskPool.length}): ${name}`);

      try {
        const detailUrl =
          `https://ehire.51job.com/Revision/talent/resume/detail?resumeId=${id}`;
        await tabNavigateAndWait(tabId, detailUrl);
        await sleep(800); // 等待 JS 渲染

        const html = await execInTab(tabId,
          () => document.documentElement.outerHTML) || '';

        if (html.length < 1000) {
          // 页面太短，可能是登录页或被重定向
          await sendProgress(cfg, jobName, 'scraping', i + 1, taskPool.length,
            `⚠ ${name} 页面异常（可能需要重新登录），已跳过`);
          continue;
        }

        const ok = await apiFetch(`${cfg.url}/api/scrape/upload_html`, 'POST', {
          job_name:    jobName,
          resume_id:   id,
          name_hint:   name,
          html,
          session_id:  sessionId,
          device_id:   cfg.id,
          device_name: cfg.name,
          index:       i + 1,
          total:       taskPool.length,
        }, 30_000);

        if (ok) uploaded++;
      } catch (e) {
        await sendProgress(cfg, jobName, 'scraping', i + 1, taskPool.length,
          `⚠ ${name} 出错: ${e.message}`);
      }

      // 随机延迟，避免触发反爬限制
      await sleep(1500 + Math.random() * 1500);
    }

    await sendProgress(cfg, jobName, 'done', uploaded, taskPool.length,
      `✓ 抓取完成，已上传 ${uploaded}/${taskPool.length} 份`);

  } catch (err) {
    await sendProgress(cfg, jobName, 'error', 0, 0, `抓取异常: ${err.message}`);
  } finally {
    await setSS({ isScraping: false, currentJob: null });
    stopKeepAlive();
  }
}

// ── 下载完成等待（修复 erase 过早取消 in_progress 下载的 bug）────────

function waitDownloadDone(dlId) {
  return new Promise(resolve => {
    let settled = false;
    const alarmName = `dltimeout_${dlId}`;

    function finish(ok, err) {
      if (settled) return;
      settled = true;
      chrome.downloads.onChanged.removeListener(onChange);
      chrome.alarms.onAlarm.removeListener(onAlarm);
      chrome.alarms.clear(alarmName);
      resolve({ ok, err: err || null });
    }

    function onChange(delta) {
      if (delta.id !== dlId) return;
      const st = delta.state?.current;
      if (st === 'complete') finish(true);
      else if (st === 'interrupted') finish(false, delta.error?.current || '下载中断');
    }

    function onAlarm(alarm) {
      if (alarm.name === alarmName) finish(false, '写入超时（60s）');
    }

    chrome.downloads.onChanged.addListener(onChange);
    chrome.alarms.onAlarm.addListener(onAlarm);
    chrome.alarms.create(alarmName, { when: Date.now() + 60_000 });

    // 攻击2修复：注册监听器后立刻补检当前状态，防止 complete 早于监听器触发
    chrome.downloads.search({ id: dlId }, items => {
      if (!items?.length) { finish(false, 'download not found'); return; }
      const st = items[0].state;
      if (st === 'complete') finish(true);
      else if (st === 'interrupted') finish(false, items[0].error || '下载中断');
    });
  });
}

// ── CDP 页面下载完成等待 ──────────────────────────────────────────
// 监听 Page.downloadWillBegin（记录 GUID）和 Page.downloadProgress（检测 completed/canceled）。
// 返回 { promise, cancel }：
//   promise  — 下载成功时 resolve，失败/超时/取消时 reject
//   cancel() — 外部主动取消（幂等），用于注入脚本失败时清理监听器，防止泄漏（攻击5解法）

function waitForPageDownload(tabId) {
  const alarmName = `cdpdl_${++_cdpSeq}`;
  let guid    = null;
  let settled = false;
  let _finish;

  const promise = new Promise((resolve, reject) => {
    function finish(ok, msg) {
      if (settled) return;
      settled = true;
      chrome.debugger.onEvent.removeListener(onCdpEvent);
      chrome.alarms.onAlarm.removeListener(onAlarm);
      chrome.alarms.clear(alarmName);
      ok ? resolve() : reject(new Error(msg || 'CDP下载失败'));
    }
    _finish = finish;

    function onCdpEvent(source, method, params) {
      if (source.tabId !== tabId) return;
      if (method === 'Page.downloadWillBegin') {
        guid = params.guid;                                          // 记录本次下载的 GUID
      } else if (method === 'Page.downloadProgress' && guid && params.guid === guid) {
        if (params.state === 'completed') finish(true);
        else if (params.state === 'canceled') finish(false, 'CDP下载被取消');
      }
    }

    function onAlarm(alarm) {
      if (alarm.name === alarmName) finish(false, 'CDP下载超时（60s）');
    }

    // 攻击6解法：监听器在触发下载的脚本注入之前注册，此处已保证顺序
    chrome.debugger.onEvent.addListener(onCdpEvent);
    chrome.alarms.onAlarm.addListener(onAlarm);
    chrome.alarms.create(alarmName, { when: Date.now() + 60_000 });
  });

  return {
    promise,
    cancel: (msg) => _finish(false, msg || '下载已中止'),  // 幂等，settled 后无效
  };
}

// ── 命令处理 ─────────────────────────────────────────────────────

async function handleCommand(cmd, cfg) {
  const action  = cmd.command  || '';
  const jobName = cmd.job_name || '';

  if (action === 'open_browser') {
    const st = await getSessionState();
    if (await isTabAlive(st.job51TabId)) {
      // 已有 51job 标签页，置为激活状态
      await chrome.tabs.update(st.job51TabId, { active: true }).catch(() => {});
    } else {
      // 新开标签页导航到 51job 招聘管理
      const tab = await chrome.tabs.create({
        url: 'https://ehire.51job.com',
        active: true,
      });
      await setSS({ job51TabId: tab.id });
    }

  } else if (action === 'close_browser') {
    const st = await getSessionState();
    if (await isTabAlive(st.job51TabId)) {
      await chrome.tabs.remove(st.job51TabId).catch(() => {});
    }
    await setSS({ job51TabId: null });

  } else if (action === 'scrape') {
    const st = await getSessionState();
    if (st.isScraping) return; // 已在抓取中，忽略重复命令
    const sessionId    = new Date().toISOString().slice(0, 19);
    const targetCount  = parseInt(cmd.target_count || 30);
    // 异步执行，不阻塞轮询循环
    doScrape(cfg, jobName, targetCount, sessionId);

  } else if (action === 'stop') {
    await setSS({ stopFlag: true });

  } else if (action === 'download_file') {
    const resumeId = cmd.resume_id || '';
    const filetype = cmd.filetype  || 'pdf';
    if (!resumeId) return;
    const st2 = await getSessionState();
    if (!await isTabAlive(st2.job51TabId)) return;
    const tabId = st2.job51TabId;

    await tabNavigateAndWait(tabId,
      `https://ehire.51job.com/Revision/talent/resume/detail?resumeId=${resumeId}`, 15000);
    await sleep(1200);

    // 在标签页主世界拦截下载 URL，取回文件内容后上传服务器
    // 必须用 execInTabMain（主世界）——隔离世界的原型 patch 对页面 JS 无效
    const dlResult = await execInTabMain(tabId, async (ft) => {
      const labels = ft === 'pdf' ? ['PDF', 'pdf'] : ['Word', 'WORD', 'word', 'Doc'];

      let capturedBlob = null;
      let capturedUrl  = null;

      const origCOU = URL.createObjectURL;
      URL.createObjectURL = function(obj) {
        if (obj instanceof Blob && obj.size > 100) capturedBlob = obj;
        return origCOU.call(URL, obj);
      };
      const origCE = document.createElement.bind(document);
      document.createElement = function(tag) {
        const el = origCE(tag);
        if (typeof tag === 'string' && tag.toLowerCase() === 'a') {
          const origClick = el.click.bind(el);
          el.click = function() {
            const h = el.href || '';
            if (h && !h.startsWith('javascript:') && !h.startsWith('#') && !h.startsWith('data:')) {
              capturedUrl = capturedUrl || h; return;
            }
            origClick();
          };
        }
        return el;
      };
      const origXHROpen = XMLHttpRequest.prototype.open;
      XMLHttpRequest.prototype.open = function(m, url) {
        if (url && typeof url === 'string' && !/poll|heartbeat|ping|alive|status|check/i.test(url))
          capturedUrl = url;
        return origXHROpen.apply(this, arguments);
      };
      const origOpen = window.open;
      window.open = (url) => { if (url) capturedUrl = capturedUrl || String(url); return null; };

      function restoreAll() {
        URL.createObjectURL = origCOU;
        document.createElement = origCE;
        XMLHttpRequest.prototype.open = origXHROpen;
        window.open = origOpen;
      }

      const mainBtn = Array.from(document.querySelectorAll(
        'button, a, [role="button"], [class*="download"]'
      )).find(el => {
        const t = el.textContent.trim(), c = el.className || '';
        return (t.includes('下载简历') || t === '下载' ||
                c.includes('download') || c.includes('Download')) &&
               el.offsetParent !== null;
      });
      if (!mainBtn) { restoreAll(); return { ok: false, error: '未找到下载按钮' }; }
      mainBtn.click();
      await new Promise(r => setTimeout(r, 1500));

      const item = Array.from(document.querySelectorAll(
        'li, a, button, [role="option"], [role="menuitem"], [class*="item"]'
      )).find(el => el.offsetParent !== null && labels.some(l => el.textContent.trim().includes(l)));
      if (item) item.click();

      for (let i = 0; i < 10; i++) {
        await new Promise(r => setTimeout(r, 400));
        if (capturedBlob || capturedUrl) break;
      }
      restoreAll();

      if (capturedBlob && capturedBlob.size > 100) {
        return new Promise(resolve => {
          const reader = new FileReader();
          reader.onload  = () => resolve({ ok: true, data: reader.result.split(',')[1] });
          reader.onerror = () => resolve({ ok: false, error: 'Blob读取失败' });
          reader.readAsDataURL(capturedBlob);
        });
      }
      if (!capturedUrl) return { ok: false, error: '未捕获到下载URL，请确认已登录且有下载权限' };

      if (capturedUrl.startsWith('blob:')) {
        try {
          const r = await fetch(capturedUrl);
          if (!r.ok) return { ok: false, error: `BlobURL HTTP ${r.status}` };
          const b = await r.blob();
          return new Promise(resolve => {
            const reader = new FileReader();
            reader.onload  = () => resolve({ ok: true, data: reader.result.split(',')[1] });
            reader.onerror = () => resolve({ ok: false, error: 'BlobURL读取失败' });
            reader.readAsDataURL(b);
          });
        } catch(e) { return { ok: false, error: `BlobURL访问失败: ${e.message}` }; }
      }

      try {
        const resp = await fetch(capturedUrl, { credentials: 'include' });
        if (!resp.ok) return { ok: false, error: `HTTP ${resp.status}` };
        const blob = await resp.blob();
        if (blob.size < 200) return { ok: false, error: `文件过小(${blob.size}字节)，可能需重新登录` };
        return new Promise(resolve => {
          const reader = new FileReader();
          reader.onload  = () => resolve({ ok: true, data: reader.result.split(',')[1] });
          reader.onerror = () => resolve({ ok: false, error: 'FileReader读取失败' });
          reader.readAsDataURL(blob);
        });
      } catch(e) { return { ok: false, error: `Fetch失败: ${e.message}` }; }
    }, [filetype]).catch(e => ({ ok: false, error: `脚本注入异常: ${String(e)}` }));

    if (dlResult?.ok === true && dlResult.data) {
      await apiFetch(`${cfg.url}/api/scrape/upload_file`, 'POST', {
        resume_id: resumeId, filetype, data_b64: dlResult.data,
      }, 30_000);
    }

  } else if (action === 'batch_download') {
    // ── 批量下载到本机（CDP 静默模式 + chrome.downloads 降级）───────
    // 主路径：Page.setDownloadBehavior 拦截下载，Edge 全程不弹下载面板；
    // 降级路径：Page.setDownloadBehavior 不可用时，退回 chrome.downloads（行为与旧代码相同）。
    const items   = cmd.items    || [];
    const jobName = cmd.job_name || '未知岗位';
    const batchId = cmd.batch_id || '';
    if (!items.length) return;

    const stBd = await getSessionState();
    if (!await isTabAlive(stBd.job51TabId)) {
      await apiFetch(`${cfg.url}/api/agent/progress`, 'POST', {
        device_id: cfg.id, device_name: cfg.name,
        job_name: jobName, phase: 'dl_error',
        current: 0, total: items.length, failed: items.length,
        message: '51job 标签页未打开，请先点击「打开浏览器」并登录51job',
        batch_id: batchId,
      });
      return;
    }
    const tabIdBd = stBd.job51TabId;
    startKeepAlive();

    const dlConf    = await chrome.storage.local.get(['localDownloadFolder']);
    const rawFolder = (dlConf.localDownloadFolder || '51job简历下载').trim() || '51job简历下载';
    const folder    = rawFolder.replace(/\\/g, '/').replace(/^[A-Za-z]:\//, '').replace(/^\/+/, '') || '51job简历下载';

    function _sanitize(s, maxLen) {
      return String(s || '').replace(/[/\\:*?"<>|]/g, '_').trim().slice(0, maxLen) || 'resume';
    }
    const safeJob = _sanitize(jobName, 20);

    function _makeDatePart(d) {
      if (!d) return '';
      const parts = String(d).split('-');
      if (parts.length < 3) return '';
      const m   = parseInt(parts[1], 10);
      const day = parseInt(parts[2], 10);
      return (m && day) ? `${m}.${day}` : '';
    }

    // ── 攻击1/2解法：探针下载，取得绝对目录路径并建好子目录结构 ─────
    // 下载 1 字节占位文件 → 从 filename 字段截取绝对目录路径 → removeFile + erase 清理。
    // 探针文件不显示简历名，符合"不弹出页面显示正在下载什么简历"的要求。
    // Page.setDownloadBehavior 需要绝对路径；chrome.downloads 自动建子目录，探针顺带完成这一步。
    let absTargetDir = null;  // 带末尾分隔符的绝对路径，供 Page.setDownloadBehavior 使用
    let cdpEnabled   = false; // 若探针失败或 Page.setDownloadBehavior 不支持，退回降级路径

    {
      const { id: pId } = await new Promise(resolve =>
        chrome.downloads.download({
          url: 'data:application/octet-stream;base64,AA==',
          filename: `${folder}/${safeJob}/_cdl_probe.tmp`,
          saveAs: false, conflictAction: 'overwrite',
        }, id => {
          const err = chrome.runtime.lastError?.message || null;
          resolve({ id: (err || id == null) ? null : id });
        })
      );
      if (pId != null) {
        await waitDownloadDone(pId);
        const [pi] = await new Promise(r => chrome.downloads.search({ id: pId }, r));
        if (pi?.filename) {
          const last = Math.max(pi.filename.lastIndexOf('/'), pi.filename.lastIndexOf('\\'));
          if (last !== -1) { absTargetDir = pi.filename.slice(0, last + 1); cdpEnabled = true; }
        }
        // 攻击2解法（续）：先 removeFile 再 erase（removeFile 要求 item 仍在历史里）
        await new Promise(r => chrome.downloads.removeFile(pId, () => r())).catch(() => {});
        chrome.downloads.erase({ id: pId });
      }
    }

    let dlDone = 0, dlFailed = 0;
    let lastGoodDlId = null;  // 仅降级路径使用
    const seenNames = new Set();  // 批次内文件名去重，防同名覆盖

    try {
      for (let i = 0; i < items.length; i++) {
        const { resume_id, name, date } = items[i];

        if ((await chrome.storage.session.get('stopFlag')).stopFlag) {
          await apiFetch(`${cfg.url}/api/agent/progress`, 'POST', {
            device_id: cfg.id, device_name: cfg.name,
            job_name: jobName, phase: 'stopped',
            current: dlDone, total: items.length, failed: dlFailed,
            message: '已停止', batch_id: batchId,
          });
          break;
        }

        await apiFetch(`${cfg.url}/api/agent/progress`, 'POST', {
          device_id: cfg.id, device_name: cfg.name,
          job_name: jobName, phase: 'downloading',
          current: dlDone, total: items.length, failed: dlFailed,
          message: String(name || resume_id), batch_id: batchId,
        });

        await tabNavigateAndWait(tabIdBd,
          `https://ehire.51job.com/Revision/talent/resume/detail?resumeId=${resume_id}`, 15000);
        await sleep(1200);

        const safeCand  = _sanitize(name, 20);
        const rid8      = String(resume_id).slice(-8);
        const datePart  = _makeDatePart(date ? String(date).slice(0, 10) : '');

        let pdfFname;
        if (datePart && safeJob && safeCand) {
          const base = `${datePart}-${safeJob}-${safeCand}`;
          let candidate = `${base}.pdf`;
          let n = 2;
          while (seenNames.has(candidate)) { candidate = `${base}_${n++}.pdf`; }
          pdfFname = candidate;
        } else {
          pdfFname = `${safeCand}_${rid8}.pdf`;
        }
        seenNames.add(pdfFname);

        let itemErr = null;

        try {
          await chrome.debugger.attach({ tabId: tabIdBd }, '1.3');
        } catch (e) {
          itemErr = `无法附加调试器（请关闭该标签的 DevTools 再重试）: ${e.message}`;
        }

        if (!itemErr) {
          let didSetBehavior = false;
          try {
            // ── Step 1: 尝试设置静默下载行为（新版浏览器可能拒绝，失败则降级）──
            if (cdpEnabled) {
              try {
                await chrome.debugger.sendCommand({ tabId: tabIdBd }, 'Page.setDownloadBehavior', {
                  behavior: 'allowAndName', downloadPath: absTargetDir, eventsEnabled: true,
                });
                didSetBehavior = true;
              } catch (setBhvErr) {
                // "Cannot not access browser-level commands" 及同类错误 → 降级
                if (/browser.level|Cannot|Unknown command|setDownloadBehavior/i.test(setBhvErr.message)) {
                  cdpEnabled = false;  // 本项及后续项全部走 chrome.downloads
                } else {
                  throw setBhvErr;    // 其他错误（如调试器未附加）继续上抛
                }
              }
            }

            // ── Step 2: printToPDF（两条路径共用，Page.printToPDF 始终可用）──
            const pdfResult = await chrome.debugger.sendCommand(
              { tabId: tabIdBd }, 'Page.printToPDF',
              { printBackground: true, preferCSSPageSize: false }
            );
            const pdfB64 = pdfResult?.data;
            if (!pdfB64) throw new Error('CDP 返回了空 PDF，请确认简历页面已完全加载');

            if (didSetBehavior) {
              // ── CDP 静默下载路径（Page.setDownloadBehavior 成功）────────────
              // 攻击6解法：先注册监听器，再注入触发脚本，消除竞态
              const { promise: dlPromise, cancel: cancelDl } = waitForPageDownload(tabIdBd);

              try {
                // 攻击8/9解法：通过 executeScript args 传递大 base64，延迟 300ms 再撤销 BlobURL
                const injectRes = await chrome.scripting.executeScript({
                  target: { tabId: tabIdBd },
                  world: 'MAIN',
                  func: (b64, fname) => {
                    try {
                      const bin = atob(b64);
                      const buf = new Uint8Array(bin.length);
                      for (let k = 0; k < bin.length; k++) buf[k] = bin.charCodeAt(k);
                      const blob = new Blob([buf], { type: 'application/pdf' });
                      const url  = URL.createObjectURL(blob);
                      const a    = Object.assign(document.createElement('a'),
                                                { href: url, download: fname });
                      document.body.appendChild(a);
                      a.click();
                      document.body.removeChild(a);
                      setTimeout(() => URL.revokeObjectURL(url), 300);
                      return { ok: true };
                    } catch (e) { return { ok: false, error: e.message }; }
                  },
                  args: [pdfB64, pdfFname],
                });
                const injected = injectRes?.[0]?.result;
                if (!injected?.ok) throw new Error(`注入下载脚本失败: ${injected?.error || '未知错误'}`);
                // 攻击4解法：等待 Page.downloadProgress state=completed
                await dlPromise;
              } catch (e) {
                // 攻击5解法：任何失败路径都调用 cancel()，移除监听器防止泄漏（幂等）
                cancelDl();
                throw e;
              }

              dlDone++;

            } else {
              // ── 降级路径：chrome.downloads（Page.setDownloadBehavior 不可用时）──
              const fallbackFile = `${folder}/${safeJob}/${pdfFname}`;
              const { dlId, dlErr } = await new Promise(resolve =>
                chrome.downloads.download({
                  url: `data:application/pdf;base64,${pdfB64}`,
                  filename: fallbackFile,
                  saveAs: false,
                  conflictAction: 'uniquify',
                }, id => {
                  const err = chrome.runtime.lastError?.message || null;
                  resolve({ dlId: (err || id == null) ? null : id, dlErr: err });
                })
              );
              if (dlId == null) throw new Error(
                dlErr ? `chrome.downloads 报错: ${dlErr}（文件名: ${fallbackFile}）`
                      : `本地保存失败（文件名: ${fallbackFile}）`
              );

              const { ok, err: writeErr } = await waitDownloadDone(dlId);
              if (!ok) {
                chrome.downloads.erase({ id: dlId });
                throw new Error(writeErr || '文件写入失败');
              }

              dlDone++;
              if (lastGoodDlId != null) chrome.downloads.erase({ id: lastGoodDlId });
              lastGoodDlId = dlId;
            }

          } catch (e) {
            itemErr = `${didSetBehavior ? 'CDP' : ''}下载失败: ${e.message}`;
          } finally {
            // 只有成功 set 过才需要 restore，避免对失败项重复调用
            if (didSetBehavior) {
              await chrome.debugger.sendCommand({ tabId: tabIdBd }, 'Page.setDownloadBehavior', {
                behavior: 'default',
              }).catch(() => {});
            }
            await chrome.debugger.detach({ tabId: tabIdBd }).catch(() => {});
          }
        }

        if (itemErr) {
          dlFailed++;
          await apiFetch(`${cfg.url}/api/agent/progress`, 'POST', {
            device_id: cfg.id, device_name: cfg.name,
            job_name: jobName, phase: 'downloading',
            current: dlDone, total: items.length, failed: dlFailed,
            message: `⚠ ${name}: ${itemErr}`, batch_id: batchId,
          });
        }

        await sleep(1500 + Math.random() * 1500);
      }
    } finally {
      stopKeepAlive();
    }

    // ── 批量结束：弹出文件资源管理器 ───────────────────────────────
    if (dlDone > 0) {
      if (cdpEnabled) {
        // 攻击3解法：CDP 下载无 download ID，用哨兵文件获取 ID 供 show() 使用。
        // show() 打开正确目录后，removeFile + erase 清理哨兵（先 removeFile 再 erase）。
        const { id: sId } = await new Promise(resolve =>
          chrome.downloads.download({
            url: 'data:application/octet-stream;base64,AA==',
            filename: `${folder}/${safeJob}/_cdl_done.tmp`,
            saveAs: false, conflictAction: 'overwrite',
          }, id => {
            const err = chrome.runtime.lastError?.message || null;
            resolve({ id: (err || id == null) ? null : id });
          })
        );
        if (sId != null) {
          await waitDownloadDone(sId);
          chrome.downloads.show(sId);
          setTimeout(() => {
            chrome.downloads.removeFile(sId, () => chrome.downloads.erase({ id: sId }));
          }, 500);
        }
      } else if (lastGoodDlId != null) {
        // 降级模式：行为与原代码相同
        chrome.downloads.show(lastGoodDlId);
        setTimeout(() => chrome.downloads.erase({ id: lastGoodDlId }), 500);
      }
    }

    // 最终上报完成（dl_done）或全部失败（dl_error）
    await apiFetch(`${cfg.url}/api/agent/progress`, 'POST', {
      device_id: cfg.id, device_name: cfg.name,
      job_name: jobName,
      phase: dlFailed === items.length && dlDone === 0 ? 'dl_error' : 'dl_done',
      current: dlDone, total: items.length, failed: dlFailed,
      message: `下载完成：${dlDone}份成功，${dlFailed}份失败`,
      batch_id: batchId,
    });
  }
}

// ── 长轮询主循环 ─────────────────────────────────────────────────
// 每次 fetch 长达 25 秒，服务器有命令时立即返回；SW 始终有进行中的网络请求，
// 不会因空闲被浏览器挂起。Alarm 每分钟触发一次作为保底重启机制。

let _longPollRunning = false;

async function startLongPoll() {
  if (_longPollRunning) return;
  _longPollRunning = true;
  try {
    while (true) {
      await doOneLongPoll();
    }
  } finally {
    _longPollRunning = false;
  }
}

async function doOneLongPoll() {
  const cfg = await getConfig();
  if (!cfg.url) { await sleep(5000); return; }

  const st       = await getSessionState();
  const tabAlive = await isTabAlive(st.job51TabId);
  const params   = new URLSearchParams({
    device_id:    cfg.id,
    device_name:  cfg.name,
    browser_open: tabAlive ? '1' : '0',
    status:       st.isScraping ? 'scraping' : 'idle',
    current_job:  st.currentJob || '',
    long_poll:    '1',
  });

  const ctrl = new AbortController();
  const t    = setTimeout(() => ctrl.abort(), 30_000); // 25s 服务端 + 5s 缓冲
  try {
    const res = await fetch(`${cfg.url}/api/agent/poll?${params}`,
      { signal: ctrl.signal });
    clearTimeout(t);
    if (res.ok) {
      const data = await res.json();
      for (const cmd of (data.commands || [])) {
        await handleCommand(cmd, cfg);
      }
    } else {
      await sleep(3000);
    }
  } catch {
    clearTimeout(t);
    await sleep(5000); // 网络故障后等待再重试
  }
}

// ── Service Worker 生命周期 ───────────────────────────────────────

chrome.runtime.onInstalled.addListener(() => {
  // 保留 1 分钟闹钟作为保底（SW 意外挂起后唤醒并重启长轮询）
  chrome.alarms.clearAll(() => chrome.alarms.create('poll', { periodInMinutes: 1 }));
  startLongPoll();
});

chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create('poll', { periodInMinutes: 1 });
  startLongPoll();
});

// 闹钟触发：若长轮询循环意外停止则重启
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === 'poll') startLongPoll();
});

// Popup 连接时：确保长轮询在运行，并在 popup 期间快速轮询
chrome.runtime.onConnect.addListener(port => {
  if (port.name !== 'keepalive') return;
  startLongPoll(); // 幂等，不会开多个循环
  // popup 打开时每 3 秒触发一次普通（非长轮询）心跳，确保状态实时刷新
  const interval = setInterval(async () => {
    const cfg = await getConfig();
    if (!cfg.url) return;
    const st = await getSessionState();
    const ta = await isTabAlive(st.job51TabId);
    const p  = new URLSearchParams({
      device_id: cfg.id, device_name: cfg.name,
      browser_open: ta ? '1' : '0',
      status: st.isScraping ? 'scraping' : 'idle',
      current_job: st.currentJob || '',
    });
    fetch(`${cfg.url}/api/agent/poll?${p}`, { signal: AbortSignal.timeout(4000) })
      .catch(() => {});
  }, 3000);
  port.onDisconnect.addListener(() => clearInterval(interval));
});

// SW 初次加载时启动长轮询
startLongPoll();
