/* ============================================================
   华阳精机简历筛选系统 - 通用客户端逻辑 v2
   主要交互在 triage.html 内联脚本中实现，本文件仅保留公共工具。
   ============================================================ */

// 监听 base 页面的旧式 .btn-action（兼容数据库视图等场景）
document.addEventListener("click", async (e) => {
  if (!e.target.classList.contains("btn-action")) return;
  const btn = e.target;
  const card = btn.closest(".card");
  if (!card) return;
  const evalId = card.dataset.evalId;
  const action = btn.dataset.action;
  if (!evalId || !action) return;

  btn.disabled = true;
  try {
    const resp = await fetch("/api/outcome", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({evaluation_id: evalId, action})
    });
    const data = await resp.json();
    if (data.ok) {
      card.querySelectorAll(".btn-action").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
    } else {
      alert("操作失败：" + (data.message || ""));
    }
  } catch (err) {
    alert("网络错误：" + err.message);
  } finally {
    btn.disabled = false;
  }
});
