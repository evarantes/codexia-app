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

  function apiErrorText(payload, fallback = 'Não foi possível concluir a operação.') {
    const seen = new WeakSet();
    const unpack = value => {
      if (value == null) return '';
      if (typeof value === 'string') {
        const text = value.trim();
        return text === '[object Object]' ? '' : text;
      }
      if (typeof value === 'number' || typeof value === 'boolean') return String(value);
      if (Array.isArray(value)) return value.map(unpack).filter(Boolean).join(' | ');
      if (typeof value === 'object') {
        if (seen.has(value)) return '';
        seen.add(value);
        const preferred = [
          value.message,
          value.msg,
          value.detail,
          value.error,
          value.reason,
          value.title,
          value.exception,
          value.body,
        ].map(unpack).filter(Boolean);
        if (preferred.length) return [...new Set(preferred)].join(' | ');
        try { return JSON.stringify(value); } catch (_) { return ''; }
      }
      return String(value || '').trim();
    };

    try {
      return unpack(payload) || fallback;
    } catch (_) {
      return fallback;
    }
  }

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
    if (!response.ok) {
      throw new Error(apiErrorText(data, `Falha na comunicação com o servidor (HTTP ${response.status}).`));
    }
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

  function durationText(seconds) {
    const total = Math.max(0, Math.round(Number(seconds || 0)));
    if (!total) return '—';
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    if (hours) return `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
    return `${minutes}:${String(secs).padStart(2, '0')}`;
  }

  function artifactChecklist(task) {
    const checklist = task?.artifact_checklist || {};
    const items = Array.isArray(checklist.items) ? checklist.items : [];
    if (!items.length) return '';
    const states = {
      ok: { icon: '✓', bg: '#e9f8f1', fg: '#14704f', label: 'OK' },
      partial: { icon: '!', bg: '#fff7df', fg: '#8a6100', label: 'Verificar' },
      failed: { icon: '×', bg: '#feeceb', fg: '#a22820', label: 'Falhou' },
      missing: { icon: '×', bg: '#feeceb', fg: '#a22820', label: 'Ausente' },
      pending: { icon: '…', bg: '#eef1f8', fg: '#526079', label: 'Pendente' },
    };
    const detail = item => {
      if (item.key === 'images') return item.summary || `${Number(item.actual || 0)}/${item.expected || '?'}`;
      if (item.key === 'narration' || item.key === 'captions') {
        const duration = item.duration_sec ? durationText(item.duration_sec) : 'sem duração';
        const target = item.target_sec ? ` / meta ${durationText(item.target_sec)}` : '';
        const entries = item.key === 'captions' && item.entries ? ` · ${Number(item.entries)} blocos` : '';
        return `${duration}${target}${entries}${item.preserved ? ' · preservado para reutilização' : ''}`;
      }
      if (item.key === 'narration_caption_sync') {
        const delta = Number(item.duration_difference_sec || 0).toFixed(1).replace('.', ',');
        const text = item.text_matches ? 'texto confere' : 'texto não confere';
        const timing = item.timing_source_verified ? 'tempos da narração real' : 'tempos não comprovados';
        return `${item.summary || ''} · diferença ${delta}s · ${text} · ${timing}`;
      }
      return item.summary || '—';
    };
    const rows = items.map(item => {
      const state = states[item.status] || states.pending;
      return `<div class="oq-artifact-row">
        <span aria-hidden="true" style="display:grid;place-items:center;width:28px;height:28px;border-radius:999px;background:${state.bg};color:${state.fg};font-weight:900">${state.icon}</span>
        <b>${esc(item.label || item.key || 'Ativo')}</b>
        <span class="oq-artifact-detail" style="color:#536079;line-height:1.35">${esc(detail(item))}</span>
        <span style="color:${state.fg};font-size:11px;font-weight:850;white-space:nowrap">${esc(state.label)}</span>
      </div>`;
    }).join('');
    const reusable = Number(checklist.reusable_count || 0);
    const reusableTotal = Number(checklist.reusable_total || 0);
    const director = checklist.director_validation || {};
    const verdict = director.verdict
      ? `<div class="notice" style="margin-top:12px"><b>Verificação do Claude Diretor:</b> ${esc(director.verdict)}</div>`
      : '';
    return `<section style="margin-top:18px" aria-label="Ativos da produção">
      <div style="display:flex;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap">
        <div><b style="font-size:17px">Ativos da produção</b><div style="color:var(--muted);font-size:12px;margin-top:3px">O que foi concluído, validado e preservado para a correção.</div></div>
        <span style="font-size:12px;color:#536079;font-weight:750">${reusable}/${reusableTotal || 4} ativos reutilizáveis</span>
      </div>
      <div class="card" style="box-shadow:none;margin-top:10px;padding:2px 14px">${rows}</div>
      ${verdict}
    </section>`;
  }

  function summaryCard(label, value, hint = '') {
    return `<div class="card metric" style="padding:14px 16px"><div class="label">${esc(label)}</div><div class="value" style="font-size:22px;margin:2px 0">${esc(value)}</div>${hint ? `<div class="sub">${esc(hint)}</div>` : ''}</div>`;
  }

  function ensureQueueStyles() {
    if (document.getElementById('operationalQueueMobileScrollStyles')) return;
    const style = document.createElement('style');
    style.id = 'operationalQueueMobileScrollStyles';
    style.textContent = `
      #operationalQueueList { min-width: 0; }
      .oq-project-scroll {
        max-width: 100%;
        overflow-x: auto;
        overflow-y: hidden;
        overscroll-behavior-x: contain;
        -webkit-overflow-scrolling: touch;
        scrollbar-width: auto;
        scrollbar-color: var(--blue) #e7eaf2;
      }
      .oq-project-scroll::-webkit-scrollbar { height: 10px; }
      .oq-project-scroll::-webkit-scrollbar-track {
        background: #e7eaf2;
        border-radius: 999px;
      }
      .oq-project-scroll::-webkit-scrollbar-thumb {
        background: linear-gradient(90deg, var(--blue), var(--violet));
        border: 2px solid #e7eaf2;
        border-radius: 999px;
      }
      .oq-project-row {
        display: grid;
        grid-template-columns: minmax(260px, 1fr) minmax(150px, .45fr) minmax(260px, auto);
        gap: 16px;
        align-items: center;
      }
      .oq-artifact-row {
        display: grid;
        grid-template-columns: 34px minmax(100px,.55fr) minmax(160px,1.45fr) auto;
        gap: 10px;
        align-items: center;
        padding: 11px 0;
        border-bottom: 1px solid var(--line);
      }
      .oq-mobile-scroll-hint { display: none; }
      @media (max-width: 700px) {
        .oq-project-scroll {
          overflow-x: scroll;
          padding: 12px 14px 9px;
          scroll-behavior: smooth;
          scrollbar-gutter: stable;
          touch-action: pan-x pan-y;
        }
        .oq-project-row { min-width: 760px; }
        .oq-artifact-row { grid-template-columns: 34px minmax(0,1fr) auto; }
        .oq-artifact-detail { grid-column: 2 / 4; padding-bottom: 3px; }
        .oq-mobile-scroll-hint {
          display: block;
          position: sticky;
          left: 0;
          width: max-content;
          margin: 0 0 8px;
          color: var(--muted);
          font-size: 11px;
          font-weight: 700;
        }
      }
    `;
    document.head.appendChild(style);
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
    ensureQueueStyles();

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
    if (task.can_approve) {
      items.push(`<button type="button" class="btn btn-primary oq-review" data-action="approve" data-id="${esc(task.id)}">✓ Aprovar</button>`);
    }
    if (task.can_reject) {
      items.push(`<button type="button" class="btn btn-danger oq-review" data-action="reject" data-id="${esc(task.id)}">Solicitar correção</button>`);
    }
    if (task.can_publish) {
      items.push(`<button type="button" class="btn btn-primary oq-publish" data-id="${esc(task.id)}">Publicar no YouTube</button>`);
    }
    if (task.youtube_url) {
      items.push(`<a class="btn btn-primary" href="${esc(task.youtube_url)}" target="_blank" rel="noopener" style="text-decoration:none">Abrir no YouTube</a>`);
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
    const preserved = Number(task?.artifact_checklist?.reusable_count || 0);
    const preservedTotal = Number(task?.artifact_checklist?.reusable_total || 0);
    return `
      <div class="oq-project-scroll" data-task-id="${esc(task.id)}" style="border-bottom:1px solid var(--line)" tabindex="0" aria-label="Projeto ${esc(task.title || 'Produção')}. Deslize horizontalmente para ver progresso e comandos.">
        <div class="oq-mobile-scroll-hint">Deslize para o lado para ver progresso e comandos →</div>
        <div class="oq-project-row">
          <div style="min-width:0">
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
              <div style="font-size:15px;font-weight:850;word-break:break-word">${esc(task.title || 'Produção')}</div>
              ${statusBadge(task.status)}
            </div>
            <div style="margin-top:4px;color:var(--muted);font-size:12px">${esc(meta)}</div>
            ${task.message ? `<div style="margin-top:5px;color:#536079;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${esc(task.message)}">${esc(task.message)}</div>` : ''}
            ${preservedTotal ? `<div style="margin-top:5px;color:#3152c8;font-size:11px;font-weight:750">Ativos preservados: ${preserved}/${preservedTotal}</div>` : ''}
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

  async function reconcileActiveProjects(tasks) {
    const active = (Array.isArray(tasks) ? tasks : []).filter(task =>
      ['pending', 'processing', 'pause_requested'].includes(String(task?.status || '').toLowerCase())
    );
    if (!active.length) return false;

    let changed = false;
    await Promise.all(active.map(async task => {
      try {
        // The canonical status endpoint owns runtime-heartbeat reconciliation.
        // Reading it is side-effect free for healthy jobs; an abandoned worker
        // is converted to a recoverable pause with all completed assets kept.
        const canonical = await api(`/youtube/task/${encodeURIComponent(task.id)}`);
        const canonicalStatus = String(canonical?.status || '').toLowerCase();
        if (
          canonicalStatus && canonicalStatus !== String(task.status || '').toLowerCase()
          || Number(canonical?.progress || 0) !== Number(task.progress || 0)
          || String(canonical?.message || '') !== String(task.message || '')
        ) {
          changed = true;
        }
      } catch (error) {
        console.warn('V2 runtime reconciliation:', task?.id, error);
      }
    }));
    return changed;
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
      let data = await api(queueUrl());
      let tasks = Array.isArray(data.tasks) ? data.tasks : [];
      if (await reconcileActiveProjects(tasks)) {
        data = await api(queueUrl());
        tasks = Array.isArray(data.tasks) ? data.tasks : [];
      }
      const counts = data.counts || {};
      summary.innerHTML = [
        summaryCard('Total V2', counts.all || 0, 'somente sistema novo'),
        summaryCard('Em andamento', counts.active || 0),
        summaryCard('Prontos / revisão', counts.ready || 0),
        summaryCard('Falhas / cancelados', Number(counts.failed || 0) + Number(counts.cancelled || 0)),
      ].join('');
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
        ${artifactChecklist(task)}
        ${task.message ? `<div style="margin-top:15px"><b>Última informação</b><div style="color:#536079;margin-top:5px;line-height:1.5">${esc(task.message)}</div></div>` : ''}
        ${failure}
        <div style="margin-top:18px">${projectActions(task)}</div>`;
    } catch (error) {
      title.textContent = 'Não foi possível abrir o projeto';
      body.innerHTML = `<div class="notice warn">${esc(error.message)}</div>`;
    }
  }

  function optimizationConfirmationText(plan) {
    const valid = Number(plan?.valid_image_count || 0);
    const target = Number(plan?.target_visual_count || 0);
    const missing = Number(plan?.missing_visual_count || 0);
    const savings = Number(plan?.estimated_savings_usd || 0);
    const strict = Boolean(plan?.quality_completion_required);

    if (strict) {
      const newCalls = Number(plan?.max_new_image_calls ?? plan?.estimated_new_image_calls ?? missing ?? 0);
      const imageUsd = Number(plan?.estimated_new_image_cost_usd || 0);
      const imageBrl = Number(plan?.estimated_new_image_cost_brl || 0);
      const audioUsd = Number(plan?.estimated_new_audio_cost_usd || 0);
      const audioBrl = Number(plan?.estimated_new_audio_cost_brl || 0);
      const totalUsd = Number(plan?.estimated_total_additional_cost_usd || (imageUsd + audioUsd));
      const totalBrl = Number(plan?.estimated_total_additional_cost_brl || (imageBrl + audioBrl));
      const imageUnit = Number(plan?.image_unit_cost_usd || 0);
      const audioUnit = Number(plan?.audio_unit_cost_usd_per_minute || 0);
      const fx = Number(plan?.usd_brl || 0);
      const duration = Number(plan?.duration_minutes || 0);
      const durationText = duration > 0 ? ` para ${duration.toFixed(0)} minuto(s)` : '';
      const pricingReference = fx > 0
        ? `\nReferência de conversão usada: US$ 1 = R$ ${fx.toFixed(2)}.`
        : '';

      return `CORREÇÃO DE QUALIDADE VISUAL\n\n` +
        `O Claude Diretor recalculou a quantidade mínima de visuais${durationText}.\n\n` +
        `Imagens válidas que serão reaproveitadas: ${valid}\n` +
        `Nova meta visual: ${target}\n` +
        `Novas imagens necessárias: ${newCalls}\n\n` +
        `CUSTO PREVENTIVO ANTES DE AUTORIZAR:\n` +
        `• Novas imagens: US$ ${imageUsd.toFixed(4)} / aprox. R$ ${imageBrl.toFixed(2)}` +
        (imageUnit > 0 ? ` (US$ ${imageUnit.toFixed(4)} por imagem)` : '') + `\n` +
        `• Nova narração: US$ ${audioUsd.toFixed(4)} / aprox. R$ ${audioBrl.toFixed(2)}` +
        (audioUnit > 0 ? ` (referência US$ ${audioUnit.toFixed(4)}/min)` : '') + `\n` +
        `• Custo adicional máximo estimado desta correção: US$ ${totalUsd.toFixed(4)} / aprox. R$ ${totalBrl.toFixed(2)}` +
        pricingReference + `\n\n` +
        `LIMITE RÍGIDO:\n` +
        `• Máximo de ${newCalls} novas chamadas pagas de imagem.\n` +
        `• Se o limite for atingido, nenhuma imagem paga adicional poderá ser solicitada, inclusive em retry.\n\n` +
        `GARANTIAS:\n` +
        `• As imagens já pagas e válidas serão preservadas.\n` +
        `• Serão geradas somente as imagens que faltarem.\n` +
        `• A narração será refeita para respeitar a duração solicitada.\n` +
        `• A legenda continuará usando os tempos da narração real.\n` +
        `• Os valores monetários são estimativas preventivas baseadas nas unidades configuradas.\n` +
        `• O vídeo só poderá ser aprovado depois do Quality Gate.\n\n` +
        `Deseja autorizar este limite de custo e retomar a produção?`;
    }

    const savedCalls = Number(plan?.estimated_image_calls_avoided || missing || 0);
    const savingsText = savings > 0 ? `\nEconomia estimada: US$ ${savings.toFixed(4)}.` : '';
    return `OTIMIZAÇÃO INTELIGENTE DE CUSTO\n\n` +
      `O Codexia pode concluir este vídeo sem gerar novas imagens pagas.\n\n` +
      `Imagens válidas disponíveis: ${valid}\n` +
      `Meta visual original: ${target}\n` +
      `Imagens que deixarão de ser compradas: ${savedCalls}\n\n` +
      `GARANTIAS:\n` +
      `• A narração completa será preservada.\n` +
      `• Nenhum texto será cortado.\n` +
      `• A mensagem e a ordem narrativa serão preservadas.\n` +
      `• Novas chamadas pagas de imagem: 0.` + savingsText + `\n\n` +
      `Deseja aplicar esta otimização e retomar a produção?`;
  }

  async function confirmedRetryUrl(id) {
    const encodedId = encodeURIComponent(id);
    const plan = await api(`/youtube/task/${encodedId}/retry-plan`);
    if (plan?.quality_correction_required) {
      const message = String(plan.message ||
        'Esta produção foi reprovada na revisão e será refeita para cumprir duração, variedade visual e sincronização das legendas.');
      const costNote = '\n\nA correção poderá gerar uma nova narração e novas imagens. Os ativos defeituosos não serão reutilizados à força.';
      return confirm(`${message}${costNote}\n\nDeseja iniciar a correção de qualidade?`)
        ? `/youtube/task/${encodedId}/retry`
        : '';
    }
    if (!plan?.requires_confirmation) {
      return confirm('Deseja reiniciar/retomar esta produção?')
        ? `/youtube/task/${encodedId}/retry`
        : '';
    }
    if (!confirm(optimizationConfirmationText(plan))) return '';
    const planHash = String(plan.plan_hash || '').trim();
    if (!planHash) throw new Error('O plano de economia não recebeu uma assinatura válida. Atualize a fila e tente novamente.');
    return `/youtube/task/${encodedId}/retry?optimization_plan_hash=${encodeURIComponent(planHash)}`;
  }

  async function runAction(action, id, button) {
    const text = action === 'retry' ? 'reiniciar/retomar' : action === 'pause' ? 'pausar' : 'cancelar';
    if (action !== 'retry' && !confirm(`Deseja ${text} esta produção?`)) return;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = action === 'retry' ? 'Verificando…' : 'Aguarde…';
    try {
      const url = action === 'retry'
        ? await confirmedRetryUrl(id)
        : `/youtube/task/${encodeURIComponent(id)}/${action}`;
      if (!url) {
        button.disabled = false;
        button.textContent = original;
        return;
      }
      button.textContent = 'Aguarde…';
      await api(url, { method: 'POST' });
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

  async function reviewProject(action, id, button) {
    let notes = '';
    if (action === 'reject') {
      notes = prompt('Descreva o que o Claude Diretor deve corrigir antes de uma nova produção:') || '';
      if (!notes.trim()) return;
    } else if (!confirm('Aprovar este vídeo para publicação? O Codexia fará uma última validação de duração, variedade visual e sincronização.')) {
      return;
    }
    const original = button.textContent;
    button.disabled = true;
    button.textContent = action === 'approve' ? 'Validando…' : 'Registrando…';
    try {
      await api(`/youtube/cinematic/queue/${encodeURIComponent(id)}/${action}`, {
        method: 'POST',
        body: JSON.stringify({ notes: notes.trim() || undefined }),
      });
      await loadQueue(false);
      const modal = document.getElementById('v2ProjectModal');
      if (modal && !modal.classList.contains('hidden')) await openProject(id, false);
      alert(action === 'approve'
        ? 'Vídeo aprovado. O botão “Publicar no YouTube” já está disponível.'
        : 'Reprovação registrada. As observações foram anexadas ao projeto para a próxima correção.');
    } catch (error) {
      alert(error.message);
      button.disabled = false;
      button.textContent = original;
    }
  }

  async function publishProject(id, button) {
    const answer = (prompt('Visibilidade no YouTube: public, unlisted ou private', 'unlisted') || '').trim().toLowerCase();
    if (!answer) return;
    if (!['public', 'unlisted', 'private'].includes(answer)) {
      alert('Use public, unlisted ou private.');
      return;
    }
    if (!confirm(`Publicar agora no canal do YouTube com visibilidade “${answer}”?`)) return;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = 'Publicando…';
    try {
      const data = await api(`/youtube/cinematic/queue/${encodeURIComponent(id)}/publish`, {
        method: 'POST',
        body: JSON.stringify({ visibility: answer }),
      });
      await loadQueue(false);
      if (data.youtube_url) {
        alert(`Publicado com sucesso.\n\n${data.youtube_url}`);
        window.open(data.youtube_url, '_blank', 'noopener');
      } else {
        alert('Publicado com sucesso no YouTube.');
      }
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

    const review = event.target.closest?.('.oq-review');
    if (review?.dataset.id && review?.dataset.action) {
      void reviewProject(review.dataset.action, review.dataset.id, review);
      return;
    }

    const publish = event.target.closest?.('.oq-publish');
    if (publish?.dataset.id) {
      void publishProject(publish.dataset.id, publish);
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
