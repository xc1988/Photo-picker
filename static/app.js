const $ = s => document.querySelector(s);
let photos = [];
let filter = 'all';
let vIdx = -1;               // viewer 中当前照片在 visible 列表里的位置
let visible = [];

/* ------------------------------------------------------------ helpers ---- */
async function api(url, body) {
  const r = await fetch(url, body ? {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  } : undefined);
  if (!r.ok) throw new Error(r.status);
  return r.json();
}
let toastTimer;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg; t.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), 2600);
}

/* --------------------------------------------------------------- stats --- */
async function refreshStats() {
  const s = await api('/api/stats');
  $('#stats').innerHTML =
    `<b class="p">已选 ${s.pick}</b><b class="n">已废 ${s.no}</b><b>待选 ${s.todo}</b>`;
}

/* ---------------------------------------------------------------- grid --- */
function visiblePhotos() {
  return photos.filter(p => filter === 'all' ? true : (p.status || 'todo') === filter);
}

function renderGrid() {
  visible = visiblePhotos();
  const grid = $('#grid');
  grid.innerHTML = '';
  $('#empty').classList.toggle('hidden', photos.length > 0);
  for (const [i, p] of visible.entries()) {
    const card = document.createElement('div');
    card.className = 'card' + (p.status ? ' st-' + p.status : '');
    card.innerHTML = `
      <div class="skel"></div>
      <img loading="lazy" src="/thumb/${p.id}" alt="">
      <span class="flag"></span>
      <span class="badge">${p.hasRaw && p.hasJpg ? 'RAW+JPG' : p.hasRaw ? 'RAW' : 'JPG'}</span>
      <span class="name">${p.name}</span>
      <span class="quick">
        <button class="ok" title="选它 (S)">✓</button>
        <button class="ng" title="淘汰 (X)">✕</button>
      </span>`;
    const img = card.querySelector('img');
    img.onload = () => { img.classList.add('done'); card.querySelector('.skel').remove(); };
    img.onerror = () => { img.src = ''; card.querySelector('.skel').style.animation = 'none'; };
    card.addEventListener('click', e => {
      if (e.target.closest('.quick')) return;
      openViewer(i);
    });
    card.querySelector('.ok').addEventListener('click', () => mark(p, 'pick'));
    card.querySelector('.ng').addEventListener('click', () => mark(p, 'no'));
    grid.appendChild(card);
  }
}

async function mark(p, status) {
  if (p.status === status) status = '';            // 再按一次取消
  p.status = status;
  await api('/api/mark', { id: p.id, status });
  refreshStats();
  renderGrid();
  if (!$('#viewer').classList.contains('hidden')) syncViewerButtons();
}

/* ------------------------------------------------------------- viewer ---- */
function openViewer(i) {
  vIdx = i;
  $('#viewer').classList.remove('hidden');
  showCurrent();
}
function closeViewer() {
  $('#viewer').classList.add('hidden');
  $('#vImg').src = '';
}
function showCurrent() {
  const p = visible[vIdx];
  if (!p) return closeViewer();
  $('#vName').textContent = p.name;
  $('#vIdx').textContent = `${vIdx + 1} / ${visible.length}`;
  $('#vImg').src = `/full/${p.id}`;
  syncViewerButtons();
}
function syncViewerButtons() {
  const p = visible[vIdx];
  for (const btn of document.querySelectorAll('.mark'))
    btn.classList.toggle('on', p && btn.dataset.s === p.status && p.status !== '');
}
function move(d) {
  if (vIdx < 0) return;
  const n = vIdx + d;
  if (n >= 0 && n < visible.length) { vIdx = n; showCurrent(); }
}

/* -------------------------------------------------------- browse modal --- */
let bCur = '';
async function openBrowse(target /* 'scan' | 'export' */) {
  $('#browse').dataset.target = target;
  $('#browse').classList.remove('hidden');
  await browseTo(target === 'export' && bCur ? bCur : '');
}
async function browseTo(path) {
  const d = await api('/api/browse' + (path ? '?path=' + encodeURIComponent(path) : ''));
  bCur = d.current || '';
  $('#bPath').textContent = d.current || '我的电脑';
  $('#bUp').disabled = !d.parent;
  const list = $('#bList');
  list.innerHTML = '';
  if (d.parent) addRow(list, '↑ 上级目录', () => browseTo(d.parent));
  for (const x of d.dirs)
    addRow(list, '📁 ' + x.name, () => browseTo(x.path));
  return d;
}
function addRow(list, label, fn) {
  const b = document.createElement('button');
  b.className = 'row'; b.textContent = label;
  b.onclick = fn; list.appendChild(b);
}
async function chooseFolder() {
  if (!bCur) return toast('请进入一个文件夹');
  $('#browse').classList.add('hidden');
  if ($('#browse').dataset.target === 'export') return openExport(bCur + '\\已选照片');
  toast('正在扫描…');
  const r = await api('/api/scan', { path: bCur });
  await loadPhotos();
  toast(`${r.count} 张照片`);
}

