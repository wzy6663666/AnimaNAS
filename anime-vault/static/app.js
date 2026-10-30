/* ANIME VAULT — front-end */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const state = {
  drives: [],
  driveId: null,
  allMode: false,
  overview: null,
  view: 'overview',
  lastView: 'overview',
  data: null,
  filter: 'all',
  sort: 'size',
  q: '',
  browsePath: '',
  scan: null,
  match: null,
  modal: null,
};

/* ---------------- helpers ---------------- */
const fmt = (b, d = 1) => {
  if (!b && b !== 0) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, n = b;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(n < 10 ? d : 0)) + ' ' + u[i];
};
const pct = (a, b) => (b ? (a * 100 / b) : 0);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const hue = s => { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360; return h; };
const ago = t => {
  if (!t) return '未扫描';
  const d = new Date(t.replace(/-/g, '/'));
  const m = Math.floor((Date.now() - d.getTime()) / 60000);
  if (m < 1) return '刚刚';
  if (m < 60) return m + ' 分钟前';
  if (m < 1440) return Math.floor(m / 60) + ' 小时前';
  if (m < 43200) return Math.floor(m / 1440) + ' 天前';
  return t.slice(0, 10);
};
const api = async (path, opt) => {
  const r = await fetch(path, opt || {});
  return r.json();
};
const post = (path, body) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });

function toast(msg, kind) {
  const el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = 0; el.style.transition = '.3s'; setTimeout(() => el.remove(), 320); }, 3200);
}

/* ---------------- sidebar ---------------- */
function renderSidebar() {
  const box = $('#driveList');
  $('#driveCount').textContent = state.drives.length;
  const allMeta = $('#allMeta');
  if (allMeta) {
    const shows = state.drives.reduce((a, d) => a + (d.show_count || 0), 0);
    const online = state.drives.filter(d => d.online).length;
    allMeta.textContent = `${state.drives.length} 块 · ${shows} 部 · ${online} 在线`;
  }
  const btnAll = $('#btnAll');
  if (btnAll) btnAll.classList.toggle('on', !!state.allMode);
  if (!state.drives.length) {
    box.innerHTML = `<div style="padding:26px 10px;text-align:center;color:var(--dim);font-size:11.5px;line-height:1.7">
      还没有记录任何硬盘<br>接入硬盘后点下方按钮开始快照</div>`;
    return;
  }
  box.innerHTML = state.drives.map(d => {
    const used = Math.max(0, d.total_bytes - d.free_bytes);
    const p = pct(used, d.total_bytes);
    const on = d.online;
    const hs = d.health && d.health.score;
    const hcol = hs == null ? 'var(--dim)' : hs >= 85 ? 'var(--ok)' : hs >= 70 ? 'var(--warn)' : 'var(--bad)';
    return `<button class="drive-item ${d.id === state.driveId ? 'active' : ''}" data-id="${d.id}">
      <div class="di-top">
        <span class="di-letter">${esc(d.letter || '?')}:</span>
        <span class="di-name">${esc(d.label)}</span>
        <span class="di-state ${on ? 'on' : ''}" title="${on ? '已连接' : '未连接（快照模式）'}"></span>
      </div>
      <div class="di-bar"><i style="width:${p.toFixed(1)}%"></i></div>
      <div class="di-meta">
        <span>${fmt(used)} / ${fmt(d.total_bytes, 0)}</span>
        <span class="h">${d.show_count} 部${hs != null ? ` · <b style="color:${hcol}">${hs}</b>` : ''}</span>
      </div>
    </button>`;
  }).join('');
  $$('.drive-item', box).forEach(b => b.onclick = () => selectDrive(+b.dataset.id));
}

/* ---------------- view switching ---------------- */
function setView(v) {
  state.view = v;
  state.q = '';
  const si = $('#search'); if (si) si.value = '';
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.view === v));
  renderView();
}
$$('.tab').forEach(t => t.onclick = () => setView(t.dataset.view));

/* ---------------- overview ---------------- */
const ringSVG = (score) => {
  const r = 38, c = 2 * Math.PI * r;
  const p = Math.max(0, Math.min(100, score == null ? 0 : score));
  const col = score == null ? '#3a404a' : p >= 85 ? '#4ade80' : p >= 70 ? '#fbbf24' : p >= 55 ? '#fb923c' : '#f87171';
  return `<div class="ring"><svg width="92" height="92">
      <circle cx="46" cy="46" r="${r}" stroke="rgba(255,255,255,.07)" stroke-width="6" fill="none"/>
      <circle cx="46" cy="46" r="${r}" stroke="${col}" stroke-width="6" fill="none" stroke-linecap="round"
        stroke-dasharray="${c}" stroke-dashoffset="${c - c * p / 100}"
        style="filter:drop-shadow(0 0 6px ${col}55);transition:stroke-dashoffset .8s"/>
    </svg><div class="ring-txt"><b style="color:${col}">${score == null ? '—' : score}</b><span>健康指数</span></div></div>`;
};

