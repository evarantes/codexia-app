(() => {
  'use strict';

  const STORAGE_KEY = 'codexia.logoOnlyVisuals.v1';
  const HEADER = 'X-Codexia-Logo-Only-Visuals';
  const delegatedFetch = window.fetch.bind(window);

  function normalizeText(value) {
    return String(value || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n').trim();
  }

  function enabled() {
    return localStorage.getItem(STORAGE_KEY) === '1';
  }

  function setEnabled(value) {
    localStorage.setItem(STORAGE_KEY, value ? '1' : '0');
    document.querySelectorAll('[data-logo-only-checkbox]').forEach(input => {
      input.checked = Boolean(value);
    });
    document.querySelectorAll('[data-logo-only-state]').forEach(el => {
      el.textContent = value
        ? 'ATIVO: a logo oficial substituirá imagens e thumbnails geradas por IA.'
        : 'Desativado: a geração visual funciona normalmente.';
      el.style.color = value ? '#047857' : '#64748b';
    });
    window.dispatchEvent(new CustomEvent('codexia:logo-only-visuals', { detail: { enabled: Boolean(value) } }));
  }

  function checkboxMarkup(compact = false) {
    return `
      <label class="flex items-start gap-2 cursor-pointer select-none ${compact ? 'text-xs' : 'text-sm'}">
        <input type="checkbox" data-logo-only-checkbox class="mt-0.5 h-4 w-4" ${enabled() ? 'checked' : ''}>
        <span>
          <strong>Usar apenas a logo do canal</strong>
          <span class="block text-slate-600">Não gerar imagens nem thumbnail por IA; manter o restante do fluxo normal.</span>
          <span data-logo-only-state class="block mt-1 ${compact ? 'text-[11px]' : 'text-xs'}"></span>
        </span>
      </label>`;
  }

  function bindBox(box) {
    const input = box.querySelector('[data-logo-only-checkbox]');
    if (!input) return;
    input.checked = enabled();
    input.addEventListener('change', () => setEnabled(input.checked));
    setEnabled(enabled());
  }

  function mountNarrationGate() {
    const panel = document.querySelector('[data-youtube-narration-gate]');
    if (!panel || panel.querySelector('[data-global-logo-only-mode]')) return;
    const box = document.createElement('div');
    box.dataset.globalLogoOnlyMode = '1';
    box.className = 'mb-3 rounded-lg border border-emerald-300 bg-emerald-50 p-3';
    box.innerHTML = checkboxMarkup(false);
    const buttons = panel.querySelector('.flex.flex-wrap.gap-2.mb-3');
    if (buttons) buttons.insertAdjacentElement('afterend', box);
    else panel.appendChild(box);
    bindBox(box);
  }

  function isImageActionButton(button) {
    const text = normalizeText(button?.textContent).toLowerCase();
    if (!text) return false;
    return [
      'gerar imagem', 'gerar imagens', 'regenerar imagem', 'regenerar imagens',
      'gerar thumbnail', 'regenerar thumbnail', 'gerar capa', 'criar imagem',
      'gerar cenas', 'gerar storyboard'
    ].some(term => text.includes(term));
  }

  function mountNearImageActions() {
    const buttons = [...document.querySelectorAll('button')].filter(isImageActionButton);
    const mountedContainers = new Set();
    buttons.forEach(button => {
      let host = button.parentElement;
      for (let depth = 0; host && depth < 3; depth += 1, host = host.parentElement) {
        if (host.querySelector?.('[data-global-logo-only-mode]')) return;
        if (host.classList?.contains('bg-white') || host.tagName === 'SECTION' || host.tagName === 'FORM') break;
      }
      host = host || button.parentElement;
      if (!host || mountedContainers.has(host) || host.querySelector?.('[data-global-logo-only-mode]')) return;
      mountedContainers.add(host);
      const box = document.createElement('div');
      box.dataset.globalLogoOnlyMode = '1';
      box.className = 'mt-2 mb-2 rounded-lg border border-emerald-200 bg-emerald-50 p-2';
      box.innerHTML = checkboxMarkup(true);
      button.insertAdjacentElement('afterend', box);
      bindBox(box);
    });
  }

  function mount() {
    mountNarrationGate();
    mountNearImageActions();
  }

  function withLogoOnlyBody(init) {
    if (!enabled()) return init || {};
    const next = { ...(init || {}) };
    const headers = new Headers(next.headers || {});
    headers.set(HEADER, '1');
    next.headers = headers;
    if (typeof next.body === 'string') {
      try {
        const payload = JSON.parse(next.body);
        if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
          payload.logo_only_visuals = true;
          next.body = JSON.stringify(payload);
        }
      } catch (_) {}
    }
    return next;
  }

  window.fetch = function codexiaGlobalLogoOnlyFetch(input, init = {}) {
    return delegatedFetch(input, withLogoOnlyBody(init));
  };

  new MutationObserver(mount).observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener('DOMContentLoaded', mount);
  window.addEventListener('storage', event => {
    if (event.key === STORAGE_KEY) setEnabled(event.newValue === '1');
  });
  window.setInterval(mount, 1500);
})();