/* --------------------------------------------------------- export modal -- */
function openExport(defPath) {
  $('#export').classList.remove('hidden');
  $('#ePath').value = defPath || (window.folderPath + '\\已选照片');
  browseExport(defPath || window.folderPath);
}
async function browseExport(path) {
  try {
    const d = await api('/api/browse' + (path ? '?path=' + encodeURIComponent(path) : ''));
    const list = $('#eList'); list.innerHTML = '';
    if (d.parent) addRow(list, '↑ 上级目录', () => browseExport(d.parent));
    for (const x of d.dirs)
      addRow(list, '📁 ' + x.name, () => {
        $('#ePath').value = x.path; browseExport(x.path);
      });
  } catch { /* 目录不可用则忽略 */ }
}
async function doExport() {
  const out = $('#ePath').value.trim();
  if (!out) return toast('请填写目标文件夹');
  $('#export').classList.add('hidden');
  toast('正在复制…');
  const r = await api('/api/export', { output: out });
  toast(`完成：${r.photos} 张照片（${r.files} 个文件）→ ${out}`);
}

/* ------------------------------------------------------------- keyboard -- */
document.addEventListener('keydown', e => {
  if (!$('#browse').classList.contains('hidden') ||
      !$('#export').classList.contains('hidden')) {
    if (e.key === 'Escape') { $('#browse').classList.add('hidden'); $('#export').classList.add('hidden'); }
    return;
  }
  const viewerOpen = !$('#viewer').classList.contains('hidden');
  if (!viewerOpen) {
    if (e.key === 'Enter' && photos.length) openViewer(0);
    return;
  }
  const p = visible[vIdx];
  switch (e.key) {
    case 'Escape': closeViewer(); break;
    case 'ArrowLeft': case 'a': case 'A': move(-1); break;
    case 'ArrowRight': case 'd': case 'D': move(1); break;
    case 's': case 'S': if (p) mark(p, 'pick'); break;
    case 'x': case 'X': if (p) mark(p, 'no'); break;
    case 'z': case 'Z': if (p) mark(p, ''); break;
  }
});

/* --------------------------------------------------------------- wire ---- */
async function loadPhotos() {
  const d = await api('/api/photos');
  window.folderPath = d.folder || '';
  photos = d.photos;
  $('#folderLabel').textContent = d.folder || '未选择文件夹';
  renderGrid();
  refreshStats();
}

$('#btnPick0').onclick = () => openBrowse('scan');
$('#folderLabel').onclick = () => openBrowse('scan');
$('#bChoose').onclick = chooseFolder;
$('#bClose').onclick = () => $('#browse').classList.add('hidden');
$('#bUp').onclick = async () => {
  const d = await api('/api/browse' + (bCur ? '?path=' + encodeURIComponent(bCur) : ''));
  if (d.parent) browseTo(d.parent);
};
$('#btnExport').onclick = () => {
  const n = photos.filter(p => p.status === 'pick').length;
  if (!n) return toast('还没有标记任何“已选”照片（打开照片按 S）');
  openExport();
};
$('#eGo').onclick = doExport;
$('#eCancel').onclick = () => $('#export').classList.add('hidden');
$('#vClose').onclick = closeViewer;
$('#vPrev').onclick = () => move(-1);
$('#vNext').onclick = () => move(1);
$('#mPick').onclick = () => visible[vIdx] && mark(visible[vIdx], 'pick');
$('#mNo').onclick = () => visible[vIdx] && mark(visible[vIdx], 'no');
$('#mClear').onclick = () => visible[vIdx] && mark(visible[vIdx], '');

for (const tab of document.querySelectorAll('#tabs button'))
  tab.onclick = () => {
    document.querySelector('#tabs .on')?.classList.remove('on');
    tab.classList.add('on');
    filter = tab.dataset.f;
    renderGrid();
  };

loadPhotos();