function overviewHTML(d) {
  const used = Math.max(0, d.total_bytes - d.free_bytes);
  const h = d.health;
  const shows = state.data.shows;
  const total = d.show_bytes || 1;
  const offsets = shows.reduce((a, s) => a + s.size, 0);

  const top = shows.slice(0, 11);
  const maxSize = top.length ? top[0].size : 1;

  const groupBy = (key) => {
    const m = {};
    shows.forEach(s => { const k = s[key] || '未知'; m[k] = (m[k] || 0) + s.size; });
    return Object.entries(m).sort((a, b) => b[1] - a[1]);
  };
  const srcs = groupBy('source'), codecs = groupBy('codec'), subs = groupBy('subtitle'), res = groupBy('resolution');
  const colors = ['#e6bb84', '#7dd3fc', '#a78bfa', '#4ade80', '#f472b6', '#fbbf24', '#94a3b8'];
  const splitBlock = (label, entries) => {
    const tot = entries.reduce((a, e) => a + e[1], 0) || 1;
    const bars = entries.map((e, i) => `<i style="width:${(e[1] * 100 / tot).toFixed(2)}%;background:${colors[i % colors.length]}"></i>`).join('');
    const legend = entries.slice(0, 5).map((e, i) => `<span><em style="background:${colors[i % colors.length]}"></em>${esc(e[0])} ${(e[1] * 100 / tot).toFixed(0)}%</span>`).join('');
    return `<div class="split-row"><span class="lbl">${label}</span><div class="split-bar">${bars || '<u style="width:100%"></u>'}</div></div><div class="split-legend">${legend}</div>`;
  };

  const healthDetail = h && h.available ? `
    <div class="health-detail">
      <div><span>通电时长</span><b>${h.power_on_hours ? (h.power_on_hours + ' 小时') : '—'}</b></div>
      <div><span>约合</span><b>${h.power_on_hours ? (h.power_on_hours / 8760).toFixed(1) + ' 年' : '—'}</b></div>
      <div><span>温度</span><b>${h.temperature != null ? h.temperature + ' °C' : '—'}</b></div>
      <div><span>启停次数</span><b>${h.start_stop != null ? h.start_stop : '—'}</b></div>
      <div><span>读取重试</span><b>${h.read_errors != null ? h.read_errors : '—'}</b></div>
      <div><span>厂商状态</span><b>${esc(h.health_status || '—')}</b></div>
    </div>
    <div style="font-size:10.5px;color:var(--dim);line-height:1.65;margin-top:10px">
      ${(h.notes || []).map(n => '· ' + esc(n)).join('<br>')}
    </div>` : `<div style="font-size:11.5px;color:var(--dim);line-height:1.8">${esc((h && h.reason) || '尚未读取该盘的 SMART 数据')}</div>`;

  return `
  <div class="ov-hero">
    <div class="hero-grid">
      <div class="hero-left">
        <div class="hero-label">
          <span class="letter-badge">${esc(d.letter || '?')}:</span>
          <h1>${esc(d.label)}</h1>
          ${d.online ? '' : '<span class="offline-note">● 未连接 · 快照模式</span>'}
          <button class="btn sm ghost" id="btnRename">重命名</button>
          <button class="btn sm ghost" id="btnRescan" ${d.online ? '' : 'disabled style="opacity:.4"'}>重新扫描</button>
          <button class="btn sm ghost" id="btnMatch">更新封面</button>
        </div>
        <div class="hero-meta">
          <span>${esc(d.volume_label || '未命名卷')} · ${esc(d.health && d.health.disk ? d.health.disk : (d.model || '磁盘'))}</span>
          <span>上次扫描 ${ago(d.last_scan)}</span>
          <span>${d.file_count.toLocaleString()} 个文件</span>
        </div>
        ${(state.data.roots && state.data.roots.length) ? `<div class="root-chips">
          ${state.data.roots.map(r => `<span class="root-chip">${r.path ? '📁 ' + esc(r.path) : '整个硬盘'} · <b>${r.show_count || 0}</b> 部</span>`).join('')}
        </div>` : ''}
        <div class="cap-wrap">
          <div class="cap-bar"><i style="width:${pct(used, d.total_bytes).toFixed(2)}%"></i><u></u></div>
          <div class="cap-legend">
            <span>已用 <b>${fmt(used)}</b> <span class="dim">/ ${fmt(d.total_bytes, 0)}</span></span>
            <span>剩余 <b>${fmt(d.free_bytes)}</b></span>
          </div>
        </div>
      </div>
      <div class="health-box">
        ${ringSVG(h && h.available ? h.score : null)}
        <div class="health-info">
          <b>硬盘健康 ${h && h.available ? `<span class="grade" style="background:${h.score >= 85 ? 'rgba(74,222,128,.14);color:#4ade80' : h.score >= 70 ? 'rgba(251,191,36,.14);color:#fbbf24' : 'rgba(248,113,113,.16);color:#f87171'}">${esc(h.grade)}</span>` : ''}</b>
          <div>${h && h.available ? `已通电 ${(h.power_on_hours || 0).toLocaleString()} 小时` : '未读取到 SMART'}</div>
          <div style="margin-top:6px"><button class="btn sm" id="btnHealth">重新检测</button></div>
        </div>
      </div>
    </div>
  </div>

  <div class="stat-grid">
    <div class="stat"><div class="k">番剧条目</div><div class="v">${d.show_count}<small>部</small></div></div>
    <div class="stat"><div class="k">番剧总体积</div><div class="v">${fmt(total).split(' ')[0]}<small>${fmt(total).split(' ')[1]}</small></div></div>
    <div class="stat"><div class="k">文件总数</div><div class="v">${d.file_count.toLocaleString()}<small>个</small></div></div>
    <div class="stat"><div class="k">平均体积</div><div class="v">${d.show_count ? fmt(total / d.show_count).split(' ')[0] : '—'}<small>${d.show_count ? fmt(total / d.show_count).split(' ')[1] + ' / 部' : ''}</small></div></div>
  </div>

  <div class="cols">
    <div class="card">
      <div class="panel-head"><h3>容量占比 · Top ${top.length}</h3><span class="hint">点击查看详情</span></div>
      <div class="panel-body">
        ${top.map((s, i) => `
          <div class="rank-row" data-show="${s.id}">
            <span class="rank-idx">${i + 1}</span>
            <div class="rank-main">
              <div class="rank-top"><span class="rank-name">${esc(s.title)}${s.season ? ` <span class="dim">S${s.season}</span>` : ''}</span><span class="rank-size">${fmt(s.size)}</span></div>
              <div class="rank-bar"><i style="width:${Math.max(2, s.size * 100 / maxSize)}%"></i></div>
            </div>
            <span class="rank-pct">${pct(s.size, total).toFixed(1)}%</span>
          </div>`).join('') || '<div class="empty">暂无数据</div>'}
      </div>
    </div>

    <div style="display:flex;flex-direction:column;gap:14px">
      <div class="card">
        <div class="panel-head"><h3>规格分布</h3><span class="hint">按体积</span></div>
        <div class="panel-body">
          ${splitBlock('来源', srcs)}
          ${splitBlock('编码', codecs)}
          ${splitBlock('清晰度', res)}
          ${splitBlock('字幕', subs)}
        </div>
      </div>
      <div class="card">
        <div class="panel-head"><h3>硬盘健康</h3><span class="hint">SMART 估算</span></div>
        <div class="panel-body">${healthDetail}</div>
      </div>
    </div>
  </div>`;
}

/* ---------------- all drives ---------------- */
const SPEC_COLORS = ['#e6bb84', '#7dd3fc', '#a78bfa', '#4ade80', '#f472b6', '#fbbf24', '#94a3b8'];

function specSplit(label, entries) {
  const tot = entries.reduce((a, e) => a + e[1], 0) || 1;
  const bars = entries.map((e, i) => `<i style="width:${(e[1] * 100 / tot).toFixed(2)}%;background:${SPEC_COLORS[i % SPEC_COLORS.length]}"></i>`).join('');
  const legend = entries.slice(0, 5).map((e, i) => `<span><em style="background:${SPEC_COLORS[i % SPEC_COLORS.length]}"></em>${esc(e[0])} ${(e[1] * 100 / tot).toFixed(0)}%</span>`).join('');
  return `<div class="split-row"><span class="lbl">${label}</span><div class="split-bar">${bars || '<u style="width:100%"></u>'}</div></div><div class="split-legend">${legend}</div>`;
}

