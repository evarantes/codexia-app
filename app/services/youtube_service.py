import os
import json
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from app.database import SessionLocal
from app.models import Settings

# Escopos necessários
SCOPES = [
    'https://www.googleapis.com/auth/youtube.upload',
    'https://www.googleapis.com/auth/youtube.force-ssl',
    'https://www.googleapis.com/auth/youtube.readonly'
]

class YouTubeService:
    def __init__(self):
        self.credentials = None
        self.service = None
        self._load_credentials()

    def _load_credentials(self):
        """Carrega credenciais do banco ou arquivo"""
        
        # 1. Tentar carregar do Banco de Dados
        db = SessionLocal()
        settings = db.query(Settings).first()
        db.close()

        if settings and settings.youtube_refresh_token and settings.youtube_client_id and settings.youtube_client_secret:
            try:
                info = {
                    "client_id": settings.youtube_client_id,
                    "client_secret": settings.youtube_client_secret,
                    "refresh_token": settings.youtube_refresh_token,
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
                # Não passar SCOPES aqui para evitar erro de invalid_scope se o token tiver escopos diferentes
                self.credentials = Credentials.from_authorized_user_info(info, scopes=None)
            except Exception as e:
                print(f"Erro ao carregar credenciais do YouTube do banco: {e}")

        # 2. Fallback para arquivo local (Desenvolvimento)
        if not self.credentials and os.path.exists('token.json'):
            try:
                self.credentials = Credentials.from_authorized_user_file('token.json', SCOPES)
            except Exception as e:
                print(f"Erro ao carregar token.json: {e}")
            
        # 3. Atualizar token se expirado
        if self.credentials and self.credentials.expired and self.credentials.refresh_token:
            try:
                self.credentials.refresh(Request())
            except Exception as e:
                print(f"Erro ao atualizar token: {e}")
                self.credentials = None

        if self.credentials:
            self.service = build('youtube', 'v3', credentials=self.credentials)

    def get_auth_url(self):
        """Gera URL para o usuário autorizar (Fluxo simplificado)"""
        # Nota: Em produção, isso seria um fluxo Web Server, não InstalledApp
        flow = InstalledAppFlow.from_client_secrets_file(
            'client_secret.json', SCOPES) # Requer arquivo client_secret.json do Google Cloud Console
        auth_url, _ = flow.authorization_url(prompt='consent')
        return auth_url

    def _get_my_channel(self):
        """Helper para buscar o canal autenticado"""
        if not self.service:
            return None
            
        try:
            request = self.service.channels().list(
                part="snippet,statistics,brandingSettings",
                mine=True
            )
            response = request.execute()
            
            if response['items']:
                return response['items'][0]
        except Exception as e:
            print(f"Erro no helper _get_my_channel: {e}")
            raise e
        return None

    def get_channel_stats(self):
        """Retorna estatísticas do canal"""
        if not self.service:
            return {
                "title": "Desconectado",
                "subscribers": 0,
                "views": 0,
                "videos": 0,
                "connected": False
            }
        
        try:
            item = self._get_my_channel()
            
            if item:
                return {
                    "id": item['id'],
                    "title": item['snippet']['title'],
                    "description": item['snippet']['description'],
                    "subscribers": item['statistics']['subscriberCount'],
                    "views": item['statistics']['viewCount'],
                    "videos": item['statistics']['videoCount'],
                    "connected": True
                }
        except Exception as e:
            print(f"Erro ao buscar stats do YouTube: {e}")
            return {"error": str(e), "connected": False}
            
        return {"error": "Canal não encontrado", "connected": False}

    def upload_video(self, file_path, title, description, tags=[], category_id="27"): # 27 = Education
        """Faz upload de um vídeo para o YouTube"""
        if not self.service:
            print("[MOCK] Upload de vídeo simulado (Sem credenciais)")
            return {"id": "mock_video_id", "status": "uploaded_mock"}
            
        try:
            body = {
                'snippet': {
                    'title': title,
                    'description': description,
                    'tags': tags,
                    'categoryId': category_id
                },
                'status': {
                    'privacyStatus': 'public',
                    'selfDeclaredMadeForKids': False, 
                }
            }

            media = MediaFileUpload(file_path, chunksize=-1, resumable=True)

            request = self.service.videos().insert(
                part=','.join(body.keys()),
                body=body,
                media_body=media
            )

            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    print(f"Upload progresso: {int(status.progress() * 100)}%")

            return response
        except Exception as e:
            print(f"Erro no upload para YouTube: {e}")
            return {"error": str(e)}

    def optimize_channel(self, ai_service):
        """Analisa e otimiza o canal usando IA"""
        stats = self.get_channel_stats()
        
        # Se stats tem descrição, usa. Senão, mock.
        current_description = stats.get('description', "Canal sobre livros e motivação.")
        
        analysis = ai_service.analyze_channel_strategy(stats, current_description)
        
        # Retorna a análise para o usuário confirmar a execução
        return analysis

    def update_channel_info(self, title=None, description=None):
        """Atualiza título e descrição do canal"""
        if not self.service:
            return {"status": "mock_updated", "title": title, "description": description}
        
        try:
            # 1. Get current channel info using helper
            item = self._get_my_channel()
            if not item:
                return {"error": "Channel not found"}
                
            channel_id = item['id']
            # We need to preserve the categoryId if it exists, otherwise update might fail or reset it
            current_snippet = item['snippet']
            
            # Construct a MINIMAL snippet with only the fields we want to update.
            # IMPORTANT: For channels.update with 'snippet', we MUST include 'title' and 'description' at minimum if we are updating them.
            # But we should also be careful about other fields.
            
            new_snippet = {
                "title": title if title else current_snippet.get('title'),
                "description": description if description else current_snippet.get('description'),
                # It is often required to send back the categoryId if present
                "categoryId": current_snippet.get('categoryId') 
            }
            
            # Remove None values
            new_snippet = {k: v for k, v in new_snippet.items() if v is not None}
            
            # 2. Update using snippet
            # If this still fails with 400, it might be due to immutable fields or specific channel state.
            # An alternative is using 'brandingSettings' BUT ensuring we nest it correctly and dont send read-only stuff.
            # Let's try brandingSettings again but properly this time, as it is the "Modern" way to update channel metadata.
            
            branding_settings = item.get('brandingSettings', {})
            if 'channel' not in branding_settings:
                branding_settings['channel'] = {}
            
            # Update values in brandingSettings
            if title:
                branding_settings['channel']['title'] = title
            if description:
                branding_settings['channel']['description'] = description
                
            # Execute update with brandingSettings part
            update_request = self.service.channels().update(
                part="brandingSettings",
                body={
                    "id": channel_id,
                    "brandingSettings": branding_settings
                }
            )
            update_response = update_request.execute()
            return update_response

        except Exception as e:
            print(f"Error updating channel: {e}")
            return {"error": str(e)}

