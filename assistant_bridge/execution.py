"""Authenticated handoff: approved brief -> existing Codexia music pipeline.

Requires the regular Codexia user session; bridge bearer token alone cannot
spend provider credits. No publishing or automatic clip generation.
"""
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from app.models import User
from app.routers.auth import get_current_user
from app.routers.music import (GenerateMusicRequest, GenerateClipRequest,
                               generate_music_from_lyrics, generate_music_clip_task)
from app.services.task_manager import get_task
from app.database import get_db
from sqlalchemy.orm import Session
from assistant_bridge.codexia_adapter import clip_payload
from app.services.suno_service import get_suno_api_key
from assistant_bridge.router import database, now, ProductionRequest
from assistant_bridge.ownership import is_project_owner
from assistant_bridge.codexia_adapter import music_payload

router = APIRouter(prefix="/api/assistant-production", tags=["assistant-production"])


@router.post("/projects", status_code=201)
def create_owned_project(request: ProductionRequest,
                         user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Create a private draft bound to the authenticated Codexia account."""
    project_id = str(uuid.uuid4())
    stamp = now()
    brief = request.model_dump()
    brief["_production"] = {"user_id": user.id}
    with database() as db:
        db.execute(
            "INSERT INTO assistant_projects VALUES (?, ?, ?, ?, ?)",
            (project_id, stamp, stamp, "awaiting_review", json.dumps(brief)),
        )
    return {"id": project_id, "status": "awaiting_review"}


@router.get("/projects")
def list_owned_projects(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """List only drafts and jobs owned by the authenticated account."""
    with database() as db:
        rows = db.execute(
            """SELECT id, status, updated_at, payload FROM assistant_projects
               WHERE json_type(payload, '$._production.user_id') = 'integer'
                 AND json_extract(payload, '$._production.user_id') = ?
               ORDER BY updated_at DESC LIMIT 500""", (user.id,)
        ).fetchall()
    projects = []
    for row in rows:
        brief = json.loads(row["payload"])
        if is_project_owner(brief, user.id):
            projects.append({
                "id": row["id"], "status": row["status"],
                "updated_at": row["updated_at"], "title": brief.get("title", ""),
            })
    return {"projects": projects}


@router.get("/projects/{project_id}")
def get_owned_project(project_id: str,
                      user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Return a private draft without exposing internal task ownership metadata."""
    with database() as db:
        row = db.execute(
            "SELECT status, updated_at, payload FROM assistant_projects WHERE id=?",
            (project_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    brief = json.loads(row["payload"])
    if not is_project_owner(brief, user.id):
        raise HTTPException(403, "Project belongs to another user")
    return {
        "id": project_id, "status": row["status"], "updated_at": row["updated_at"],
        "brief": {key: value for key, value in brief.items() if key != "_production"},
    }


@router.post("/projects/{project_id}/cancel")
def cancel_owned_project(project_id: str,
                         user: User = Depends(get_current_user)) -> dict[str, str]:
    """Cancel only before paid generation begins; never cancel another user's work."""
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT status, payload FROM assistant_projects WHERE id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Project not found")
        if not is_project_owner(json.loads(row["payload"]), user.id):
            raise HTTPException(403, "Project belongs to another user")
        if row["status"] not in ("awaiting_review", "approved_for_generation"):
            raise HTTPException(409, "Project cannot be cancelled after generation begins")
        db.execute(
            "UPDATE assistant_projects SET status='cancelled', updated_at=? WHERE id=?",
            (now(), project_id),
        )
    return {"id": project_id, "status": "cancelled"}


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def production_dashboard() -> str:
    """Login-enabled HTML shell; project APIs enforce owner-bound JWT authentication."""
    return """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codexia | Minhas produções</title>
<style>
:root{font-family:system-ui,sans-serif;color:#eef1ff;background:#101426}
*{box-sizing:border-box}body{margin:0}header{padding:22px;background:#222951}
main{max-width:1000px;margin:auto;padding:20px}h1{margin:0;font-size:1.5rem}
section{background:#1b2340;border:1px solid #3d4773;border-radius:16px;padding:18px;margin:16px 0}
input,textarea,select{width:100%;background:#111a32;color:white;border:1px solid #62719e;border-radius:8px;padding:10px;margin:6px 0 12px}
textarea{min-height:110px}button{background:#6659df;color:white;border:0;border-radius:8px;padding:10px 14px;margin:5px;cursor:pointer}
button:disabled{opacity:.5}.item{padding:12px;border-bottom:1px solid #394365}
small{color:#bac4e5}#feedback{white-space:pre-wrap}
</style></head><body><header><h1>Codexia · Minhas produções</h1></header>
<main><section><h2>Acesso à conta</h2><p>Entre no Codexia para acessar seus projetos.</p><label>E-mail<input id="email" type="email"></label><label>Senha<input id="password" type="password"></label><button id="login">Entrar</button><button id="logout">Sair</button><p id="authstatus" role="status"></p></section><section><h2>Novo projeto</h2><label>Título<input id="title"></label>
<label>Estilo<input id="style"></label><label>Letra<textarea id="lyrics"></textarea></label>
<label>Direção do clipe<textarea id="direction"></textarea></label>
<button id="create">Salvar para revisão</button><p id="feedback" role="status"></p></section>
<section><h2>Meus projetos</h2><button id="refresh">Atualizar</button>
<div id="projects"></div><small>A aprovação não consome créditos. A geração musical exige confirmação separada.</small></section></main>
<script>
const base='/api/assistant-production/projects';
let accessToken=null;
const el=id=>document.getElementById(id);
async function api(path,method='GET',body){
 if(!accessToken)throw Error('Entre na sua conta Codexia');
 const r=await fetch(path,{method,credentials:'omit',
 headers:{'Content-Type':'application/json','Authorization':'Bearer '+accessToken},body:body?JSON.stringify(body):undefined});
 const data=await r.json();if(!r.ok)throw Error(data.detail||('HTTP '+r.status));return data;
}
function node(tag,txt){const e=document.createElement(tag);e.textContent=txt;return e}
async function refresh(){
 const root=el('projects');root.replaceChildren(node('p','Carregando...'));
 try{const data=await api(base);root.replaceChildren();
 if(!data.projects.length)root.append(node('p','Nenhum projeto encontrado.'));
 for(const p of data.projects){
 const box=node('div','');box.className='item';
 box.append(node('strong',p.title||'Sem título'),node('p','Estado: '+p.status));
 const detail=node('button','Detalhes');
 detail.onclick=async()=>{try{const d=await api(base+'/'+encodeURIComponent(p.id));
 alert(JSON.stringify(d.brief,null,2))}catch(e){alert(e.message)}};
 box.append(detail);
 if(p.status==='awaiting_review'){
 const approve=node('button','Aprovar');
 approve.onclick=async()=>{try{await api(base+'/'+encodeURIComponent(p.id)+'/approve','POST');await refresh()}catch(e){alert(e.message)}};
 const cancel=node('button','Cancelar');
 cancel.onclick=async()=>{try{await api(base+'/'+encodeURIComponent(p.id)+'/cancel','POST');await refresh()}catch(e){alert(e.message)}};
 box.append(approve,cancel);
 }
 if(p.status==='approved_for_generation'){
 const generate=node('button','Gerar música');
 generate.onclick=async()=>{
 if(!confirm('Confirmar geração musical? Esta ação pode consumir créditos do Suno.'))return;
 try{await api(base+'/'+encodeURIComponent(p.id)+'/start-music?confirmed=true','POST');await refresh()}
 catch(e){alert(e.message)}
 };
 box.append(generate);
 }
 if(['music_starting','music_processing','music_ready','music_failed'].includes(p.status)){
 const status=node('button','Verificar música');
 status.onclick=async()=>{try{const result=await api(base+'/'+encodeURIComponent(p.id)+'/music-status');
 alert('Música: '+result.status);await refresh()}catch(e){alert(e.message)}};
 box.append(status);
 }
 if(p.status==='music_ready'){
 const clip=node('button','Gerar clipe');
 clip.onclick=async()=>{
 if(!confirm('Confirmar início da geração do clipe? Não haverá publicação automática.'))return;
 try{await api(base+'/'+encodeURIComponent(p.id)+'/start-clip','POST');await refresh()}
 catch(e){alert(e.message)}
 };
 box.append(clip);
 }
 if(['clip_starting','clip_processing','awaiting_clip_review','clip_failed'].includes(p.status)){
 const clipStatus=node('button','Verificar clipe');
 clipStatus.onclick=async()=>{try{const result=await api(base+'/'+encodeURIComponent(p.id)+'/clip-status');
 alert('Clipe: '+result.status);await refresh()}catch(e){alert(e.message)}};
 box.append(clipStatus);
 }
 root.append(box);
 }
 }catch(e){root.replaceChildren(node('p','Não foi possível carregar: '+e.message))}
}
el('create').onclick=async()=>{el('feedback').textContent='Salvando...';
 try{await api(base,'POST',{title:el('title').value,style:el('style').value,
 lyrics:el('lyrics').value,clip_direction:el('direction').value});
 el('feedback').textContent='Projeto salvo para revisão.';await refresh()}
 catch(e){el('feedback').textContent=e.message}};
el('login').onclick=async()=>{
 el('authstatus').textContent='Autenticando...';
 try{
 const form=new URLSearchParams({username:el('email').value,password:el('password').value});
 const response=await fetch('/token',{method:'POST',credentials:'omit',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:form});
 const data=await response.json();
 if(!response.ok||!data.access_token)throw Error(data.detail||'Login recusado');
 accessToken=data.access_token;
 el('password').value='';
 el('authstatus').textContent='Conectado ao Codexia.';
 await refresh();
 }catch(error){accessToken=null;el('authstatus').textContent='Erro: '+error.message}
};
el('logout').onclick=()=>{
 accessToken=null;
 el('projects').replaceChildren();
 el('authstatus').textContent='Sessão local encerrada.';
};
el('refresh').onclick=refresh;
</script></body></html>"""


@router.post("/projects/{project_id}/approve")
def approve_owned_project(project_id: str,
                          user: User = Depends(get_current_user)) -> dict[str, str]:
    """Only the owner may approve their own draft; no paid work starts here."""
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT status, payload FROM assistant_projects WHERE id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Project not found")
        if not is_project_owner(json.loads(row["payload"]), user.id):
            raise HTTPException(403, "Project belongs to another user")
        if row["status"] != "awaiting_review":
            raise HTTPException(409, "Project is not awaiting review")
        db.execute(
            "UPDATE assistant_projects SET status='approved_for_generation', updated_at=? WHERE id=?",
            (now(), project_id),
        )
    return {"id": project_id, "status": "approved_for_generation"}


@router.post("/projects/{project_id}/start-music")
def start_music(project_id: str, user: User = Depends(get_current_user), confirmed: bool = False) -> dict[str, Any]:
    """One-shot, user-confirmed start. Fail closed without Suno configuration."""
    if not confirmed:
        raise HTTPException(428, "Confirm music generation explicitly")
    if not get_suno_api_key():
        raise HTTPException(503, "Suno API Key required for vocal production")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT status, payload FROM assistant_projects WHERE id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Project not found")
        if not is_project_owner(json.loads(row["payload"]), user.id):
            raise HTTPException(403, "Project must be assigned to the current user before generation")
        if row["status"] != "approved_for_generation":
            raise HTTPException(409, "Project is not approved or already started")
        db.execute(
            "UPDATE assistant_projects SET status='music_starting', updated_at=? WHERE id=?",
            (now(), project_id),
        )
        db.commit()
    try:
        brief = json.loads(row["payload"])
        request = GenerateMusicRequest(**music_payload(brief))
        result = generate_music_from_lyrics(request, user=user)
    except Exception:
        with database() as db:
            db.execute(
                "UPDATE assistant_projects SET status='music_start_failed', updated_at=? "
                "WHERE id=? AND status='music_starting'", (now(), project_id)
            )
        raise
    with database() as db:
        db.execute(
            "UPDATE assistant_projects SET status='music_processing', updated_at=?, payload=? "
            "WHERE id=? AND status='music_starting'",
            (now(), json.dumps({**brief, "_production": {
                "task_id": result.get("task_id"), "user_id": user.id
            }}), project_id),
        )
    return {"id": project_id, "status": "music_processing", "task_id": result.get("task_id"),
            "auto_upload_youtube": False}


@router.get("/projects/{project_id}/music-status")
def music_status(project_id: str, user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Read owned music job and synchronize its state without starting new work."""
    with database() as db:
        row = db.execute(
            "SELECT status, payload FROM assistant_projects WHERE id=?", (project_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    brief = json.loads(row["payload"])
    production = brief.get("_production") or {}
    if not is_project_owner(brief, user.id):
        raise HTTPException(403, "Project is not owned by current user")
    task_id = production.get("task_id")
    if not task_id:
        raise HTTPException(409, "No music task attached")
    # Verify the persisted task owner, not only the bridge payload.
    from app.database import SessionLocal
    from app.models import VideoTask
    with SessionLocal() as owner_db:
        owned = owner_db.query(VideoTask).filter(VideoTask.id == task_id).first()
        if owned is None or owned.user_id != user.id:
            raise HTTPException(403, "Music task ownership could not be verified")
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Music task not found")
    status = task.get("status")
    result = task.get("result") or {}
    filename = result.get("music_filename") if isinstance(result, dict) else None
    if status == "completed" and filename:
        next_status = "music_ready"
        production["music_filename"] = filename
    elif status in ("failed", "cancelled"):
        next_status = "music_failed"
    else:
        next_status = "music_processing"
    brief["_production"] = production
    with database() as db:
        db.execute(
            "UPDATE assistant_projects SET status=?, updated_at=?, payload=? "
            "WHERE id=? AND status IN ('music_processing','music_ready','music_failed')",
            (next_status, now(), json.dumps(brief), project_id),
        )
    effective_status = next_status if row["status"] in ("music_processing", "music_ready", "music_failed") else row["status"]
    return {"id": project_id, "status": effective_status,
            "task_id": task_id, "task_status": status,
            "music_filename": filename if next_status == "music_ready" else None}


@router.post("/projects/{project_id}/start-clip")
def start_clip(project_id: str, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)) -> dict[str, Any]:
    """Explicit second confirmation: start clip only after owned music completes."""
    with database() as bridge_db:
        bridge_db.execute("BEGIN IMMEDIATE")
        row = bridge_db.execute(
            "SELECT status, payload FROM assistant_projects WHERE id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Project not found")
        brief = json.loads(row["payload"])
        production = brief.get("_production") or {}
        if not is_project_owner(brief, user.id):
            raise HTTPException(403, "Project is not owned by current user")
        if row["status"] != "music_ready":
            raise HTTPException(409, "Music must be ready and clip not previously started")
        filename = production.get("music_filename")
        if not filename:
            raise HTTPException(409, "Music file is missing")
        bridge_db.execute(
            "UPDATE assistant_projects SET status='clip_starting', updated_at=? WHERE id=?",
            (now(), project_id),
        )
        bridge_db.commit()
    try:
        clip_request = GenerateClipRequest(**clip_payload(brief, filename))
        result = generate_music_clip_task(clip_request, user=user, db=db)
    except Exception:
        with database() as bridge_db:
            bridge_db.execute(
                "UPDATE assistant_projects SET status='clip_start_failed', updated_at=? "
                "WHERE id=? AND status='clip_starting'", (now(), project_id)
            )
        raise
    production["clip_task_id"] = result.get("task_id")
    brief["_production"] = production
    with database() as bridge_db:
        bridge_db.execute(
            "UPDATE assistant_projects SET status='clip_processing', updated_at=?, payload=? "
            "WHERE id=? AND status='clip_starting'",
            (now(), json.dumps(brief), project_id),
        )
    return {"id": project_id, "status": "clip_processing",
            "task_id": result.get("task_id"), "auto_upload_youtube": False}


@router.get("/projects/{project_id}/clip-status")
def clip_status(project_id: str, user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Read the owned clip task and return its deliverable for manual review."""
    with database() as bridge_db:
        row = bridge_db.execute(
            "SELECT status, payload FROM assistant_projects WHERE id=?", (project_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    brief = json.loads(row["payload"])
    production = brief.get("_production") or {}
    if production.get("user_id") != user.id:
        raise HTTPException(403, "Project is not owned by current user")
    task_id = production.get("clip_task_id")
    if not task_id:
        raise HTTPException(409, "No clip task attached")
    # Verify the persisted task owner, not only the bridge payload.
    from app.database import SessionLocal
    from app.models import VideoTask
    with SessionLocal() as owner_db:
        owned = owner_db.query(VideoTask).filter(VideoTask.id == task_id).first()
        if owned is None or owned.user_id != user.id:
            raise HTTPException(403, "Clip task ownership could not be verified")
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Clip task not found")
    task_state = task.get("status")
    if task_state == "completed":
        next_status = "awaiting_clip_review"
    elif task_state in ("failed", "cancelled"):
        next_status = "clip_failed"
    else:
        next_status = "clip_processing"
    with database() as bridge_db:
        bridge_db.execute(
            "UPDATE assistant_projects SET status=?, updated_at=? "
            "WHERE id=? AND status IN ('clip_processing','awaiting_clip_review','clip_failed')",
            (next_status, now(), project_id),
        )
    return {
        "id": project_id,
        "status": next_status,
        "task_id": task_id,
        "task_status": task_state,
        "progress": task.get("progress"),
        "message": task.get("message"),
        "result": task.get("result") if next_status == "awaiting_clip_review" else None,
        "requires_review_before_publication": True,
        "auto_upload_youtube": False,
    }