function allHTML() {
  const o = state.overview, t = o.totals;
  const used = Math.max(0, t.capacity - t.free);
  const maxTop = o.top.length ? o.top[0].size : 1;
  return `
  <div class="ov-hero">
    <div class="hero-grid">
      <div class="hero-left">
        <div class="hero-label"><h1>全部硬盘</h1>
          <span class="offline-note">${t.drives} 块盘 · ${t.online} 块在线</span></div>
        <div class="hero-meta">
          <span>${t.shows} 部番剧</span><span>${t.files.toLocaleString()} 个文件</span>
          <span>跨盘搜索可在右上角切换范围</span>
        </div>
        <div class="cap-wrap">
          <div class="cap-bar"><i style="width:${pct(used, t.capacity).toFixed(2)}%"></i><u></u></div>
          <div class="cap-legend">
            <span>累计已用 <b>${fmt(used)}</b> <span class="dim">/ ${fmt(t.capacity)}</span></span>
            <span>累计剩余 <b>${fmt(t.free)}</b></span>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div class="stat-grid">
    <div class="stat"><div class="k">已记录硬盘</div><div class="v">${t.drives}<small>块</small></div></div>
    <div class="stat"><div class="k">番剧总数</div><div class="v">${t.shows}<small>部</small></div></div>
    <div class="stat"><div class="k">番剧总体积</div><div class="v">${fmt(t.bytes).split(' ')[0]}<small>${fmt(t.bytes).split(' ')[1]}</small></div></div>
    <div class="stat"><div class="k">文件总数</div><div class="v">${t.files.toLocaleString()}<small>个</small></div></div>
  </div>

  <div class="panel-head" style="padding:0 2px 10px"><h3 style="margin:0;font-size:13px">硬盘一览</h3>
    <span class="hint">点击进入单块硬盘</span></div>
  <div class="drive-cards">
    ${o.drives.map(d => {
      const u = Math.max(0, (d.total_bytes || 0) - (d.free_bytes || 0));
      const hs = d.health && d.health.score;
      const col = hs == null ? 'var(--dim)' : hs >= 85 ? 'var(--ok)' : hs >= 70 ? 'var(--warn)' : 'var(--bad)';
      return `<div class="dcard" data-id="${d.id}">
        <div class="dcard-top">
          <span class="di-letter">${esc(d.letter || '?')}:</span>
          <span class="dcard-name">${esc(d.label)}</span>
          <span class="di-state ${d.online ? 'on' : ''}" title="${d.online ? '已连接' : '未连接'}"></span>
        </div>
        <div class="dcard-bar"><i style="width:${pct(u, d.total_bytes).toFixed(1)}%"></i></div>
        <div class="dcard-meta"><span>${fmt(u)} / ${fmt(d.total_bytes, 0)}</span><span>${d.show_count} 部</span></div>
        <div class="dcard-foot">
          ${hs != null ? `<span class="chip" style="color:${col};border-color:${col}33">健康 ${hs}</span>` : '<span class="chip dim">健康未知</span>'}
          <span class="chip dim">${(d.file_count || 0).toLocaleString()} 文件</span>
          <span class="chip dim">${d.online ? '在线' : '离线快照'}</span>
        </div>
      </div>`;
    }).join('')}
  </div>

  <div class="cols">
    <div class="card">
      <div class="panel-head"><h3>跨盘容量排行</h3><span class="hint">占全部番剧体积</span></div>
      <div class="panel-body">
        ${o.top.map((s_, i) => `
          <div class="rank-row" data-show="${s_.id}">
            <span class="rank-idx">${i + 1}</span>
            <div class="rank-main">
              <div class="rank-top"><span class="rank-name">${esc(s_.title)}${s_.season ? ` <span class="dim">S${s_.season}</span>` : ''}</span><span class="rank-size">${fmt(s_.size)}</span></div>
              <div class="rank-bar"><i style="width:${Math.max(2, s_.size * 100 / maxTop)}%"></i></div>
            </div>
            <span class="rank-pct">${(s_.pct || 0).toFixed(1)}%</span>
          </div>`).join('')}
      </div>
    </div>
    <div class="card">
      <div class="panel-head"><h3>全库规格分布</h3><span class="hint">按体积</span></div>
      <div class="panel-body">
        ${specSplit('来源', o.spec.source)}
        ${specSplit('编码', o.spec.codec)}
        ${specSplit('清晰度', o.spec.resolution)}
        ${specSplit('字幕', o.spec.subtitle)}
      </div>
    </div>
  </div>`;
}

function wireAll() {
  $$('.dcard').forEach(c => c.onclick = () => selectDrive(+c.dataset.id));
  $$('.rank-row').forEach(r => r.onclick = () => openShow(+r.dataset.show));
}

/* ---------------- library ---------------- */
function posterInner(s) {
  if (s.cover) return `<img src="${s.cover}" alt="" loading="lazy" onerror="this.remove()">`;
  const h = hue(s.title || 'x');
  return `<div class="poster-ph" style="background:linear-gradient(160deg,hsl(${h} 26% 24%),hsl(${(h + 45) % 360} 30% 11%))">
      <div class="ph-char">${esc((s.title || '?').slice(0, 1))}</div>
    </div>`;
}
function chipsOf(s, compact) {
  const out = [];
  if (s.source) out.push(`<span class="chip ${s.source === 'BD' ? 'bd' : 'web'}">${esc(s.source)}</span>`);
  if (s.codec) out.push(`<span class="chip ${s.codec === 'HEVC' ? 'hevc' : 'h264'}">${esc(s.codec)}</span>`);
  if (s.resolution) out.push(`<span class="chip">${esc(s.resolution)}${s.bitdepth ? ' ' + esc(s.bitdepth) : ''}</span>`);
  if (s.subtitle) out.push(`<span class="chip hi">${esc(s.subtitle)}</span>`);
  if (s.audio && !compact) out.push(`<span class="chip dim">${esc(s.audio)}</span>`);
  if (s.container && !compact) out.push(`<span class="chip dim">${esc(s.container)}</span>`);
  return out.slice(0, compact ? 4 : 8).join('');
}

function libraryHTML(shows, totalBytes, label, allMode) {
  let list = (shows || []).slice();
  const f = state.filter;
  if (f === 'bd') list = list.filter(s => s.source === 'BD');
  else if (f === 'web') list = list.filter(s => s.source && s.source.indexOf('WEB') === 0);
  else if (f === 'hevc') list = list.filter(s => s.codec === 'HEVC');
  else if (f === 'chs') list = list.filter(s => (s.subtitle || '').match(/简|GB|CHS/i));
  else if (f === 'cht') list = list.filter(s => (s.subtitle || '').match(/繁|CHT|BIG5/i));
  const sorters = {
    size: (a, b) => b.size - a.size,
    name: (a, b) => a.title.localeCompare(b.title, 'zh'),
    eps: (a, b) => b.video_count - a.video_count,
    date: (a, b) => (b.bgm_score || 0) - (a.bgm_score || 0),
  };
  list.sort(sorters[state.sort] || sorters.size);
  const total = totalBytes || 1;
  const maxSize = list.length ? Math.max(...list.map(s => s.size)) : 1;

  const filters = [['all', '全部'], ['bd', 'BD 原盘'], ['web', 'WEB 录制'], ['hevc', 'HEVC'], ['chs', '含简体'], ['cht', '含繁体']];
  return `
  <div class="page-head">
    <div>
      <div class="page-title">番剧墙 <span class="sub">${esc(label)} · ${list.length} 部</span></div>
      <div class="page-desc">自动识别片名、汉化组与视频规格，点击封面查看文件构成</div>
    </div>
  </div>
  <div class="toolbar">
    ${filters.map(([k, t]) => `<button class="fchip ${f === k ? 'on' : ''}" data-f="${k}">${t}</button>`).join('')}
    <div class="spacer"></div>
    <select class="sortsel" id="sortsel">
      <option value="size" ${state.sort === 'size' ? 'selected' : ''}>按体积排序</option>
      <option value="name" ${state.sort === 'name' ? 'selected' : ''}>按名称排序</option>
      <option value="eps" ${state.sort === 'eps' ? 'selected' : ''}>按集数排序</option>
      <option value="date" ${state.sort === 'date' ? 'selected' : ''}>按评分排序</option>
    </select>
  </div>
  <div class="grid-shows">
    ${list.map((s, i) => `
      <div class="show-card" data-show="${s.id}" style="animation-delay:${Math.min(i * 14, 320)}ms">
        <div class="poster">
          ${posterInner(s)}
          <div class="poster-shade"></div>
          <div class="poster-badges">
            <span class="pb ${s.source === 'BD' ? 'bd' : s.source && s.source.indexOf('WEB') === 0 ? 'web' : ''}">${esc(s.source || '—')}</span>
            <span class="pb">${fmt(s.size)}</span>
          </div>
          <div class="poster-foot">
            <div class="poster-title">${s.season ? `<span class="season-tag">S${s.season}</span>` : ''}${esc(s.title)}</div>
            <div class="poster-sub">${esc(s.fansub || '未知汉化组')} · ${s.video_count} 话${allMode ? ' · ' + esc(s.drive_label || '') : ''}</div>
          </div>
        </div>
        <div class="card-body">
          <div class="card-chips">${chipsOf(s, true)}</div>
          <div class="card-meta"><span>${pct(s.size, total).toFixed(1)}% 占盘</span><span>${fmt(s.size / (s.video_count || 1))} / 话</span></div>
          <div class="mini-bar"><i style="width:${Math.max(1.5, s.size * 100 / maxSize)}%"></i></div>
        </div>
      </div>`).join('')}
  </div>`;
}

