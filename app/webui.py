"""网页 UI 的单页界面（HTML + CSS + JS 全部内联，不引入任何前端依赖）。

放在单独的模块里，方便以后换皮而不动服务端逻辑。
风格取向：深色、信息密度高、手机上也能看。
"""

from __future__ import annotations

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RSS Media Hub</title>
<style>
  /* ---------- 设计变量：深色为主，支持浅色模式 ---------- */
  :root{
    --bg:#0b0d12; --bg-soft:#0f1218; --panel:#151922; --panel-2:#1c2230;
    --line:#252b39; --line-soft:#1e2430;
    --fg:#e8ecf4; --fg-dim:#9aa3b8; --fg-faint:#6b7488;
    --accent:#5b8cff; --accent-soft:rgba(91,140,255,.13);
    --ok:#3fb950; --ok-soft:rgba(63,185,80,.13);
    --warn:#d9a03a; --warn-soft:rgba(217,160,58,.13);
    --err:#f4564e; --err-soft:rgba(244,86,78,.13);
    --radius:14px; --radius-sm:9px;
    --shadow:0 1px 2px rgba(0,0,0,.3), 0 8px 24px -12px rgba(0,0,0,.6);
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
    color-scheme:dark;
  }
  html[data-theme="light"]{
    --bg:#f4f6fa; --bg-soft:#eef1f7; --panel:#ffffff; --panel-2:#f4f6fa;
    --line:#dfe4ee; --line-soft:#e9edf5;
    --fg:#151a24; --fg-dim:#5c6579; --fg-faint:#8d95a8;
    --accent:#2f6bec; --accent-soft:rgba(47,107,236,.09);
    --ok:#1a7f37; --ok-soft:rgba(26,127,55,.1);
    --warn:#9a6700; --warn-soft:rgba(154,103,0,.1);
    --err:#cf222e; --err-soft:rgba(207,34,46,.1);
    --shadow:0 1px 2px rgba(16,24,40,.06), 0 8px 24px -14px rgba(16,24,40,.18);
    color-scheme:light;
  }

  *{box-sizing:border-box}
  html,body{height:100%}
  body{
    margin:0;background:var(--bg);color:var(--fg);
    font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
    -webkit-font-smoothing:antialiased;
    font-variant-numeric:tabular-nums;
  }
  a{color:var(--accent);text-decoration:none;border-bottom:1px solid transparent;transition:border-color .15s}
  a:hover{border-bottom-color:var(--accent)}
  code{background:var(--panel-2);border:1px solid var(--line-soft);padding:1px 6px;
    border-radius:6px;font-size:12.5px;font-family:var(--mono);color:var(--fg)}
  ::selection{background:var(--accent-soft)}

  /* ---------- 顶栏 ---------- */
  header{
    position:sticky;top:0;z-index:20;
    background:color-mix(in srgb,var(--bg) 86%,transparent);
    backdrop-filter:saturate(180%) blur(14px);
    -webkit-backdrop-filter:saturate(180%) blur(14px);
    border-bottom:1px solid var(--line);
    padding:11px 20px;display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  }
  header h1{font-size:15px;margin:0;font-weight:650;letter-spacing:.2px;white-space:nowrap}
  .ver{color:var(--fg-faint);font-size:12px;font-family:var(--mono)}
  nav{display:flex;gap:4px;margin-left:auto;flex-wrap:wrap;align-items:center}
  nav button{
    position:relative;background:transparent;border:1px solid transparent;color:var(--fg-dim);
    padding:6px 14px;border-radius:999px;cursor:pointer;font-size:13px;font-weight:500;
    transition:background .15s,color .15s,border-color .15s;
  }
  nav button:hover{background:var(--panel-2);color:var(--fg)}
  nav button.on{background:var(--accent);border-color:var(--accent);color:#fff;box-shadow:0 2px 10px -2px var(--accent)}
  .icon-btn{background:transparent;border:1px solid var(--line);color:var(--fg-dim);
    width:32px;height:32px;border-radius:9px;cursor:pointer;font-size:14px;line-height:1;
    display:inline-flex;align-items:center;justify-content:center;transition:all .15s}
  .icon-btn:hover{color:var(--fg);border-color:var(--accent)}

  main{padding:20px;max-width:1200px;margin:0 auto;animation:fade .25s ease}
  @keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

  /* ---------- 卡片 / 栅格 ---------- */
  .grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(232px,1fr))}
  .card{
    background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
    padding:16px 18px;box-shadow:var(--shadow);
  }
  .card h2{
    margin:0 0 12px;font-size:12px;color:var(--fg-dim);font-weight:650;
    text-transform:uppercase;letter-spacing:.08em;
  }
  .card.tight{padding:13px 15px}
  .kpi{font-size:27px;font-weight:650;letter-spacing:-.01em;line-height:1.25}
  .kpi small{font-size:13px;color:var(--fg-dim);font-weight:450;margin-left:5px;letter-spacing:0}
  .row{display:flex;justify-content:space-between;gap:12px;padding:3.5px 0;font-size:13px}
  .row>span:first-child{color:var(--fg-dim)}
  .row>span:last-child{text-align:right}
  .muted{color:var(--fg-dim)}
  .faint{color:var(--fg-faint)}
  .mono{font-family:var(--mono);font-size:12.5px}

  /* ---------- 徽标 ---------- */
  .pill{
    display:inline-block;padding:1.5px 9px;border-radius:999px;font-size:12px;
    border:1px solid var(--line);background:var(--panel-2);color:var(--fg-dim);white-space:nowrap;
  }
  .pill.ok{color:var(--ok);border-color:var(--ok);background:var(--ok-soft)}
  .pill.warn{color:var(--warn);border-color:var(--warn);background:var(--warn-soft)}
  .pill.err{color:var(--err);border-color:var(--err);background:var(--err-soft)}
  .pill.accent{color:var(--accent);border-color:var(--accent);background:var(--accent-soft)}
  .chips{display:flex;gap:5px;flex-wrap:wrap}
  .chip{font-size:11.5px;padding:1px 7px;border-radius:6px;background:var(--panel-2);
    border:1px solid var(--line-soft);color:var(--fg-dim);font-family:var(--mono)}
  .chip.hi{color:var(--accent);border-color:var(--accent);background:var(--accent-soft)}

  /* ---------- 表格 ---------- */
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line-soft);vertical-align:middle}
  th{color:var(--fg-faint);font-weight:600;font-size:11.5px;text-transform:uppercase;
    letter-spacing:.06em;white-space:nowrap}
  tbody tr{transition:background .12s}
  tbody tr:hover{background:var(--bg-soft)}
  tr:last-child td{border-bottom:none}
  .num{font-family:var(--mono);font-size:12.5px;white-space:nowrap}

  /* ---------- 进度条 ---------- */
  .bar{height:6px;border-radius:99px;background:var(--panel-2);overflow:hidden;min-width:64px;
    border:1px solid var(--line-soft)}
  .bar>i{display:block;height:100%;background:var(--accent);border-radius:99px;transition:width .4s ease}
  .bar>i.done{background:var(--ok)}
  .bar-wrap{display:flex;align-items:center;gap:9px}

  /* ---------- 追更卡片（MP 风格） ---------- */
  .sub-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
  .sub-card{display:flex;gap:12px;padding:10px;border:1px solid var(--line-soft);
    border-radius:var(--radius-sm);background:var(--panel-2);transition:border-color .15s}
  .sub-card:hover{border-color:var(--accent)}
  .sub-card.done{opacity:.75}
  .sub-poster{width:74px;min-height:104px;border-radius:8px;overflow:hidden;flex-shrink:0;
    background:linear-gradient(135deg,var(--panel),var(--panel-2));border:1px solid var(--line-soft)}
  .sub-poster img{width:100%;height:100%;object-fit:cover;display:block}
  .sub-poster.noimg::after{content:'🎬';display:flex;align-items:center;justify-content:center;
    height:104px;font-size:26px;opacity:.35}
  .sub-info{flex:1;min-width:0;display:flex;flex-direction:column;justify-content:center}
  .sub-year{font-size:11.5px;color:var(--fg-faint);font-family:var(--mono)}
  .sub-name{font-weight:600;font-size:14.5px;margin:2px 0;overflow:hidden;text-overflow:ellipsis;
    white-space:nowrap}
  .sub-meta{display:flex;align-items:center;gap:6px}

  /* ---------- 按钮 ---------- */
  button.act{
    background:var(--panel-2);border:1px solid var(--line);color:var(--fg);
    padding:5.5px 12px;border-radius:var(--radius-sm);cursor:pointer;font-size:12.5px;
    font-weight:500;transition:all .15s;white-space:nowrap;
  }
  button.act:hover{border-color:var(--accent);color:var(--accent)}
  button.act:active{transform:translateY(1px)}
  button.act.danger:hover{border-color:var(--err);color:var(--err)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  button.primary:hover{filter:brightness(1.08);color:#fff}
  button.ghost{background:transparent}
  button:disabled{opacity:.45;cursor:not-allowed;transform:none!important}

  /* ---------- 表单 ---------- */
  label{display:block;font-size:12px;color:var(--fg-dim);margin:10px 0 5px;font-weight:500}
  label.inline{display:inline-flex;align-items:center;gap:7px;margin:0;cursor:pointer}
  input,select,textarea{
    width:100%;background:var(--panel-2);border:1px solid var(--line);color:var(--fg);
    padding:8.5px 11px;border-radius:var(--radius-sm);font:inherit;font-size:13px;
    transition:border-color .15s,box-shadow .15s;
  }
  input:hover,select:hover,textarea:hover{border-color:var(--fg-faint)}
  input:focus,select:focus,textarea:focus{
    outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft);
  }
  input[type=checkbox]{width:auto;accent-color:var(--accent)}
  select{cursor:pointer;appearance:none;
    background-image:linear-gradient(45deg,transparent 50%,var(--fg-dim) 50%),linear-gradient(135deg,var(--fg-dim) 50%,transparent 50%);
    background-position:calc(100% - 17px) 52%,calc(100% - 12px) 52%;
    background-size:5px 5px,5px 5px;background-repeat:no-repeat;padding-right:34px}
  .f2{display:grid;gap:12px 16px;grid-template-columns:repeat(auto-fit,minmax(248px,1fr))}
  .actions{display:flex;gap:10px;margin-top:16px;flex-wrap:wrap;align-items:center}
  .hint{font-size:12px;color:var(--fg-dim);margin-top:6px;line-height:1.6}
  .hint code{font-size:12px}

  /* ---------- 提示条 ---------- */
  .callout{display:flex;gap:10px;padding:11px 14px;border-radius:var(--radius-sm);
    background:var(--panel-2);border:1px solid var(--line-soft);font-size:12.5px;color:var(--fg-dim);
    line-height:1.6;margin-top:12px}
  .callout.warn{background:var(--warn-soft);border-color:var(--warn);color:var(--fg)}
  .callout.err{background:var(--err-soft);border-color:var(--err);color:var(--fg)}

  /* ---------- Toast ---------- */
  .toast{position:fixed;right:18px;bottom:18px;z-index:99;display:flex;
    flex-direction:column;gap:9px;max-width:min(430px,92vw)}
  .toast div{
    background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--accent);
    padding:11px 15px;border-radius:var(--radius-sm);font-size:13px;box-shadow:var(--shadow);
    animation:toastIn .22s cubic-bezier(.2,.9,.3,1.2);
  }
  .toast div.ok{border-left-color:var(--ok)}
  .toast div.err{border-left-color:var(--err)}
  @keyframes toastIn{from{opacity:0;transform:translateX(14px) scale(.97)}to{opacity:1;transform:none}}

  /* ---------- 弹窗 ---------- */
  dialog{
    background:var(--panel);color:var(--fg);border:1px solid var(--line);
    border-radius:var(--radius);padding:0;max-width:680px;width:94vw;box-shadow:var(--shadow);
  }
  dialog::backdrop{background:rgba(0,0,0,.55);backdrop-filter:blur(3px)}
  dialog .head{padding:16px 19px;border-bottom:1px solid var(--line);font-weight:600;font-size:14px}
  dialog .body{padding:17px 19px;max-height:68vh;overflow:auto}
  dialog .foot{padding:14px 19px;border-top:1px solid var(--line);display:flex;
    justify-content:flex-end;gap:10px;background:var(--bg-soft)}

  /* ---------- 空状态 / 加载 ---------- */
  .empty{color:var(--fg-dim);text-align:center;padding:34px 16px;font-size:13px}
  .empty .big{font-size:26px;display:block;margin-bottom:8px;opacity:.7}
  .spinner{display:inline-block;width:13px;height:13px;border:2px solid var(--line);
    border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;vertical-align:-2px}
  @keyframes spin{to{transform:rotate(360deg)}}
  .skeleton{height:13px;border-radius:6px;background:linear-gradient(90deg,var(--panel-2) 25%,var(--line) 50%,var(--panel-2) 75%);
    background-size:200% 100%;animation:sk 1.3s linear infinite}
  @keyframes sk{to{background-position:-200% 0}}

  /* ---------- 移动端：表格变卡片，不横向滚动 ---------- */
  @media(max-width:760px){
    main{padding:13px}
    header{padding:10px 13px;gap:9px}
    header h1{font-size:14px}
    nav{width:100%;order:3;margin-left:0;justify-content:space-between}
    nav button{flex:1;padding:6px 8px;font-size:12.5px}
    .kpi{font-size:23px}
    table,thead,tbody,tr,td{display:block;width:100%}
    thead{display:none}
    tbody tr{border:1px solid var(--line);border-radius:var(--radius-sm);
      padding:11px 12px;margin-bottom:10px;background:var(--bg-soft)}
    tbody tr:hover{background:var(--bg-soft)}
    td{border:none;padding:3px 0;display:flex;justify-content:space-between;gap:12px;align-items:center}
    td::before{content:attr(data-label);color:var(--fg-faint);font-size:11.5px;
      text-transform:uppercase;letter-spacing:.05em;flex:0 0 auto}
    td:empty{display:none}
    dialog{width:100vw;max-width:100vw;border-radius:var(--radius) var(--radius) 0 0;
      margin:0;position:fixed;bottom:0;top:auto;max-height:92vh}
    .toast{right:10px;left:10px;bottom:10px;max-width:none}
  }
