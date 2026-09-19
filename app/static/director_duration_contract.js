(() => {
  const sendButton = document.getElementById('baseProductionBtn');
  if (!sendButton) return;

  const PENDING_LIBRARY_KEY = 'codexia_v2_pending_library_tasks_v1';

  const moneySafe = value => {
    try { return Number(value || 0).toLocaleString('pt-BR', { minimumFractionDigits: 0, maximumFractionDigits: 0 }); }
    catch (_) { return String(value || 0); }
  };

  function pendingRegistrations() {
    try {
      const parsed = JSON.parse(localStorage.getItem(PENDING_LIBRARY_KEY) || '[]');
      return Array.isArray(parsed) ? parsed.filter(item => item && item.task_id) : [];
    } catch (_) {
      return [];
    }
  }

  function savePendingRegistrations(items) {
    try {
      if (!items.length) localStorage.removeItem(PENDING_LIBRARY_KEY);
      else localStorage.setItem(PENDING_LIBRARY_KEY, JSON.stringify(items.slice(-20)));
    } catch (_) {}
  }

  function rememberPendingRegistration(item) {
    const items = pendingRegistrations().filter(existing => existing.task_id !== item.task_id);
    items.push(item);
    savePendingRegistrations(items);
  }

  async function registerLibraryTask(item) {
    if (!item || !item.task_id) return false;
    await authFetch('/youtube/cinematic/library/register', {
      method: 'POST',
      body: JSON.stringify({
        task_id: item.task_id,
        title: item.title || undefined,
        project_slot: item.project_slot || 'story',
        duration_minutes: item.duration_minutes || undefined,
      }),
    });
    const remaining = pendingRegistrations().filter(existing => existing.task_id !== item.task_id);
    savePendingRegistrations(remaining);
    return true;
  }

  async function flushPendingRegistrations() {
    const pending = pendingRegistrations();
    if (!pending.length) return;
    for (const item of pending) {
      try { await registerLibraryTask(item); } catch (_) { /* retry on the next page load */ }
    }
  }

  function contractBox() {
    let box = document.getElementById('directorDurationContract');
    if (box) return box;
    box = document.createElement('div');
    box.id = 'directorDurationContract';
    box.className = 'notice';
    box.style.marginTop = '10px';
    const meta = document.getElementById('directorMeta');
    if (meta && meta.parentElement) meta.insertAdjacentElement('afterend', box);
    else {
      const output = document.getElementById('directorOutput');
      if (output) output.prepend(box);
    }
    return box;
  }

  function fmtSeconds(seconds) {
    const raw = Math.max(0, Number(seconds || 0));
    const minutes = Math.floor(raw / 60);
    const secs = Math.round(raw % 60);
    return `${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
  }

  function renderDurationContract(plan) {
    const box = contractBox();
    const contract = plan && plan.duration_contract && typeof plan.duration_contract === 'object'
      ? plan.duration_contract
      : null;
    if (!contract) {
      box.className = 'notice warn';
      box.innerHTML = '<b>Contrato de produção ausente.</b><br><small>Este plano não pode ser enviado à produção até o Claude validar a duração.</small>';
      sendButton.disabled = true;
      sendButton.title = 'A duração ainda não foi validada pelo Diretor.';
      return;
    }

    const valid = contract.validated === true;
    box.className = valid ? 'notice ok' : 'notice warn';
    box.innerHTML = [
      `<b>${valid ? '✅ Contrato da produção validado pelo Diretor' : '⚠️ Contrato da produção ainda não validado'}</b>`,
      `<div style="margin-top:7px;display:flex;gap:14px;flex-wrap:wrap">`,
      `<span><b>Duração contratada:</b> ${fmtSeconds(contract.target_seconds)}</span>`,
      `<span><b>Duração prevista:</b> ${fmtSeconds(contract.estimated_seconds)}</span>`,
      `<span><b>Narração:</b> ${moneySafe(contract.actual_words)} palavras</span>`,
      `<span><b>Ritmo:</b> ${moneySafe(contract.voice_wpm)} ppm</span>`,
      `<span><b>Faixa aceita:</b> ${moneySafe(contract.min_words)}–${moneySafe(contract.max_words)} palavras</span>`,
      contract.repair_attempts ? `<span><b>Correções automáticas:</b> ${contract.repair_attempts}</span>` : '',
      `</div>`,
      `<small style="display:block;margin-top:7px">${valid ? 'A narração aprovada fica congelada para o pipeline; etapas seguintes executam este contrato e não podem alongar o roteiro.' : 'O Codexia deve devolver o plano ao Claude antes de gerar voz, imagens ou vídeo.'}</small>`,
    ].join('');
    sendButton.disabled = !valid;
    sendButton.title = valid ? '' : 'A duração ainda não foi validada pelo Diretor.';
  }

  const originalRenderPlan = typeof renderPlan === 'function' ? renderPlan : null;
  if (originalRenderPlan) {
    globalThis.renderPlan = function wrappedRenderPlan(data) {
      originalRenderPlan(data);
      renderDurationContract(data && data.plan ? data.plan : null);
    };
  }

  // Replace the old V2 handoff. Claude's validated full_script is the canonical
  // narration. Mark it as editorially approved so downstream stages execute it
  // instead of treating the director-approved text as a fresh draft.
  sendButton.onclick = async event => {
    event.preventDefault();
    if (typeof currentPlan === 'undefined' || !currentPlan) return;
    const contract = currentPlan.duration_contract;
    if (!contract || contract.validated !== true) {
      alert('A produção foi bloqueada: o Claude ainda não validou o contrato de duração. Gere/revise a direção antes de continuar.');
      return;
    }
    const targetMinutes = Number(currentPlan.duration_minutes || document.getElementById('duration')?.value || 10);
    const predicted = fmtSeconds(contract.estimated_seconds);
    const target = fmtSeconds(contract.target_seconds);
    if (!confirm(`Enviar esta direção validada ao pipeline?\n\nDuração contratada: ${target}\nDuração prevista: ${predicted}\n\nA narração será tratada como aprovada e não deverá ser reescrita pelas etapas posteriores.`)) return;

    sendButton.disabled = true;
    const oldText = sendButton.textContent;
    sendButton.textContent = 'Enviando contrato aprovado…';
    try {
      const firstTitle = Array.isArray(currentPlan.title_options) && currentPlan.title_options.length
        ? String(currentPlan.title_options[0] || '').trim()
        : '';
      const projectSlot = currentPlan.content_type === 'devotional'
        ? 'devotional'
        : currentPlan.content_type === 'short' ? 'short' : 'story';
      const data = await authFetch('/youtube/generate_video', {
        method: 'POST',
        body: JSON.stringify({
          topic: currentPlan.theme || document.getElementById('theme')?.value || '',
          duration: targetMinutes,
          auto_upload: false,
          mode: 'story',
          kind: projectSlot === 'devotional' ? 'devotional' : 'story',
          story_content: currentPlan.full_script,
          image_mode: 'multiple',
          aspect_ratio: '16:9',
          override_title: firstTitle || undefined,
          editorial_reviewed: true,
          editorial_review_ready: true,
          director_quality_required: true,
          // Codexia V2 premium productions must never silently degrade to
          // Edge/gTTS. If the configured premium provider is unavailable the
          // task stops before paid visual generation and explains the cause.
          premium_voice_required: true,
        }),
      });

      let librarySynced = true;
      if (data.task_id) {
        const registration = {
          task_id: String(data.task_id),
          title: firstTitle || currentPlan.theme || '',
          project_slot: projectSlot,
          duration_minutes: targetMinutes,
        };
        rememberPendingRegistration(registration);
        try {
          await registerLibraryTask(registration);
        } catch (_) {
          librarySynced = false;
          // The production is already safe in the canonical pipeline. Keep a
          // durable browser-side reminder and retry registration automatically.
        }
      }

      alert(
        `Produção enviada com contrato de duração validado. ${data.task_id ? `Tarefa: ${data.task_id}` : ''}`
        + (librarySynced ? '\n\nO projeto já foi adicionado à biblioteca do Codexia V2.' : '\n\nA produção foi enviada, mas a biblioteca ainda está sincronizando. O Codexia tentará novamente automaticamente.')
      );
      try {
        const queueButton = document.querySelector('[data-page="queue"]');
        if (queueButton) queueButton.click();
      } catch (_) {}
    } catch (error) {
      alert(error && error.message ? error.message : 'Falha ao enviar a produção.');
    } finally {
      sendButton.disabled = false;
      sendButton.textContent = oldText;
      renderDurationContract(currentPlan);
    }
  };

  // A persisted plan may already be on screen before this controller loads.
  try {
    if (typeof currentPlan !== 'undefined' && currentPlan) renderDurationContract(currentPlan);
  } catch (_) {}

  // Recover a task that was created successfully but whose registration response
  // was lost during a browser/network interruption. Registration is idempotent.
  setTimeout(() => { void flushPendingRegistrations(); }, 800);
})();
