(() => {
  const token = localStorage.getItem('access_token');
  if (!token) return;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const esc = v => String(v || '').replace(/[&<>]/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));
  let project = null;

  async function api(url, opt = {}) {
    const headers = { ...(opt.headers || {}), Authorization: `Bearer ${token}` };
    if (opt.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
    const r = await fetch(url, { ...opt, headers, cache: 'no-store' });
    let d = {}; try { d = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error(d.detail || d.message || `HTTP ${r.status}`);
    return d;
  }

  async function saveProject(extra = {}) {
    const payload = {
      theme: document.getElementById('theme')?.value || project?.theme || '',
      content_type: document.getElementById('contentType')?.value || project?.content_type || 'story',
      duration_minutes: Number(document.getElementById('duration')?.value || project?.duration_minutes || 10),
      budget_brl: Number(document.getElementById('videoBudget')?.value || project?.budget_brl || 85),
      ...(typeof currentPlan !== 'undefined' && currentPlan ? { plan: currentPlan } : {}),
      ...extra,
    };
    const d = await api('/youtube/cinematic/project/active', { method: 'PUT', body: JSON.stringify(payload) });
    project = d.project || project;
    return project;
  }

  function sceneHolder(index) {
    const btn = document.querySelector(`.pilot[data-i="${index}"]`);
    const card = btn?.closest('.scene-card');
    if (!card) return null;
    let holder = card.querySelector('.pilot-state');
    if (!holder) {
      holder = document.createElement('div');
      holder.className = 'pilot-state notice';
      holder.style.marginTop = '10px';
      card.appendChild(holder);
    }
    return holder;
  }

  function pilotSuccessHtml(index, outputUrl, approved = false, correctionNotes = '') {
    const status = approved ? '✅ Clipe aprovado e armazenado.' : '✅ Piloto concluído e armazenado.';
    const note = esc(correctionNotes || '');
    return `${status}<video controls playsinline preload="metadata" style="width:100%;margin-top:10px;border-radius:12px;background:#000" src="${esc(outputUrl)}"></video><div style="margin-top:10px"><label style="display:block;font-weight:700;margin-bottom:5px">Se precisar refazer, diga exatamente o que deve ser corrigido:</label><textarea class="pilot-correction" data-i="${index}" rows="4" style="width:100%;box-sizing:border-box;border:1px solid #d7dbea;border-radius:10px;padding:10px;font:inherit" placeholder="Ex.: não mostrar Davi nesta cena; manter apenas o vale, os dois exércitos e Golias; preservar a iluminação e a câmera aérea.">${note}</textarea><small style="display:block;margin-top:5px;opacity:.75">Estas observações ficam salvas no projeto e entram automaticamente no prompt da próxima geração.</small></div><div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">${approved ? '' : `<button class="btn btn-primary approve-pilot" data-i="${index}">Aprovar clipe</button>`}<button class="btn btn-ghost reject-pilot" data-i="${index}">Descartar e gerar outro</button><a class="btn btn-ghost" href="${esc(outputUrl)}" target="_blank" rel="noopener" style="text-decoration:none">Abrir vídeo</a></div>`;
  }

  function sceneCharacterRules(scene) {
    const all = Array.isArray(currentPlan?.character_bible) ? currentPlan.character_bible : [];
    const sceneText = [scene.narration, scene.visual_prompt, scene.motion_prompt]
      .filter(Boolean)
      .join(' ')
      .toLocaleLowerCase('pt-BR');
    const allowed = all.filter(c => {
      const name = String(c?.name || '').trim();
      return name && sceneText.includes(name.toLocaleLowerCase('pt-BR'));
    });
    const notRequested = all.filter(c => !allowed.includes(c)).map(c => String(c?.name || '').trim()).filter(Boolean);
    const continuity = allowed
      .map(c => `${c.name || ''}: ${c.fixed_visual_description || ''}; roupa: ${c.wardrobe || ''}; idade: ${c.age || ''}`)
      .filter(Boolean)
      .join(' | ');
    return { continuity, notRequested };
  }

  function combinedPrompt(scene) {
    const { continuity, notRequested } = sceneCharacterRules(scene);
    const state = project?.scenes?.[String(scene.index)] || {};
    const correction = String(state.correction_notes || '').trim();
    return [
      `CONTEXTO BÍBLICO/HISTÓRICO: ${currentPlan?.theme || document.getElementById('theme')?.value || ''}. Ambientação coerente com o antigo Levante/Israel bíblico; evitar paisagem europeia moderna, arquitetura moderna, roupas modernas e elementos anacrônicos.`,
      `NARRATIVA DESTA CENA: ${scene.narration || ''}`,
      `VISUAL DA CENA: ${scene.visual_prompt || ''}`,
      `AÇÃO/CÂMERA: ${scene.motion_prompt || ''}`,
      continuity ? `CONTINUIDADE APENAS DOS PERSONAGENS PEDIDOS NESTA CENA: ${continuity}` : 'CONTINUIDADE: esta cena não pede nenhum personagem nomeado; não invente protagonista ou personagem reconhecível.',
      notRequested.length ? `REGRA AUTOMÁTICA DE CONTINUIDADE: não introduzir como personagem reconhecível nenhum personagem do elenco que não esteja descrito nesta cena. Por padrão, NÃO incluir: ${notRequested.join(', ')}. Figurantes genéricos coerentes com a cena são permitidos. Uma correção manual abaixo, quando existir, tem prioridade sobre esta regra.` : '',
      correction ? `CORREÇÕES OBRIGATÓRIAS DA ÚLTIMA REPROVAÇÃO — PRIORIDADE MÁXIMA: ${correction}. Cumpra literalmente inclusive instruções negativas como “não mostrar”, “não incluir” e “não aparecer”; o elemento proibido não deve surgir nem ao fundo, em silhueta ou como figurante.` : '',
      'ESTILO: cinematográfico realista, composição épica, iluminação natural dramática, continuidade visual, sem texto na imagem.'
    ].filter(Boolean).join('\n');
  }

  async function saveScene(index, state) {
    const d = await api(`/youtube/cinematic/project/active/scenes/${index}`, { method: 'PUT', body: JSON.stringify(state) });
    project = d.project || project;
  }

  async function monitorScene(index, state) {
    if (!state?.job_id) return;
    const holder = sceneHolder(index);
    if (!holder) return;
    for (let i = 0; i < 90; i++) {
      holder.className = 'pilot-state notice';
      holder.innerHTML = `⏳ Gerando/recuperando piloto no ${esc((state.provider || 'veo').toUpperCase())}… <small>Job salvo; pode atualizar a página.</small>`;
      try {
        const d = await api('/youtube/cinematic/project/scene/poll', { method: 'POST', body: JSON.stringify({ scene_index: index, provider: state.provider || 'veo', job_id: state.job_id, status_url: state.status_url || null, response_url: state.response_url || null, model_path: state.model_path || null }) });
        if (d.status === 'SUCCEEDED' && d.output_url) {
          await saveScene(index, { status: 'SUCCEEDED', output_url: d.output_url, filename: d.filename || '' });
          const saved = project?.scenes?.[String(index)] || {};
          holder.className = 'pilot-state notice ok';
          holder.innerHTML = pilotSuccessHtml(index, d.output_url, false, saved.correction_notes || '');
          return;
        }
        if (d.status === 'FAILED') {
          await saveScene(index, { status: 'FAILED', error: d.error || 'Falha no provedor' });
          holder.className = 'pilot-state notice warn'; holder.textContent = `❌ Piloto falhou: ${d.error || 'erro desconhecido'}`; return;
        }
      } catch (e) {
        holder.className = 'pilot-state notice warn'; holder.textContent = `Conexão oscilou. O Job continua salvo. Tentaremos novamente: ${e.message}`;
      }
      await sleep(10000);
    }
  }

  async function persistentPilot(index) {
    const s = (currentPlan?.scenes || []).find(x => Number(x.index) === Number(index));
    if (!s) return;
    const existing = project?.scenes?.[String(index)];
    if (existing?.job_id && !['FAILED','REJECTED'].includes(String(existing.status || '').toUpperCase())) { void monitorScene(index, existing); return; }
    const available = statusData?.providers || {};
    let provider = s.recommended_provider;
    if (provider === 'kling' && !available.kling?.configured) provider = available.veo?.configured ? 'veo' : 'runway';
    if (provider === 'runway' && !available.runway?.configured) provider = available.veo?.configured ? 'veo' : 'runway';
    if (provider === 'veo' && !available.veo?.configured) provider = available.runway?.configured ? 'runway' : 'veo';
    const seconds = Math.max(4, Math.min(8, Number(s.generative_video_seconds || 5)));
    const correction = String(existing?.correction_notes || '').trim();
    const correctionMessage = correction ? `\n\nCorreções salvas que serão aplicadas automaticamente:\n${correction}` : '';
    if (!confirm(`Gerar um clipe piloto de ${seconds}s em ${provider.toUpperCase()}? Esta ação pode consumir créditos. O Job será salvo automaticamente.${correctionMessage}`)) return;
    const prompt = combinedPrompt(s);
    const holder = sceneHolder(index); if (holder) holder.textContent = correction ? 'Enviando piloto com as correções salvas…' : 'Enviando piloto e salvando Job…';
    try {
      const d = await api('/youtube/cinematic/project/scene/submit', { method: 'POST', body: JSON.stringify({ scene_index:index, provider, prompt, duration_seconds:seconds, premium:s.tier === 'A', aspect_ratio:'16:9' }) });
      const state = { provider:d.provider || provider, requested_provider:provider, job_id:d.job_id, status:d.status || 'PENDING', prompt };
      await saveScene(index, state);
      void monitorScene(index, state);
    } catch (e) { if (holder) { holder.className='pilot-state notice warn'; holder.textContent = e.message; } else alert(e.message); }
  }

  async function restore() {
    try {
      const d = await api('/youtube/cinematic/project/active');
      project = d.project;
      if (!project?.plan) return;
      document.getElementById('theme').value = project.theme || project.plan.theme || '';
      document.getElementById('contentType').value = project.content_type || project.plan.content_type || 'story';
      document.getElementById('duration').value = String(project.duration_minutes || project.plan.duration_minutes || 10);
      document.getElementById('videoBudget').value = String(project.budget_brl || project.plan.budget_limit_brl || 85);
      currentPlan = project.plan;
      renderPlan({ plan: project.plan, director: project.director || { provider:'recuperado', model:'Claude', estimated_cost_brl:0 } });
      if (project.estimate && document.getElementById('estimateBox')) {
        const x = project.estimate; document.getElementById('estimateBox').innerHTML = `<div class="notice ok"><b>Estimativa salva: ${Number(x.total_brl || 0).toLocaleString('pt-BR',{style:'currency',currency:'BRL'})}</b></div>`;
      }
      const scenes = project.scenes || {};
      const lastVeo = localStorage.getItem('codexia_last_veo_job');
      if (lastVeo && Object.keys(scenes).length === 0) {
        const first = (project.plan.scenes || []).find(s => s.tier !== 'C');
        if (first) {
          await saveScene(first.index, { provider:'veo', requested_provider:first.recommended_provider || 'veo', job_id:lastVeo, status:'PENDING' });
          project = (await api('/youtube/cinematic/project/active')).project;
        }
      }
      Object.entries(project.scenes || {}).forEach(([idx, st]) => {
        if (st.output_url && st.status === 'SUCCEEDED') {
          const h = sceneHolder(Number(idx)); if (h) { h.className='pilot-state notice ok'; h.innerHTML=pilotSuccessHtml(Number(idx), st.output_url, Boolean(st.approved), st.correction_notes || ''); }
        } else if (st.status === 'REJECTED') {
          const h = sceneHolder(Number(idx)); if (h) { h.className='pilot-state notice warn'; h.innerHTML=`Piloto descartado e arquivo anterior preservado.<br><b>Correções salvas:</b> ${esc(st.correction_notes || 'sem observações')}<br><small>Toque em “Gerar clipe piloto”; estas correções entrarão automaticamente no novo prompt.</small>`; }
        } else if (st.job_id && st.status !== 'FAILED') void monitorScene(Number(idx), st);
      });
      const box = document.getElementById('directorAsyncStatus'); if (box) { box.style.display='block'; box.className='notice ok'; box.textContent='Projeto recuperado do servidor. Roteiro, cenas, correções e Jobs estão armazenados; atualizar a página não exige refazer a direção.'; }
    } catch (e) { console.warn('Cinematic project restore:', e); }
  }

  const originalRender = renderPlan;
  renderPlan = function(d) {
    originalRender(d);
    if (d?.plan) {
      currentPlan = d.plan;
      void saveProject({ plan:d.plan, director:d.director || {}, status:'directed' }).catch(() => {});
    }
  };

  const estimateBtn = document.getElementById('estimateBtn');
  if (estimateBtn) estimateBtn.onclick = async () => {
    if (!currentPlan) return;
    const scenes=currentPlan.scenes||[], motion=scenes.reduce((a,s)=>a+(+s.generative_video_seconds||0),0), premium=scenes.filter(s=>s.tier==='A').reduce((a,s)=>a+(+s.generative_video_seconds||0),0);
    try {
      const x=await api('/youtube/cinematic/estimate',{method:'POST',body:JSON.stringify({content_type:currentPlan.content_type,duration_minutes:currentPlan.duration_minutes,motion_seconds:motion,premium_motion_seconds:premium,estimated_images:scenes.length,voice:'elevenlabs'})});
      document.getElementById('estimateBox').innerHTML=`<div class="notice ok"><b>Estimativa: ${Number(x.total_brl||0).toLocaleString('pt-BR',{style:'currency',currency:'BRL'})}</b><br><small>Claude US$${x.components_usd.claude} · imagens US$${x.components_usd.images} · movimento US$${x.components_usd.motion} · voz US$${x.components_usd.voice} · reserva US$${x.components_usd.recovery_reserve}</small></div>`;
      await saveProject({ estimate:x });
    } catch(e) { alert(e.message); }
  };

  document.addEventListener('click', ev => {
    const p = ev.target.closest?.('.pilot');
    if (p) { ev.preventDefault(); ev.stopImmediatePropagation(); void persistentPilot(Number(p.dataset.i)); return; }
    const a = ev.target.closest?.('.approve-pilot');
    if (a) {
      ev.preventDefault(); ev.stopImmediatePropagation(); const i=Number(a.dataset.i);
      void saveScene(i,{approved:true,status:'SUCCEEDED'}).then(()=>{ const st=project?.scenes?.[String(i)]; const h=sceneHolder(i); if(h && st?.output_url){h.className='pilot-state notice ok';h.innerHTML=pilotSuccessHtml(i,st.output_url,true,st.correction_notes || '');} });
      return;
    }
    const r = ev.target.closest?.('.reject-pilot');
    if (r) {
      ev.preventDefault(); ev.stopImmediatePropagation(); const i=Number(r.dataset.i);
      const card = r.closest('.scene-card');
      const field = card?.querySelector(`.pilot-correction[data-i="${i}"]`);
      const notes = String(field?.value || '').trim();
      if (!notes) {
        alert('Antes de reprovar, escreva no campo o que precisa ser corrigido. Essas observações serão salvas e usadas automaticamente na próxima geração.');
        field?.focus();
        return;
      }
      if (!confirm('Descartar este piloto como versão ativa? O MP4 antigo continuará arquivado. As observações de correção serão salvas e aplicadas automaticamente ao próximo prompt. Uma nova geração terá nova cobrança do provedor.')) return;
      const st = project?.scenes?.[String(i)] || {};
      const history = Array.isArray(st.rejection_history) ? [...st.rejection_history] : [];
      history.push({
        rejected_at: new Date().toISOString(),
        correction_notes: notes,
        output_url: st.output_url || null,
        filename: st.filename || null,
        job_id: st.job_id || null,
        prompt: st.prompt || null,
      });
      void saveScene(i,{approved:false,status:'REJECTED',correction_notes:notes,rejection_history:history}).then(()=>{ const h=sceneHolder(i); if(h){h.className='pilot-state notice warn';h.innerHTML=`Piloto descartado. O arquivo anterior foi preservado.<br><b>Correções salvas:</b> ${esc(notes)}<br><small>Agora toque em “Gerar clipe piloto”. O Codexia incluirá estas correções automaticamente no novo prompt.</small>`;} });
    }
  }, true);

  setTimeout(() => { void restore(); }, 500);
})();
