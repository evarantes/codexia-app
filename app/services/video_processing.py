import json
import os
import gc
from app.database import SessionLocal
from app.models import ScheduledVideo
from app.services.ai_generator import AIContentGenerator
from app.services.video_generator import VideoGenerator

def process_scheduled_video(video_id: int):
    # Re-instanciar DB session pois estamos em thread separada
    db = SessionLocal()
    video = None
    try:
        video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
        if not video:
            return
            
        # Double-check status to avoid race conditions if called from multiple places
        if video.status == "processing":
            print(f"Video {video_id} já está sendo processado.")
            return

        video.status = "processing"
        db.commit()
        
        # Recuperar dados do script
        script_data = json.loads(video.script_data)
        
        ai_service = AIContentGenerator()
        video_service = VideoGenerator(ai_service=ai_service)
        
        topic = video.title
        concept = video.description or ""
        
        # Limpar créditos de música antigos do conceito/descrição para não contaminar o prompt
        if "Music:" in concept:
            concept = concept.split("Music:")[0].strip()
        if "http" in concept: # Remove URLs comuns em créditos
            concept = concept.split("http")[0].strip()
            
        # Gerar roteiro detalhado
        # Se for short, 1 min. Se video, 5 min (padrão solicitado pelo user antes)
        # Prioridade: Duração solicitada > Tipo Short (1min) > Padrão (3min)
        duration = 3 # Padrão
        
        if script_data.get('duration'):
             try:
                 duration = int(script_data.get('duration'))
             except:
                 pass
        elif video.video_type == 'short':
             duration = 1
        
        print(f"Gerando script para video {video_id}: {topic}")
        final_script = ai_service.generate_motivational_script(f"{topic}. Conceito: {concept}", duration)
        
        # Gerar vídeo
        def progress_callback(p, m):
            try:
                # p is 0-100
                video.progress = int(p)
                db.commit()
            except:
                pass
            
        ratio = "9:16" if video.video_type == 'short' else "16:9"
        
        print(f"Renderizando video {video_id}...")
        result = video_service.create_video_from_plan(
            final_script, 
            aspect_ratio=ratio, 
            progress_callback=progress_callback,
            voice_style=video.voice_style,
            voice_gender=video.voice_gender
        )
        video_path = result["video_url"]
        
        # Adicionar créditos ao script_data se possível ou salvar na descrição do vídeo
        if result.get("music_credit"):
            credit = f"\n\n{result['music_credit']}"
            if not video.description:
                video.description = ""
            if credit not in video.description:
                video.description += credit
        
        video.status = "completed"
        video.progress = 100
        video.video_url = video_path # path relativo /static/videos/...
        db.commit()
        print(f"Video {video_id} concluído: {video_path}")
        
    except Exception as e:
        import traceback
        error_msg = f"{str(e)}\n{traceback.format_exc()}"
        print(f"Erro ao gerar video agendado {video_id}: {error_msg}")
        if video:
            video.status = "failed"
            video.progress = 0
            # Append error to description for visibility in UI
            current_desc = video.description or ""
            # Avoid duplicating error messages
            if "[ERRO]" not in current_desc:
                video.description = f"{current_desc}\n\n[ERRO]: {error_msg}"[:5000] # Increased limit for traceback
            db.commit()
    finally:
        db.close()
        gc.collect()
