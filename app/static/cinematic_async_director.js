(() => {
  const STORAGE_KEY = 'codexia_active_director_job_v1';
  const button = document.getElementById('directBtn');
  if (!button) return;

  const originalHtml = button.innerHTML;
  let pollGeneration = 0;

  function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  function jobId() {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
      return globalThis.crypto.randomUUID();
    }
    return `job-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
  }

  function getStoredJob() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      return parsed && parsed.id ? parsed : null;
    } catch (_) {
      return null;
    }
  }

  function storeJob(id) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ id, created_at: Date.now() }));
  }

  function clearJob() {
    localStorage.removeItem(STORAGE_KEY);
  }

  function ensureStatusBox() {
    let box = document.getElementById('directorAsyncStatus');
    if (box) return box;
    box = document.createElement('div');
    box.id = 'directorAsyncStatus';
    box.className = 'notice';
    box.style.marginTop = '10px';
    box.style.display = 'none';
    const parent = button.closest('.card') || button.parentElement;
    if (parent) parent.appendChild(box);
    return box;
  }

  function setUi(message, { busy = true, kind = 'normal' } = {}) {
    button.disabled = busy;
    button.innerHTML = busy ? '<i class="spinner"></i> Claude trabalhando em segundo plano…' : originalHtml;
    const box = ensureStatusBox();
    box.style.display = 'block';
    box.className = kind === 'error' ? 'notice warn' : kind === 'ok' ? 'notice ok' : 'notice';
    box.textContent = message;
  }

  function resetUi(message = '') {
    button.disabled = false;
    button.innerHTML = originalHtml;
    const box = ensureStatusBox();
    if (message) {
      box.style.display = 'block';
      box.className = 'notice ok';
      box.textContent = message;
    } else {
      box.style.display = 'none';
    }
  }

  async function api(url, options = {}) {
    const token = localStorage.getItem('access_token');
    const headers = { ...(options.headers || {}), Authorization: `Bearer ${token}` };
    if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
    let response;
    try {
      response = await fetch(url, { ...options, headers, cache: 'no-store' });
    } catch (networkError) {
      const error = new Error('Conexão temporariamente indisponível.');
      error.network = true;
      error.cause = networkError;
      throw error;
    }
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (response.status === 401) {
      localStorage.removeItem('access_token');
      location.href = '/login.html';
      const error = new Error('Sessão expirada.');
      error.status = 401;
      throw error;
    }
    if (!response.ok) {
      const error = new Error(data.detail || data.message || `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function payload() {
    return {
      theme: document.getElementById('theme').value,
      content_type: document.getElementById('contentType').value,
      duration_minutes: Number(document.getElementById('duration').value),
      budget_brl: Number(document.getElementById('videoBudget').value),
    };
  }

  async function poll(id, { submissionUncertain = false } = {}) {
    const myGeneration = ++pollGeneration;
    let consecutiveNetworkErrors = 0;
    let consecutiveNotFound = 0;

    for (let attempt = 0; attempt < 240; attempt += 1) {
      if (myGeneration !== pollGeneration) return;
      try {
        const job = await api(`/youtube/cinematic/director/jobs/${encodeURIComponent(id)}`);
        consecutiveNetworkErrors = 0;
        consecutiveNotFound = 0;
        const progress = Number(job.progress || 0);

        if (job.status === 'completed' && job.result) {
          clearJob();
          currentPlan = job.result.plan;
          renderPlan(job.result);
          resetUi('Direção concluída. O plano foi recuperado do servidor e está pronto para revisão.');
          return;
        }

        if (job.status === 'failed') {
          clearJob();
          setUi(job.error || job.message || 'A direção falhou no servidor.', { busy: false, kind: 'error' });
          button.disabled = false;
          button.innerHTML = originalHtml;
          return;
        }

        setUi(`${job.message || 'Claude está trabalhando em segundo plano.'} ${progress ? `(${progress}%)` : ''}`);
      } catch (error) {
        if (error.status === 404) {
          consecutiveNotFound += 1;
          const tolerated = submissionUncertain ? 10 : 3;
          if (consecutiveNotFound > tolerated) {
            clearJob();
            setUi('O servidor não encontrou a tarefa. Clique novamente para criar uma nova direção.', { busy: false, kind: 'error' });
            button.disabled = false;
            button.innerHTML = originalHtml;
            return;
          }
          setUi('Confirmando se o servidor recebeu o pedido…');
        } else if (error.network) {
          consecutiveNetworkErrors += 1;
          setUi('A conexão oscilou, mas a tarefa continua no servidor. Tentando reconectar automaticamente…');
          if (consecutiveNetworkErrors >= 40) {
            button.disabled = false;
            button.innerHTML = 'Retomar direção';
            const box = ensureStatusBox();
            box.textContent = 'A tarefa continua registrada. Toque em “Retomar direção” quando a conexão estabilizar; isso não cria uma nova cobrança.';
            return;
          }
        } else {
          clearJob();
          setUi(error.message || 'Falha ao consultar a direção.', { busy: false, kind: 'error' });
          button.disabled = false;
          button.innerHTML = originalHtml;
          return;
        }
      }
      await sleep(2500);
    }

    button.disabled = false;
    button.innerHTML = 'Retomar direção';
    const box = ensureStatusBox();
    box.textContent = 'A direção ainda está registrada no servidor. Toque em “Retomar direção” para continuar acompanhando sem repetir a chamada ao Claude.';
  }

  async function startOrResume() {
    const existing = getStoredJob();
    if (existing && existing.id) {
      setUi('Retomando a direção já enviada ao servidor, sem nova cobrança…');
      await poll(existing.id, { submissionUncertain: true });
      return;
    }

    const id = jobId();
    const body = { request_id: id, ...payload() };
    storeJob(id); // persist before POST: even a lost response can be recovered safely
    setUi('Enviando a direção ao servidor. Você pode trocar de tela; o processamento continuará.');

    let uncertain = false;
    try {
      await api('/youtube/cinematic/director/jobs', {
        method: 'POST',
        body: JSON.stringify(body),
      });
    } catch (error) {
      if (error.network || (error.status && error.status >= 500)) {
        uncertain = true;
        setUi('A resposta da conexão se perdeu. Verificando a mesma tarefa no servidor para evitar cobrança duplicada…');
      } else {
        clearJob();
        setUi(error.message || 'Não foi possível iniciar a direção.', { busy: false, kind: 'error' });
        button.disabled = false;
        button.innerHTML = originalHtml;
        return;
      }
    }
    await poll(id, { submissionUncertain: uncertain });
  }

  // Override the old long synchronous request. The server now answers the POST
  // immediately and the browser only polls short status requests.
  button.onclick = event => {
    event.preventDefault();
    void startOrResume();
  };

  const pending = getStoredJob();
  if (pending && pending.id) {
    setTimeout(() => {
      setUi('Foi encontrada uma direção em andamento. Retomando automaticamente sem nova cobrança…');
      void poll(pending.id, { submissionUncertain: true });
    }, 350);
  }
})();
