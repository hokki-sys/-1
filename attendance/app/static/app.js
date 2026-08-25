// 근무표 격자와 대시보드 실시간 갱신. 프레임워크 없이 필요한 만큼만.

// ---------------------------------------------------------------- 근무표 격자
(function schedule() {
  const grid = document.getElementById('sched-grid');
  if (!grid) return;

  const form = document.getElementById('sched-form');
  const payload = document.getElementById('grid-payload');
  const saveBtn = document.getElementById('save-btn');
  const dirtyMark = document.getElementById('dirty-mark');
  let dirty = false;
  let lastFocused = null;

  const cells = () => grid.querySelectorAll('input.cell');

  function minutesOf(text) {
    const m = (text || '').trim().match(
      /^(\d{1,2})\s*[:.]?\s*(\d{2})?\s*[-–~—]\s*(\d{1,2})\s*[:.]?\s*(\d{2})?$/);
    if (!m) return null;
    const a = (+m[1]) * 60 + (+(m[2] || 0));
    let b = (+m[3]) * 60 + (+(m[4] || 0));
    if (b <= a) b += 24 * 60;            // 자정 넘김
    return b - a;
  }

  function refresh() {
    grid.querySelectorAll('tr[data-emp]').forEach(tr => {
      let total = 0, bad = false;
      tr.querySelectorAll('input.cell').forEach(input => {
        const text = input.value.trim();
        input.parentElement.classList.toggle('has', text !== '');
        if (!text) return;
        const mins = minutesOf(text);
        if (mins === null) { bad = true; input.style.color = 'var(--alert)'; }
        else { input.style.color = ''; total += mins; }
      });
      const tot = tr.querySelector('td.tot');
      if (!tot) return;
      const hours = total / 60;
      const std = parseFloat(grid.dataset.standardHours || '40');
      tot.querySelector('b').textContent = bad ? '?' : (Math.round(hours * 10) / 10);
      tot.classList.toggle('over', !bad && hours > std);
      const bar = tot.querySelector('.bar');
      if (bar) bar.style.width = Math.min(100, (hours / (std * 1.5)) * 100) + '%';
    });
  }

  function markDirty() {
    if (dirty) return;
    dirty = true;
    if (dirtyMark) dirtyMark.hidden = false;
    if (saveBtn) saveBtn.disabled = false;
  }

  grid.addEventListener('input', e => {
    if (!e.target.classList.contains('cell')) return;
    markDirty(); refresh();
  });
  grid.addEventListener('focusin', e => {
    if (e.target.classList.contains('cell')) lastFocused = e.target;
  });

  // 프리셋: 마지막으로 만진 칸에 값을 넣습니다. 매번 10:00 을 타이핑하지 않게.
  document.querySelectorAll('[data-preset]').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = lastFocused || grid.querySelector('input.cell');
      if (!target) return;
      target.value = btn.dataset.preset;
      target.focus();
      markDirty(); refresh();
    });
  });

  // 방향키로 격자 이동 — 스프레드시트처럼
  grid.addEventListener('keydown', e => {
    if (!e.target.classList.contains('cell')) return;
    const map = { ArrowUp: [-1, 0], ArrowDown: [1, 0], Enter: [1, 0] };
    const delta = map[e.key];
    if (!delta) return;
    e.preventDefault();
    const td = e.target.parentElement;
    const col = [...td.parentElement.children].indexOf(td);
    const rows = [...grid.querySelectorAll('tr[data-emp]')];
    const idx = rows.indexOf(td.parentElement);
    const next = rows[idx + delta[0]];
    const input = next && next.children[col] && next.children[col].querySelector('input.cell');
    if (input) { input.focus(); input.select(); }
  });

  if (form) {
    form.addEventListener('submit', () => {
      const data = {};
      cells().forEach(input => {
        const v = input.value.trim();
        if (v) data[input.dataset.key] = v;
      });
      payload.value = JSON.stringify(data);
      dirty = false;
    });
  }

  window.addEventListener('beforeunload', e => {
    if (!dirty) return;
    e.preventDefault();
    e.returnValue = '';
  });

  document.querySelectorAll('form[data-confirm-dirty]').forEach(f => {
    f.addEventListener('submit', e => {
      if (dirty && !confirm('저장하지 않은 변경이 있습니다. 그대로 진행할까요?')) {
        e.preventDefault();
      }
    });
  });

  refresh();
  if (saveBtn) saveBtn.disabled = true;
})();

// ------------------------------------------------------------ 대시보드 SSE
(function live() {
  const root = document.getElementById('live');
  if (!root || !root.dataset.stream) return;

  const status = document.getElementById('live-status');
  const es = new EventSource(root.dataset.stream);

  es.onopen = () => setStatus('연결됨', 'live');
  es.onerror = () => setStatus('재연결 중', 'off');
  es.onmessage = ev => {
    let d;
    try { d = JSON.parse(ev.data); } catch { return; }
    setText('live-headcount', d.headcount);
    setText('live-late', d.late);
    setText('live-attention', d.attention);
    renderList('live-onduty', d.on_duty, r =>
      `<td>${esc(r.name)}${r.late ? ' <span class="tag warn">지각</span>' : ''}</td>
       <td class="t">${esc(r.since)}부터</td>`);
    renderList('live-recent', d.recent, r =>
      `<td>${esc(r.name)}</td><td>${esc(r.kind)}</td><td class="t">${esc(r.at)}</td>`);
  };

  function setStatus(text, cls) {
    if (!status) return;
    status.innerHTML = `<span class="dot ${cls}"></span>${text}`;
  }
  function setText(id, v) {
    const el = document.getElementById(id);
    if (el) el.textContent = v;
  }
  function renderList(id, rows, tpl) {
    const body = document.getElementById(id);
    if (!body) return;
    const cols = body.dataset.cols || 2;
    body.innerHTML = rows.length
      ? rows.map(r => `<tr>${tpl(r)}</tr>`).join('')
      : `<tr><td colspan="${cols}" class="empty">없습니다</td></tr>`;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
})();
