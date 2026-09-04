(() => {
  'use strict';

  const STORAGE_KEY = 'codexia.youtubeAuto.approvedNarration.coreV1';
  const LEGACY_STORAGE_KEYS = [
    'codexia.youtubeAuto.approvedNarration.v1',
    'codexia.youtubeAuto.approvedNarration.v4'
  ];
  const CORE_VERSION = 1;
  const CORE_NAMESPACE = 'codexia-narration-core-v1';
  const originalFetch = window.fetch.bind(window);
  let mountedCard = null;
  let preview = null;
  let approved = null;
  let audioObjectUrl = '';
  let approvedLaunchArmed = false;
  let approvedInjectionCount = 0;

  function tokenHeaders(extra = {}) {
    const headers = new Headers(extra || {});
    const token = localStorage.getItem('access_token');
    if (token) headers.set('Authorization', `Bearer ${token}`);
    return headers;
  }

  async function authFetch(url, options = {}) {
    const response = await originalFetch(url, { ...options, headers: tokenHeaders(options.headers) });
    if (response.status === 401) {
      localStorage.removeItem('access_token');
      window.location.href = '/static/login.html';
    }
    return response;
  }

  function normalizeText(value) {
    return String(value || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n').trim();
  }

  async function sha256(value) {
    const bytes = new TextEncoder().encode(normalizeText(value));
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
  }

  function storyCard() {
    const heading = [...document.querySelectorAll('h2')].find(el => normalizeText(el.textContent).includes('História/Devocional'));
    if (!heading) return null;
    let node = heading;
    while (node && node !== document.body) {
      if (node.classList && node.classList.contains('bg-white') && node.querySelector('textarea')) return node;
      node = node.parentElement;
    }
    return heading.parentElement && heading.parentElement.parentElement;
  }

  function storyTextarea(card) {
    if (!card) return null;
    const label = [...card.querySelectorAll('label')].find(el => normalizeText(el.textContent) === 'Texto para narração');
    if (label && label.parentElement) {
      const textarea = label.parentElement.querySelector('textarea');
      if (textarea) return textarea;
    }
    return card.querySelector('textarea[placeholder*="história"], textarea[placeholder*="devocional"]');
  }

  function existingGenerateButton(card) {
    return [...card.querySelectorAll('button')].find(btn => !btn.closest('[data-youtube-narration-gate]') && normalizeText(btn.textContent).toLowerCase().includes('gerar vídeo narrado')) || null;
  }

  function isCurrentApproved(value) {
    if (!value || !value.reuse_audio_from || !value.text_sha256 || !value.source_text_sha256) return false;
    const reuse = value.reuse_audio_from || {};
    return Number(value.narration_core_version || reuse.narration_core_version || 0) === CORE_VERSION
      && String(value.narration_core_namespace || reuse.narration_core_namespace || '') === CORE_NAMESPACE;
  }

  function loadApproved() {
    LEGACY_STORAGE_KEYS.forEach(key => localStorage.removeItem(key));
    try {
      const value = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
      if (isCurrentApproved(value)) approved = value;
      else localStorage.removeItem(STORAGE_KEY);
    } catch (_) {
      localStorage.removeItem(STORAGE_KEY);
    }
  }

  function saveApproved(value) {
    if (!isCurrentApproved(value)) throw new Error('A narração não pertence ao Narration Core v1.');
    approved = value;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
  }

  function clearApproved() {
    approved = null;
    approvedLaunchArmed = false;
    approvedInjectionCount = 0;
    localStorage.removeItem(STORAGE_KEY);
  }

  function detailMessage(data, fallback) {
    const detail = data && data.detail;
    if (detail && typeof detail === 'object') return detail.message || detail.code || JSON.stringify(detail);
    return detail || (data && data.message) || fallback;
  }

  function setStatus(panel, text, kind = 'info') {
    const el = panel.querySelector('[data-ng-status]');
    if (!el) return;
    el.textContent = text || '';
    el.style.color = kind === 'error' ? '#991b1b' : kind === 'success' ? '#047857' : '#475569';
  }

  async function loadProtectedAudio(url, panel) {
    if (audioObjectUrl) URL.revokeObjectURL(audioObjectUrl);
    audioObjectUrl = '';
    const response = await authFetch(url, { cache: 'no-store' });
    if (!response.ok) throw new Error('Não foi possível carregar o áudio protegido.');
    const blob = await response.blob();
    audioObjectUrl = URL.createObjectURL(blob);
    const audio = panel.querySelector('[data-ng-audio]');
    audio.src = audioObjectUrl;
    audio.hidden = false;
  }

  async function generatePreview(panel, card, feedback = '') {
    const textarea = storyTextarea(card);
    const text = normalizeText(textarea && textarea.value);
    if (!text) {
      setStatus(panel, 'Gere ou cole o texto da narração antes de criar o áudio.', 'error');
      return false;
    }
    const btn = panel.querySelector('[data-ng-start]');
    if (btn) btn.disabled = true;
    const redo = panel.querySelector('[data-ng-redo]');
    if (redo) redo.disabled = true;
    clearApproved();
    preview = null;
    panel.querySelector('[data-ng-continue]').disabled = true;
    panel.querySelector('[data-ng-approve]').disabled = true;
    setStatus(panel, 'Gerando somente o áudio para sua análise. Imagens e renderização continuam bloqueados…');
    try {
      const response = await authFetch('/youtube/narration-lab/production-preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text,
          voice: 'auto',
          voice_gender: 'female',
          feedback: normalizeText(feedback),
          revision_instruction: normalizeText(feedback)
        })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(detailMessage(data, 'Falha ao gerar a narração.'));
      if (Number(data.narration_core_version || 0) !== CORE_VERSION || String(data.narration_core_namespace || '') !== CORE_NAMESPACE) {
        throw new Error('O servidor respondeu com uma versão antiga da narração. Atualize o deploy antes de continuar.');
      }
      preview = data;
      panel.querySelector('[data-ng-spoken]').textContent = data.spoken_text_sent_to_tts || '';
      panel.querySelector('[data-ng-result]').hidden = false;
      panel.querySelector('[data-ng-approve]').disabled = false;
      await loadProtectedAudio(data.audio_url, panel);
      const duration = Number(data.audio_duration_sec || 0);
      const removed = Number(data.removed_technical_blocks || 0);
      const removedLabel = removed ? ` • ${removed} bloco(s) técnico(s) removido(s)` : '';
      setStatus(panel, `Áudio pronto para análise${duration ? ` • ${Math.floor(duration / 60)}m ${Math.round(duration % 60)}s` : ''}${data.cache_hit ? ' • áudio reaproveitado' : ''}${removedLabel}. A produção do vídeo NÃO começou.`, 'success');
      return true;
    } catch (err) {
      preview = null;
      setStatus(panel, err && err.message ? err.message : String(err), 'error');
      return false;
    } finally {
      if (btn) btn.disabled = false;
      if (redo) redo.disabled = false;
    }
  }

  async function approvePreview(panel, card) {
    if (!preview || !preview.preview_id) {
      setStatus(panel, 'Gere a narração antes de aprovar.', 'error');
      return false;
    }
    const text = normalizeText(storyTextarea(card)?.value);
    const btn = panel.querySelector('[data-ng-approve]');
    btn.disabled = true;
    try {
      const response = await authFetch('/youtube/narration-lab/production-preview/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preview_id: preview.preview_id, expected_text: text })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(detailMessage(data, 'Não foi possível aprovar a narração.'));
      const value = {
        preview_id: data.preview_id,
        text_sha256: data.text_sha256,
        source_text_sha256: await sha256(text),
        narration_core_version: Number(data.narration_core_version || data.reuse_audio_from?.narration_core_version || 0),
        narration_core_namespace: String(data.narration_core_namespace || data.reuse_audio_from?.narration_core_namespace || ''),
        reuse_audio_from: data.reuse_audio_from,
        approved_at: new Date().toISOString()
      };
      saveApproved(value);
      panel.querySelector('[data-ng-continue]').disabled = false;
      setStatus(panel, 'Áudio aprovado. Agora, e somente agora, você pode iniciar imagens e render usando exatamente este MP3.', 'success');
      return true;
    } catch (err) {
      clearApproved();
      setStatus(panel, err && err.message ? err.message : String(err), 'error');
      return false;
    } finally {
      btn.disabled = false;
    }
  }

  async function continueWithApproved(panel, card) {
    const textarea = storyTextarea(card);
    const text = normalizeText(textarea && textarea.value);
    if (!approved || !isCurrentApproved(approved)) {
      clearApproved();
      return setStatus(panel, 'Aprove a narração antes de iniciar a produção do vídeo.', 'error');
    }
    const currentHash = await sha256(text);
    if (!approved.source_text_sha256 || currentHash !== approved.source_text_sha256) {
      clearApproved();
      panel.querySelector('[data-ng-continue]').disabled = true;
      return setStatus(panel, 'O texto foi alterado após a aprovação. Gere e aprove uma nova narração.', 'error');
    }
    const button = existingGenerateButton(card);
    if (!button) return setStatus(panel, 'Não encontrei o acionador interno da produção. Recarregue a página.', 'error');
    approvedLaunchArmed = true;
    approvedInjectionCount = 0;
    setStatus(panel, 'Iniciando imagens e render com o MP3 que você aprovou…', 'success');
    button.click();
    window.setTimeout(() => {
      if (approvedLaunchArmed && approvedInjectionCount === 0) {
        approvedLaunchArmed = false;
        setStatus(panel, 'O pedido de vídeo não recebeu o áudio aprovado. Produção bloqueada.', 'error');
      }
    }, 2500);
  }

  async function startSupervisedFlow(panel, card) {
    clearApproved();
    panel.querySelector('[data-ng-result]').hidden = true;
    await generatePreview(panel, card, '');
  }

  function mount() {
    const card = storyCard();
    if (!card || card === mountedCard || card.querySelector('[data-youtube-narration-gate]')) return;
    mountedCard = card;
    loadApproved();
    const textarea = storyTextarea(card);
    if (!textarea) return;

    const originalButton = existingGenerateButton(card);
    if (originalButton) {
      originalButton.dataset.ngOriginalGenerate = '1';
      originalButton.style.display = 'none';
    }

    const panel = document.createElement('div');
    panel.dataset.youtubeNarrationGate = '1';
    panel.className = 'mt-4 border-2 border-indigo-200 bg-indigo-50 rounded-xl p-4';
    panel.innerHTML = `
      <div class="font-bold text-indigo-900 mb-1">🎙️ Narração supervisionada — etapa obrigatória antes do vídeo</div>
      <div class="text-sm text-slate-600 mb-3">Ao clicar em “Gerar vídeo narrado”, o Codexia gera SOMENTE o áudio. Imagens e render ficam bloqueados até você ouvir e aprovar.</div>
      <div class="flex flex-wrap gap-2 mb-3">
        <button type="button" data-ng-start class="bg-green-600 text-white px-4 py-2 rounded-lg font-semibold">🎬 Gerar vídeo narrado</button>
      </div>
      <div data-ng-status class="text-sm mb-3"></div>
      <div data-ng-result hidden class="bg-white border border-indigo-200 rounded-lg p-3">
        <div class="font-semibold text-sm mb-2">Áudio gerado para análise</div>
        <audio data-ng-audio hidden controls preload="metadata" style="width:100%"></audio>
        <div class="text-xs font-bold mt-3 mb-1">Texto exato enviado ao TTS</div>
        <div data-ng-spoken class="text-xs bg-slate-50 border rounded p-2 max-h-32 overflow-auto whitespace-pre-wrap"></div>
        <label class="block text-sm font-semibold mt-3 mb-1">O que você não gostou no áudio? (opcional)</label>
        <textarea data-ng-feedback class="w-full border rounded-lg p-2 text-sm" rows="3" placeholder="Ex.: fale mais devagar, use mais emoção no fechamento, dê mais pausa entre as frases..."></textarea>
        <div class="flex flex-wrap gap-2 mt-3">
          <button type="button" data-ng-approve disabled class="bg-emerald-600 text-white px-4 py-2 rounded-lg font-semibold disabled:opacity-50">✅ Aprovar esta narração</button>
          <button type="button" data-ng-redo class="border border-slate-300 bg-white px-4 py-2 rounded-lg font-semibold">🔄 Refazer seguindo minha observação</button>
          <button type="button" data-ng-continue disabled class="bg-green-700 text-white px-4 py-2 rounded-lg font-semibold disabled:opacity-50">🎬 Agora gerar o vídeo com este áudio</button>
        </div>
      </div>
      <span class="hidden">Gerar primeiro o áudio da narração</span>`;

    const target = textarea.parentElement;
    target.insertAdjacentElement('afterend', panel);
    panel.querySelector('[data-ng-start]').addEventListener('click', () => startSupervisedFlow(panel, card));
    panel.querySelector('[data-ng-redo]').addEventListener('click', () => {
      const feedback = normalizeText(panel.querySelector('[data-ng-feedback]')?.value);
      generatePreview(panel, card, feedback);
    });
    panel.querySelector('[data-ng-approve]').addEventListener('click', () => approvePreview(panel, card));
    panel.querySelector('[data-ng-continue]').addEventListener('click', () => continueWithApproved(panel, card));
    textarea.addEventListener('input', async () => {
      if (!approved) return;
      if (!approved.source_text_sha256 || await sha256(textarea.value) !== approved.source_text_sha256) {
        clearApproved();
        panel.querySelector('[data-ng-continue]').disabled = true;
        setStatus(panel, 'Texto alterado: a aprovação foi invalidada. Gere uma nova narração.', 'error');
      }
    });
  }

  window.fetch = async function narrationGateFetch(input, init = {}) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const method = String(init.method || (input && input.method) || 'GET').toUpperCase();
    const isVideoRequest = method === 'POST' && /\/youtube\/generate_video(?:\?|$)/.test(url);
    if (isVideoRequest) {
      try {
        if (!approvedLaunchArmed || !approved || !isCurrentApproved(approved) || typeof init.body !== 'string') {
          throw new Error('A produção foi bloqueada: gere, ouça e aprove o áudio antes de criar o vídeo.');
        }
        const payload = JSON.parse(init.body);
        const text = normalizeText(payload.story_content || payload.script_text || '');
        if (!text || !approved.source_text_sha256 || await sha256(text) !== approved.source_text_sha256) {
          approvedLaunchArmed = false;
          clearApproved();
          throw new Error('O texto do vídeo não corresponde ao texto aprovado; produção bloqueada.');
        }
        payload.reuse_audio_from = approved.reuse_audio_from;
        payload.approved_narration_preview_id = approved.preview_id;
        payload.approved_narration_text_sha256 = approved.text_sha256;
        payload.approved_narration_required = true;
        payload.narration_core_version = CORE_VERSION;
        payload.narration_core_namespace = CORE_NAMESPACE;
        approvedInjectionCount += 1;
        approvedLaunchArmed = false;
        init = { ...init, body: JSON.stringify(payload) };
      } catch (err) {
        approvedLaunchArmed = false;
        console.error('Codexia bloqueou produção sem áudio aprovado:', err);
        return Promise.reject(err);
      }
    }
    return originalFetch(input, init);
  };

  new MutationObserver(mount).observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener('DOMContentLoaded', mount);
  window.setInterval(mount, 1500);
})();