/* ---------------- browse ---------------- */
async function browseHTML() {
  const d = curDrive();
  const path = state.browsePath;
  const js = await api(`/api/entries?drive=${d.id}&path=${encodeURIComponent(path)}`);
  const items = js.items || [];
  const used = Math.max(0, d.total_bytes - d.free_bytes);
  const total = path ? (items.reduce((a, i) => a + i.size, 0) || 1) : used;
  const crumb = (js.crumb || []);
  const crumbsHTML = [`<button data-p="">${esc(d.letter)}:\\</button>`]
    .concat(crumb.map((c, i) => `<span class="sep">/</span><button data-p="${esc(crumb.slice(0, i + 1).join('/'))}">${esc(c)}</button>`))
    .join('');
  const maxItem = items.reduce((a, i) => Math.max(a, i.size), 1);
  const rows = items.map(it => {
    const p = pct(it.size, total);
    const rel = Math.max(1.5, it.size * 100 / maxItem);
    return `<tr class="${it.is_dir ? 'dir' : ''}" data-path="${esc(it.path)}" data-dir="${it.is_dir}">
      <td><div class="tname ${it.is_dir ? 'dir' : ''}"><span class="ico">${it.is_dir ? folderIcon : fileIcon}</span><span class="nm">${esc(it.name)}</span></div></td>
      <td class="num">${fmt(it.size)}</td>
      <td class="num"><div class="bar-cell"><div class="b"><i style="width:${rel.toFixed(1)}%"></i></div><span style="width:42px;text-align:right">${p < 0.1 ? '<0.1' : p.toFixed(1)}%</span></div></td>
      <td class="num">${it.is_dir ? (it.file_count || 0) + ' 文件' : '—'}</td>
      <td class="num dim">${it.is_dir ? (it.dir_count || 0) + ' 目录' : (it.ext || '')}</td>
    </tr>`;
  }).join('');

  return `
  <div class="page-head">
    <div>
      <div class="page-title">目录浏览 <span class="sub">${items.length} 项</span></div>
      <div class="page-desc">按体积从大到小排列，点击文件夹进入下一层</div>
    </div>
  </div>
  <div class="crumbs">${crumbsHTML}</div>
  <div class="tbl-wrap">
    <table class="tbl">
      <thead><tr><th>名称</th><th class="num">大小</th><th class="num">占比</th><th class="num">内容</th><th class="num">类型</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="5" style="text-align:center;padding:40px;color:var(--dim)">此目录为空</td></tr>'}</tbody>
    </table>
  </div>`;
}

const folderIcon = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 7h6l2 2h10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>`;
const fileIcon = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>`;

/* ---------------- search ---------------- */
async function searchHTML() {
  const q = state.q.trim();
  if (!q) return `<div class="empty"><b>输入关键词开始搜索</b>支持番剧名、文件夹名、文件名</div>`;
  const all = state.allMode || state.searchScope === 'all';
  const js = await api(`/api/search?q=${encodeURIComponent(q)}&drive=${all ? 0 : (state.driveId || 0)}`);
  const items = js.items || [];
  const hl = (t) => esc(t).replace(new RegExp('(' + q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'ig'), '<mark>$1</mark>');
  return `
  <div class="page-head"><div>
    <div class="page-title">搜索结果 <span class="sub">“${esc(q)}” · 命中 ${js.total} 项</span></div>
    <div class="page-desc">含已拔下硬盘的历史快照，可随时翻查</div>
  </div>
  <div class="toolbar" style="margin-bottom:12px">
    <button class="fchip ${all ? '' : 'on'}" data-scope="drive">仅当前盘</button>
    <button class="fchip ${all ? 'on' : ''}" data-scope="all">所有硬盘（${state.drives.length}）</button>
  </div></div>
  <div class="card" style="padding:6px">
    ${items.map(it => `
      <div class="res-item" data-show="${it.show_id || ''}" data-path="${esc(it.path)}" data-drive="${it.drive_id}">
        ${it.cover ? `<div class="res-thumb"><img src="${it.cover}" loading="lazy"></div>` : `<div class="res-thumb" style="opacity:.25;display:grid;place-items:center">${it.is_dir ? folderIcon : fileIcon}</div>`}
        <div class="res-main">
          <div class="res-name">${it.is_dir ? '📁 ' : ''}${hl(it.name)}</div>
          <div class="res-path">${esc(it.drive_label || '')} · ${esc(it.path)}</div>
        </div>
        <div class="res-size">${fmt(it.size)}</div>
      </div>`).join('') || `<div class="empty">没有找到匹配项</div>`}
  </div>`;
}

/* ---------------- render ---------------- */
function curDrive() { return state.drives.find(d => d.id === state.driveId) || state.drives[0]; }

