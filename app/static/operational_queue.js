(() => {
  const token = localStorage.getItem('access_token');
  if (!token) return;

  const state = {
    page: 1,
    pageSize: 8,
    filter: 'all',
    search: '',
    loading: false,
    searchTimer: null,
  };

  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));

  const labels = {
    pending: 'Aguardando',
    processing: 'Em produção',
    pause_requested: 'Pausa solicitada',
    paused: 'Pausado',
    completed: 'Concluído',
    awaiting_review: 'Aguardando revisão',
    ready: 'Pronto',
    awaiting_publish: 'Aguardando publicação',
    approved: 'Aprovado',
    published: 'Publicado',
    failed: 'Falhou',
    cancelled: 'Cancelado',
    canceled: 'Cancelado',
  };

  const colors = {
    pending: ['#fff7df', '#8a6100'],
    processing: ['#eaf0ff', '#3152c8'],
    pause_requested: ['#fff3d6', '#8a6100'],
    paused: ['#f2eefc', '#6048a8'],
    completed: ['#e9f8f1', '#14704f'],
    awaiting_review: ['#edf1ff', '#394fa7'],
    ready: ['#e9f8f1', '#14704f'],
    awaiting_publish: ['#eef7ff', '#27628e'],
    approved: ['#e9f8f1', '#14704f'],
    published: ['#e8f8ee', '#12643f'],
    failed: ['#feeceb', '#a22820'],
    cancelled: ['#f2f3f5', '#5e6573'],
    canceled: ['#f2f3f5', '#5e6573'],
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

  function dateText(value) {
    if (!value) return '—';
    try {
      return new Intl.DateTimeFormat('pt-BR', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(value));
    } catch (_) {
      return String(value);
    }
  }

  function statusBadge(status) {
    const [bg, fg] = colors[status] || ['#eef1f8', '#526079'];
    return `<span style="display:inline-flex;padding:5px 9px;border-radius:999px;background:${bg};color:${fg};font-size:11px;font-weight:800;white-space:nowrap">${esc(labels[status] || status || 'Desconhecido')}</span>`;
  }

  function summaryCard(label, value, hint = '') {
    return `<div class="card metric" style="padding:14px 16px"><div class="label">${esc(label)}</div><div class="value" style="font-size:22px;margin:2px 0">${esc(value)}</div>${hint ? `<div class="sub">${esc(hint)}</div>` : ''}</div>`;
  }

  function ensureModal() {
    let modal = document.getElementById('v2ProjectModal');
    if (modal) return modal;
    modal = document.createElement('div');
    modal.id = 'v2ProjectModal';
    modal.className = 'hidden';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.style.cssText = 'position:fixed;inset:0;background:rgba(13,20,35,.58);z-index:1000;padding:24px;overflow:auto;';
    modal.innerHTML = `
      <div style="max-width:900px;margin:28px auto;background:#fff;border-radius:18px;border:1px solid var(--line);box-shadow:0 24px 70px rgba(15,23,42,.25);overflow:hidden">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:16px;padding:18px 20px;border-bottom:1px solid var(--line)">
          <div style="min-width:0"><div id="v2ProjectModalTitle" style="font-weight:850;font-size:19px;word-break:break-word">Projeto</div><div id="v2ProjectModalSubtitle" style="color:var(--muted);font-size:12px;margin-top:4px"></div></div>
          <button type="button" class="btn btn-ghost" id="v2ProjectModalClose">Fechar</button>
        </div>
        <div id="v2ProjectModalBody" style="padding:20px"></div>
      </div>`;
    document.body.appendChild(modal);
    modal.querySelector('#v2ProjectModalClose')?.addEventListener('click', closeModal);
    modal.addEventListener('click', event => { if (event.target === modal) closeModal(); });
    return modal;
  }

  function closeModal() {
    const modal = document.getElementById('v2ProjectModal');
    if (!modal) return;
    const video = modal.querySelector('video');
    try { video?.pause(); } catch (_) {}
    modal.classList.add('hidden');
  }

  function ensurePanel() {
    const page = document.getElementById('page-queue');
    if (!page) return null;

    const oldLink = page.querySelector('a[href="/static/legacy/index.html"]');
    if (oldLink) {
      oldLink.style.display = 'none';
      const subtitle = oldLink.closest('.section-title')?.querySelector('p');
      if (subtitle) subtitle.textContent = 'Esta área mostra somente projetos criados no Codexia novo. O histórico antigo continua no Sistema anterior.';
    }

    let wrap = document.getElementById('operationalQueuePanel');
    if (wrap) return wrap;

    wrap = document.createElement('div');
    wrap.id = 'operationalQueuePanel';
    wrap.innerHTML = `
      <div class="section-title" style="margin-top:22px;align-items:center;gap:12px;flex-wrap:wrap">
        <div>
          <h2>Projetos do Codexia V2</h2>
          <p>Somente produções iniciadas neste sistema novo. O sistema antigo permanece separado.</p>
        </div>
        <button type="button" class="btn btn-ghost" id="refreshOperationalQueue">Atualizar</button>
      </div>

      <div class="grid grid4" id="operationalQueueSummary" style="margin-bottom:14px"></div>

      <div class="card" style="padding:14px 16px;margin-bottom:14px">
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <div class="field" style="flex:1;min-width:220px">
            <label for="v2ProjectSearch">Buscar projeto</label>
            <input id="v2ProjectSearch" type="search" placeholder="Digite título ou ID…" autocomplete="off" />
          </div>
          <div style="display:flex;gap:7px;align-self:flex-end;flex-wrap:wrap" id="v2ProjectFilters">
            <button type="button" class="btn btn-primary oq-filter" data-filter="all">Todos</button>
            <button type="button" class="btn btn-ghost oq-filter" data-filter="active">Em andamento</button>
            <button type="button" class="btn btn-ghost oq-filter" data-filter="ready">Prontos</button>
            <button type="button" class="btn btn-ghost oq-filter" data-filter="failed">Falharam</button>
            <button type="button" class="btn btn-ghost oq-filter" data-filter="cancelled">Cancelados</button>
          </div>
        </div>
      </div>

      <div class="card" style="padding:0;overflow:hidden">
        <div id="operationalQueueList"></div>
        <div id="operationalQueuePagination" style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;padding:13px 16px;border-top:1px solid var(--line)"></div>
      </div>

      <div style="margin-top:10px;color:var(--muted);font-size:12px">
        Excluir remove o projeto desta biblioteca V2, preservando o registro técnico do pipeline para auditoria. Atualização automática a cada 10 segundos.
      </div>`;

    const criterion = Array.from(page.querySelectorAll('.section-title h2')).find(x => x.textContent.includes('Critério'))?.closest('.section-title');
    if (criterion) page.insertBefore(wrap, criterion);
    else page.appendChild(wrap);

    wrap.querySelector('#refreshOperationalQueue')?.addEventListener('click', () => void loadQueue(true));
    wrap.querySelector('#v2ProjectSearch')?.addEventListener('input', event => {
      clearTimeout(state.searchTimer);
      state.searchTimer = setTimeout(() => {
        state.search = String(event.target.value || '').trim();
        state.page = 1;
        void loadQueue(false);
      }, 350);
    });
    wrap.querySelectorAll('.oq-filter').forEach(button => button.addEventListener('click', () => {
      state.filter = button.dataset.filter || 'all';
      state.page = 1;
      wrap.querySelectorAll('.oq-filter').forEach(item => {
        item.classList.toggle('btn-primary', item === button);
        item.classList.toggle('btn-ghost', item !== button);
      });
      void loadQueue(false);
    }));
    ensureModal();
    return wrap;
  }

  function projectActions(task) {
    const items = [
      `<button type="button" class="btn btn-ghost oq-detail" data-id="${esc(task.id)}">Visualizar</button>`,
    ];
    if (task.can_watch && task.video_url) {
      items.push(`<button type="button" class="btn btn-primary oq-watch" data-id="${esc(task.id)}">▶ Assistir</button>`);
    }
    if (task.can_retry) {
      items.push(`<button type="button" class="btn btn-ghost oq-action" data-action="retry" data-id="${esc(task.id)}">${task.status === 'paused' ? 'Retomar' : 'Reiniciar'}</button>`);
    }
    if (task.can_pause && task.status !== 'pause_requested') {
      items.push(`<button type="button" class="btn btn-ghost oq-action" data-action="pause" data-id="${esc(task.id)}">Pausar</button>`);
    }
    if (task.can_cancel) {
      items.push(`<button type="button" class="btn btn-danger oq-action" data-action="cancel" data-id="${esc(task.id)}">Cancelar</button>`);
    }
    if (task.can_delete) {
      items.push(`<button type="button" class="btn btn-danger oq-delete" data-id="${esc(task.id)}">Excluir</button>`);
    }
    return `<div class="right-actions" style="justify-content:flex-end">${items.join('')}</div>`;
  }

  function projectRow(task) {
    const progress = Math.max(0, Math.min(100, Number(task.progress || 0)));
    const kind = task.kind === 'devotional' ? 'Devocional' : task.kind === 'short' ? 'Short' : 'História';
    const meta = [
      kind,
      task.duration_minutes ? `${task.duration_minutes} min` : '',
      task.updated_at ? `Atualizado ${dateText(task.updated_at)}` : '',
    ].filter(Boolean).join(' · ');
    return `
      <div data-task-id="${esc(task.id)}" style="padding:15px 16px;border-bottom:1px solid var(--line)">
        <div style="display:grid;grid-template-columns:minmax(260px,1fr) minmax(150px,.45fr) minmax(260px,auto);gap:16px;align-items:center">
          <div style="min-width:0">
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
              <div style="font-size:15px;font-weight:850;word-break:break-word">${esc(task.title || 'Produção')}</div>
              ${statusBadge(task.status)}
            </div>
            <div style="margin-top:4px;color:var(--muted);font-size:12px">${esc(meta)}</div>
            ${task.message ? `<div style="margin-top:5px;color:#536079;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${esc(task.message)}">${esc(task.message)}</div>` : ''}
          </div>
          <div>
            <div style="display:flex;justify-content:space-between;gap:8px;font-size:11px;color:var(--muted)"><span>Progresso</span><b style="color:var(--text)">${progress}%</b></div>
            <div class="progress" style="margin-top:5px"><i style="width:${progress}%"></i></div>
          </div>
          ${projectActions(task)}
        </div>
      </div>`;
  }

  function emptyState() {
    const hasFilter = state.filter !== 'all' || Boolean(state.search);
    return `<div style="padding:34px 20px;text-align:center">
      <div style="font-size:28px;margin-bottom:8px">${hasFilter ? '⌕' : '▣'}</div>
      <b>${hasFilter ? 'Nenhum projeto encontrado com este filtro.' : 'Nenhum projeto do Codexia V2 ainda.'}</b>
      <div style="color:var(--muted);font-size:13px;margin-top:6px">${hasFilter ? 'Altere a busca ou selecione “Todos”.' : 'Quando uma nova produção for enviada pelo sistema novo, ela aparecerá aqui.'}</div>
    </div>`;
  }

  function pagination(data) {
    const page = Number(data?.page || 1);
    const pages = Number(data?.pages || 0);
    const total = Number(data?.total || 0);
    if (!pages) return `<span style="color:var(--muted);font-size:12px">0 projetos</span>`;
    return `
      <span style="color:var(--muted);font-size:12px">${total} projeto${total === 1 ? '' : 's'} · Página ${page} de ${pages}</span>
      <div class="right-actions">
        <button type="button" class="btn btn-ghost oq-page" data-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>← Anterior</button>
        <button type="button" class="btn btn-ghost oq-page" data-page="${page + 1}" ${page >= pages ? 'disabled' : ''}>Próxima →</button>
      </div>`;
  }

  function queueUrl() {
    const params = new URLSearchParams({
      page: String(state.page),
      page_size: String(state.pageSize),
      status: state.filter,
    });
    if (state.search) params.set('q', state.search);
    return `/youtube/cinematic/queue?${params.toString()}`;
  }

  async function loadQueue(showBusy = false) {
    const panel = ensurePanel();
    if (!panel || state.loading) return;
    state.loading = true;
    const list = panel.querySelector('#operationalQueueList');
    const summary = panel.querySelector('#operationalQueueSummary');
    const pager = panel.querySelector('#operationalQueuePagination');
    const btn = panel.querySelector('#refreshOperationalQueue');
    if (showBusy && btn) { btn.disabled = true; btn.textContent = 'Atualizando…'; }
    try {
      const data = await api(queueUrl());
      const counts = data.counts || {};
      summary.innerHTML = [
        summaryCard('Total V2', counts.all || 0, 'somente sistema novo'),
        summaryCard('Em andamento', counts.active || 0),
        summaryCard('Prontos / revisão', counts.ready || 0),
        summaryCard('Falhas / cancelados', Number(counts.failed || 0) + Number(counts.cancelled || 0)),
      ].join('');
      const tasks = Array.isArray(data.tasks) ? data.tasks : [];
      list.innerHTML = tasks.length ? tasks.map(projectRow).join('') : emptyState();
      pager.innerHTML = pagination(data.pagination || {});
      state.page = Number(data.pagination?.page || 1);
    } catch (error) {
      list.innerHTML = `<div class="notice warn" style="margin:16px"><b>Não foi possível carregar os projetos do Codexia V2.</b><br>${esc(error.message)}</div>`;
      pager.innerHTML = '';
    } finally {
      state.loading = false;
      if (btn) { btn.disabled = false; btn.textContent = 'Atualizar'; }
    }
  }

  async function fetchProject(id) {
    const data = await api(`/youtube/cinematic/queue/${encodeURIComponent(id)}`);
    return data.project || {};
  }

  async function openProject(id, autoplay = false) {
    const modal = ensureModal();
    const title = modal.querySelector('#v2ProjectModalTitle');
    const subtitle = modal.querySelector('#v2ProjectModalSubtitle');
    const body = modal.querySelector('#v2ProjectModalBody');
    modal.classList.remove('hidden');
    title.textContent = 'Carregando projeto…';
    subtitle.textContent = '';
    body.innerHTML = '<div class="notice">Buscando os dados mais recentes…</div>';
    try {
      const task = await fetchProject(id);
      title.textContent = task.title || 'Projeto';
      subtitle.textContent = `${labels[task.status] || task.status || ''} · ${task.duration_minutes ? `${task.duration_minutes} min · ` : ''}Atualizado ${dateText(task.updated_at)}`;
      const video = task.video_url
        ? `<div style="margin-bottom:18px"><video controls ${autoplay ? 'autoplay' : ''} preload="metadata" style="display:block;width:100%;max-height:480px;background:#111;border-radius:12px" src="${esc(task.video_url)}"></video><div style="margin-top:8px"><a class="btn btn-ghost" style="display:inline-block;text-decoration:none" href="${esc(task.video_url)}" target="_blank" rel="noopener">Abrir vídeo em nova aba</a></div></div>`
        : `<div class="notice" style="margin-bottom:18px">O vídeo final ainda não está disponível para reprodução.</div>`;
      const failure = task.error ? `<div class="notice warn" style="margin-top:14px"><b>Detalhe da falha</b><br>${esc(task.error)}</div>` : '';
      body.innerHTML = `${video}
        <div class="grid grid2">
          <div class="card" style="box-shadow:none"><div class="label">Status</div><div style="margin-top:7px">${statusBadge(task.status)}</div></div>
          <div class="card" style="box-shadow:none"><div class="label">Progresso</div><div style="font-weight:850;font-size:20px;margin-top:4px">${Number(task.progress || 0)}%</div></div>
          <div class="card" style="box-shadow:none"><div class="label">Tipo</div><div style="font-weight:750;margin-top:4px">${esc(task.kind || '—')}</div></div>
          <div class="card" style="box-shadow:none"><div class="label">ID técnico</div><div style="font-size:11px;word-break:break-all;margin-top:5px">${esc(task.id || '')}</div></div>
        </div>
        ${task.message ? `<div style="margin-top:15px"><b>Última informação</b><div style="color:#536079;margin-top:5px;line-height:1.5">${esc(task.message)}</div></div>` : ''}
        ${failure}`;
    } catch (error) {
      title.textContent = 'Não foi possível abrir o projeto';
      body.innerHTML = `<div class="notice warn">${esc(error.message)}</div>`;
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

  async function deleteProject(id, button) {
    if (!confirm('Excluir este projeto da biblioteca do Codexia V2?\n\nEle desaparecerá desta tela. O registro técnico do pipeline será preservado para auditoria e segurança.')) return;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = 'Excluindo…';
    try {
      await api(`/youtube/cinematic/queue/${encodeURIComponent(id)}`, { method: 'DELETE' });
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

    const pageButton = event.target.closest?.('.oq-page');
    if (pageButton && !pageButton.disabled) {
      state.page = Math.max(1, Number(pageButton.dataset.page || 1));
      void loadQueue(false);
      return;
    }

    const detail = event.target.closest?.('.oq-detail');
    if (detail?.dataset.id) { void openProject(detail.dataset.id, false); return; }

    const watch = event.target.closest?.('.oq-watch');
    if (watch?.dataset.id) { void openProject(watch.dataset.id, true); return; }

    const action = event.target.closest?.('.oq-action');
    if (action) {
      event.preventDefault();
      const id = action.dataset.id;
      const kind = action.dataset.action;
      if (id && kind) void runAction(kind, id, action);
      return;
    }

    const remove = event.target.closest?.('.oq-delete');
    if (remove?.dataset.id) void deleteProject(remove.dataset.id, remove);
  });

  ensurePanel();
  if (document.getElementById('page-queue')?.classList.contains('active')) void loadQueue(false);

  setInterval(() => {
    const page = document.getElementById('page-queue');
    if (page?.classList.contains('active')) void loadQueue(false);
  }, 10000);
})();
