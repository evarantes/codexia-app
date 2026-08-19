(function () {
  'use strict';

  const DEFAULT_USD_BRL = 5.25;
  const IMAGE_UNIT_USD = 0.05;
  const IMAGES_PER_MINUTE = 8;
  const REGEN_RATE = 0.10;
  const FIXED_USD = 0.10;
  let lastCostSummary = null;
  let lastTaskId = null;
  let budgetBypassUntil = 0;
  let renderQueued = false;

  function number(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function brl(value) {
    return Number(value || 0).toLocaleString('pt-BR', { style: 'currency', currency: 'BRL' });
  }

  function usd(value) {
    return Number(value || 0).toLocaleString('en-US', { style: 'currency', currency: 'USD' });
  }

  function parseDurationMinutes() {
    const labels = Array.from(document.querySelectorAll('label'));
    const label = labels.find((el) => (el.textContent || '').trim().includes('Duração prevista do vídeo'));
    if (!label) return 2;
    const box = label.parentElement || label;
    const input = box.querySelector('input');
    const text = ((input && input.value) || box.textContent || '').toLowerCase();
    let seconds = 0;
    const h = text.match(/(\d+)\s*h/);
    const m = text.match(/(\d+)\s*min/);
    const s = text.match(/(\d+)\s*s/);
    if (h) seconds += Number(h[1]) * 3600;
    if (m) seconds += Number(m[1]) * 60;
    if (s) seconds += Number(s[1]);
    if (!seconds) {
      const clock = text.match(/\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b/);
      if (clock) {
        if (clock[3]) seconds = Number(clock[1]) * 3600 + Number(clock[2]) * 60 + Number(clock[3]);
        else seconds = Number(clock[1]) * 60 + Number(clock[2]);
      }
    }
    return Math.max(0.25, seconds ? seconds / 60 : 2);
  }

  function localEstimate(minutes) {
    const baseImages = Math.max(1, Math.ceil(minutes * IMAGES_PER_MINUTE));
    const regens = Math.max(0, Math.ceil(baseImages * REGEN_RATE));
    const endcards = 1;
    const totalUsd = FIXED_USD + (baseImages + regens + endcards) * IMAGE_UNIT_USD;
    const rate = number(localStorage.getItem('codexiaUsdBrlRate'), DEFAULT_USD_BRL);
    const projected10 = FIXED_USD + (((Math.max(0, totalUsd - FIXED_USD)) / minutes) * 10);
    return { minutes, baseImages, regens, endcards, totalUsd, rate, totalBrl: totalUsd * rate, projected10Brl: projected10 * rate };
  }

  function findAnchor() {
    const labels = Array.from(document.querySelectorAll('label'));
    const durationLabel = labels.find((el) => (el.textContent || '').trim().includes('Duração prevista do vídeo'));
    if (!durationLabel) return null;
    const box = durationLabel.parentElement || durationLabel;
    return box.parentElement || box;
  }

  function ensurePanel() {
    let panel = document.getElementById('codexia-video-cost-panel');
    if (panel) return panel;
    const anchor = findAnchor();
    if (!anchor) return null;
    panel = document.createElement('div');
    panel.id = 'codexia-video-cost-panel';
    panel.className = 'mt-3 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-gray-800';
    panel.innerHTML = '<div class="font-bold text-emerald-900">Custos desta produção</div><div id="codexia-video-cost-body" class="mt-2"></div>';
    anchor.insertAdjacentElement('afterend', panel);
    return panel;
  }

  function render() {
    const panel = ensurePanel();
    if (!panel) return;
    const body = document.getElementById('codexia-video-cost-body');
    if (!body) return;
    const est = localEstimate(parseDurationMinutes());
    const budget = Math.max(0, number(localStorage.getItem('codexiaVideoBudgetBrl'), 30));
    const summary = lastCostSummary;
    const model = summary && summary.model ? summary.model : 'gpt-image-2';
    const provider = summary && summary.provider ? summary.provider : 'OpenAI';
    let html = '';
    html += '<div class="grid grid-cols-1 md:grid-cols-3 gap-2">';
    html += `<div><span class="text-gray-500">Antes de gerar</span><br><strong>${brl(est.totalBrl)}</strong> <span class="text-xs text-gray-500">aprox.</span></div>`;
    html += `<div><span class="text-gray-500">Projeção 10 min</span><br><strong>${brl(est.projected10Brl)}</strong> <span class="text-xs text-gray-500">aprox.</span></div>`;
    html += `<div><span class="text-gray-500">Imagem</span><br><strong>${provider} • ${model}</strong></div>`;
    html += '</div>';
    html += `<div class="mt-2 text-xs text-gray-600">Base: ~${est.baseImages} imagens + até ${est.regens} regenerações previstas • cotação de referência ${brl(est.rate)}/US$.</div>`;
    html += '<div class="mt-2 flex flex-wrap items-center gap-2">';
    html += '<label class="text-xs font-semibold">Orçamento máximo do vídeo (R$)</label>';
    html += `<input id="codexia-video-budget" type="number" min="0" step="1" value="${budget}" class="w-24 border rounded px-2 py-1 bg-white">`;
    html += '</div>';
    if (summary) {
      const sourceLabels = {
        provider_measured: 'medido pelo provedor',
        tracked_calls_with_estimated_unit_cost: 'chamadas rastreadas + custo unitário estimado',
        pre_generation_estimate: 'estimativa (não houve chamadas rastreadas)'
      };
      html += '<div class="mt-3 pt-3 border-t border-emerald-200">';
      html += '<div class="font-bold text-emerald-900">Depois de gerar</div>';
      html += '<div class="grid grid-cols-1 md:grid-cols-4 gap-2 mt-2">';
      html += `<div><span class="text-gray-500">Custo rastreado</span><br><strong>${brl(summary.tracked_total_brl)}</strong> <span class="text-xs text-gray-500">(${usd(summary.tracked_total_usd)})</span></div>`;
      html += `<div><span class="text-gray-500">Por minuto</span><br><strong>${brl(summary.cost_per_minute_brl)}</strong></div>`;
      html += `<div><span class="text-gray-500">Imagens chamadas</span><br><strong>${Number(summary.image_operation_count || 0)}</strong></div>`;
      html += `<div><span class="text-gray-500">Projeção 10 min</span><br><strong>${brl(summary.projected_10_min_brl)}</strong></div>`;
      html += '</div>';
      html += `<div class="mt-2 text-xs text-gray-600">Fonte: ${sourceLabels[summary.cost_source] || summary.cost_source || 'rastreamento Codexia'}. ${summary.note || ''}</div>`;
      html += '</div>';
    }
    if (body.innerHTML !== html) {
      body.innerHTML = html;
    }
    const budgetInput = document.getElementById('codexia-video-budget');
    if (budgetInput) {
      budgetInput.onchange = function () {
        localStorage.setItem('codexiaVideoBudgetBrl', String(Math.max(0, number(this.value, 30))));
      };
    }
  }

  function scheduleRender() {
    if (renderQueued) return;
    renderQueued = true;
    window.requestAnimationFrame(function () {
      renderQueued = false;
      render();
    });
  }

  function acceptTaskData(data, taskId) {
    if (!data || typeof data !== 'object') return;
    if (taskId) lastTaskId = String(taskId);
    if (data.cost_summary && typeof data.cost_summary === 'object') {
      lastCostSummary = data.cost_summary;
      if (data.cost_summary.brl_rate) {
        localStorage.setItem('codexiaUsdBrlRate', String(data.cost_summary.brl_rate));
      }
      scheduleRender();
    }
  }

  async function pollKnownTask() {
    const taskId = localStorage.getItem('ytStoryTaskId') || lastTaskId;
    if (!taskId) return;
    const token = localStorage.getItem('access_token');
    try {
      const res = await window.fetch(`/youtube/task/${encodeURIComponent(taskId)}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {}
      });
      if (!res.ok) return;
      const data = await res.json();
      acceptTaskData(data, taskId);
    } catch (_) {}
  }

  const nativeFetch = window.fetch.bind(window);
  window.fetch = async function (...args) {
    const response = await nativeFetch(...args);
    try {
      const rawUrl = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
      const match = String(rawUrl).match(/\/youtube\/task\/([^/?#]+)/);
      if (match && !String(rawUrl).includes('/watch') && !String(rawUrl).includes('/media')) {
        const clone = response.clone();
        clone.json().then((data) => acceptTaskData(data, decodeURIComponent(match[1]))).catch(() => {});
      }
    } catch (_) {}
    return response;
  };

  document.addEventListener('click', function (event) {
    const button = event.target && event.target.closest ? event.target.closest('button') : null;
    if (!button) return;
    const text = (button.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
    if (!text.includes('gerar vídeo narrado')) return;
    if (Date.now() < budgetBypassUntil) return;
    const est = localEstimate(parseDurationMinutes());
    const budget = Math.max(0, number(localStorage.getItem('codexiaVideoBudgetBrl'), 30));
    if (budget > 0 && est.totalBrl > budget) {
      event.preventDefault();
      event.stopPropagation();
      if (event.stopImmediatePropagation) event.stopImmediatePropagation();
      const ok = window.confirm(
        `Estimativa desta produção: ${brl(est.totalBrl)}.\n` +
        `Seu orçamento máximo está em ${brl(budget)}.\n\n` +
        'A duração é uma referência editorial e o custo pode variar com regenerações. Deseja continuar mesmo assim?'
      );
      if (ok) {
        budgetBypassUntil = Date.now() + 2000;
        setTimeout(() => button.click(), 0);
      }
    }
  }, true);

  const observer = new MutationObserver(function (mutations) {
    const meaningful = mutations.some((mutation) => {
      if (mutation.type === 'characterData') return true;
      return Array.from(mutation.addedNodes || []).some((node) => {
        return !(node.nodeType === 1 && node.id === 'codexia-video-cost-panel');
      });
    });
    if (meaningful) scheduleRender();
  });

  function boot() {
    render();
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    setInterval(function () { scheduleRender(); pollKnownTask(); }, 5000);
    pollKnownTask();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