async function renderView() {
  const box = $('#view');
  if (!state.drives.length) {
    box.innerHTML = `<div class="empty" style="padding-top:14vh">
      <b>还没有记录任何硬盘</b>
      把装着番剧的硬盘接上电脑，点击左下角「记录新硬盘」，几秒钟就能生成一份完整快照。<br>
      硬盘拔掉之后，这份快照依然能查看容量、目录结构和番剧识别结果。
    </div>`;
    return;
  }
  if (state.allMode) {
    if (state.q.trim()) {
      $$('.tab').forEach(t => t.classList.remove('active'));
      box.innerHTML = await searchHTML();
      wireSearch();
      return;
    }
    if (!state.overview) {
      box.innerHTML = `<div class="skeleton" style="height:180px;margin-bottom:14px"></div>
        <div class="skeleton" style="height:96px"></div>`;
      await loadOverview();
    }
    if (state.view === 'library') {
      box.innerHTML = libraryHTML(state.overview.shows, state.overview.totals.bytes, '全部硬盘', true);
      wireLibrary();
    } else if (state.view === 'browse') {
      box.innerHTML = `<div class="empty"><b>目录浏览需要先选一块硬盘</b>
        在左侧点选具体硬盘，再进入「目录」查看它的文件夹结构。</div>`;
    } else {
      box.innerHTML = allHTML();
      wireAll();
    }
    return;
  }
  if (!state.data || state.data.drive.id !== state.driveId) {
    box.innerHTML = `<div class="skeleton" style="height:180px;margin-bottom:14px"></div>
      <div class="skeleton" style="height:96px"></div>`;
    await loadDrive(state.driveId);
    return;
  }
  const d = state.data.drive;
  if (state.q.trim()) {
    $$('.tab').forEach(t => t.classList.remove('active'));
    box.innerHTML = `<div class="skeleton" style="height:90px"></div>`;
    box.innerHTML = await searchHTML();
    wireSearch();
    return;
  }
  if (state.view === 'overview') {
    box.innerHTML = overviewHTML(d);
    wireOverview();
  } else if (state.view === 'library') {
    box.innerHTML = libraryHTML(state.data.shows, d.show_bytes, d.label, false);
    wireLibrary();
  } else if (state.view === 'browse') {
    box.innerHTML = `<div class="skeleton" style="height:90px"></div>`;
    box.innerHTML = await browseHTML();
    wireBrowse();
  }
}

function wireOverview() {
  const b = $('#btnRename');
  if (b) b.onclick = () => renameDrive();
  const hb = $('#btnHealth');
  if (hb) hb.onclick = async () => {
    hb.textContent = '检测中…';
    const js = await post('/api/health/refresh', { drive_id: state.driveId });
    if (js.health && js.health.available) { toast('健康数据已更新', 'ok'); await refreshAll(); }
    else { toast((js.health && js.health.reason) || '读取失败'); hb.textContent = '重新检测'; }
  };
  $$('.rank-row').forEach(r => r.onclick = () => openShow(+r.dataset.show));
  const rb = $('#btnRescan');
  if (rb) rb.onclick = async () => {
    const d = state.data.drive;
    if (!d.online) { toast('该硬盘当前未连接'); return; }
    const res = await post('/api/drive/rescan', { drive_id: d.id });
    if (res.error) { toast(res.error); return; }
    showScanProgress();
  };
  const mb = $('#btnMatch');
  if (mb) mb.onclick = async () => {
    mb.textContent = '匹配中…';
    const res = await post('/api/match', { drive_id: state.driveId, scope: 'covers' });
    if (res.error) { toast(res.error); mb.textContent = '更新封面'; return; }
    matchToast();
  };
}

function wireLibrary() {
  $$('.fchip').forEach(b => b.onclick = () => { state.filter = b.dataset.f; renderView(); });
  const s = $('#sortsel');
  if (s) s.onchange = () => { state.sort = s.value; renderView(); };
  $$('.show-card').forEach(c => c.onclick = () => openShow(+c.dataset.show));
}

function wireBrowse() {
  $$('.crumbs button').forEach(b => b.onclick = () => { state.browsePath = b.dataset.p; renderView(); });
  $$('tr[data-path]').forEach(tr => tr.onclick = () => {
    if (tr.dataset.dir === '1') { state.browsePath = tr.dataset.path; renderView(); }
  });
}

function wireSearch() {
  $$('.fchip[data-scope]').forEach(b => b.onclick = () => {
    state.searchScope = b.dataset.scope;
    renderView();
  });
  $$('.res-item').forEach(r => r.onclick = () => {
    if (r.dataset.show) openShow(+r.dataset.show);
    else if (r.dataset.path) { state.browsePath = r.dataset.path; setView('browse'); }
  });
}

/* ---------------- data loading ---------------- */
async function loadState() {
  const js = await api('/api/state');
  state.drives = js.drives || [];
  state.scan = js.scan;
  state.match = js.match;
  if (!state.driveId && state.drives.length) state.driveId = state.drives[0].id;
  if (state.driveId && !state.drives.find(d => d.id === state.driveId)) state.driveId = state.drives[0] ? state.drives[0].id : null;
  renderSidebar();
}

const healthTried = {};
async function loadDrive(id) {
  const js = await api('/api/drive?id=' + id);
  if (js.error) { toast('读取失败'); return; }
  state.data = js;
  if (!js.drive.health && js.drive.online && !healthTried[js.drive.id]) {
    healthTried[js.drive.id] = true;
    post('/api/health/refresh', { drive_id: id }).then(h => {
      if (h.health && h.health.available) refreshAll();
    }).catch(() => {});
  }
  renderView();
}

async function selectDrive(id) {
  state.driveId = id;
  state.allMode = false;
  state.browsePath = '';
  state.data = null;
  renderSidebar();
  await loadDrive(id);
}

async function loadOverview() {
  state.overview = await api('/api/overview');
  renderView();
}

async function selectAll() {
  state.allMode = true;
  state.driveId = null;
  state.data = null;
  state.browsePath = '';
  if (state.view === 'browse') state.view = 'overview';
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.view === state.view));
  renderSidebar();
  await loadOverview();
}

async function matchToast() {
  const js = await api('/api/tasks');
  const m = js.match;
  if (m.active) {
    toast(`正在更新封面 ${m.done}/${m.total}${m.current ? ' · ' + m.current : ''}`);
    setTimeout(matchToast, 2200);
  } else if ((m.message || '').indexOf('无法连接') >= 0) {
    toast('无法连接 Bangumi，请检查网络后重试', 'err');
    await refreshAll();
  } else {
    toast(`封面更新完成：成功 ${m.matched} 部，未匹配 ${m.failed} 部`, 'ok');
    await refreshAll();
  }
}

async function refreshAll() {
  if (state.allMode) {
    await loadState();
    state.allMode = true;
    state.driveId = null;
    await loadOverview();
    return;
  }
  const keep = state.driveId;
  await loadState();
  if (keep) { state.driveId = keep; await loadDrive(keep); }
}

