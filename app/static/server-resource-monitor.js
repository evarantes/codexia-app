(() => {
  if (window.top !== window.self) return;

  const TOKEN_KEY = 'access_token';
  const REFRESH_MS = 30000;
  const style = document.createElement('style');
  style.textContent = `
    #codexia-resource-monitor{position:fixed;z-index:99999;right:14px;bottom:calc(12px + env(safe-area-inset-bottom,0px));width:min(460px,calc(100vw - 24px));font:13px/1.4 Inter,system-ui,-apple-system,"Segoe UI",sans-serif;color:#172033}
    #codexia-resource-monitor *{box-sizing:border-box}
    #codexia-resource-monitor .crm-toggle{width:100%;border:1px solid #cbd5e1;border-radius:14px;background:#101828;color:#f8fafc;box-shadow:0 8px 28px #0f172a42;padding:9px 12px;display:flex;align-items:center;gap:9px;text-align:left;cursor:pointer}
    #codexia-resource-monitor .crm-dot{width:10px;height:10px;flex:0 0 10px;border-radius:50%;background:#94a3b8;box-shadow:0 0 0 3px #ffffff18}
    #codexia-resource-monitor[data-health="good"] .crm-dot{background:#34d399}
    #codexia-resource-monitor[data-health="warning"] .crm-dot{background:#fbbf24}
    #codexia-resource-monitor[data-health="critical"],#codexia-resource-monitor[data-health="offline"]{--crm-accent:#f87171}
    #codexia-resource-monitor[data-health="critical"] .crm-dot,#codexia-resource-monitor[data-health="offline"] .crm-dot{background:#f87171}
    #codexia-resource-monitor .crm-toggle-label{font-weight:750;white-space:nowrap}
    #codexia-resource-monitor .crm-summary{min-width:0;flex:1;color:#e2e8f0;font-size:11px;line-height:1.35}
    #codexia-resource-monitor .crm-chevron{font-size:15px;color:#cbd5e1}
    #codexia-resource-monitor .crm-panel{margin-bottom:8px;max-height:min(72vh,580px);overflow:auto;border:1px solid #cbd5e1;border-radius:16px;background:#f8fafc;box-shadow:0 16px 45px #0f172a38;padding:12px}
    #codexia-resource-monitor .crm-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:9px}
    #codexia-resource-monitor .crm-head strong{font-size:14px}
    #codexia-resource-monitor .crm-head small{display:block;color:#64748b;font-size:10px}
    #codexia-resource-monitor .crm-refresh{border:1px solid #cbd5e1;background:white;border-radius:8px;padding:6px 9px;color:#334155;cursor:pointer}
    #codexia-resource-monitor .crm-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
    #codexia-resource-monitor .crm-card{min-width:0;background:white;border:1px solid #e2e8f0;border-radius:12px;padding:10px}
    #codexia-resource-monitor .crm-card-title{display:flex;align-items:center;justify-content:space-between;gap:6px;margin-bottom:7px;font-weight:750}
    #codexia-resource-monitor .crm-status{font-size:10px;font-weight:700;padding:2px 6px;border-radius:999px;background:#e2e8f0;color:#475569}
    #codexia-resource-monitor .crm-status.good{background:#dcfce7;color:#166534}
    #codexia-resource-monitor .crm-status.warning{background:#fef3c7;color:#92400e}
    #codexia-resource-monitor .crm-status.critical,#codexia-resource-monitor .crm-status.offline{background:#fee2e2;color:#991b1b}
    #codexia-resource-monitor .crm-metric{margin:7px 0}
    #codexia-resource-monitor .crm-metric-line{display:flex;justify-content:space-between;gap:8px;color:#475569;font-size:11px}
    #codexia-resource-monitor .crm-metric-line b{color:#172033;text-align:right;font-weight:700}
    #codexia-resource-monitor .crm-bar{height:6px;margin-top:4px;background:#e2e8f0;border-radius:8px;overflow:hidden}
    #codexia-resource-monitor .crm-bar i{display:block;height:100%;background:#4f46e5;border-radius:8px}
    #codexia-resource-monitor .crm-bar.warning i{background:#f59e0b}
    #codexia-resource-monitor .crm-bar.critical i{background:#ef4444}
    #codexia-resource-monitor .crm-storage{border-top:1px solid #eef2f7;margin-top:7px;padding-top:6px}
    #codexia-resource-monitor .crm-storage-name{font-size:10px;color:#64748b;margin-bottom:2px}
    #codexia-resource-monitor .crm-empty{color:#64748b;font-size:11px;padding:4px 0}
    #codexia-resource-monitor .crm-drive{margin-top:8px;background:white;border:1px solid #e2e8f0;border-radius:12px;padding:9px 10px}
    #codexia-resource-monitor .crm-foot{color:#64748b;font-size:10px;text-align:right;margin-top:8px}
    @media(max-width:520px){#codexia-resource-monitor{right:8px;bottom:calc(8px + env(safe-area-inset-bottom,0px));width:calc(100vw - 16px)}#codexia-resource-monitor .crm-toggle{padding:8px 10px;gap:7px}#codexia-resource-monitor .crm-summary{font-size:10px}#codexia-resource-monitor .crm-panel{max-height:68vh;padding:9px}#codexia-resource-monitor .crm-grid{grid-template-columns:1fr 1fr;gap:6px}#codexia-resource-monitor .crm-card{padding:8px}}
    @media(max-width:350px){#codexia-resource-monitor .crm-grid{grid-template-columns:1fr}}
  `;
  document.head.appendChild(style);

  const root = document.createElement('aside');
  root.id = 'codexia-resource-monitor';
  root.dataset.health = 'checking';
  root.setAttribute('aria-label', 'Monitoramento dos servidores Codexia');
  root.innerHTML = `
    <section class="crm-panel" id="crm-panel" hidden>
      <div class="crm-head">
        <div><strong>Recursos dos servidores</strong><small id="crm-updated">Coletando dados...</small></div>
        <button class="crm-refresh" id="crm-refresh" type="button">Atualizar</button>
      </div>
      <div class="crm-grid">
        <article class="crm-card" id="crm-principal"></article>
        <article class="crm-card" id="crm-cx33"></article>
      </div>
      <article class="crm-drive" id="crm-drive"></article>
      <div class="crm-foot">Atualização automática a cada 30 segundos</div>
    </section>
    <button class="crm-toggle" id="crm-toggle" type="button" aria-expanded="false" aria-controls="crm-panel">
      <span class="crm-dot" aria-hidden="true"></span>
      <span class="crm-toggle-label">Servidores</span>
      <span class="crm-summary" id="crm-summary">Carregando uso de memória e espaço…</span>
      <span class="crm-chevron" id="crm-chevron" aria-hidden="true">▴</span>
    </button>
  `;
  document.body.appendChild(root);

  const $ = (id) => root.querySelector(`#${id}`);
  const panel = $('crm-panel');
  const toggle = $('crm-toggle');
  const refresh = $('crm-refresh');

  toggle.addEventListener('click', () => {
    const open = panel.hidden;
    panel.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    $('crm-chevron').textContent = open ? '▾' : '▴';
  });
  refresh.addEventListener('click', (event) => {
    event.stopPropagation();
    load(true);
  });

  const number = (value, digits = 1) => {
    const n = Number(value);
    return Number.isFinite(n) ? n.toLocaleString('pt-BR', { maximumFractionDigits: digits, minimumFractionDigits: 0 }) : '—';
  };
  const gb = (bytes) => `${number(Number(bytes || 0) / (1024 ** 3), 2)} GB`;
  const mb = (value) => `${number(Number(value || 0) / 1024, 2)} GB`;
  const percent = (value) => Math.max(0, Math.min(100, Number(value) || 0));
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  function metricRow(label, value, usedPercent) {
    const wrap = el('div', 'crm-metric');
    const line = el('div', 'crm-metric-line');
    line.append(el('span', '', label), el('b', '', value));
    wrap.append(line);
    if (usedPercent !== undefined && usedPercent !== null) {
      const bar = el('div', `crm-bar${usedPercent >= 95 ? ' critical' : usedPercent >= 85 ? ' warning' : ''}`);
      const fill = document.createElement('i');
      fill.style.width = `${percent(usedPercent)}%`;
      bar.append(fill);
      wrap.append(bar);
    }
    return wrap;
  }

  function serverHealth(server) {
    if (server && server.status === 'starting') return 'warning';
    if (!server || server.status !== 'online') return 'offline';
    const resources = server.resources || {};
    const memory = resources.memory || {};
    const swap = resources.swap || {};
    const storage = Array.isArray(resources.storage) ? resources.storage : [];
    if (storage.some((disk) => Number(disk.free_bytes || 0) <= 2 * (1024 ** 3)) ||
        (Number(memory.total_mb || 0) > 0 && Number(memory.available_mb || 0) <= 384) ||
        Number(swap.used_percent || 0) >= 95) return 'critical';
    if (storage.some((disk) => Number(disk.free_bytes || 0) <= 5 * (1024 ** 3)) ||
        (Number(memory.total_mb || 0) > 0 && Number(memory.available_mb || 0) <= 768) ||
        Number(swap.used_percent || 0) >= 85 ||
        Number((resources.cpu || {}).load_per_core || 0) >= 2.5) return 'warning';
    return 'good';
  }

  function renderServer(target, label, server) {
    target.replaceChildren();
    const status = serverHealth(server);
    const title = el('div', 'crm-card-title');
    title.append(el('span', '', label), el('span', `crm-status ${status}`, status === 'good' ? 'Online' : status === 'warning' ? 'Atenção' : status === 'critical' ? 'Crítico' : 'Sem sinal'));
    target.append(title);
    if (status === 'offline') {
      target.append(el('div', 'crm-empty', 'Sem atualização recente deste servidor.'));
      return;
    }
    if (server.status === 'starting') {
      target.append(el('div', 'crm-empty', 'Worker conectado; aguardando o primeiro envio de métricas.'));
      return;
    }

    const r = server.resources || {};
    const memory = r.memory || {};
    target.append(metricRow('RAM usada', `${mb(memory.used_mb)} / ${mb(memory.total_mb)} · ${number(memory.used_percent)}%`, memory.used_percent));
    const swap = r.swap || {};
    if (Number(swap.total_mb || 0) > 0) {
      target.append(metricRow('Swap usada', `${mb(swap.used_mb)} / ${mb(swap.total_mb)} · ${number(swap.used_percent)}%`, swap.used_percent));
    }
    const cpu = r.cpu || {};
    if (Number(cpu.logical_cores || 0) > 0) {
      target.append(metricRow('Carga CPU', `${number(cpu.load_1m)} · ${number(cpu.logical_cores, 0)} núcleos`));
    }
    const storage = Array.isArray(r.storage) ? r.storage : [];
    if (!storage.length) target.append(el('div', 'crm-empty', 'Volumes não identificados.'));
    storage.forEach((disk) => {
      const entry = el('div', 'crm-storage');
      entry.append(el('div', 'crm-storage-name', (disk.areas || []).join(' · ') || 'Armazenamento'));
      entry.append(metricRow('Livre / total', `${gb(disk.free_bytes)} / ${gb(disk.total_bytes)} · ${number(disk.used_percent)}% usado`, disk.used_percent));
      target.append(entry);
    });
  }

  function renderDrive(drive) {
    const target = $('crm-drive');
    target.replaceChildren();
    const row = el('div', 'crm-metric-line');
    const connected = drive && drive.connected === true;
    row.append(el('b', '', 'Google Drive · arquivo Codexia'));
    row.append(el('b', '', connected ? `${gb(drive.archive_bytes)} · ${number(drive.archive_files, 0)} arquivos` :
      drive && drive.status === 'checking' ? 'Consultando…' :
      drive && drive.status === 'error' ? 'Consulta indisponível' : 'Não conectado'));
    target.append(row);
  }

  function compactServer(server, label) {
    if (server && server.status === 'starting') return `${label}: coletando métricas`;
    if (!server || server.status !== 'online') return `${label}: sem sinal`;
    const r = server.resources || {};
    const memory = r.memory || {};
    const storage = Array.isArray(r.storage) ? r.storage : [];
    const free = storage.length ? Math.min(...storage.map((disk) => Number(disk.free_bytes || 0))) : null;
    return `${label}: RAM ${mb(memory.available_mb)} livre · disco ${free === null ? '—' : gb(free)} livre`;
  }

  function overallHealth(data) {
    const servers = (data && data.servers) || {};
    const states = [serverHealth(servers.principal), serverHealth(servers.cx33)];
    if (states.includes('critical')) return 'critical';
    if (states.includes('offline')) return servers.cx33 && servers.cx33.status === 'offline' ? 'warning' : 'offline';
    if (states.includes('warning')) return 'warning';
    return 'good';
  }

  async function load(force) {
    if (document.hidden && !force) return;
    const token = localStorage.getItem(TOKEN_KEY);
    if (!token) {
      root.remove();
      return;
    }
    try {
      const response = await fetch('/admin/server-resources', {
        headers: { Authorization: `Bearer ${token}` },
        cache: 'no-store',
      });
      if (response.status === 401 || response.status === 403) {
        root.remove();
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const servers = data.servers || {};
      renderServer($('crm-principal'), 'Servidor principal', servers.principal);
      renderServer($('crm-cx33'), 'CX33 · produção', servers.cx33);
      renderDrive(data.google_drive || {});
      root.dataset.health = overallHealth(data);
      const mainText = compactServer(servers.principal, 'Principal');
      const workerText = compactServer(servers.cx33, 'CX33');
      $('crm-summary').textContent = `${mainText} | ${workerText}`;
      $('crm-updated').textContent = `Métricas atualizadas ${new Date(data.captured_at || Date.now()).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
    } catch (_) {
      root.dataset.health = 'offline';
      $('crm-summary').textContent = 'Não foi possível atualizar as métricas.';
      $('crm-updated').textContent = 'Falha de comunicação com o monitor.';
    }
  }

  load(false);
  window.setInterval(() => load(false), REFRESH_MS);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) load(false);
  });
})();
