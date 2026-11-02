"""Small browser inbox for assistant-generated music/video production briefs.

This is an isolated opt-in UI. Mount in the FastAPI app:
    from assistant_bridge.ui import router as assistant_ui_router
    app.include_router(assistant_ui_router)

For production use, replace the bearer-token browser prompt with the app's
authenticated user session and server-side access checks.
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

router = APIRouter()

HTML = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codexia | Central de Produção</title>
<style>
:root{font-family:system-ui,Arial,sans-serif;color:#e9e9fb;background:#11142b}
*{box-sizing:border-box}body{margin:0}header{background:#302b84;padding:20px 5vw}
main{max-width:900px;margin:24px auto;padding:0 16px}
h1{margin:0;font-size:1.5rem}p{line-height:1.55;color:#bcbfda}
section{background:#1b2040;border:1px solid #383d69;border-radius:16px;padding:20px;margin:18px 0}
label{display:block;font-weight:600;margin:12px 0 5px}
input,textarea,select{width:100%;background:#10152e;color:white;border:1px solid #5d638a;border-radius:9px;padding:11px;font:inherit}
textarea{min-height:110px}button{background:#6355ed;border:0;border-radius:9px;padding:12px 16px;color:white;font-weight:700;cursor:pointer;margin-top:12px}
button:disabled{opacity:.5}#feedback{white-space:pre-wrap;color:#a8e9c2}
.small{font-size:.9rem}.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:550px){.row{grid-template-columns:1fr}}
</style></head><body><header><h1>Codexia · Central de Produção</h1></header>
<main><p>Receba um briefing preparado com o assistente, revise e aprove antes de iniciar qualquer geração.</p>
<section><h2>Conexão</h2><label>Token da integração (somente nesta sessão)</label>
<input id="token" type="password" autocomplete="off" placeholder="Token configurado no servidor">
<p class="small">Não compartilhe este token em conversas ou capturas. Em produção, substitua por login do Codexia.</p></section>
<section><h2>Novo projeto</h2>
<label>Título</label><input id="title" placeholder="Título da música">
<label>Estilo musical</label><input id="style" placeholder="Piseiro brasileiro, sanfona, voz masculina">
<label>Letra</label><textarea id="lyrics" placeholder="Cole aqui a letra aprovada"></textarea>
<label>Direção do videoclipe</label><textarea id="direction" placeholder="Descreva cenas, fontes de fotografias, créditos e avisos"></textarea>
<div class="row"><div><label>Formato</label><select id="ratio"><option>16:9</option><option>9:16</option><option>1:1</option></select></div>
<div><label>Voz</label><select id="voice"><option value="automatic">Automática</option><option value="male">Masculina</option><option value="female">Feminina</option></select></div></div>
<button id="submit">Salvar projeto para revisão</button>
<p id="feedback" role="status"></p></section>
<section><h2>Consultar projeto</h2><label>ID do projeto</label><input id="projectId" placeholder="UUID retornado após salvar">
<button id="load">Consultar status</button><button id="approve">Aprovar briefing</button>
<button id="cancel">Cancelar</button><pre id="project" style="white-space:pre-wrap;overflow-wrap:anywhere"></pre>
<p class="small">Aprovar o briefing não gera música nem publica vídeos. A execução será adicionada após integração com a fila existente.</p>
</section></main>
<script>
const el=id=>document.getElementById(id);
const endpoint='/api/assistant-bridge/projects';
async function request(path,method='GET',body){
 const token=el('token').value.trim();
 if(!token)throw Error('Informe o token de integração');
 const response=await fetch(path,{method,headers:{'Authorization':'Bearer '+token,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
 const result=await response.json();
 if(!response.ok)throw Error(result.detail||'Erro '+response.status);
 return result;
}
el('submit').onclick=async()=>{
 el('feedback').textContent='Enviando...';
 try{const payload={title:el('title').value,style:el('style').value,lyrics:el('lyrics').value,
 clip_direction:el('direction').value,aspect_ratio:el('ratio').value,voice:el('voice').value};
 const result=await request(endpoint,'POST',payload);
 el('projectId').value=result.id;el('feedback').textContent='Projeto salvo: '+result.id+' (aguardando revisão)';
 }catch(e){el('feedback').textContent=e.message}
};
async function act(suffix='',method='GET'){
 try{const id=el('projectId').value.trim();if(!id)throw Error('Informe o ID do projeto');
 const result=await request(endpoint+'/'+encodeURIComponent(id)+suffix,method);
 el('project').textContent=JSON.stringify(result,null,2);
 }catch(e){el('project').textContent='Erro: '+e.message}
}
el('load').onclick=()=>act();
el('approve').onclick=()=>act('/approve','POST');
el('cancel').onclick=()=>act('/cancel','POST');
</script></body></html>"""

@router.get("/assistant-production", response_class=HTMLResponse, include_in_schema=False)
def assistant_production_page() -> str:
    raise HTTPException(503, 'Shared-token prototype disabled pending multi-user UI')