/* ---------------- show drawer ---------------- */
async function openShow(id) {
  const js = await api('/api/show?id=' + id);
  if (!js.show) return;
  const s = js.show, d = js.drive;
  $('#drawer').innerHTML = `
    <div class="drawer-head">
      <div class="d-cover">${posterInner(s)}</div>
      <div class="d-head-main">
        <div class="d-title">${esc(s.title)}${s.season ? ` <span class="dim" style="font-size:13px">第 ${s.season} 季</span>` : ''}</div>
        <div class="d-sub">
          ${esc(s.fansub || '汉化组未识别')}${s.episodes ? ' · ' + esc(s.episodes) + ' 话' : ''}<br>
          <span class="dim">${esc(d.letter)}:\\${esc(s.folder_name)}</span>
        </div>
        <div class="d-chips">${chipsOf(s)}</div>
        <div class="d-actions">
          <button class="btn sm" id="dOpen">在资源管理器中打开</button>
          <button class="btn sm" id="dEdit">编辑信息</button>
          <button class="btn sm" id="dCover">更换封面</button>
        </div>
      </div>
      <button class="close-x" id="dClose"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 6l12 12M18 6 6 18"/></svg></button>
    </div>
    <div class="drawer-body">
      <div class="d-section">
        <h4>概况</h4>
        <div class="kv">
          <div><span>体积</span><b>${fmt(s.size)}</b></div>
          <div><span>占本盘</span><b>${s.pct.toFixed(2)}%</b></div>
          <div><span>视频数</span><b>${s.video_count} 话</b></div>
          <div><span>文件数</span><b>${s.file_count}</b></div>
          <div><span>平均每话</span><b>${fmt(s.size / (s.video_count || 1))}</b></div>
          <div><span>容器</span><b>${esc(s.container || '—')}</b></div>
          ${s.bgm_score ? `<div><span>Bangumi</span><b style="color:var(--gold)">★ ${s.bgm_score}</b></div>` : ''}
          ${s.bgm_date ? `<div><span>放送开始</span><b>${esc(s.bgm_date)}</b></div>` : ''}
        </div>
      </div>
      <div class="d-section">
        <h4>子目录 <span class="dim">${(js.dirs || []).length}</span></h4>
        <div class="file-list">
          ${(js.dirs || []).map(x => `<div class="file-row dir-row" data-p="${esc(x.path)}" style="cursor:pointer">
            <span style="color:var(--gold);display:flex">${folderIcon}</span>
            <span class="fn">${esc(x.name)}</span>
            <span class="fs">${fmt(x.size)}</span></div>`).join('') || '<div class="file-row"><span class="fn dim">无子目录</span></div>'}
        </div>
      </div>
      <div class="d-section">
        <h4>文件构成 <span class="dim">${(js.files || []).length} 项</span></h4>
        <div class="file-list">
          ${(js.files || []).slice(0, 80).map(f => `<div class="file-row">
            <span style="color:var(--dim);display:flex">${fileIcon}</span>
            <span class="fn" title="${esc(f.name)}">${esc(f.name)}</span>
            <span class="fb"><i style="width:${Math.min(100, f.pct * 4)}%"></i></span>
            <span class="fs">${fmt(f.size)}</span></div>`).join('')}
        </div>
      </div>
    </div>`;
  $('#drawer').classList.add('on');
  $('#drawerMask').classList.add('on');

  $('#dClose').onclick = closeDrawer;
  $$('.dir-row').forEach(r => r.onclick = () => { closeDrawer(); state.browsePath = r.dataset.p; setView('browse'); });
  $('#dOpen').onclick = async () => {
    const res = await post('/api/open', { drive_id: s.drive_id, path: s.path });
    if (res.error) toast(res.error);
  };
  $('#dEdit').onclick = () => editShow(s);
  $('#dCover').onclick = () => { closeDrawer(); openCoverPicker(s); };
}

function closeDrawer() {
  $('#drawer').classList.remove('on');
  $('#drawerMask').classList.remove('on');
}
$('#drawerMask').onclick = closeDrawer;

/* ---------------- cover picker ---------------- */
const SRC_LABEL = { anilist: 'AniList', bangumi: 'Bangumi', local: '文件夹图片', manual: '手动设置', bgm: 'Bangumi' };

async function openCoverPicker(s) {
  const curSrc = SRC_LABEL[s.cover_src] || '无';
  const q = encodeURIComponent(s.title_cn || s.title || '');
  openModal(`<h2>更换封面</h2><div class="m-desc">正在检索候选海报…</div>
    <div class="skeleton" style="height:220px"></div>`, true);
  const js = await api('/api/show/candidates?show_id=' + s.id);
  const cands = js.candidates || [];
  const locals = js.local || [];

  const item = (c, kind) => `
    <div class="cover-item" title="${esc(c.name || '')} ${esc(c.year || '')}"
         data-kind="${kind}" data-url="${esc(c.image || c.url || '')}" data-path="${esc(c.path || '')}">
      <img src="${kind === 'net' ? '/api/img?u=' + encodeURIComponent(c.image) : c.url}" loading="eager" alt="">
      ${c.source ? `<span class="ci-tag">${esc(c.source)}</span>` : ''}
      <div class="ci-name">${esc((c.name || c.name === '' ? c.name : '') && (c.name || '') )}${c.year ? ' · ' + esc(c.year) : ''}${c.w ? esc(c.w + '×' + c.h) : ''}</div>
      ${(kind === 'local' && s.cover && c.url && false) ? '' : ''}
    </div>`;

  openModal(`<h2>更换封面</h2>
    <div class="m-desc">共 ${cands.length} 个网络候选 · ${locals.length} 张文件夹图片</div>
    <div class="pick-current">
      <div class="pc-img">${s.cover ? `<img src="${s.cover}">` : ''}</div>
      <div class="pc-main">
        <div class="pc-name">${esc(s.title)}</div>
        <div class="dim" style="font-size:11px;margin-top:2px">当前封面：${esc(curSrc)}${s.bgm_name ? ' · ' + esc(s.bgm_name) : ''}</div>
        <div class="pick-links">
          <a href="${esc('https://bgm.tv/subject_search/' + q + '?cat=2')}" target="_blank">在 Bangumi 搜「${esc((s.title_cn || s.title).slice(0, 12))}」</a>
          <a href="${esc('https://www.dandanplay.com/search?keyword=' + q)}" target="_blank">在弹弹play搜索</a>
        </div>
      </div>
    </div>

    ${cands.length ? `<label class="m-label">网络候选（点击套用）</label>
      <div class="cover-grid" id="netGrid">${cands.map(c => item(c, 'net')).join('')}</div>` :
      `<div class="dim" style="font-size:11.5px;margin:14px 0">没有联网候选：Bangumi / AniList 都没匹配到，或当前网络不通。</div>`}

    ${locals.length ? `<label class="m-label">文件夹里的图片（点击套用）</label>
      <div class="cover-grid" id="localGrid">${locals.map(c => item(c, 'local')).join('')}</div>` : ''}

    <label class="m-label">或者直接贴图片地址 / 上传</label>
    <div style="display:flex;gap:7px;flex-wrap:wrap">
      <input class="m-input" id="cvUrl" placeholder="https://…（任意图片地址）" style="flex:1;min-width:170px">
      <button class="btn sm" id="cvUse">使用</button>
      <label class="btn sm" style="cursor:pointer">上传<input type="file" id="cvFile" accept="image/*" hidden></label>
    </div>
    <div class="m-foot">
      <button class="btn ghost" id="dmAuto">自动匹配一次</button>
      <button class="btn ghost" onclick="closeModal()">关闭</button>
    </div>`, true);

  const apply = async (body) => {
    const r = await post('/api/show/pick', Object.assign({ show_id: s.id }, body));
    if (r.error) { toast(r.error); return; }
    toast('封面已更新', 'ok');
    closeModal();
    await refreshAll();
    openShow(s.id);
  };
  $$('.cover-item[data-kind="net"]').forEach(el => el.onclick = () => apply({ url: el.dataset.url }));
  $$('.cover-item[data-kind="local"]').forEach(el => el.onclick = () => apply({ path: el.dataset.path }));
  $('#cvUse').onclick = () => {
    const u = $('#cvUrl').value.trim();
    if (!u.startsWith('http')) { toast('请输入 http(s) 图片地址'); return; }
    apply({ url: u });
  };
  $('#cvFile').onchange = (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const rd = new FileReader();
    rd.onload = async () => {
      const r = await post('/api/show/cover', { show_id: s.id, data: rd.result });
      if (r.ok) { toast('封面已更新', 'ok'); closeModal(); await refreshAll(); openShow(s.id); }
      else toast(r.error || '上传失败');
    };
    rd.readAsDataURL(f);
  };
  const auto = $('#dmAuto');
  if (auto) auto.onclick = async () => {
    auto.textContent = '匹配中…';
    const res = await post('/api/show/rematch', { show_id: s.id });
    if (res.ok) { toast('已自动匹配：' + (res.name || ''), 'ok'); closeModal(); await refreshAll(); openShow(s.id); }
    else { toast(res.error || '没有匹配到'); auto.textContent = '自动匹配一次'; }
  };
}

