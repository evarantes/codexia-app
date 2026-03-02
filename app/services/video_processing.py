import json
import os
import gc
import datetime
from app.database import SessionLocal
from app.models import ScheduledVideo

def process_scheduled_video(video_id: int):
    # Lazy import para reduzir uso de memória no startup (moviepy/PIL/numpy são pesados)
    from app.services.ai_generator import AIContentGenerator
    from app.services.video_generator import VideoGenerator

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

        # CRITICAL SAFETY: Prevent reprocessing of already completed videos (saves OpenAI cost)
        if video.status in ("completed", "ready", "published"):
             print(f"Video {video_id} já está concluído ({video.status}). Ignorando reprocessamento.")
             return
        
        # Check if file exists to recover from "stuck" state without cost
        if video.video_url:
             try:
                 from app.config import absolute_path_for_video
                 abs_path = absolute_path_for_video(video.video_url)
                 if os.path.exists(abs_path):
                     print(f"Vídeo {video_id} já possui arquivo gerado em {abs_path}. Recuperando status para 'completed'.")
                     video.status = "completed"
                     video.progress = 100
                     db.commit()
                     return
             except Exception as e:
                 print(f"Erro ao verificar arquivo existente: {e}")

        video.status = "processing"
        db.commit()
        
        # Recuperar dados do script (safe load)
        script_data = {}
        if video.script_data:
            try:
                script_data = json.loads(video.script_data)
            except Exception as e:
                print(f"Erro ao decodificar script_data: {e}")
                script_data = {}
        
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
        
        final_script = None
        
        # OTIMIZAÇÃO DE CUSTO: Verificar se já existe script gerado (recuperação de falha/crash)
        if script_data.get("scenes") and isinstance(script_data.get("scenes"), list):
             print(f"Usando script em cache (DB) para video {video_id}. Economizando chamada OpenAI.")
             final_script = script_data
        
        # MODO VIDEO CLIP MUSICAL
        elif video.music_file_path and os.path.exists(video.music_file_path):
             print(f"Gerando plano visual para música: {video.music_file_path}")
             try:
                 # Obter duração da música
                 try:
                    from moviepy.editor import AudioFileClip
                 except ImportError:
                    from moviepy import AudioFileClip
                 
                 audioclip = AudioFileClip(video.music_file_path)
                 music_duration = int(audioclip.duration)
                 audioclip.close()
                 
                 final_script = ai_service.generate_visual_plan_for_music(topic, concept, music_duration)
                 final_script["title"] = topic # Ensure title is present
                 
                 # Salvar cache
                 if final_script and "scenes" in final_script:
                     script_data.update(final_script)
                     video.script_data = json.dumps(script_data)
                     db.commit()
             except Exception as e:
                 print(f"Erro ao processar modo música: {e}")
                 raise e

        else:
             print(f"Gerando script para video {video_id}: {topic}")
             final_script = ai_service.generate_motivational_script(f"{topic}. Conceito: {concept}", duration)
             
             # Salvar script gerado no banco imediatamente para evitar regerar em caso de crash
             if final_script and "scenes" in final_script:
                 try:
                     # Merge generated script into existing plan data
                     script_data.update(final_script)
                     video.script_data = json.dumps(script_data)
                     db.commit()
                     print(f"Script salvo em cache para video {video_id}")
                 except Exception as e:
                     print(f"Erro ao salvar cache do script: {e}")

        if not final_script or not isinstance(final_script.get("scenes"), list) or len(final_script.get("scenes", [])) == 0:
            raise ValueError(
                "IA não retornou roteiro válido. Configure OPENAI_API_KEY ou GEMINI_API_KEY em Configurações."
            )

        # ENRICHMENT: IA gera image_prompts profissionais com base na narração (imagens próprias para vídeo profissional)
        # Skip enrichment for music videos as they are already visual-focused
        if not video.music_file_path:
            print("Enriquecendo cenas com descrições visuais geradas pela IA (narração → imagem)...")
            final_script = ai_service.enrich_scenes_with_image_prompts(final_script)
        
        # Gerar vídeo
        def progress_callback(p, m):
            try:
                # p is 0-100; nunca diminuir progresso (evita mostrar 95% -> 24% se o logger/enviar valor errado)
                new_p = int(p)
                if new_p >= (video.progress or 0):
                    video.progress = new_p
                    video.updated_at = datetime.datetime.now()
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
            voice_gender=video.voice_gender,
            music_file_path=video.music_file_path # Pass music file if exists
        )
        video_path = result["video_url"]
        
        # Adicionar créditos ao script_data se possível ou salvar na descrição do vídeo
        if result.get("music_credit"):
            credit = f"\n\n{result['music_credit']}"
            if not video.description:
                video.description = ""
            if credit not in video.description:
                video.description += credit
        
        video.status = "AWAITING_PUBLISH"
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
