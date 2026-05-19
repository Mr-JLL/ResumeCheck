'use strict';
// ── 共享简历卡片组件 resume_card.js ─────────────────────────────
// 用于猎头助手、SQL查询等页面，渲染与驾驶舱右栏一致的可折叠卡片。
// cockpit.html 保持自有渲染逻辑不变，本文件不影响驾驶舱。

(function (global) {

  const ESC = s => s == null ? '' : String(s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

  const ACTION_LBL = {
    approved:    '✓ 已通过',
    disapproved: '✗ 已不通过',
    contacted:   '联系中',
    interviewed: '面试中',
    hired:       '已录用',
  };

  // 标准字段集合，用于从 SQL 结果中过滤额外字段
  const STD_KEYS = new Set([
    'id', 'resume_id', 'name', 'age', 'first_degree', 'school', 'major',
    'english_level', 'total_years', 'verdict', 'matches', 'mismatches',
    'verdict_reason', 'match_reason', 'similarity', 'cross_jobs',
    'latest_action', 'evaluation_id', 'candidate_id', 'structured_json',
    'raw_html_path', 'created_at', 'updated_at',
  ]);

  // ── 渲染单张卡片 ────────────────────────────────────────────────
  // opts: { jobName, showActions, uidPrefix }
  function render(c, opts) {
    opts = opts || {};
    const prefix  = opts.uidPrefix || 'rc';
    const uid     = prefix + '-' + (c.evaluation_id || c.resume_id || c.id
                    || Math.random().toString(36).slice(2, 8));
    const verdict = c.verdict || '';
    const acted   = ACTION_LBL[c.latest_action] || '';
    const isActed = !!c.latest_action && c.latest_action !== 'skipped';

    // 相似度徽章
    const simBadge = c.similarity != null
      ? `<span class="rc-sim">${Math.round(c.similarity * 100)}%</span>` : '';

    // 匹配/不匹配条目
    const mHtml = (c.matches || []).map(m =>
      `<li><b>${ESC(m['条件'] || '')}</b>${m['条件'] && m['证据'] ? '：' : ''}${ESC(m['证据'] || '')}</li>`
    ).join('');
    const nmHtml = (c.mismatches || []).map(m =>
      `<li><b>${ESC(m['条件'] || '')}</b>${m['条件'] && m['原因'] ? '：' : ''}${ESC(m['原因'] || '')}</li>`
    ).join('');

    // 跨岗位评估
    const crossHtml = (c.cross_jobs || []).slice(0, 5).map(cj =>
      `<span class="rc-cross vd-${ESC(cj.verdict)}">${ESC(cj.job_name)}：${ESC(cj.verdict || '')}</span>`
    ).join('');

    // 操作按钮（仅当有 evaluation_id 且调用方要求显示时）
    let actHtml = '';
    if (opts.showActions && c.evaluation_id && opts.jobName) {
      const safeJn = ESC(opts.jobName);
      const encJn  = encodeURIComponent(opts.jobName);
      actHtml = `
      <div class="cc-acts">
        <button class="act-ok"  ${isActed ? 'disabled' : ''}
                onclick="rcAct(${c.evaluation_id},'approved',this,'${safeJn}')">通过</button>
        <button class="act-no"  ${isActed ? 'disabled' : ''}
                onclick="rcAct(${c.evaluation_id},'disapproved',this,'${safeJn}')">不通过</button>
        <button class="act-go"
                onclick="window.open('/triage/${encJn}?start_id=${c.evaluation_id}','_blank')">处理台</button>
      </div>`;
    }

    // SQL 额外字段（不在标准集合中的列）
    const extras = Object.entries(c)
      .filter(([k, v]) => !STD_KEYS.has(k) && v != null && String(v).trim() !== '')
      .map(([k, v]) => `<span class="rc-extra"><b>${ESC(k)}</b>：${ESC(String(v).slice(0, 150))}</span>`)
      .join('');

    return `
    <div class="cc rc-card ${ESC(verdict)}" id="${uid}">
      <div class="cc-head" onclick="rcToggle('${uid}')">
        <span class="rc-arrow" id="arr-${uid}">▶</span>
        <span class="cc-name">${ESC(c.name || '未知')}</span>
        ${c.age         ? `<span class="cc-meta">${ESC(String(c.age))}岁</span>`         : ''}
        ${c.first_degree? `<span class="cc-meta">${ESC(c.first_degree)}</span>`           : ''}
        ${c.school      ? `<span class="cc-meta">${ESC(c.school)}</span>`                : ''}
        ${c.total_years ? `<span class="cc-meta" style="color:#999">${ESC(String(c.total_years))}年</span>` : ''}
        ${verdict       ? `<span class="cc-verdict vd-${ESC(verdict)}">${ESC(verdict)}</span>` : ''}
        ${simBadge}
        ${acted         ? `<span class="cc-action-lbl">${acted}</span>`                  : ''}
      </div>
      <div class="cc-body" id="body-${uid}" style="display:none">
        ${mHtml  ? `<ul class="ev-list">${mHtml}</ul>`      : ''}
        ${nmHtml ? `<ul class="ev-list neg">${nmHtml}</ul>` : ''}
        ${c.verdict_reason ? `<div class="cc-reason">${ESC(c.verdict_reason)}</div>`          : ''}
        ${c.match_reason   ? `<div class="cc-reason">推荐理由：${ESC(c.match_reason)}</div>`  : ''}
        ${crossHtml ? `<div class="rc-cross-wrap">${crossHtml}</div>`                         : ''}
        ${(c.major || c.english_level) ? `<div class="cc-reason" style="font-style:normal">
          ${c.major        ? `专业：${ESC(c.major)}`        : ''}
          ${c.major && c.english_level ? ' · ' : ''}
          ${c.english_level ? `英语：${ESC(c.english_level)}` : ''}
        </div>` : ''}
        ${extras ? `<div class="rc-extras">${extras}</div>` : ''}
        ${actHtml}
      </div>
    </div>`;
  }

  // ── 折叠 / 展开 ────────────────────────────────────────────────
  function toggle(uid) {
    const body = document.getElementById('body-' + uid);
    const arr  = document.getElementById('arr-'  + uid);
    if (!body) return;
    const isOpen = body.style.display !== 'none';
    body.style.display = isOpen ? 'none' : 'block';
    if (arr) arr.textContent = isOpen ? '▶' : '▼';
  }

  // ── 操作按钮回调 ───────────────────────────────────────────────
  async function doAct(evalId, action, btn, jobName) {
    btn.disabled = true;
    try {
      const r = await fetch('/api/triage/action', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ evaluation_id: evalId, action, job_name: jobName }),
      });
      const d = await r.json();
      if (!d.ok) { btn.disabled = false; alert(d.message || '操作失败'); }
    } catch (e) {
      btn.disabled = false;
      alert('网络错误：' + e.message);
    }
  }

  // ── 候选人结果检测（用于 SQL 查询结果）──────────────────────────
  // 当 SQL 结果列中含有 name + 至少一个候选人特征字段时，认为是候选人数据
  function isCandidateLike(rows) {
    if (!rows || !rows.length) return false;
    const keys = Object.keys(rows[0]).map(k => k.toLowerCase());
    return keys.includes('name') &&
      (keys.includes('age') || keys.includes('first_degree') ||
       keys.includes('total_years') || keys.includes('school') ||
       keys.includes('resume_id'));
  }

  // ── 对外导出 ──────────────────────────────────────────────────
  global.ResumeCard = { render, toggle, isCandidateLike };

  // 供 HTML onclick 属性直接调用的全局函数
  global.rcToggle = uid => toggle(uid);
  global.rcAct    = (evalId, action, btn, jobName) => doAct(evalId, action, btn, jobName);

})(window);