/* ---------------- modals ---------------- */
function openModal(html, wide) {
  $('#modal').classList.toggle('wide', !!wide);
  $('#modal').innerHTML = html;
  $('#modal').classList.add('on');
  $('#modalMask').classList.add('on');
}
function closeModal() {
  $('#modal').classList.remove('on');
  $('#modalMask').classList.remove('on');
}
$('#modalMask').onclick = closeModal;

async function renameDrive() {
  const d = curDrive();
  openModal(`<h2>重命名硬盘</h2><div class="m-desc">这个名字只保存在本地快照里，不会改动硬盘本身的卷标</div>
    <input class="m-input" id="mName" value="${esc(d.label)}">
    <div class="m-foot"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn primary" id="mOk">保存</button></div>`);
  $('#mName').focus();
  $('#mName').select();
  $('#mOk').onclick = async () => {
    const v = $('#mName').value.trim();
    if (!v) return;
    await post('/api/drive/label', { drive_id: d.id, label: v });
    closeModal(); await refreshAll(); toast('已重命名', 'ok');
  };
}

function editShow(s) {
  openModal(`<h2>编辑番剧信息</h2><div class="m-desc">用于自动识别不准确的情况，修改后重新扫描也会保留</div>
    <label class="m-label">显示标题</label>
    <input class="m-input" id="eTitle" value="${esc(s.title)}">
    <label class="m-label">中文名（用于匹配）</label>
    <input class="m-input" id="eCn" value="${esc(s.title_cn || '')}" placeholder="留空则使用标题">
    <label class="m-label">汉化组</label>
    <input class="m-input" id="eFs" value="${esc(s.fansub || '')}">
    <label class="m-label">封面</label>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <input class="m-input" id="eCover" placeholder="粘贴图片 URL" style="flex:1;min-width:180px">
      <label class="btn sm" style="cursor:pointer">上传本地图片<input type="file" id="eFile" accept="image/*" hidden></label>
      <button class="btn sm ghost" id="eReset">恢复自动</button>
    </div>
    <div class="m-foot"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn primary" id="eOk">保存</button></div>`);
  $('#eFile').onchange = async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = async () => {
      const res = await post('/api/show/cover', { show_id: s.id, data: reader.result });
      if (res.ok) { toast('封面已更新', 'ok'); closeModal(); await refreshAll(); openShow(s.id); }
      else toast(res.error || '上传失败');
    };
    reader.readAsDataURL(f);
  };
  $('#eReset').onclick = async () => {
    await post('/api/show/update', { show_id: s.id, reset_cover: true });
    closeModal(); await refreshAll(); openShow(s.id);
  };
  $('#eOk').onclick = async () => {
    const body = { show_id: s.id, title: $('#eTitle').value.trim(), title_cn: $('#eCn').value.trim(), fansub: $('#eFs').value.trim() };
    const c = $('#eCover').value.trim();
    await post('/api/show/update', body);
    if (c) {
      const res = await post('/api/cover/fetch', { show_id: s.id, url: c });
      if (!res.ok) toast(res.error || '封面下载失败');
    }
    closeModal(); await refreshAll(); openShow(s.id); toast('已保存', 'ok');
  };
}

/* ---------------- scan flow ---------------- */
let scanDlg = null;

async function startScanFlow() {
  openModal(`<h2>记录新硬盘</h2><div class="m-desc">正在读取已接入的磁盘…</div>
    <div class="skeleton" style="height:56px;margin-bottom:8px"></div>
    <div class="skeleton" style="height:56px"></div>`);
  const js = await api('/api/volumes');
  const vols = js.volumes || [];
  if (!vols.length) {
    openModal(`<h2>记录新硬盘</h2><div class="m-desc">没有检测到可用的磁盘，请确认硬盘已接入并分配了盘符</div>
      <div class="m-foot"><button class="btn ghost" onclick="closeModal()">关闭</button></div>`);
    return;
  }
  scanDlg = { vols, picked: vols.length === 1 ? vols[0] : null, path: '', name: '' };
  if (scanDlg.picked) scanDlg.name = scanDlg.picked.volume_label || (scanDlg.picked.letter + ' 盘');
  renderScanForm();
}

function renderScanForm() {
  const dlg = scanDlg;
  const vols = dlg.vols;
  openModal(`<h2>记录新硬盘</h2><div class="m-desc">只读取目录和文件大小，不会修改硬盘里的任何文件</div>
    <label class="m-label">选择盘符</label>
    <div class="vol-list" id="volList">
      ${vols.map(v => `<button class="vol-item ${dlg.picked && dlg.picked.letter === v.letter ? 'on' : ''}" data-letter="${v.letter}">
        <span class="vol-ico"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="5" width="18" height="6" rx="2"/><rect x="3" y="13" width="18" height="6" rx="2"/><circle cx="7" cy="8" r="1" fill="currentColor" stroke="none"/><circle cx="7" cy="16" r="1" fill="currentColor" stroke="none"/></svg></span>
        <span class="vol-main">
          <span class="vol-name">${v.letter}: ${esc(v.volume_label || '未命名')} ${v.known_drive_id ? '<span class="vol-tag">已记录</span>' : ''}</span>
          <span class="vol-sub">${fmt(v.total_bytes, 0)} · 可用 ${fmt(v.free_bytes)} · ${esc(v.fs)}</span>
        </span>
      </button>`).join('')}
    </div>

    <label class="m-label">扫描范围</label>
    <div class="scope-row">
      <button class="fchip ${dlg.path ? '' : 'on'}" data-scope="all">整个硬盘</button>
      <button class="fchip ${dlg.path ? 'on' : ''}" data-scope="pick">指定文件夹…</button>
    </div>
    ${dlg.path
      ? `<div class="dirbar" style="margin-top:7px">已选：<b style="color:var(--text)">${esc(dlg.picked ? dlg.picked.letter + ':' : '')}\\${esc(dlg.path)}</b></div>`
      : `<div class="dim" style="font-size:11px;margin-top:7px">整个硬盘：根目录下每个文件夹算一部番剧。番剧在子目录里时，改用「指定文件夹」。</div>`}

    <label class="m-label">给它起个名字</label>
    <input class="m-input" id="scanName" value="${esc(dlg.name)}" placeholder="例如：番剧盘 01 · 2024 冬">
    <div class="m-foot">
      <button class="btn ghost" onclick="closeModal()">取消</button>
      <button class="btn primary" id="scanGo">开始扫描</button>
    </div>`);

  $$('.vol-item').forEach(b => b.onclick = () => {
    dlg.picked = vols.find(v => v.letter === b.dataset.letter);
    dlg.path = '';
    if (!dlg.name.trim()) dlg.name = dlg.picked.volume_label || (dlg.picked.letter + ' 盘');
    renderScanForm();
  });
  $$('.fchip[data-scope]').forEach(b => b.onclick = () => {
    if (!dlg.picked) { toast('请先选择盘符'); return; }
    if (b.dataset.scope === 'pick') browseFolder(dlg.path || '');
    else { dlg.path = ''; renderScanForm(); }
  });
  $('#scanGo').onclick = async () => {
    if (!dlg.picked) { toast('请先选择盘符'); return; }
    const name = $('#scanName').value.trim() || (dlg.picked.letter + ' 盘');
    const res = await post('/api/scan', { letter: dlg.picked.letter, label: name, path: dlg.path });
    if (res.error) { toast(res.error); return; }
    showScanProgress();
  };
}

