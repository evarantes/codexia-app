(() => {
  const LEGACY_JOB_KEY = 'codexia_active_director_job_v1';
  const STORY_JOB_BACKUP_KEY = 'codexia_story_director_job_backup_v1';
  const normalize = value => ['story', 'devotional', 'short'].includes(String(value || '').toLowerCase())
    ? String(value).toLowerCase()
    : 'story';

  let desiredSlot = normalize(document.getElementById('contentType')?.value || 'story');

  function hidePlan(message = '') {
    try { currentPlan = null; } catch (_) {}
    const out = document.getElementById('directorOutput');
    if (out) out.classList.add('hidden');
    const scenes = document.getElementById('scenes');
    if (scenes) scenes.innerHTML = '';
    const estimate = document.getElementById('estimateBox');
    if (estimate) estimate.innerHTML = '';
    const box = document.getElementById('directorAsyncStatus');
    if (box && message) {
      box.style.display = 'block';
      box.className = 'notice';
      box.textContent = message;
    }
  }

  function migrateLegacyStoryJobAwayFromOtherSlots(slot) {
    if (slot === 'story') return;
    try {
      const raw = localStorage.getItem(LEGACY_JOB_KEY);
      if (!raw) return;
      localStorage.setItem(STORY_JOB_BACKUP_KEY, raw);
      localStorage.removeItem(LEGACY_JOB_KEY);
    } catch (_) {}
  }

  function selectSlot(slot, message = '') {
    desiredSlot = normalize(slot);
    migrateLegacyStoryJobAwayFromOtherSlots(desiredSlot);
    hidePlan(message);
    document.documentElement.dataset.codexiaProjectSlot = desiredSlot;
  }

  document.addEventListener('click', event => {
    if (event.target.closest?.('.idea') || event.target.closest?.('#newDevotional')) {
      selectSlot('devotional', 'Novo devocional selecionado. A história cinematográfica continua salva separadamente.');
      return;
    }
    if (event.target.closest?.('[data-page="studio"], [data-go="studio"]')) {
      selectSlot('story', 'Carregando o projeto cinematográfico salvo…');
    }
  }, true);

  document.getElementById('contentType')?.addEventListener('change', event => {
    selectSlot(event.target.value, 'Tipo de produção alterado. O plano anterior foi ocultado para evitar mistura entre projetos.');
  }, true);

  const originalRender = window.renderPlan;
  if (typeof originalRender === 'function') {
    window.renderPlan = function guardedRenderPlan(data) {
      const formSlot = normalize(document.getElementById('contentType')?.value || desiredSlot);
      const planSlot = normalize(data?.plan?.content_type || formSlot);
      const slotNow = normalize(window.CodexiaProjectSlots?.get?.() || desiredSlot || formSlot);
      desiredSlot = formSlot;
      if (planSlot !== formSlot || planSlot !== slotNow) {
        hidePlan(`Plano de ${planSlot === 'devotional' ? 'devocional' : 'história'} ignorado porque você está em outro projeto.`);
        console.warn('Codexia blocked cross-project render', { planSlot, formSlot, slotNow });
        return;
      }
      return originalRender(data);
    };
  }

  // A stale story plan may already be visible before this controller loads.
  // Re-check shortly after startup and hide it if the form is on another slot.
  setTimeout(() => {
    const formSlot = normalize(document.getElementById('contentType')?.value || desiredSlot);
    desiredSlot = formSlot;
    const visible = !document.getElementById('directorOutput')?.classList.contains('hidden');
    const planSlot = normalize((typeof currentPlan !== 'undefined' && currentPlan?.content_type) || formSlot);
    if (visible && planSlot !== formSlot) {
      hidePlan('O plano de outro projeto foi ocultado. Gere a direção deste conteúdo sem perder o projeto anterior.');
    }
  }, 900);

  window.CodexiaProjectSlotGuard = {
    get: () => desiredSlot,
    select: selectSlot,
    hidePlan,
  };
})();
