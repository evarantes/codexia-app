import sys
import os
import json
import logging
from datetime import datetime

# Setup environment
sys.path.append(os.getcwd())

from app.database import SessionLocal, Base, engine
from app.models import ScheduledVideo
from app.services.video_processing import process_scheduled_video
import app.models # Importar todo o módulo para registrar os modelos

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def debug_video_processing():
    print("Registrando tabelas...")
    # Garantir que as tabelas existam
    Base.metadata.create_all(bind=engine)
    print(f"Tabelas registradas: {list(Base.metadata.tables.keys())}")
    
    db = SessionLocal()
    try:
        print("Criando vídeo de teste no banco de dados...")
        
        # Simular estrutura do script_data
        script_data = {
            "title": "Debug Video",
            "scenes": [
                {"text": "Cena 1 de teste debug."},
                {"text": "Cena 2 de teste debug."}
            ],
            "music_mood": "happy",
            "duration": 1 # 1 minuto para ser rápido
        }
        
        video = ScheduledVideo(
            theme="Test Debug",
            title="Debug Video Auto",
            description="Video criado para debug do YouTube Auto",
            scheduled_for=datetime.utcnow(),
            script_data=json.dumps(script_data),
            voice_style="Humana (Natural)",
            voice_gender="Feminina",
            status="queued",
            video_type="video", # ou 'short'
        )
        
        db.add(video)
        db.commit()
        db.refresh(video)
        
        print(f"Vídeo criado com ID: {video.id}")
        print("Iniciando processamento...")
        
        # Chamar processamento diretamente
        process_scheduled_video(video.id)
        
        # Verificar resultado
        db.refresh(video)
        print(f"Status final: {video.status}")
        if video.status == "failed":
            print(f"Descrição (Erro): {video.description}")
        else:
            print(f"Sucesso! URL: {video.video_url}")
            
    except Exception as e:
        import traceback
        print(f"Erro no script de debug: {e}")
        print(traceback.format_exc())
    finally:
        db.close()

if __name__ == "__main__":
    # init_db() # Assumindo que o banco já existe
    debug_video_processing()