async function browseFolder(path) {
  const dlg = scanDlg;
  if (!dlg || !dlg.picked) return;
  openModal(`<h2>选择扫描文件夹</h2><div class="m-desc">${dlg.picked.letter}: 盘中挑一个文件夹</div>
    <div class="skeleton" style="height:220px"></div>`, true);
  const js = await api('/api/dirs', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ letter: dlg.picked.letter, path }) });
  if (js.error) { toast(js.error); renderScanForm(); return; }
  const crumb = js.crumb || [];
  const cur = js.path || '';
  const parent = cur.includes('/') ? cur.slice(0, cur.lastIndexOf('/')) : '';
  openModal(`<h2>选择扫描文件夹</h2>
    <div class="m-desc">当前：${esc(dlg.picked.letter)}:\${esc(cur)}　·　该文件夹下的每个子文件夹会被识别为一部番剧</div>
    <div class="dirbar">
      <button data-p="">${esc(dlg.picked.letter)}:\</button>
      ${crumb.map((c, i) => `<span class="sep">/</span><button data-p="${esc(crumb.slice(0, i + 1).join('/'))}">${esc(c)}</button>`).join('')}
    </div>
    <div class="dirlist">
      ${cur ? `<button class="dirrow" data-p="${esc(parent)}" data-up="1">↩ 上一级</button>` : ''}
      ${(js.items || []).map(it => `<button class="dirrow" data-p="${esc(it.path)}">
        ${folderIcon}<span>${esc(it.name)}</span><span class="kid">${it.kids} 项</span></button>`).join('')
        || '<div class="dim" style="padding:14px">这个文件夹里没有子文件夹</div>'}
    </div>
    <div class="m-foot">
      <button class="btn ghost" id="cbBack">返回</button>
      <button class="btn primary" id="cbUse">用这个文件夹</button>
    </div>`, true);

  $$('.dirrow').forEach(r => r.onclick = () => browseFolder(r.dataset.p));
  $$('.dirbar button').forEach(r => r.onclick = () => browseFolder(r.dataset.p));
  $('#cbBack').onclick = () => renderScanForm();
  $('#cbUse').onclick = () => { dlg.path = cur; renderScanForm(); };
}

function showScanProgress() {
  openModal(`<h2>正在建立快照</h2>
    <div class="m-desc" id="pgDesc">读取目录结构…</div>
    <div class="prog-num" id="pgFiles">0</div>
    <div class="dim" style="font-size:11px;margin-top:-2px">个文件已索引</div>
    <div class="prog-bar indet" id="pgBar"><i></i></div>
    <div class="prog-path" id="pgPath"></div>
    <div class="prog-stats">
      <span>体积 <b id="pgSize">0</b></span>
      <span>目录 <b id="pgDirs">0</b></span>
      <span>耗时 <b id="pgTime">0s</b></span>
    </div>`);
  pollScan(true);
}

let pollTimer = null;
async function pollScan(fromModal) {
  const js = await api('/api/tasks');
  state.scan = js.scan; state.match = js.match;
  const s = js.scan;
  if (fromModal && $('#pgFiles')) {
    $('#pgFiles').textContent = (s.files || 0).toLocaleString();
    $('#pgSize').textContent = fmt(s.bytes || 0);
    $('#pgDirs').textContent = s.dirs || 0;
    $('#pgTime').textContent = (s.elapsed || 0) + 's';
    $('#pgPath').textContent = (s.scan_path ? s.scan_path + '  ·  ' : '') + (s.path || s.message || '');
    $('#pgDesc').textContent = s.phase === 'indexing' ? '正在写入快照…' : s.phase === 'starting' ? '准备中…' : '读取目录结构…';
    if (s.files > 6) $('#pgBar').classList.remove('indet');
  }
  if (s.active) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(() => pollScan(fromModal), 600);
  } else if (fromModal && (s.phase === 'done' || s.phase === 'error')) {
    if (s.phase === 'done') {
      toast(`扫描完成：${(s.files || 0).toLocaleString()} 个文件 / ${fmt(s.bytes || 0)}`, 'ok');
      closeModal();
      const did = s.drive_id;
      await loadState();
      if (did) { state.driveId = did; state.data = null; await loadDrive(did); }
      if (did) post('/api/health/refresh', { drive_id: did }).then(h => { if (h.health && h.health.available) refreshAll(); });
    } else {
      toast(s.message || '扫描失败');
    }
  }
}

/* ---------------- search wiring ---------------- */
let searchTimer = null;
$('#search').addEventListener('input', (e) => {
  state.q = e.target.value;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    if (state.q.trim()) { if (state.view !== 'search') { state.lastView = state.view; } renderView(); }
    else { renderView(); }
  }, 220);
});
document.addEventListener('keydown', (e) => {
  if (e.key === '/' && document.activeElement !== $('#search')) { e.preventDefault(); $('#search').focus(); }
  if (e.key === 'Escape') { closeDrawer(); closeModal(); if ($('#search').value) { $('#search').value = ''; state.q = ''; renderView(); } }
});

/* ---------------- boot ---------------- */
$('#btnAdd').onclick = startScanFlow;
$('#btnAll').onclick = () => selectAll();
window.closeModal = closeModal;

(async function boot() {
  await loadState();
  if (state.drives.length) {
    state.driveId = state.drives[0].id;
    state.allMode = false;
    await loadDrive(state.driveId);
    if (state.scan && state.scan.active) pollScan(false);
  } else {
    renderView();
  }
  setInterval(async () => {
    const js = await api('/api/tasks');
    if (js.scan.active && !$('#modal').classList.contains('on')) pollScan(false);
  }, 4000);
})();
