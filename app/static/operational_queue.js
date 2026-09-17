(() => {
  const token = localStorage.getItem('access_token');
  if (!token) return;

  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));

  const labels = {
    pending: 'Aguardando',
    processing: 'Em execução',
    pause_requested: 'Pausa solicitada',
    paused: 'Pausada',
    completed: 'Concluída',
    failed: 'Falhou',
    cancelled: 'Cancelada',
  };

  const colors = {
    pending: ['#fff7df', '#8a6100'],
    processing: ['#eaf0ff', '#3152c8'],
    pause_requested: ['#fff3d6', '#8a6100'],
    paused: ['#f2eefc', '#6048a8'],
    completed: ['#e9f8f1', '#14704f'],
    failed: ['#feeceb', '#a22820'],
    cancelled: ['#f2f3f5', '#5e6573'],
  };

  async function api(url, options = {}) {
    const headers = { ...(options.headers || {}), Authorization: `Bearer ${token}` };
    if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
    const response = await fetch(url, { ...options, headers, cache: 'no-store' });
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (response.status === 401) {
      localStorage.removeItem('access_token');
      location.href = '/static/login.html';
      throw new Error('Sessão expirada.');
    }
    if (!response.ok) throw new Error(data.detail || data.message || `HTTP ${response.status}`);
    return data;
  }

  function ensurePanel() {
    const page = document.getElementById('page-queue');
    if (!page) return null;

    const oldLink = page.querySelector('a[href="/static/legacy/index.html"]');
    if (oldLink) {
      oldLink.style.display = 'none';
      const subtitle = oldLink.closest('.section-title')?.querySelector('p');
      if (subtitle) subtitle.textContent = 'Acompanhe as produções diretamente aqui, sem abrir o sistema antigo.';
    }

    let wrap = document.getElementById('operationalQueuePanel');
    if (wrap) return wrap;

    wrap = document.createElement('div');
    wrap.id = 'operationalQueuePanel';
    wrap.innerHTML = `
      <div class="section-title" style="margin-top:22px">
        <div>
          <h2>Fila operacional</h2>
          <p>Status do pipeline canônico em tempo quase real.</p>
        </div>
        <button class="btn btn-ghost" id="refreshOperationalQueue">Atualizar agora</button>
      </div>
      <div class="grid grid4" id="operationalQueueSummary" style="margin-bottom:14px"></div>
      <div id="operationalQueueList" class="grid" style="gap:12px"></div>
      <div style="margin-top:12px;color:var(--muted);font-size:12px">
        Atualização automática a cada 10 segundos enquanto esta tela estiver aberta.
      </div>`;

    const criterion = Array.from(page.querySelectorAll('.section-title h2')).find(x => x.textContent.includes('Critério'))?.closest('.section-title');
    if (criterion) page.insertBefore(wrap, criterion);
    else page.appendChild(wrap);

    wrap.querySelector('#refreshOperationalQueue')?.addEventListener('click', () => void loadQueue(true));
    return wrap;
  }

  function summaryCard(label, value) {
    return `<div class="card metric"><div class="label">${esc(label)}</div><div class="value" style="font-size:22px">${esc(value)}</div></div>`;
  }

  function statusBadge(status) {
    const [bg, fg] = colors[status] || ['#eef1f8', '#526079'];
    return `<span style="display:inline-flex;padding:5px 9px;border-radius:999px;background:${bg};color:${fg};font-size:12px;font-weight:800">${esc(labels[status] || status)}</span>`;
  }

  function dateText(value) {
    if (!value) return '';
    try {
      return new Intl.DateTimeFormat('pt-BR', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(value));
    } catch (_) {
      return value;
    }
  }

  function taskActions(task) {
    const items = [];
    if (task.can_retry) {
      const label = task.status === 'paused' ? 'Retomar' : 'Reiniciar agora';
      items.push(`<button class="btn btn-primary oq-action" data-action="retry" data-id="${esc(task.id)}">${label}</button>`);
    }
    if (task.can_pause && task.status !== 'pause_requested') {
      items.push(`<button class="btn btn-ghost oq-action" data-action="pause" data-id="${esc(task.id)}">Pausar</button>`);
    }
    if (task.can_cancel) {
      items.push(`<button class="btn btn-danger oq-action" data-action="cancel" data-id="${esc(task.id)}">Cancelar</button>`);
    }
    return items.length ? `<div class="right-actions" style="margin-top:10px">${items.join('')}</div>` : '';
  }

  function taskCard(task, index) {
    const progress = Math.max(0, Math.min(100, Number(task.progress || 0)));
    const meta = [
      `#${index + 1}`,
      task.duration_minutes ? `${task.duration_minutes} min` : '',
      task.stage ? `Etapa: ${task.stage}` : '',
      task.updated_at ? `Atualizado: ${dateText(task.updated_at)}` : '',
    ].filter(Boolean).join(' · ');

    const failure = task.status === 'failed' && task.error
      ? `<div class="notice warn" style="margin-top:10px"><b>Motivo da falha</b><br>${esc(task.error)}</div>`
      : '';

    return `
      <div class="card" data-task-id="${esc(task.id)}">
        <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap">
          <div style="min-width:0;flex:1">
            <div style="font-size:17px;font-weight:800;word-break:break-word">${esc(task.title || 'Produção')}</div>
            <div style="margin-top:4px;color:var(--muted);font-size:12px">${esc(meta)}</div>
          </div>
          ${statusBadge(task.status)}
        </div>
        <div style="margin-top:12px;display:flex;justify-content:space-between;gap:10px;font-size:12px;color:var(--muted)">
          <span>Progresso</span><b style="color:var(--text)">${progress}%</b>
        </div>
        <div class="progress" style="margin-top:6px"><i style="width:${progress}%"></i></div>
        ${task.message ? `<div style="margin-top:10px;color:#536079;font-size:13px;line-height:1.45">${esc(task.message)}</div>` : ''}
        ${failure}
        <div style="margin-top:8px;color:var(--muted);font-size:11px;word-break:break-all">ID: ${esc(task.id)}</div>
        ${taskActions(task)}
      </div>`;
  }

  async function loadQueue(showBusy = false) {
    const panel = ensurePanel();
    if (!panel) return;
    const list = panel.querySelector('#operationalQueueList');
    const summary = panel.querySelector('#operationalQueueSummary');
    const btn = panel.querySelector('#refreshOperationalQueue');
    if (showBusy && btn) { btn.disabled = true; btn.textContent = 'Atualizando…'; }
    try {
      const data = await api('/youtube/cinematic/queue?limit=50');
      summary.innerHTML = [
        summaryCard('Em execução / aguardando', data.active || 0),
        summaryCard('Falharam', data.failed || 0),
        summaryCard('Pausadas', data.paused || 0),
        summaryCard('Concluídas recentes', data.completed || 0),
      ].join('');
      const tasks = Array.isArray(data.tasks) ? data.tasks : [];
      list.innerHTML = tasks.length
        ? tasks.map(taskCard).join('')
        : '<div class="notice">Nenhuma produção recente encontrada.</div>';
    } catch (error) {
      list.innerHTML = `<div class="notice warn"><b>Não foi possível carregar a fila.</b><br>${esc(error.message)}</div>`;
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = 'Atualizar agora'; }
    }
  }

  async function runAction(action, id, button) {
    const text = action === 'retry' ? 'reiniciar/retomar' : action === 'pause' ? 'pausar' : 'cancelar';
    if (!confirm(`Deseja ${text} esta produção?`)) return;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = 'Aguarde…';
    try {
      await api(`/youtube/task/${encodeURIComponent(id)}/${action}`, { method: 'POST' });
      await loadQueue(false);
    } catch (error) {
      alert(error.message);
      button.disabled = false;
      button.textContent = original;
    }
  }

  document.addEventListener('click', event => {
    const nav = event.target.closest?.('[data-page="queue"]');
    if (nav) setTimeout(() => void loadQueue(false), 80);

    const action = event.target.closest?.('.oq-action');
    if (action) {
      event.preventDefault();
      const id = action.dataset.id;
      const kind = action.dataset.action;
      if (id && kind) void runAction(kind, id, action);
    }
  });

  ensurePanel();
  if (document.getElementById('page-queue')?.classList.contains('active')) void loadQueue(false);

  setInterval(() => {
    const page = document.getElementById('page-queue');
    if (page?.classList.contains('active')) void loadQueue(false);
  }, 10000);
})();
