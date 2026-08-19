(function () {
    'use strict';

    const state = {
        mode: 'balanced',
        maxCostBrl: 0,
        estimate: null,
        overrideApproved: false,
        timer: null,
    };

    function headers(extra) {
        const token = localStorage.getItem('access_token');
        return Object.assign({}, extra || {}, token ? { Authorization: `Bearer ${token}` } : {});
    }

    async function api(url, options) {
        const opts = Object.assign({}, options || {});
        opts.headers = headers(opts.headers);
        const res = await fetch(url, opts);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const detail = data && data.detail;
            const msg = typeof detail === 'string' ? detail : (detail && detail.message) || data.message || `HTTP ${res.status}`;
            throw new Error(msg);
        }
        return data;
    }

    function brl(value) {
        return `R$ ${Number(value || 0).toFixed(2).replace('.', ',')}`;
    }

    function durationFor(app) {
        try {
            const predicted = Math.max(0, Number(app && app.ytStoryPredictedDurationSeconds || 0)) / 60;
            if (predicted > 0) return predicted;
            const max = Math.max(1, Math.min(60, Number(app && (app.ytStoryDurationMax || app.ytStoryDurationMin) || 2)));
            return max;
        } catch (_) {
            return 2;
        }
    }

    async function estimate(app) {
        const data = await api('/video-costs/estimate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                duration_minutes: durationFor(app),
                mode: state.mode,
                regeneration_rate: 0.10,
                projection_minutes: [2, 5, 10, 15],
            }),
        });
        state.estimate = data;
        renderEstimate();
        return data;
    }

    function panel() {
        return document.getElementById('codexia-video-cost-panel');
    }

    function renderEstimate() {
        const root = panel();
        if (!root) return;
        const summary = root.querySelector('[data-cost-summary]');
        const projections = root.querySelector('[data-cost-projections]');
        if (!state.estimate) {
            if (summary) summary.textContent = 'Clique em Atualizar para calcular.';
            if (projections) projections.innerHTML = '';
            return;
        }
        if (summary) {
            summary.innerHTML = `<strong>${brl(state.estimate.total_cost_brl)}</strong> ` +
                `<span style="color:#64748b">(US$ ${Number(state.estimate.total_cost_usd || 0).toFixed(2)})</span><br>` +
                `<span style="font-size:12px">${state.estimate.estimated_images || 0} imagens + ${state.estimate.estimated_regenerations || 0} reserva(s) • ${state.estimate.image_quality_label || ''}</span>`;
        }
        if (projections) {
            projections.innerHTML = (state.estimate.projections || []).map(p =>
                `<div style="background:white;border:1px solid #c7d2fe;border-radius:6px;padding:6px;text-align:center"><b>${p.duration_minutes} min</b><br>~ ${brl(p.total_cost_brl)}</div>`
            ).join('');
        }
    }

    async function loadHistory() {
        const root = panel();
        if (!root) return;
        const history = root.querySelector('[data-cost-history]');
        if (history) history.textContent = 'Carregando...';
        try {
            const data = await api('/video-costs/history?limit=20');
            const items = Array.isArray(data.items) ? data.items.slice(0, 5) : [];
            const rows = items.map(item => {
                const actual = item.actual_cost_available ? brl(item.actual_cost_brl) : 'indisponível';
                return `<tr><td style="padding:6px;border-top:1px solid #e5e7eb">${String(item.title || `Vídeo #${item.unified_video_id}`).slice(0, 55)}</td>` +
                    `<td style="padding:6px;border-top:1px solid #e5e7eb;text-align:center">${Number(item.duration_minutes || 0).toFixed(1)} min</td>` +
                    `<td style="padding:6px;border-top:1px solid #e5e7eb;text-align:center">${brl(item.estimated_cost_brl)}</td>` +
                    `<td style="padding:6px;border-top:1px solid #e5e7eb;text-align:center">${actual}</td></tr>`;
            }).join('');
            const avg = Number(data.average_cost_per_minute_brl || 0);
            if (history) history.innerHTML = `${data.measurable_count ? `<div style="margin-bottom:6px"><b>Média realizada:</b> ${brl(avg)}/min</div>` : ''}` +
                (rows ? `<div style="overflow:auto"><table style="width:100%;font-size:12px;background:white"><thead><tr><th style="padding:6px;text-align:left">Produção</th><th>Duração</th><th>Previsto</th><th>Realizado*</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<span style="color:#64748b">Ainda não há custos realizados mensuráveis.</span>') +
                `<div style="font-size:11px;color:#64748b;margin-top:4px">* Calculado pelas operações registradas; pode variar da fatura oficial do provedor.</div>`;
        } catch (e) {
            if (history) history.textContent = `Falha ao carregar histórico: ${e.message || e}`;
        }
    }

    function appFromElement(input) {
        try {
            const appEl = document.getElementById('app');
            return appEl && appEl.__vue_app__ && appEl.__vue_app__._instance && appEl.__vue_app__._instance.proxy;
        } catch (_) {
            return null;
        }
    }

    function schedule(app) {
        if (state.timer) clearTimeout(state.timer);
        state.timer = setTimeout(() => estimate(app).catch(() => {}), 350);
    }

    function installPanel() {
        if (panel()) return true;
        const maxInput = document.querySelector('input[v-model="ytStoryDurationMax"]');
        if (!maxInput || !maxInput.parentElement || !maxInput.parentElement.parentElement) return false;
        const grid = maxInput.parentElement.parentElement;
        const root = document.createElement('div');
        root.id = 'codexia-video-cost-panel';
        root.style.cssText = 'grid-column:1/-1;background:#eef2ff;border:1px solid #c7d2fe;border-radius:10px;padding:14px;margin-top:8px';
        root.innerHTML = `
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap">
                <div><b>💰 Custo previsto da produção</b><div style="font-size:12px;color:#4338ca">Antes de gerar mídia paga. Estimativa, não fatura oficial da OpenAI.</div></div>
                <button type="button" data-cost-refresh style="padding:7px 12px;border:1px solid #a5b4fc;background:white;border-radius:6px">Atualizar estimativa</button>
            </div>
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-top:10px">
                <label style="font-size:12px;font-weight:600">Qualidade OpenAI<select data-cost-mode style="display:block;width:100%;padding:7px;border:1px solid #d1d5db;border-radius:6px;background:white;margin-top:4px"><option value="economy">Econômico — baixa</option><option value="balanced" selected>Equilibrado — média</option><option value="premium">Qualidade Máxima — alta</option></select></label>
                <label style="font-size:12px;font-weight:600">Não ultrapassar (R$) — opcional<input data-cost-limit type="number" min="0" step="0.50" placeholder="Ex.: 10,00" style="display:block;width:100%;padding:7px;border:1px solid #d1d5db;border-radius:6px;background:white;margin-top:4px"></label>
                <div data-cost-summary style="background:white;border:1px solid #e5e7eb;border-radius:6px;padding:8px;font-size:14px">Clique em Atualizar para calcular.</div>
            </div>
            <div data-cost-projections style="display:grid;grid-template-columns:repeat(4,minmax(80px,1fr));gap:6px;margin-top:10px;font-size:12px"></div>
            <div style="margin-top:10px"><button type="button" data-cost-history-button style="padding:6px 10px;border:1px solid #d1d5db;background:white;border-radius:6px">Ver custos recentes</button></div>
            <div data-cost-history style="margin-top:8px;font-size:12px"></div>`;
        grid.appendChild(root);
        const app = appFromElement(maxInput);
        root.querySelector('[data-cost-mode]').addEventListener('change', e => { state.mode = e.target.value || 'balanced'; schedule(app); });
        root.querySelector('[data-cost-limit]').addEventListener('input', e => { state.maxCostBrl = Math.max(0, Number(e.target.value || 0)); });
        root.querySelector('[data-cost-refresh]').addEventListener('click', () => estimate(app).catch(e => alert(`Falha ao estimar custo: ${e.message || e}`)));
        root.querySelector('[data-cost-history-button]').addEventListener('click', loadHistory);
        const minInput = document.querySelector('input[v-model="ytStoryDurationMin"]');
        if (minInput) minInput.addEventListener('change', () => schedule(app));
        maxInput.addEventListener('change', () => schedule(app));
        schedule(app);
        return true;
    }

    async function beforeGenerate(app) {
        state.overrideApproved = false;
        let current;
        try {
            current = await estimate(app);
        } catch (e) {
            alert(`Não foi possível calcular o custo previsto. A produção não foi iniciada.\n\n${e.message || e}`);
            return false;
        }
        const estimated = Number(current.total_cost_brl || 0);
        const limit = Number(state.maxCostBrl || 0);
        if (limit > 0 && estimated > limit) {
            const ok = window.confirm(`Aviso de custo\n\nEstimativa: ${brl(estimated)}\nSeu limite: ${brl(limit)}\n\nOK = autorizar esta produção mesmo acima do limite\nCancelar = voltar e ajustar`);
            if (!ok) return false;
            state.overrideApproved = true;
        }
        return true;
    }

    window.CodexiaVideoCost = {
        installPanel,
        beforeGenerate,
        refresh: estimate,
        mode: () => state.mode,
        maxCostBrl: () => state.maxCostBrl || null,
        overrideApproved: () => !!state.overrideApproved,
        lastEstimate: () => state.estimate,
    };

    let attempts = 0;
    const timer = setInterval(() => {
        attempts += 1;
        if (installPanel() || attempts > 120) clearInterval(timer);
    }, 500);
})();