</style>
</head>
<body>
<header>
  <h1>🎬 RSS Media Hub</h1>
  <span class="ver" id="ver"></span>
  <nav>
    <button data-tab="dash" class="on">总览</button>
    <button data-tab="subs">订阅</button>
    <button data-tab="scan">媒体库</button>
    <button data-tab="settings">设置</button>
    <button class="icon-btn" id="themeBtn" title="切换深色 / 浅色">🌙</button>
  </nav>
</header>
<main>
  <section id="tab-dash"></section>
  <section id="tab-subs" hidden></section>
  <section id="tab-scan" hidden></section>
  <section id="tab-settings" hidden></section>
</main>
<div class="toast" id="toast"></div>

<dialog id="dlg">
  <form method="dialog" id="dlgform">
    <div class="head" id="dlgtitle">标题</div>
    <div class="body" id="dlgbody"></div>
    <div class="foot">
      <button class="act" value="cancel">取消</button>
      <button class="act primary" id="dlgok" value="ok">保存</button>
    </div>
  </form>
</dialog>

<script>
/* ---------- 主题切换（记住选择） ---------- */
(function initTheme(){
  const saved = localStorage.getItem('rmh_theme');
  const prefersLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
  const theme = saved || (prefersLight ? 'light' : 'dark');
  document.documentElement.dataset.theme = theme;
})();
function toggleTheme(){
  const now = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = now;
  localStorage.setItem('rmh_theme', now);
  document.getElementById('themeBtn').textContent = now === 'light' ? '☀️' : '🌙';
}

