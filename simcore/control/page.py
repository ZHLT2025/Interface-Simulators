"""Minimal manual control page (V1.1 §1 页面必须表达的操作 / §6 仅保留必要技术状态).

One static HTML document, no framework, no build step. It shows only the technical state the
frozen design allows (mode, state, progress, B stop point, last technical result, known-rejection
count, necessary error text) and offers exactly one start button.

Every button that changes state goes through the two-phase gate:
    click -> native confirm() dialog -> (only on OK) POST /_ctl/<op>/request
          -> POST /_ctl/<op>/confirm {challenge_id}
Cancel sends nothing at all, so a cancelled dialog cannot change anything. The admin token is
asked for once and kept in sessionStorage; the page is additionally restricted by source address
(simcore/ipguard.py treats /_ctl like /_sim).
"""
from __future__ import annotations

_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>A/B 模拟器人工单轮控制</title>
<style>
 body{font:14px/1.6 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;margin:0;padding:18px;background:#f6f7f9;color:#1d2129}
 h1{font-size:18px;margin:0 0 4px} .sub{color:#6b7280;font-size:12px;margin-bottom:14px}
 .card{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:14px;margin-bottom:12px}
 .row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
 button{font:inherit;padding:7px 14px;border-radius:6px;border:1px solid #c9ced6;background:#fff;cursor:pointer}
 button.primary{background:#1d4ed8;border-color:#1d4ed8;color:#fff;font-weight:600}
 button:disabled{opacity:.45;cursor:not-allowed}
 .state{font-size:22px;font-weight:700} .muted{color:#6b7280}
 table{border-collapse:collapse;width:100%;font-size:13px} td,th{border-bottom:1px solid #eef0f3;padding:4px 6px;text-align:left;vertical-align:top}
 pre{background:#0f172a;color:#d7e0f0;padding:10px;border-radius:6px;overflow:auto;max-height:260px;font-size:12px}
 .err{background:#fef2f2;border:1px solid #fecaca;color:#991b1b;padding:8px;border-radius:6px;white-space:pre-wrap}
 .warn{background:#fffbeb;border:1px solid #fde68a;color:#92400e;padding:8px;border-radius:6px}
 .pill{display:inline-block;padding:1px 8px;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:12px}
 label{margin-right:10px}
</style></head><body>
<h1>A/B 模拟器人工单轮控制</h1>
<div class="sub">仅模拟器自身的技术运行状态。不生成业务测试报告、截图档案、问题工单或通过/失败判定；发送计划完成不代表业务测试通过或平台终点成立。</div>

<div class="card">
  <div class="row">
    <span class="muted">模式</span>
    <label><input type="radio" name="mode" value="A" id="mA"> A（1:240 连续）</label>
    <label><input type="radio" name="mode" value="B" id="mB"> B（1:60，62 停点逐点确认）</label>
    <button id="btn-mode">切换模式</button>
    <span class="muted">切换只改选择，不启动本轮</span>
  </div>
</div>

<div class="card">
  <div class="row" style="justify-content:space-between">
    <div><div class="muted">当前状态</div><div class="state" id="state">…</div></div>
    <div><div class="muted">轮次</div><div id="runid" class="pill">—</div></div>
    <div><div class="muted">进度</div><div id="progress">—</div></div>
    <div><div class="muted">已知类型拒绝</div><div id="known">—</div></div>
  </div>
  <div class="row" style="margin-top:12px">
    <button class="primary" id="btn-init">初始化启动（一次点击）</button>
    <button id="btn-pause">暂停</button>
    <button id="btn-resume">继续</button>
    <button id="btn-stop">停止本轮</button>
    <button id="btn-clear">登记未知结果已核清</button>
    <button id="btn-token">设置管理令牌</button>
  </div>
</div>

<div class="card" id="sp-card" style="display:none">
  <div class="muted">当前 B 停点（最后停点也需确认；普通继续不能代替当前停点确认）</div>
  <div id="sp"></div>
  <div class="row" style="margin-top:8px"><button class="primary" id="btn-confirm">确认当前停点并继续</button></div>
</div>

<div class="card"><div class="muted">最后技术结果</div><div id="last">—</div>
  <div id="error" style="margin-top:8px"></div></div>

<div class="card"><div class="muted">计划与目标</div><table id="plan"></table></div>
<div class="card"><div class="muted">原始状态（只读）</div><pre id="raw">…</pre></div>

<script>
const TOKEN_KEY = "sim_admin_token";
function token(){ let t = sessionStorage.getItem(TOKEN_KEY); if(!t){ t = prompt("X-Sim-Admin-Token"); if(t) sessionStorage.setItem(TOKEN_KEY, t); } return t || ""; }
async function api(path, body){
  const r = await fetch(path, {method: body===undefined?"GET":"POST", headers:{"Content-Type":"application/json","X-Sim-Admin-Token":token()},
                              body: body===undefined?undefined:JSON.stringify(body)});
  const text = await r.text(); let data=null; try{ data = JSON.parse(text); }catch(e){ data = {raw:text}; }
  if(!r.ok){ const e = new Error("HTTP "+r.status); e.data = data; throw e; }
  return data;
}
/* two-phase: the dialog is shown BEFORE any request is sent; Cancel sends nothing */
async function op(name, params, question){
  if(!window.confirm(question + "\\n\\n确定执行？（取消则不做任何改变）")) return;
  try{
    const req = await api("/_ctl/"+name+"/request", params||{});
    const ok = window.confirm("二次确认：" + req.summary.effect + "\\n确认码 " + req.challenge_id + " 有效 " + req.expires_in_s + " 秒。\\n\\n确认执行？");
    if(!ok){ await api("/_ctl/"+name+"/cancel", {challenge_id: req.challenge_id}); await refresh(); return; }
    await api("/_ctl/"+name+"/confirm", Object.assign({challenge_id: req.challenge_id}, params||{}));
  }catch(e){ alert("操作未被接受：\\n" + JSON.stringify(e.data, null, 1)); }
  await refresh();
}
function esc(s){ return String(s===undefined||s===null?"—":s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
async function refresh(){
  let s; try{ s = await api("/_ctl/status"); }catch(e){ document.getElementById("raw").textContent = JSON.stringify(e.data,null,1); return; }
  document.getElementById("state").textContent = s.state_display + " (" + s.state + ")";
  document.getElementById("runid").textContent = s.run_id || "—";
  const p = s.progress || {};
  document.getElementById("progress").textContent = (p.attempted||0) + " / " + (p.total||0) +
      (s.selected_mode==="B" ? "，停点 " + (s.confirmed_stop_points||0) + "/" + (s.expected_stop_points||0) : "");
  document.getElementById("known").textContent = (s.counts||{}).known_rejected ?? "—";
  document.getElementById("mA").checked = s.selected_mode === "A";
  document.getElementById("mB").checked = s.selected_mode === "B";
  const a = s.allowed_ops || {};
  document.getElementById("btn-init").disabled = !a.init_start;
  document.getElementById("btn-pause").disabled = !a.pause;
  document.getElementById("btn-resume").disabled = !a.resume;
  document.getElementById("btn-stop").disabled = !a.stop;
  document.getElementById("btn-mode").disabled = !a.select_mode;
  document.getElementById("btn-clear").disabled = !a.clear_unknown;
  const sp = s.stop_point;
  document.getElementById("sp-card").style.display = sp ? "block" : "none";
  if(sp){ document.getElementById("sp").innerHTML =
      "<b>第 " + esc(sp.sequence) + " / " + esc(sp.total_stop_points) + " 停点</b>（阶段 " + esc(sp.name) + "，标识 " + esc(sp.label) +
      "）<br>停点依据：" + esc(JSON.stringify(sp.stop_evidence)) + "<br>业务区间：" + esc(sp.business_interval_start_utc) + " → " + esc(sp.business_interval_end_utc) +
      "<br>本段事件：" + esc(sp.event_count) + "，已完成事件：" + esc(sp.events_completed) +
      "<br>stop_point_id：<code>" + esc(sp.stop_point_id) + "</code>，confirm_seq：" + esc(sp.expected_confirm_seq); }
  const lr = s.last_result;
  document.getElementById("last").innerHTML = lr ? ("事件 " + esc(lr.event_id) + "（" + esc(lr.event_type) + "）→ <b>" + esc(lr.outcome) +
      "</b> / " + esc(lr.reason) + "，HTTP " + esc(lr.http_status) + "，" + esc(lr.attempt_at)) : "—";
  let e = "";
  if(s.error) e += '<div class="err">' + esc(JSON.stringify(s.error, null, 1)) + "</div>";
  if(s.requires_manual_review) e += '<div class="warn">需人工核查：在途/未知结果未核清前不能再次初始化启动。</div>';
  if(s.completed_with_known_rejections) e += '<div class="warn">发送计划完成，含已知类型拒绝。</div>';
  document.getElementById("error").innerHTML = e;
  const pl = s.plan || {}; const tg = s.target || {};
  document.getElementById("plan").innerHTML = Object.entries({
      "模式": s.selected_mode, "剧本": pl.scenario, "数据集": s.dataset, "速度": pl.speed_ratio,
      "事件数": pl.events, "停点数": pl.stop_points, "原类型事件": pl.soundproof_events,
      "获准目标": tg.approved_target_url, "CV_TARGET_URL": tg.cv_target_url, "readyz": tg.readyz_url,
      "期望数据域": tg.expect_data_domain, "运行锁": JSON.stringify(s.run_lock)
  }).map(([k,v]) => "<tr><th>"+esc(k)+"</th><td>"+esc(v)+"</td></tr>").join("");
  document.getElementById("raw").textContent = JSON.stringify(s, null, 1);
  window.__s = s;
}
document.getElementById("btn-token").onclick = () => { sessionStorage.removeItem(TOKEN_KEY); token(); refresh(); };
document.getElementById("btn-mode").onclick = () => {
  const m = document.getElementById("mB").checked ? "B" : "A";
  op("select_mode", {mode: m}, "切换模式为 " + m + "：只改变选择，不会自动启动本轮。");
};
document.getElementById("btn-init").onclick = () => op("init_start", {},
  "初始化启动：模拟器将核对数据集/场景/目标身份，重置发送进度到计划第一项（B 清除上一轮确认状态），随后开始该模式的一轮。\\n不撤回已发事件，不清空任何平台数据。");
document.getElementById("btn-pause").onclick = () => op("pause", {}, "暂停后续计划推进。已在途的发送仍等待实际技术结果。");
document.getElementById("btn-resume").onclick = () => op("resume", {}, "沿原剩余偏移继续。不会跨过 B 停点。");
document.getElementById("btn-stop").onclick = () => op("stop", {}, "停止本轮后续计划。不撤回已发事件，不清空平台，不自动重播。");
document.getElementById("btn-clear").onclick = () => op("clear_unknown", {note: prompt("核清说明（留痕用，可留空）") || ""},
  "登记在途/未知结果已由技术人员核清。实际技术结果留痕保留，不删除。");
document.getElementById("btn-confirm").onclick = () => {
  const s = window.__s || {}; const sp = s.stop_point;
  if(!sp){ alert("当前没有待确认的停点"); return; }
  op("confirm_stop_point", {stop_point_id: sp.stop_point_id, run_id: sp.run_id, confirm_seq: sp.expected_confirm_seq},
     "确认第 " + sp.sequence + "/" + sp.total_stop_points + " 停点（" + sp.label + "）并继续下一段。\\n确认只代表允许继续，不代表业务测试通过。");
};
refresh(); setInterval(refresh, 2000);
</script></body></html>
"""


def render_page() -> str:
    return _PAGE
