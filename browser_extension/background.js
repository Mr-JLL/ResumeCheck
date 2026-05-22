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
    // ── 批量下载到本机（保存到 chrome.downloads，不上传服务器）──────
    const items   = cmd.items    || [];
    const jobName = cmd.job_name || '未知岗位';
    const batchId = cmd.batch_id || '';
    if (!items.length) return;

    // 漏洞8修复：提前检查 51job 标签页是否存活
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

    // 读取用户设置的下载文件夹名
    const dlConf = await chrome.storage.local.get(['localDownloadFolder']);
    const folder = (dlConf.localDownloadFolder || '51job简历下载').trim() || '51job简历下载';

    // 漏洞6修复：截断过长名称，保证 Windows 路径安全
    function _sanitize(s, maxLen) {
      return String(s || '').replace(/[/\\:*?"<>|]/g, '_').trim().slice(0, maxLen) || 'resume';
    }
    const safeJob = _sanitize(jobName, 20);

    let dlDone = 0, dlFailed = 0;

    try {
      for (let i = 0; i < items.length; i++) {
        const { resume_id, name } = items[i];

        // 上报当前项进度
        await apiFetch(`${cfg.url}/api/agent/progress`, 'POST', {
          device_id: cfg.id, device_name: cfg.name,
          job_name: jobName, phase: 'downloading',
          current: dlDone, total: items.length, failed: dlFailed,
          message: String(name || resume_id),
          batch_id: batchId,
        });

        // 导航到简历详情页
        await tabNavigateAndWait(tabIdBd,
          `https://ehire.51job.com/Revision/talent/resume/detail?resumeId=${resume_id}`, 15000);
        await sleep(1200);

        // ── 综合拦截：捕获51job的真实下载机制 ─────────────────────────
        // 51job 可能使用以下任意机制触发下载（按优先级排序）：
        //   1. URL.createObjectURL(blob)  → 直接持有blob，无需二次fetch
        //   2. <a download>.click()       → 动态创建锚元素后程序性点击
        //   3. XMLHttpRequest             → XHR请求下载URL
        //   4. window.fetch              → fetch API 请求下载URL
        //   5. window.open               → 打开新窗口下载
        // 拦截器必须在主世界（MAIN world）运行才能影响51job的原型链；
        // 隔离世界（Isolated world，默认）的 prototype patch 对页面 JS 完全无效。
        const result = await execInTabMain(tabIdBd, async () => {
          let capturedBlob      = null;  // 优先级1：直接blob（URL.createObjectURL）
          let capturedAnchorUrl = null;  // 优先级2：锚元素href（first-wins）
          let capturedXHRUrl    = null;  // 优先级3：XHR URL（last-wins，排除后台轮询）
          let capturedFetchUrl  = null;  // 优先级4：fetch URL（last-wins）

          // ── 拦截1：URL.createObjectURL ────────────────────────────
          const origCOU = URL.createObjectURL;
          URL.createObjectURL = function(obj) {
            if (obj instanceof Blob && obj.size > 100) capturedBlob = obj;
            return origCOU.call(URL, obj);
          };

          // ── 拦截2a：document.createElement('a')（动态创建的锚元素）──
          // 51job最常见模式：const a=createElement('a'); a.href=url; a.click();
          const _origAnchorProto = HTMLAnchorElement.prototype.click;
          const origCE = document.createElement.bind(document);
          document.createElement = function(tagName) {
            const el = origCE(tagName);
            if (typeof tagName === 'string' && tagName.toLowerCase() === 'a') {
              el.click = function() {
                const h = el.href || '';
                if (h && !h.startsWith('javascript:') && !h.startsWith('#') && !h.startsWith('data:')) {
                  capturedAnchorUrl = capturedAnchorUrl || h;
                  return; // 阻止浏览器直接下载，由我们接管
                }
                _origAnchorProto.call(el);
              };
            }
            return el;
          };

          // ── 拦截2b：HTMLAnchorElement.prototype.click（已存在的锚元素）──
          const origAnchorClick = HTMLAnchorElement.prototype.click;
          HTMLAnchorElement.prototype.click = function() {
            const h = this.href || '';
            if (h && !h.startsWith('javascript:') && !h.startsWith('#') && !h.startsWith('data:') &&
                (this.hasAttribute('download') || /\.(pdf|doc|docx)$/i.test(h) ||
                 /download|resume/i.test(h))) {
              capturedAnchorUrl = capturedAnchorUrl || h;
              return; // 阻止浏览器直接下载
            }
            origAnchorClick.call(this);
          };

          // ── 拦截3：XMLHttpRequest（过滤掉明显的后台心跳请求）──────
          const origXO = XMLHttpRequest.prototype.open;
          XMLHttpRequest.prototype.open = function(method, url) {
            if (url && typeof url === 'string' &&
                !/poll|heartbeat|ping|alive|status|check/i.test(url)) {
              capturedXHRUrl = url; // last-wins：下载请求在心跳之后
            }
            return origXO.apply(this, arguments);
          };

          // ── 拦截4：window.fetch ────────────────────────────────────
          const origFetch = window.fetch;
          window.fetch = function(input, init) {
            const url = typeof input === 'string' ? input
                      : (input instanceof Request ? input.url : '');
            if (url && !/poll|heartbeat|ping|alive|status|check/i.test(url)) {
              capturedFetchUrl = url;
            }
            return origFetch.apply(this, arguments);
          };

          // ── 拦截5：window.open ────────────────────────────────────
          const origWO = window.open;
          window.open = url => {
            if (url) capturedAnchorUrl = capturedAnchorUrl || String(url);
            return null;
          };

          function restoreAll() {
            URL.createObjectURL = origCOU;
            document.createElement = origCE;
            HTMLAnchorElement.prototype.click = origAnchorClick;
            XMLHttpRequest.prototype.open = origXO;
            window.fetch = origFetch;
            window.open = origWO;
          }

          // ── 查找下载按钮（宽松匹配：含"下载"二字或 class 含 download）──
          const allVisible = Array.from(document.querySelectorAll(
            'button, a, [role="button"], [class*="download"], [class*="Download"], [class*="btn"]'
          )).filter(el => el.offsetParent !== null);

          const mainBtn = allVisible.find(el => {
            const t = el.textContent.trim();
            const c = (el.className || '').toLowerCase();
            return t.includes('下载') || c.includes('download');
          });

          if (!mainBtn) {
            restoreAll();
            const labels = allVisible.slice(0, 10)
              .map(e => `"${e.textContent.trim().slice(0, 15)}"`)
              .filter(Boolean).join(', ');
            return { ok: false, error: `未找到下载按钮（页面可见元素: [${labels || '无'}]，请确认已登录51job且页面已完全加载）` };
          }

          mainBtn.click();

          // ── 等待下拉菜单出现（轮询最多3s，每200ms检查一次）──────
          let pdfItem = null;
          for (let t = 0; t < 15; t++) {
            await new Promise(r => setTimeout(r, 200));
            pdfItem = Array.from(document.querySelectorAll(
              'li, a, button, [role="option"], [role="menuitem"], ' +
              '[class*="item"], [class*="option"], [class*="type"], [class*="format"]'
            )).find(el => el.offsetParent !== null &&
              ['PDF', 'pdf'].some(l => el.textContent.trim().includes(l)));
            if (pdfItem) break;
          }

          const pdfLabel = pdfItem
            ? `已点击"${pdfItem.textContent.trim().slice(0, 20)}"`
            : '未找到PDF选项（可能直接触发下载）';

          if (pdfItem) pdfItem.click();

          // ── 等待下载触发（最多5s，任意捕获即立即退出）──────
          for (let t = 0; t < 10; t++) {
            await new Promise(r => setTimeout(r, 500));
            if (capturedBlob || capturedAnchorUrl || capturedXHRUrl || capturedFetchUrl) break;
          }

          restoreAll();

          // ── 优先级1：直接持有的blob ───────────────────────────────
          if (capturedBlob && capturedBlob.size > 100) {
            return new Promise(resolve => {
              const reader = new FileReader();
              reader.onload  = () => resolve({ type: 'blob', data: reader.result.split(',')[1] });
              reader.onerror = () => resolve({ ok: false, error: `读取Blob失败 (${pdfLabel})` });
              reader.readAsDataURL(capturedBlob);
            });
          }

          // ── 优先级2-4：返回 URL，让后台脚本用 chrome.downloads 下载 ──
          // 不在页面上下文内 fetch，避免 CDN CORS 限制
          const capturedUrl = capturedAnchorUrl || capturedXHRUrl || capturedFetchUrl;
          if (!capturedUrl) {
            return { ok: false, error: `URL未捕获 (${pdfLabel}，请确认账号有简历下载权限并已重新登录51job)` };
          }

          // blob: URL 必须在页面内读取，不能跨上下文
          if (capturedUrl.startsWith('blob:')) {
            try {
              const r = await fetch(capturedUrl);
              if (!r.ok) return { ok: false, error: `BlobURL HTTP ${r.status}` };
              const b = await r.blob();
              return new Promise(resolve => {
                const reader = new FileReader();
                reader.onload  = () => resolve({ type: 'blob', data: reader.result.split(',')[1] });
                reader.onerror = () => resolve({ ok: false, error: 'BlobURL读取失败' });
                reader.readAsDataURL(b);
              });
            } catch(e) { return { ok: false, error: `BlobURL访问失败: ${e.message}` }; }
          }

          // https: URL —— 直接返回给后台脚本，由 chrome.downloads 下载
          // chrome.downloads 走浏览器原生下载，携带 session cookie，无 CORS 限制
          return { type: 'url', url: capturedUrl };

        }).catch(e => ({ ok: false, error: `脚本注入异常: ${String(e)}` }));

        // ── 根据捕获结果触发本机保存 ──────────────────────────────────
        const safeCand = _sanitize(name, 20);
        const rid8     = String(resume_id).slice(-8);
        const filename = `${folder}/${safeJob}/${safeCand}_${rid8}.pdf`;

        let dlUrl   = null;
        let itemErr = null;

        if (result?.type === 'blob' && result.data) {
          // Blob 数据：转为 data URL 触发 chrome.downloads
          dlUrl = `data:application/pdf;base64,${result.data}`;
        } else if (result?.type === 'url' && result.url) {
          // HTTPS URL：直接交给浏览器下载（带 session cookie，无 CORS 问题）
          dlUrl = result.url;
        } else {
          itemErr = result?.error || '返回值异常';
        }

        if (dlUrl) {
          const dlId = await new Promise(resolve => {
            chrome.downloads.download({ url: dlUrl, filename, saveAs: false }, id => {
              resolve((chrome.runtime.lastError || id == null) ? null : id);
            });
          });
          if (dlId != null) {
            dlDone++;
          } else {
            dlFailed++;
            itemErr = chrome.runtime.lastError?.message || '本地保存失败（chrome.downloads 报错）';
          }
        }

        if (itemErr) {
          dlFailed++;
          // 上报单项失败原因，让用户在进度 badge 中看到具体错误
          await apiFetch(`${cfg.url}/api/agent/progress`, 'POST', {
            device_id: cfg.id, device_name: cfg.name,
            job_name: jobName, phase: 'downloading',
            current: dlDone, total: items.length, failed: dlFailed,
            message: `⚠ ${name}: ${itemErr}`,
            batch_id: batchId,
          });
        }
      }
    } finally {
      stopKeepAlive();
    }

    // 最终上报完成（dl_done）或错误（dl_error）
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