const $ = s => document.querySelector(s);
const api = {
  async call(method, path, body){
    const token = localStorage.getItem('rmh_token') || '';
    const res = await fetch(path, {
      method,
      headers: {'Content-Type':'application/json', ...(token ? {'X-RMH-Token': token} : {})},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch(e){ data = {ok:false, error:'服务器返回了非 JSON 内容'}; }
    if (!res.ok || data.ok === false) {
      const msg = data && data.error ? data.error : ('HTTP ' + res.status);
      throw new Error(msg);
    }
    return data;
  },
  get(p){ return this.call('GET', p); },
  post(p, b){ return this.call('POST', p, b); },
  del(p, b){ return this.call('DELETE', p, b); },
};

function toast(msg, kind){
  const el = document.createElement('div');
  el.className = kind || '';
  el.textContent = msg;
  $('#toast').appendChild(el);
  setTimeout(()=>{ el.style.opacity='0'; el.style.transition='opacity .3s'; }, 4200);
  setTimeout(()=>el.remove(), 4600);
}
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
/* 移动端会把表格变成卡片，靠 data-label 给每个单元格加"字段名" */
const tdL = (label, content, cls) =>
  `<td data-label="${esc(label)}"${cls ? ` class="${cls}"` : ''}>${content}</td>`;
const chips = (arr, highlight) => (arr && arr.length)
  ? `<div class="chips">${arr.map((b, i) => `<span class="chip${highlight && i === 0 ? ' hi' : ''}">${esc(b)}</span>`).join('')}</div>`
  : '';
const pct = (a,b) => b ? Math.min(100, Math.round(a/b*100)) : 0;
function ago(ts){
  if (!ts) return '—';
  const d = Math.max(0, Math.floor(Date.now()/1000 - ts));
  if (d < 60) return d + ' 秒前';
  if (d < 3600) return Math.floor(d/60) + ' 分钟前';
  if (d < 86400) return Math.floor(d/3600) + ' 小时前';
  return Math.floor(d/86400) + ' 天前';
}
const fmtTime = ts => ts ? new Date(ts*1000).toLocaleString('zh-CN', {hour12:false}) : '—';

/* ---------------- 总览 ---------------- */
async function renderDash(){
  const box = $('#tab-dash');
  box.innerHTML = '<div class="empty"><span class="spinner"></span> 加载中…</div>';
  let d;
  try { d = await api.get('/api/dashboard'); }
  catch(e){ box.innerHTML = '<div class="card">加载失败：' + esc(e.message) + '</div>'; return; }
  $('#ver').textContent = 'v' + d.version;
  $('#ver').title = (d.build && d.build.display_full) ? ('构建：' + d.build.display_full) : '';
  if (d.build && d.build.commit) {
    // 带上构建提交，才能一眼确认 pull 到的是哪次构建
    $('#ver').textContent = d.build.display;
  }

  const h = d.health;
  const chip = (ok, label, extra) =>
    `<div class="row"><span>${label}</span><span class="pill ${ok?'ok':'err'}">${ok?'已配置':'未配置'}${extra?(' · '+esc(extra)):''}</span></div>`;

  const subs = d.subscriptions.filter(s => s.enabled);
  const done = subs.filter(s => s.done).length;
  const catching = subs.length - done;
  const missing = subs.reduce((n,s)=> n + (s.missing ? s.missing.split('、').length : 0), 0);
  // 追更中清单（show 模式才有"追"的概念）
  const catchingList = subs.filter(s => !s.done && s.mode === 'show');
  const token = localStorage.getItem('rmh_token') || '';
  const posterSrc = s => '/api/poster?id=' + encodeURIComponent(s.id) + (token ? '&token=' + encodeURIComponent(token) : '');
  // MP 风格订阅卡片
  const subCard = s => {
    const season = s.season ? ` S${String(s.season).padStart(2,'0')}` : '';
    return `<div class="sub-card${s.done?' done':''}">
      <div class="sub-poster"><img src="${posterSrc(s)}" loading="lazy" onerror="this.style.display='none';this.parentNode.classList.add('noimg')" alt=""></div>
      <div class="sub-info">
        <div class="sub-year">${esc(String(s.year || ''))}</div>
        <div class="sub-name" title="${esc(s.name)}">${esc(s.name + season)}</div>
        <div class="bar-wrap" style="margin:6px 0 4px">
          <div class="bar"><i class="${s.done?'done':''}" style="width:${pct(s.owned,s.total)}%"></i></div>
          <span class="num">${s.owned} / ${s.total}</span>
        </div>
        <div class="sub-meta">
          <span class="pill accent" style="font-size:11px">📺 RSS订阅</span>
          <span class="faint" style="font-size:11px;margin-left:auto">${ago(s.last_check)}</span>
        </div>
      </div>
    </div>`;
  };
  const showCards = subs.filter(s => s.mode === 'show');

  box.innerHTML = `
  <div class="grid">
    <div class="card">
      <h2>追更中</h2>
      <div class="kpi">${catching}<small>部</small></div>
      <div class="row"><span>已完成</span><span>${done} 部</span></div>
      <div class="row"><span>仍有缺集</span><span>${missing} 处</span></div>
      ${catchingList.length ? `<div style="margin-top:10px;border-top:1px solid var(--panel-2);padding-top:8px">
        ${catchingList.map(s => `<div class="row" style="font-size:13px">
          <span title="${esc(s.name)}">📺 ${esc(s.name)}${s.season?` S${String(s.season).padStart(2,'0')}`:''}</span>
          <span class="num">${s.owned}/${s.total}</span>
        </div>`).join('')}
      </div>` : ''}
    </div>
    <div class="card">
      <h2>运行状态</h2>
      <div class="row"><span>已运行</span><span>${Math.floor(d.uptime_seconds/3600)} 小时 ${Math.floor(d.uptime_seconds%3600/60)} 分</span></div>
      <div class="row"><span>RSS 轮询</span><span>${d.stats.polls} 次 · ${ago(d.stats.last_poll_at)}</span></div>
      <div class="row"><span>入库巡检</span><span>${d.stats.reconcile_runs} 次 · ${ago(d.stats.last_reconcile_at)}</span></div>
      <div class="row"><span>已推送</span><span>${d.stats.notifications} 条</span></div>
      <div class="row"><span>抓取失败</span><span>${d.stats.feeds_failed} 次</span></div>
    </div>
    <div class="card">
      <h2>服务连通性</h2>
      ${chip(h.telegram, 'Telegram', h.proxy_tg ? '走代理' : '')}
      ${chip(h.tmdb, 'TMDB', h.proxy_tmdb ? '走代理' : '')}
      ${chip(h.library, 'Emby/Jellyfin', h.library_url ? h.library_url.replace(/^https?:\/\//,'') : '')}
    </div>
  </div>

  ${d.stats.last_error ? `<div class="card" style="margin-top:14px;border-color:var(--err)">
      <h2>最近错误</h2><div class="muted">${esc(d.stats.last_error)}</div></div>` : ''}

  <div class="card" style="margin-top:14px">
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <h2 style="margin:0">部署自检</h2>
      <span class="faint" style="font-size:12px">目录权限 / 订阅表 / 模板 / 服务配置，离线检查</span>
      <div style="margin-left:auto;display:flex;gap:8px">
        <button class="act" id="btn-selfcheck">离线自检</button>
        <button class="act primary" id="btn-preflight">连通性预检</button>
      </div>
    </div>
    <div class="hint">
      <b>离线自检</b>秒出，只看配置对不对。<b>连通性预检</b>会真的连一次 TG / TMDB / Emby
      （默认不发推送，只调只读接口），用来确认"第一次推送"能不能成。
    </div>
    <div id="selfcheck-result"></div>
  </div>

  <div class="card" style="margin-top:14px">
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <h2 style="margin:0">订阅源体检</h2>
      <span class="faint" style="font-size:12px">检查每个 RSS 能不能抓、抓到多少、最新几条</span>
      <button class="act" style="margin-left:auto" id="btn-feeds">一键检查</button>
    </div>
    <div class="hint">配 RSS 时先点这个：不需要 Emby/TMDB 也能用，失败会直接告诉你原因和怎么修。</div>
    <div id="feeds-result"></div>
  </div>

  ${showCards.length ? `<div class="card" style="margin-top:14px">
    <h2>追更卡片</h2>
    <div class="sub-cards">${showCards.map(subCard).join('')}</div>
  </div>` : ''}

  <div class="card" style="margin-top:14px">
    <h2>订阅进度</h2>
    ${subs.length ? `<table><thead><tr>
        <th>名称</th><th style="width:12%">模式</th><th style="width:32%">进度 / 条目</th><th>状态</th><th>最近活动</th><th></th>
      </tr></thead><tbody>
      ${subs.map(s => s.mode === 'feed' ? `<tr>
        ${tdL('名称', esc(s.name))}
        ${tdL('模式', '<span class="pill accent">全量转发</span>')}
        ${tdL('条目', `<span class="num">${s.items_total}</span> 条 · 已推送 <span class="num">${s.items_notified}</span><div class="faint" style="font-size:12px">不比对媒体库</div>`)}
        ${tdL('状态', s.last_error ? '<span class="pill err">出错</span>' : '<span class="pill ok">转发中</span>')}
        ${tdL('最近活动', `<span class="faint">${ago(s.last_check || s.next_poll)}</span>`)}
        ${tdL('操作', '<span class="faint" style="font-size:12px">—</span>')}
      </tr>` : `<tr>
        ${tdL('名称', esc(s.name) + (s.tmdb_id ? `<div class="faint mono" style="font-size:12px">TMDB ${s.tmdb_id}</div>` : ''))}
        ${tdL('模式', '<span class="pill ok">按剧追踪</span>')}
        ${tdL('进度', `<div class="bar-wrap">
            <div class="bar"><i class="${s.done?'done':''}" style="width:${pct(s.owned,s.total)}%"></i></div>
            <span class="num">${s.owned}/${s.total}</span>
          </div>${s.missing?`<div class="faint" style="font-size:12px;margin-top:3px">缺 ${esc(s.missing)}</div>`:''}`)}
        ${tdL('状态', s.last_error ? `<span class="pill err">出错</span>` : (s.done ? '<span class="pill ok">完成</span>' : '<span class="pill warn">追更</span>'))}
        ${tdL('最近巡检', `<span class="faint">${ago(s.last_check)}</span>`)}
        ${tdL('操作', `<button class="act" onclick="checkOne('${esc(s.id)}')">立即比对</button>`)}
      </tr>`).join('')}
      </tbody></table>` : '<div class="empty">还没有订阅，去「订阅」标签页添加</div>'}
  </div>

  ${d.last_scan ? `<div class="card" style="margin-top:14px">
    <h2>上次媒体库扫描</h2>
    <div class="row"><span>生成时间</span><span>${fmtTime(d.last_scan.generated_at)}</span></div>
    <div class="row"><span>扫描数量</span><span>${d.last_scan.scanned} / ${d.last_scan.library_total} 部 · 耗时 ${d.last_scan.elapsed_seconds}s</span></div>
    <div class="row"><span>统计</span><span>完整 ${d.last_scan.complete} · 缺集 ${d.last_scan.partial} · 未入库 ${d.last_scan.empty} · 未匹配 ${d.last_scan.unmatched}</span></div>
  </div>` : ''}
  `;
  $('#btn-feeds').onclick = runFeeds;
  $('#btn-selfcheck').onclick = runSelfCheck;
  $('#btn-preflight').onclick = runPreflight;
}

async function runPreflight(){
  const btn = $('#btn-preflight');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 连接中…';
  try {
    const r = await api.post('/api/preflight', {});
    showSelfCheck(r.result);
    toast(r.passed ? '连通性正常' : `有 ${r.failures} 项连不上`, r.passed ? 'ok' : 'err');
  } catch(e){ toast('预检失败：' + e.message, 'err'); }
  btn.disabled = false;
  btn.textContent = '重新预检';
}

async function runSelfCheck(){
  const btn = $('#btn-selfcheck');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 检查中…';
  try {
    const r = await api.get('/api/selfcheck');
    showSelfCheck(r.result);
    toast(r.passed ? (r.warnings ? `通过，但有 ${r.warnings} 项建议处理` : '全部通过') : `有 ${r.failures} 项必须修`,
          r.passed ? (r.warnings ? '' : 'ok') : 'err');
  } catch(e){ toast('自检失败：' + e.message, 'err'); }
  btn.disabled = false;
  btn.textContent = '重新自检';
}

function showSelfCheck(result){
  const checks = result.checks || [];
  const ICON = {ok:'✅', warn:'⚠️', fail:'❌'};
  $('#selfcheck-result').innerHTML = `
    <div class="grid" style="margin-top:12px">
      <div class="card tight"><h2>通过</h2><div class="kpi" style="color:var(--ok)">${checks.filter(c=>c.status==='ok').length}<small>项</small></div></div>
      <div class="card tight"><h2>建议</h2><div class="kpi" style="color:var(--warn)">${result.warnings}<small>项</small></div></div>
      <div class="card tight"><h2>必须修</h2><div class="kpi" style="color:${result.failures?'var(--err)':'var(--fg-dim)'}">${result.failures}<small>项</small></div>
        <div class="row"><span>耗时</span><span>${result.elapsed_seconds}s</span></div></div>
    </div>
    <div style="margin-top:10px">
      ${checks.map(c => `<div class="callout ${c.status==='ok'?'':(c.status==='fail'?'err':'warn')}" style="margin-top:8px;display:block">
        <div style="display:flex;gap:9px;align-items:baseline;flex-wrap:wrap">
          <span style="flex:0 0 auto">${ICON[c.status] || '?'}</span>
          <b style="flex:0 0 auto">${esc(c.name)}</b>
          <span class="faint" style="font-size:12px;flex:1;min-width:180px">${esc(c.detail)}</span>
        </div>
        ${c.fix ? `<div style="margin-top:5px;padding-left:22px;font-size:12px;opacity:.9">↳ ${esc(c.fix)}</div>` : ''}
      </div>`).join('')}
    </div>`;
}

async function runFeeds(){
  const btn = $('#btn-feeds');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 检查中…';
  try {
    const r = await api.post('/api/feeds/check', {});
    const s = r.summary;
    showFeeds(r.result);
    toast(s.failed ? `${s.total} 个源，${s.failed} 个失败` : `${s.total} 个源全部正常`, s.failed ? 'err' : 'ok');
  } catch(e){ toast('检查失败：' + e.message, 'err'); }
  btn.disabled = false;
  btn.textContent = '重新检查';
}

function showFeeds(result){
  const checks = result.checks || [];
  if (!checks.length) {
    $('#feeds-result').innerHTML = '<div class="empty">没有启用的 RSS 源</div>';
    return;
  }
  const failed = checks.filter(c => !c.ok);
  $('#feeds-result').innerHTML = `
    <div class="grid" style="margin-top:12px">
      <div class="card tight"><h2>源总数</h2><div class="kpi">${result.total}<small>个</small></div>
        ${result.skipped_disabled ? `<div class="row"><span>已跳过停用</span><span>${result.skipped_disabled}</span></div>` : ''}</div>
      <div class="card tight"><h2>正常</h2><div class="kpi" style="color:var(--ok)">${result.ok}<small>个</small></div></div>
      <div class="card tight"><h2>失败</h2><div class="kpi" style="color:${result.failed ? 'var(--err)' : 'var(--fg-dim)'}">${result.failed}<small>个</small></div>
        <div class="row"><span>耗时</span><span>${result.elapsed_seconds}s</span></div></div>
    </div>
    ${checks.map(c => `
      <div class="callout ${c.ok ? '' : 'err'}" style="margin-top:10px;display:block">
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <span class="pill ${c.ok ? 'ok' : 'err'}">${c.ok ? '正常' : '失败'}</span>
          <b>${esc(c.sub_name)}</b>
          <span class="pill">${c.mode === 'feed' ? '全量' : '追剧'}</span>
          <span class="faint mono" style="font-size:11.5px;word-break:break-all">${esc(c.url)}</span>
          <span class="faint" style="margin-left:auto;white-space:nowrap">${c.ok ? `${c.item_count} 条 · ${c.elapsed}s` : `${c.elapsed}s`}</span>
        </div>
        ${c.ok ? '' : `<div style="margin-top:6px;color:var(--err)">${esc(c.error)}</div>`}
        ${(c.previews && c.previews.length) ? `<div style="margin-top:8px">
          ${c.previews.map(p => `<div style="display:flex;gap:8px;align-items:baseline;padding:2px 0;flex-wrap:wrap">
            ${p.episode ? `<code style="flex:0 0 auto">${esc(p.episode)}</code>` : ''}
            <span class="faint" style="flex:0 0 auto;font-size:12px">${esc(p.size)}</span>
            ${p.badges && p.badges.length ? chips(p.badges.slice(0,3)) : ''}
            <span class="faint" style="font-size:12px;word-break:break-all;flex:1">${esc(p.title)}</span>
          </div>`).join('')}
        </div>` : (c.ok ? '<div class="faint" style="margin-top:6px;font-size:12px">该源当前没有任何条目</div>' : '')}
      </div>`).join('')}
    ${failed.length ? `<div class="callout warn" style="margin-top:12px;display:block">
      <b>排错建议</b>
      <div style="margin-top:4px">· 401/403：passkey 过期或地址不完整，去站点重新复制<br>
      · 404：路径写错，或该分类已被站点移除<br>
      · 超时/连接失败：NAS 访问不了该站点，试试配 <code>RMH_PROXY</code><br>
      · 返回 HTML 而不是 XML：多半被 CF 盾拦了</div></div>` : ''}`;
}

async function checkOne(id){
  toast('正在比对…');
  try {
    const r = await api.post('/api/check', {id});
    const row = r.results[0];
    if (!row) return toast('没有结果', 'err');
    toast(row.ok ? `${row.name}：入库 ${row.owned}/${row.total}${row.missing?('，缺 '+row.missing):'，已完整'}` : `比对失败：${row.error}`, row.ok?'ok':'err');
  } catch(e){ toast('比对失败：' + e.message, 'err'); }
  renderDash();
}

/* ---------------- 订阅 ---------------- */
async function renderSubs(){
  const box = $('#tab-subs');
  box.innerHTML = '<div class="empty"><span class="spinner"></span> 加载中…</div>';
  let d;
  try { d = await api.get('/api/subscriptions'); }
  catch(e){ box.innerHTML = '<div class="card">加载失败：' + esc(e.message) + '</div>'; return; }
  const R = sub => JSON.stringify(sub).replace(/'/g, '&#39;');
  box.innerHTML = `
  <div class="card">
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <h2 style="margin:0">订阅列表（${d.subscriptions.length}）</h2>
      <button class="act primary" style="margin-left:auto" onclick="editSub(null)">+ 新增订阅</button>
    </div>
    <div class="hint">订阅写在 <code>config/subscriptions.yaml</code>，这里改动会立即生效并写回文件。</div>
  </div>
  <div class="card" style="margin-top:14px">
    ${d.subscriptions.length ? `<table><thead><tr>
      <th>名称 / id</th><th>模式</th><th>TMDB</th><th>RSS</th><th>规则</th><th></th>
    </tr></thead><tbody>
    ${d.subscriptions.map(s => `<tr>
      ${tdL('名称', esc(s.name) + `<div class="faint mono" style="font-size:12px">${esc(s.id)}</div>`)}
      ${tdL('模式', s.mode === 'feed'
            ? '<span class="pill accent">订阅源全量</span>'
            : `<span class="pill ok">按剧追踪</span>${s.remove_when_done ? '<div class="faint" style="font-size:12px">追完退订</div>' : ''}`)}
      ${tdL('TMDB', s.tmdb_id ? `<span class="mono">${s.tmdb_id}</span>${s.year?`<div class="faint" style="font-size:12px">${s.year}</div>`:''}` : '<span class="faint">—</span>')}
      ${tdL('RSS', `<span class="faint mono" style="word-break:break-all">${esc((s.rss_urls[0]||'').replace(/passkey=[^&]*/i,'passkey=***'))}</span>${s.rss_urls.length>1?`<div class="faint" style="font-size:12px">等 ${s.rss_urls.length} 条</div>`:''}`)}
      ${tdL('规则', (() => {
            const parts = [];
            if (s.name_filter) parts.push('包含:' + esc(s.name_filter));
            if (s.exclude_filter) parts.push('排除:' + esc(s.exclude_filter));
            if (s.quality && s.quality.length) parts.push('画质:' + esc(s.quality.join('/')));
            if (s.tmdb_required) parts.push('需匹配TMDB');
            return parts.length ? `<span class="faint" style="font-size:12px">${parts.join('<br>')}</span>` : '<span class="faint">—</span>';
          })())}
      ${tdL('操作', `<button class="act" onclick='editSub(${R(s)})'>编辑</button>
        <button class="act danger" onclick="delSub('${esc(s.id)}','${esc(s.name)}')">删除</button>`)}
    </tr>`).join('')}
    </tbody></table>` : '<div class="empty">还没有订阅</div>'}
  </div>`;
}

function editSub(sub){
  const s = sub || {name:'', rss:'', mode:'feed', tmdb_id:'', year:'', season:'', quality:'', name_filter:'', exclude_filter:'', notify_new:true, remove_when_done:true, enabled:true, seed:''};
  const isFeed = (s.mode || 'feed') === 'feed';
  $('#dlgtitle').textContent = sub ? ('编辑订阅：' + s.name) : '新增订阅';
  $('#dlgbody').innerHTML = `
    <label>工作模式</label>
    <select id="f_mode" onchange="toggleModeFields()">
      <option value="feed" ${isFeed?'selected':''}>订阅源全量 — RSS 里有什么就推什么（不比对媒体库，不需要 TMDB）</option>
      <option value="show" ${!isFeed?'selected':''}>按剧追踪 — 只推这部剧，算入库进度，追完自动退订</option>
    </select>
    <div class="hint" id="mode-hint"></div>
    <div class="f2" style="margin-top:6px">
      <div>
        <label>名称 *</label>
        <input id="f_name" value="${esc(s.name)}" placeholder="feed 模式填个便于识别的名字即可">
      </div>
      <div id="wrap_tmdb">
        <label>TMDB ID（按剧追踪必填）</label>
        <input id="f_tmdb" value="${esc(s.tmdb_id ?? '')}" placeholder="TMDB 剧集页 URL 里的数字">
        <div class="hint">填了就不会认错同名剧</div>
      </div>
      <div id="wrap_year">
        <label>首播年份</label>
        <input id="f_year" value="${esc(s.year ?? '')}" placeholder="2020">
      </div>
      <div id="wrap_season">
        <label>只订阅某一季（留空=全部）</label>
        <input id="f_season" value="${esc(s.season ?? '')}" placeholder="2">
      </div>
      <div>
        <label>首轮历史条目</label>
        <select id="f_seed">
          <option value="" ${s.seed===''||s.seed===null||s.seed===undefined?'selected':''}>按模式默认</option>
          <option value="false" ${s.seed===false?'selected':''}>全部补推</option>
          <option value="true" ${s.seed===true?'selected':''}>静默登记不推送</option>
        </select>
      </div>
    </div>
    <label>RSS 地址 *（多条用空格或逗号分隔）</label>
    <textarea id="f_rss" rows="3" placeholder="https://pt.example/rss?passkey=xxx">${esc(s.rss)}</textarea>
    <div class="f2" style="margin-top:4px">
      <div>
        <label>标题必须包含（正则，可空）</label>
        <input id="f_include" value="${esc(s.name_filter||'')}" placeholder="1080p|2160p">
      </div>
      <div>
        <label>标题排除（正则，可空）</label>
        <input id="f_exclude" value="${esc(s.exclude_filter||'')}" placeholder="预告|花絮|OST">
      </div>
      <div>
        <label>画质白名单（逗号分隔，可空）</label>
        <input id="f_quality" value="${esc((s.quality||[]).join(', '))}" placeholder="1080p, 2160p">
      </div>
    </div>
    <div class="f2" style="margin-top:4px">
      <div>
        <label><input type="checkbox" id="f_notify" ${s.notify_new!==false?'checked':''} style="width:auto"> 发现新种时推送</label>
      </div>
      <div id="wrap_remove">
        <label><input type="checkbox" id="f_remove" ${s.remove_when_done!==false?'checked':''} style="width:auto"> 全部入库后自动删除本订阅</label>
      </div>
      <div>
        <label><input type="checkbox" id="f_enabled" ${s.enabled!==false?'checked':''} style="width:auto"> 启用</label>
      </div>
    </div>`;
  toggleModeFields();
  const dlg = $('#dlg');
  dlg.returnValue = '';
  $('#dlgok').onclick = async (ev) => {
    ev.preventDefault();
    const seedRaw = $('#f_seed').value;
    const payload = {
      id: sub ? sub.id : undefined,
      mode: $('#f_mode').value,
      name: $('#f_name').value.trim(),
      rss: $('#f_rss').value.trim(),
      tmdb_id: $('#f_tmdb').value.trim() || undefined,
      year: $('#f_year').value.trim() || undefined,
      season: $('#f_season').value.trim() || undefined,
      seed: seedRaw === '' ? undefined : (seedRaw === 'true'),
      name_filter: $('#f_include').value.trim(),
      exclude_filter: $('#f_exclude').value.trim(),
      quality: $('#f_quality').value.split(',').map(x=>x.trim()).filter(Boolean),
      notify_new: $('#f_notify').checked,
      remove_when_done: $('#f_remove').checked,
      enabled: $('#f_enabled').checked,
    };
    if (!payload.name && !payload.rss) return toast('名称和 RSS 至少填一个', 'err');
    if (payload.mode === 'show' && !payload.rss) return toast('按剧追踪模式必须填 RSS 地址', 'err');
    try {
      const r = await api.post('/api/subscriptions', {subscription: payload, overwrite: !!sub});
      toast(r.message || '已保存', 'ok');
      dlg.close();
      renderSubs();
    } catch(e){ toast('保存失败：' + e.message, 'err'); }
  };
  dlg.showModal();
}

/* 切模式时把只对 show 有意义的字段藏起来，避免用户填了没用的东西 */
function toggleModeFields(){
  const feed = $('#f_mode').value === 'feed';
  ['#wrap_tmdb','#wrap_year','#wrap_season','#wrap_remove'].forEach(sel => {
    const el = $(sel); if (el) el.style.display = feed ? 'none' : '';
  });
  $('#mode-hint').textContent = feed
    ? '全量模式：RSS 里出现的新条目都会推送（可配合「标题必须包含/排除」和「画质白名单」过滤）。不需要 TMDB 与 Emby。'
    : '按剧追踪：每轮比对媒体库算入库进度，全部入库后推送完成通知并自动退订。需要 TMDB API 与 Emby/Jellyfin。';
}

async function delSub(id, name){
  if (!confirm(`确定删除订阅「${name}」？\n（历史条目记录会保留）`)) return;
  try {
    await api.del('/api/subscriptions?id=' + encodeURIComponent(id));
    toast('已删除', 'ok');
    renderSubs();
  } catch(e){ toast('删除失败：' + e.message, 'err'); }
}

/* ---------------- 媒体库扫描 ---------------- */
async function renderScan(){
  const box = $('#tab-scan');
  box.innerHTML = '<div class="empty"><span class="spinner"></span> 加载中…</div>';
  let last = null;
  try { last = (await api.get('/api/scan/last')).result; } catch(e){}
  box.innerHTML = `
  <div class="card">
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <h2 style="margin:0">扫描媒体库</h2>
      <span class="muted" style="font-size:12px">列出每部剧的入库进度与缺集</span>
      <button class="act primary" style="margin-left:auto" id="btn-scan">开始扫描</button>
    </div>
    <div class="hint">第一次扫描会逐部查询 TMDB（结果缓存 12 小时），库大时需要几分钟。扫描期间不要关页面。</div>
  </div>

  <div class="card" style="margin-top:14px">
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <h2 style="margin:0">查漏：缺的集现在能下吗</h2>
      <span class="muted" style="font-size:12px">拿库里缺的集去比对当前 RSS，标出哪些现在就有资源</span>
      <button class="act primary" style="margin-left:auto" id="btn-gaps">开始查漏</button>
    </div>
    <div class="hint">
      先扫描媒体库找出缺口，再看这些缺口<b>当前有没有出现在你的 RSS 里</b>（会复用上面的扫描结果，不重复扫库）。<br>
      ⚠️ RSS 只包含站点最近一批更新，<b>已经翻页过去的旧集读 RSS 是找不到的</b>——那需要站点搜索接口。
    </div>
  </div>

  <div id="scan-result"></div>
  <div id="gaps-result"></div>`;
  $('#btn-scan').onclick = runScan;
  $('#btn-gaps').onclick = runGaps;
  if (last) showScan(last);
}

async function runGaps(){
  const btn = $('#btn-gaps');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 查漏中…';
  try {
    const r = await api.post('/api/gaps', {});
    const s = r.summary;
    toast(`查漏完成：缺 ${s.total_missing} 集，其中 ${s.total_covered} 集现在有货`, 'ok');
    showGaps(r.result, s);
  } catch(e){ toast('查漏失败：' + e.message, 'err'); }
  btn.disabled = false;
  btn.textContent = '重新查漏';
}

function showGaps(result, summary){
  const s = summary || {};
  const withStock = (result.series || []).filter(x => x.covered > 0);
  const waiting = (result.series || []).filter(x => x.covered === 0);
  const rows = [];
  for (const series of withStock) {
    for (const g of (series.gaps || [])) {
      if (!g.available) continue;
      const b = g.best || {};
      rows.push(`<tr>
        ${tdL('剧名', esc(series.display_name))}
        ${tdL('集号', `<code>${esc(g.code)}</code>`)}
        ${tdL('规格', chips(b.badges, true) || '<span class="faint">—</span>')}
        ${tdL('体积', `<span class="num">${esc(b.size || '-')}</span>`)}
        ${tdL('资源', `<span class="faint" style="font-size:12px;word-break:break-all">${esc(b.title || '')}</span>`)}
        ${tdL('下载', b.download_url ? `<a class="act" href="${esc(b.download_url)}" target="_blank" rel="noopener">下载</a>` : '<span class="faint">—</span>')}
      </tr>`);
    }
  }

  $('#gaps-result').innerHTML = `
  <div class="grid" style="margin-top:14px">
    <div class="card"><h2>缺集总数</h2><div class="kpi">${s.total_missing ?? 0}<small>集</small></div></div>
    <div class="card"><h2>现在有货</h2><div class="kpi" style="color:var(--ok)">${s.total_covered ?? 0}<small>集</small></div>
      <div class="row"><span>涉及</span><span>${withStock.length} 部剧</span></div></div>
    <div class="card"><h2>还在等</h2><div class="kpi" style="color:var(--warn)">${(s.total_missing ?? 0) - (s.total_covered ?? 0)}<small>集</small></div>
      <div class="row"><span>涉及</span><span>${waiting.length} 部剧</span></div></div>
    <div class="card"><h2>RSS 源</h2><div class="kpi">${s.feed_items ?? 0}<small>条</small></div>
      <div class="row"><span>成功 / 失败</span><span>${s.feeds_ok ?? 0} / ${s.feeds_failed ?? 0}</span></div></div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>现在就能下的（${rows.length} 集）</h2>
    ${rows.length
      ? `<table><thead><tr><th>剧</th><th>集号</th><th>规格</th><th>体积</th><th>资源</th><th></th></tr></thead>
           <tbody>${rows.slice(0,200).join('')}</tbody></table>`
      : '<div class="empty"><span class="big">🕐</span>当前 RSS 里没有你缺的集<br><span style="font-size:12px">可能还没发布，或已经翻页过了 RSS 窗口</span></div>'}
  </div>

  <div class="card" style="margin-top:14px">
    <h2>暂时没货的剧（${waiting.length}）</h2>
    ${waiting.length ? `<table><thead><tr><th>剧</th><th style="width:22%">进度</th><th>缺集</th></tr></thead><tbody>
      ${waiting.slice(0,100).map(x => `<tr>
        ${tdL('剧', esc(x.display_name))}
        ${tdL('进度', `<div class="bar-wrap"><div class="bar"><i style="width:${x.total ? Math.round(x.owned/x.total*100) : 0}%"></i></div>
          <span class="num">${x.owned}/${x.total}</span></div>`)}
        ${tdL('缺集', `<span class="faint" style="word-break:break-all">${(x.gaps||[]).map(g=>esc(g.code)).join('、')}${x.gaps_total > (x.gaps||[]).length ? ` 等 ${x.gaps_total} 集` : ''}</span>`)}
      </tr>`).join('')}
    </tbody></table>` : '<div class="empty"><span class="big">✓</span>没有等待中的剧</div>'}
  </div>

  ${(s.errors && s.errors.length) ? `<div class="card" style="margin-top:14px;border-color:var(--warn)">
    <h2>抓取失败的 RSS 源</h2><div class="muted" style="font-size:12px">${s.errors.map(esc).join('<br>')}</div></div>` : ''}`;
}

async function runScan(){
  const btn = $('#btn-scan');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 扫描中…';
  try {
    const r = await api.post('/api/scan', {});
    toast(`扫描完成：${r.summary.scanned} 部，缺 ${r.summary.missing_episodes} 集`, 'ok');
    showScan(r.result);
  } catch(e){ toast('扫描失败：' + e.message, 'err'); }
  btn.disabled = false;
  btn.textContent = '重新扫描';
}

function showScan(result){
  const s = result.summary || {};
  const all = result.series || [];
  const gaps = all.filter(x => x.missing && x.missing.length);
  gaps.sort((a,b)=> b.missing.length - a.missing.length);
  // 需要人处理的两类：缺集、没匹配到 TMDB
  const needAttention = all.filter(x => x.status === 'unmatched' || x.status === 'error');
  const empty = all.filter(x => x.status === 'empty');
  const complete = all.filter(x => x.status === 'complete');

  const statusPill = st => {
    const map = {
      complete: ['ok', '✅ 完整'], partial: ['warn', '⏳ 缺集'], empty: ['', '🕐 未入库'],
      new: ['', '🆕 未播出'], unmatched: ['warn', '❓ 未匹配'], error: ['err', '❌ 出错'],
    };
    const [cls, label] = map[st] || ['', st || '未知'];
    return `<span class="pill ${cls}">${esc(label)}</span>`;
  };

  $('#scan-result').innerHTML = `
  <div class="grid" style="margin-top:14px">
    <div class="card"><h2>完整</h2><div class="kpi" style="color:var(--ok)">${s.complete ?? 0}<small>部</small></div></div>
    <div class="card"><h2>缺集</h2><div class="kpi" style="color:var(--warn)">${s.partial ?? 0}<small>部</small></div>
      <div class="row"><span>共缺</span><span>${s.missing_episodes ?? 0} 集</span></div></div>
    <div class="card"><h2>未入库</h2><div class="kpi">${s.empty ?? 0}<small>部</small></div></div>
    <div class="card"><h2>未匹配 / 出错</h2><div class="kpi" style="color:${(s.unmatched ?? 0) + (s.error ?? 0) ? 'var(--warn)' : 'var(--fg-dim)'}">${(s.unmatched ?? 0) + (s.error ?? 0)}<small>部</small></div>
      <div class="row"><span>未匹配</span><span>${s.unmatched ?? 0}</span></div>
      <div class="row"><span>出错</span><span>${s.error ?? 0}</span></div></div>
  </div>

  <div class="card" style="margin-top:14px">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <h2 style="margin:0">剧集明细（${all.length}）</h2>
      <div class="chips" style="margin-left:auto">
        <span class="chip hi" id="f-all" style="cursor:pointer">全部 ${all.length}</span>
        <span class="chip" id="f-gap" style="cursor:pointer">缺集 ${gaps.length}</span>
        <span class="chip" id="f-empty" style="cursor:pointer">未入库 ${empty.length}</span>
        <span class="chip" id="f-attn" style="cursor:pointer">需处理 ${needAttention.length}</span>
        <span class="chip" id="f-done" style="cursor:pointer">完整 ${complete.length}</span>
      </div>
    </div>
    <div id="scan-rows" style="margin-top:10px"></div>
  </div>`;

  const rowHtml = list => list.length
    ? `<table><thead><tr><th>剧名</th><th style="width:12%">状态</th><th style="width:24%">进度</th><th>缺集 / 说明</th></tr></thead><tbody>
        ${list.slice(0,300).map(x => `<tr>
          ${tdL('剧名', esc(x.tmdb_name || x.name) + (x.year?`<span class="faint">（${x.year}）</span>`:''))}
          ${tdL('状态', statusPill(x.status))}
          ${tdL('进度', `<div class="bar-wrap"><div class="bar"><i class="${x.status==='complete'?'done':''}" style="width:${x.percent}%"></i></div>
            <span class="num">${x.owned}/${x.total}</span></div>`)}
          ${tdL('缺集 / 说明', x.missing && x.missing.length
              ? `<span class="faint" style="word-break:break-all">${esc(x.missing.slice(0,8).join('、'))}${x.missing.length>8?` 等 ${x.missing.length} 集`:''}</span>`
              : (x.error ? `<span class="faint">${esc(x.error)}</span>` : '<span class="faint">—</span>'))}
        </tr>`).join('')}
      </tbody></table>`
    : '<div class="empty"><span class="big">✓</span>这一分类下没有剧</div>';

  const setFilter = (id, list) => {
    ['f-all','f-gap','f-empty','f-attn','f-done'].forEach(k => {
      const el = document.getElementById(k); if (el) el.classList.toggle('hi', k === id);
    });
    $('#scan-rows').innerHTML = rowHtml(list);
  };
  document.getElementById('f-all').onclick = () => setFilter('f-all', all);
  document.getElementById('f-gap').onclick = () => setFilter('f-gap', gaps);
  document.getElementById('f-empty').onclick = () => setFilter('f-empty', empty);
  document.getElementById('f-attn').onclick = () => setFilter('f-attn', needAttention);
  document.getElementById('f-done').onclick = () => setFilter('f-done', complete);
  $('#scan-rows').innerHTML = rowHtml(gaps.length ? gaps : all);
}

/* ---------------- 设置 ---------------- */
let CFG = null;
async function renderSettings(){
  const box = $('#tab-settings');
  box.innerHTML = '<div class="empty"><span class="spinner"></span> 加载中…</div>';
  let d;
  try { d = await api.get('/api/config'); }
  catch(e){ box.innerHTML = '<div class="card">加载失败：' + esc(e.message) + '</div>'; return; }
  CFG = d;
  const V = d.values, env = d.env_overridden;
  const sec = (g, k) => V[g] && V[g][k] && typeof V[g][k] === 'object' && 'set' in V[g][k];
  const val = (g, k) => sec(g,k) ? '' : (V[g] && V[g][k] !== undefined && V[g][k] !== null ? V[g][k] : '');
  const envTag = (g,k) => env[g+'.'+k] ? `<span class="pill warn" title="被环境变量 ${env[g+'.'+k]} 覆盖">env</span>` : '';
  const secretInput = (id, g, k, ph) => `
    <input id="${id}" data-secret="1" placeholder="${sec(g,k) ? '已设置（'+esc(V[g][k].masked)+'），留空表示不修改' : esc(ph||'')}" value="">`;
  const chk = (g,k) => val(g,k) === true ? 'checked' : '';

  box.innerHTML = `
  <div class="card">
    <h2>Telegram 推送</h2>
    <div class="f2">
      <div><label>机器人 Token ${envTag('telegram','bot_token')}</label>${secretInput('c_tg_token','telegram','bot_token','123456:AA...')}</div>
      <div><label>Chat ID ${envTag('telegram','chat_id')}</label><input id="c_tg_chat" value="${esc(val('telegram','chat_id'))}" placeholder="你的 user id 或 -100xxx"></div>
      <div><label>话题 Thread ID（私聊留空）${envTag('telegram','thread_id')}</label><input id="c_tg_thread" value="${esc(val('telegram','thread_id'))}"></div>
      <div><label>正向代理（科学上网端口）${envTag('telegram','proxy')}</label><input id="c_tg_proxy" value="${esc(val('telegram','proxy'))}" placeholder="http://192.168.31.142:10809"></div>
      <div><label>反向代理 API 地址（自建才填）${envTag('telegram','api_base')}</label><input id="c_tg_base" value="${esc(val('telegram','api_base') === 'https://api.telegram.org' ? '' : val('telegram','api_base'))}" placeholder="https://tgapi.example.com"></div>
    </div>
    <div class="actions">
      <label style="margin:0"><input type="checkbox" id="c_tg_poster" ${chk('telegram','send_poster')} style="width:auto"> 推送带海报</label>
      <button class="act" onclick="testTg()">发送测试消息</button>
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>TMDB</h2>
    <div class="f2">
      <div><label>API Key（v3 auth）${envTag('tmdb','api_key')}</label>${secretInput('c_tmdb_key','tmdb','api_key','32 位字符串')}</div>
      <div><label>语言 ${envTag('tmdb','language')}</label>
        <select id="c_tmdb_lang">
          ${['zh-CN','zh-TW','en-US','ja-JP'].map(x=>`<option value="${x}" ${val('tmdb','language')===x?'selected':''}>${x}</option>`).join('')}
        </select></div>
      <div><label>正向代理 ${envTag('tmdb','proxy')}</label><input id="c_tmdb_proxy" value="${esc(val('tmdb','proxy'))}" placeholder="http://192.168.31.142:10809"></div>
      <div><label>反向代理 API 地址（自建才填）${envTag('tmdb','api_base')}</label><input id="c_tmdb_base" value="${esc(val('tmdb','api_base') === 'https://api.themoviedb.org/3' ? '' : val('tmdb','api_base'))}" placeholder="https://tmdb.example.com"></div>
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>Emby / Jellyfin</h2>
    <div class="f2">
      <div><label>服务器地址 ${envTag('library','url')}</label><input id="c_emby_url" value="${esc(val('library','url'))}" placeholder="http://192.168.31.221:8096">
        <div class="hint">要用 NAS 的局域网 IP，不要用 localhost</div></div>
      <div><label>API 密钥 ${envTag('library','api_key')}</label>${secretInput('c_emby_key','library','api_key')}</div>
      <div><label>类型 ${envTag('library','kind')}</label>
        <select id="c_emby_kind">
          ${['auto','emby','jellyfin'].map(x=>`<option value="${x}" ${val('library','kind')===x?'selected':''}>${x}</option>`).join('')}
        </select></div>
      <div><label>用户 ID（老版 Emby 才需要）${envTag('library','user_id')}</label><input id="c_emby_user" value="${esc(val('library','user_id'))}"></div>
    </div>
    <div class="actions">
      <label style="margin:0"><input type="checkbox" id="c_emby_aired" ${chk('library','count_aired_only')} style="width:auto"> 分母只算已播出集数</label>
      <label style="margin:0"><input type="checkbox" id="c_emby_specials" ${chk('library','include_specials')} style="width:auto"> 计入特别篇</label>
      <label style="margin:0"><input type="checkbox" id="c_emby_tls" ${chk('library','verify_tls')} style="width:auto"> 校验 HTTPS 证书</label>
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>Transmission 站点标签</h2>
    <div class="f2">
      <div><label>RPC 地址 ${envTag('transmission','url')}</label><input id="c_tr_url" value="${esc(val('transmission','url'))}" placeholder="http://192.168.31.221:9092/transmission/rpc">
        <div class="hint">只贴标签、不负责下载</div></div>
      <div><label>账号 ${envTag('transmission','user')}</label><input id="c_tr_user" value="${esc(val('transmission','user'))}"></div>
      <div><label>密码 ${envTag('transmission','password')}</label>${secretInput('c_tr_pass','transmission','password','')}</div>
      <div><label>打标间隔（秒）${envTag('transmission','interval')}</label><input id="c_tr_interval" type="number" value="${esc(val('transmission','interval'))}"></div>
    </div>
    <div class="actions">
      <label style="margin:0"><input type="checkbox" id="c_tr_enabled" ${chk('transmission','enabled')} style="width:auto"> 开启定时打标</label>
      <label style="margin:0"><input type="checkbox" id="c_tr_pt" ${chk('transmission','auto_pt')} style="width:auto"> 自动补 PT 标签</label>
      <button class="act" onclick="runLabels(false)">预演打标</button>
      <button class="act primary" onclick="runLabels(true)">立即打标</button>
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>站点标签映射 <span class="muted" style="font-weight:normal">mappings.txt</span></h2>
    <textarea id="c_mappings" rows="10" style="width:100%;font-family:var(--mono,monospace);font-size:12px" placeholder="域名=标签，一行一条&#10;ubits.club=站点/ubits&#10;m-team.cc=站点/m-team"></textarea>
    <div class="actions">
      <button class="act" onclick="loadMappings()">读取</button>
      <button class="act primary" onclick="saveMappings()">保存映射</button>
    </div>
    <div class="hint">规则：tracker 域名与此相等、或为其子域时命中；以 # 开头为注释；只添加标签、绝不覆盖已有标签。</div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>通知模板 <span class="muted" style="font-weight:normal">每类事件一个输入框</span></h2>
    <div class="tpl-item" style="margin-bottom:12px">
      <div class="tpl-head"><b>添加订阅确认</b> <code>sub_added</code></div>
      <textarea id="tpl_sub_added" rows="3" style="width:100%;font-family:var(--mono,monospace);font-size:12px" placeholder='{ "text": "🎉 {{title}}{% if year %}（{{year}}）{% endif %} 已添加订阅" }'></textarea>
    </div>
    <div class="tpl-item" style="margin-bottom:12px">
      <div class="tpl-head"><b>订阅源全量新种</b> <code>feed_new</code></div>
      <textarea id="tpl_feed_new" rows="5" style="width:100%;font-family:var(--mono,monospace);font-size:12px" placeholder='{ "text": "📡 {{name}}\n🆕 新条目 ×{{count}}" }'></textarea>
    </div>
    <div class="tpl-item" style="margin-bottom:12px">
      <div class="tpl-head"><b>按剧新资源</b> <code>show_new</code></div>
      <textarea id="tpl_show_new" rows="5" style="width:100%;font-family:var(--mono,monospace);font-size:12px" placeholder='{ "text": "🎬 {{name}}\n{{progress}}" }'></textarea>
    </div>
    <div class="tpl-item" style="margin-bottom:12px">
      <div class="tpl-head"><b>入库通知</b> <code>library_update</code></div>
      <textarea id="tpl_library_update" rows="4" style="width:100%;font-family:var(--mono,monospace);font-size:12px" placeholder='{ "text": "📥 {{name}} 已入库\n{{progress}}" }'></textarea>
    </div>
    <div class="tpl-item" style="margin-bottom:12px">
      <div class="tpl-head"><b>追完通知</b> <code>done</code></div>
      <textarea id="tpl_done" rows="4" style="width:100%;font-family:var(--mono,monospace);font-size:12px" placeholder='{ "text": "🏁 {{name}} 订阅完成\n{{progress}}" }'></textarea>
    </div>
    <div class="actions">
      <button class="act" onclick="loadTemplates()">读取全部</button>
      <button class="act primary" onclick="saveTemplates()">保存全部</button>
    </div>
    <div class="hint">
      每个事件一个框，留空 = 退回内置排版。模板是 MoviePilot 同款 <b>Jinja2 字典</b>：<code>{{变量}}</code>、<code>{% if %}...{% endif %}</code>。<br>
      常用变量：<code>title</code> <code>name</code> <code>year</code> <code>season</code> <code>size</code> <code>badges</code> <code>count</code> <code>owned</code> <code>total</code> <code>progress</code> <code>link</code>。
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>运行参数</h2>
    <div class="f2">
      <div><label>RSS 轮询间隔（秒）</label><input id="c_poll" type="number" value="${esc(val('runtime','poll_interval'))}"></div>
      <div><label>入库巡检间隔（秒）</label><input id="c_reconcile" type="number" value="${esc(val('runtime','reconcile_interval'))}"></div>
      <div><label>扫描并发数</label><input id="c_scan_conc" type="number" value="${esc(val('runtime','scan_concurrency'))}"></div>
      <div><label>扫描上限（0=不限）</label><input id="c_scan_max" type="number" value="${esc(val('runtime','scan_max_series'))}"></div>
      <div><label>日志级别</label>
        <select id="c_log">
          ${['DEBUG','INFO','WARNING'].map(x=>`<option value="${x}" ${val('runtime','log_level')===x?'selected':''}>${x}</option>`).join('')}
        </select></div>
    </div>
    <div class="actions">
      <button class="act primary" onclick="saveConfig()">保存全部设置</button>
      <span class="muted" style="font-size:12px">保存后立即热重载，无需重启容器</span>
    </div>
    <div class="hint">配置写入 <code>${esc(d.overrides_file)}</code>，不会改动你手写的 config.yaml。环境变量优先级最高（带 env 标记的字段改了也不生效）。</div>
  </div>`;
  loadMappings();
  loadTemplates();
}

function collectChanges(){
  const out = {};
  const put = (g,k,v) => { (out[g] = out[g] || {})[k] = v; };
  const S = id => $(id)?.value.trim() ?? '';
  const C = id => !!$(id)?.checked;

  if (S('#c_tg_token')) put('telegram','bot_token', S('#c_tg_token'));
  put('telegram','chat_id', S('#c_tg_chat'));
  put('telegram','thread_id', S('#c_tg_thread'));
  put('telegram','proxy', S('#c_tg_proxy'));
  put('telegram','api_base', S('#c_tg_base'));
  put('telegram','send_poster', C('#c_tg_poster'));

  if (S('#c_tmdb_key')) put('tmdb','api_key', S('#c_tmdb_key'));
  put('tmdb','language', S('#c_tmdb_lang'));
  put('tmdb','proxy', S('#c_tmdb_proxy'));
  put('tmdb','api_base', S('#c_tmdb_base'));

  put('library','url', S('#c_emby_url'));
  if (S('#c_emby_key')) put('library','api_key', S('#c_emby_key'));
  put('library','kind', S('#c_emby_kind'));
  put('library','user_id', S('#c_emby_user'));
  put('library','count_aired_only', C('#c_emby_aired'));
  put('library','include_specials', C('#c_emby_specials'));
  put('library','verify_tls', C('#c_emby_tls'));

  put('transmission','url', S('#c_tr_url'));
  put('transmission','user', S('#c_tr_user'));
  if (S('#c_tr_pass')) put('transmission','password', S('#c_tr_pass'));
  put('transmission','interval', Number(S('#c_tr_interval')) || 3600);
  put('transmission','enabled', C('#c_tr_enabled'));
  put('transmission','auto_pt', C('#c_tr_pt'));

  put('runtime','poll_interval', Number(S('#c_poll')) || 900);
  put('runtime','reconcile_interval', Number(S('#c_reconcile')) || 1800);
  put('runtime','scan_concurrency', Number(S('#c_scan_conc')) || 5);
  put('runtime','scan_max_series', Number(S('#c_scan_max')) || 0);
  put('runtime','log_level', S('#c_log'));
  return out;
}

async function saveConfig(){
  try {
    const r = await api.post('/api/config', {changes: collectChanges()});
    toast(r.message || '已保存', 'ok');
    if (r.ignored && r.ignored.length) toast('忽略了不允许修改的字段：' + r.ignored.join('、'), 'err');
    renderSettings();
  } catch(e){ toast('保存失败：' + e.message, 'err'); }
}

async function testTg(){
  toast('正在发送…');
  try {
    const r = await api.post('/api/telegram/test', {});
    toast(`已发送（机器人 @${r.bot}），请查看 Telegram`, 'ok');
  } catch(e){ toast('发送失败：' + e.message, 'err'); }
}

async function runLabels(apply){
  toast(apply ? '正在打标…' : '正在预演…');
  try {
    const r = await api.post('/api/labels/run', {apply: apply});
    const x = r.result || {};
    const verb = apply ? '已写入' : '待更新';
    let msg = `种子 ${x.total} 个，${verb} ${x.changes} 个（未映射跳过 ${x.skipped_unmapped} 个）`;
    if (x.unmapped && x.unmapped.length) msg += '；未映射域名：' + x.unmapped.join(', ');
    toast(msg, apply ? 'ok' : '');
  } catch(e){ toast('打标失败：' + e.message, 'err'); }
}

async function loadMappings(){
  try {
    const r = await api.get('/api/labels/mappings');
    $('#c_mappings').value = r.text || '';
  } catch(e){ toast('读取失败：' + e.message, 'err'); }
}

async function saveMappings(){
  try {
    const text = $('#c_mappings').value;
    const r = await api.post('/api/labels/mappings', {text: text});
    toast(r.message || '已保存', 'ok');
  } catch(e){ toast('保存失败：' + e.message, 'err'); }
}

async function loadTemplates(){
  try {
    const r = await api.get('/api/templates');
    const t = r.templates || {};
    $('#tpl_sub_added').value = t.sub_added || '';
    $('#tpl_feed_new').value = t.feed_new || '';
    $('#tpl_show_new').value = t.show_new || '';
    $('#tpl_library_update').value = t.library_update || '';
    $('#tpl_done').value = t.done || '';
  } catch(e){ toast('读取失败：' + e.message, 'err'); }
}

async function saveTemplates(){
  const events = {
    sub_added: 'tpl_sub_added',
    feed_new: 'tpl_feed_new',
    show_new: 'tpl_show_new',
    library_update: 'tpl_library_update',
    done: 'tpl_done',
  };
  let saved = 0, failed = 0;
  for (const [event, id] of Object.entries(events)) {
    try {
      await api.post('/api/templates', {event: event, text: $('#' + id).value});
      saved++;
    } catch(e){ failed++; toast(`保存 ${event} 失败：${e.message}`, 'err'); }
  }
  if (saved) toast(`已保存 ${saved} 个模板` + (failed ? `，${failed} 个失败` : ''), failed ? 'err' : 'ok');
}

/* ---------------- 路由 ---------------- */
const TABS = {dash: renderDash, subs: renderSubs, scan: renderScan, settings: renderSettings};
let current = 'dash';
function go(tab){
  current = tab;
  document.querySelectorAll('nav button').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
  Object.keys(TABS).forEach(k => $('#tab-' + k).hidden = (k !== tab));
  TABS[tab]();
}
document.querySelectorAll('nav button[data-tab]').forEach(b => b.onclick = () => go(b.dataset.tab));
$('#themeBtn').onclick = toggleTheme;
$('#themeBtn').textContent = document.documentElement.dataset.theme === 'light' ? '☀️' : '🌙';
go('dash');
setInterval(()=>{ if (current === 'dash' && !document.hidden) renderDash(); }, 30000);
</script>
</body>
</html>
"""
