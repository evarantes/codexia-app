(() => {
  // Keeps story/devotional/short productions isolated in the UI. The backend
  // persists each slot separately; this controller only exposes the current slot
  // and clears stale visible plans when the user switches production type.
  const slotForType = value => ['story','devotional','short'].includes(String(value || '')) ? String(value) : 'story';
  let activeSlot = slotForType(document.getElementById('contentType')?.value || 'story');

  function clearVisiblePlan() {
    try { if (typeof currentPlan !== 'undefined') currentPlan = null; } catch (_) {}
    const out = document.getElementById('directorOutput');
    if (out) out.classList.add('hidden');
    const scenes = document.getElementById('scenes');
    if (scenes) scenes.innerHTML = '';
    const estimate = document.getElementById('estimateBox');
    if (estimate) estimate.innerHTML = '';
    const status = document.getElementById('directorAsyncStatus');
    if (status) {
      status.style.display = 'block';
      status.className = 'notice';
      status.textContent = 'Carregando o projeto deste tipo de conteúdo…';
    }
  }

  function setSlot(slot, { clear = true } = {}) {
    activeSlot = slotForType(slot);
    if (clear) clearVisiblePlan();
    window.dispatchEvent(new CustomEvent('codexia:project-slot-changed', { detail: { slot: activeSlot } }));
    return activeSlot;
  }

  window.CodexiaProjectSlot = {
    get: () => activeSlot,
    set: setSlot,
    fromContentType: () => setSlot(document.getElementById('contentType')?.value || 'story'),
  };

  document.addEventListener('click', ev => {
    const idea = ev.target.closest?.('.idea');
    if (idea) {
      // The inline handler will populate the theme and navigate; we only switch
      // the persistence slot first so the previous story cannot bleed through.
      setSlot('devotional');
      return;
    }
    if (ev.target.closest?.('#newDevotional')) {
      setSlot('devotional');
      return;
    }
    const navStudio = ev.target.closest?.('[data-page="studio"]');
    if (navStudio && !ev.target.closest?.('.idea')) {
      const type = document.getElementById('contentType');
      if (type) type.value = 'story';
      setSlot('story');
    }
  }, true);

  document.getElementById('contentType')?.addEventListener('change', ev => setSlot(ev.target.value));
})();
