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
            print("Token expirado. Tentando atualizar...")
            try:
                self.credentials.refresh(Request())
                print("Token atualizado com sucesso.")
            except Exception as e:
                print(f"Erro ao atualizar token (tentativa 1): {e}")
                # Retry once
                try:
                    import time
                    time.sleep(1)
                    self.credentials.refresh(Request())
                    print("Token atualizado com sucesso na tentativa 2.")
                except Exception as e2:
                    print(f"ERRO FATAL ao atualizar token (tentativa 2): {e2}. Desconectando.")
                    self.credentials = None

        if self.credentials:
            self.service = build('youtube', 'v3', credentials=self.credentials)

    def get_auth_url(self):
        """Gera URL para o usuário autorizar (Fluxo simplificado)"""
        # Nota: Em produção, isso seria um fluxo Web Server, não InstalledApp
        flow = InstalledAppFlow.from_client_secrets_file(
            'client_secret.json', SCOPES) # Requer arquivo client_secret.json do Google Cloud Console
        # Use urn:ietf:wg:oauth:2.0:oob for manual copy-paste
        flow.redirect_uri = 'urn:ietf:wg:oauth:2.0:oob'
        auth_url, _ = flow.authorization_url(prompt='consent')
        return auth_url

    def exchange_code_for_token(self, code):
        """Troca o código de autorização por tokens e salva no banco"""
        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                'client_secret.json', SCOPES)
            flow.redirect_uri = 'urn:ietf:wg:oauth:2.0:oob'
            flow.fetch_token(code=code)
            self.credentials = flow.credentials
            
            # Salvar no banco
            self._save_credentials_to_db()
            return True
        except Exception as e:
            print(f"Erro ao trocar código por token: {e}")
            return False

    def _save_credentials_to_db(self):
        """Salva as credenciais atuais no banco de dados"""
        if not self.credentials:
            return

        db = SessionLocal()
        try:
            settings = db.query(Settings).first()
            if not settings:
                settings = Settings()
                db.add(settings)
            
            settings.youtube_refresh_token = self.credentials.refresh_token
            settings.youtube_client_id = self.credentials.client_id
            settings.youtube_client_secret = self.credentials.client_secret
            db.commit()
            print("Credenciais do YouTube salvas no banco com sucesso.")
        except Exception as e:
            print(f"Erro ao salvar credenciais no banco: {e}")
        finally:
            db.close()


    def _get_my_channel(self):
        """Helper para buscar o canal autenticado"""
        if not self.service:
            return None
            
        try:
            request = self.service.channels().list(
                part="snippet,statistics,brandingSettings,contentDetails",
                mine=True
            )
            response = request.execute()
            
            if response['items']:
                return response['items'][0]
        except Exception as e:
            print(f"Erro no helper _get_my_channel: {e}")
            raise e
        return None

    def get_recent_videos_stats(self, limit=10):
        """Busca estatísticas dos vídeos recentes"""
        if not self.service:
            return []
        
        try:
            # 1. Get Uploads Playlist ID
            channel = self._get_my_channel()
            if not channel:
                return []
                
            uploads_playlist_id = channel['contentDetails']['relatedPlaylists']['uploads']
            
            # 2. Get Videos from Playlist
            playlist_items = self.service.playlistItems().list(
                part="snippet,contentDetails",
                playlistId=uploads_playlist_id,
                maxResults=limit
            ).execute()
            
            if not playlist_items.get('items'):
                return []
                
            video_ids = [item['contentDetails']['videoId'] for item in playlist_items['items']]
            
            # 3. Get Video Stats
            videos_response = self.service.videos().list(
                part="statistics,snippet",
                id=','.join(video_ids)
            ).execute()
            
            videos = []
            for item in videos_response['items']:
                stats = item['statistics']
                videos.append({
                    "id": item['id'],
                    "title": item['snippet']['title'],
                    "published_at": item['snippet']['publishedAt'],
                    "views": int(stats.get('viewCount', 0)),
                    "likes": int(stats.get('likeCount', 0)),
                    "comments": int(stats.get('commentCount', 0))
                })
            
            return videos
            
        except Exception as e:
            print(f"Error fetching recent videos: {e}")
            return []

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

    def upload_channel_banner(self, image_url):
        """Faz upload de uma imagem para banner do canal"""
        if not self.service:
            return None
            
        import requests
        from io import BytesIO
        from googleapiclient.http import MediaIoBaseUpload
        
        try:
            print(f"Baixando banner de {image_url}...")
            response = requests.get(image_url)
            if response.status_code != 200:
                print("Erro ao baixar imagem do banner")
                return None
            
            image_data = BytesIO(response.content)
            # Google exige mimetype image/png ou image/jpeg
            media = MediaIoBaseUpload(image_data, mimetype='image/png', resumable=True)
            
            print("Enviando banner para YouTube...")
            request = self.service.channelBanners().insert(
                body={},
                media_body=media
            )
            response = request.execute()
            print(f"Banner enviado. URL: {response.get('url')}")
            return response.get('url')
        except Exception as e:
            print(f"Erro ao fazer upload do banner: {e}")
            return None

    def update_channel_info(self, title=None, description=None, banner_external_url=None):
        """Atualiza título e descrição do canal"""
        if not self.service:
            return {"error": "Canal não conectado. Vá em Configurações > YouTube e conecte seu canal primeiro."}
        
        try:
            # 1. Get current channel info using helper
            item = self._get_my_channel()
            if not item:
                return {"error": "Channel not found"}
                
            channel_id = item['id']
            
            # Prepare update parts
            parts = []
            body = {"id": channel_id}
            
            # Update Branding Settings (Banner, and legacy title/desc)
            branding_settings = item.get('brandingSettings', {})
            if 'channel' not in branding_settings:
                branding_settings['channel'] = {}
            if 'image' not in branding_settings:
                branding_settings['image'] = {}
            
            branding_updated = False
            if title:
                branding_settings['channel']['title'] = title
                branding_updated = True
            if description:
                branding_settings['channel']['description'] = description
                branding_updated = True
            if banner_external_url:
                branding_settings['image']['bannerExternalUrl'] = banner_external_url
                branding_updated = True
                
            if branding_updated:
                parts.append("brandingSettings")
                body["brandingSettings"] = branding_settings
            
            # Update Snippet (Main Title/Description)
            snippet = item.get('snippet', {})
            snippet_updated = False
            if title:
                snippet['title'] = title
                snippet_updated = True
            if description:
                snippet['description'] = description
                snippet_updated = True
            
            if snippet_updated:
                parts.append("snippet")
                body["snippet"] = snippet
                
            if not parts:
                return {"message": "Nada a atualizar"}

            # Execute update
            update_request = self.service.channels().update(
                part=",".join(parts),
                body=body
            )
            update_response = update_request.execute()
            return update_response

        except Exception as e:
            print(f"Error updating channel: {e}")
            return {"error": str(e)}

    def get_recent_videos_performance(self, max_results: int = 20):
        """
        Retorna uma lista simplificada de vídeos recentes com métricas básicas
        (título, data de publicação, views, likes se disponíveis).
        Usa apenas escopos já configurados (readonly).
        """
        if not self.service:
            print("[YouTubeService] get_recent_videos_performance: serviço não conectado, retornando lista vazia.")
            return []

        try:
            channel = self._get_my_channel()
            if not channel:
                return []

            uploads_playlist_id = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
            if not uploads_playlist_id:
                # Fallback simples: usar search.list por canal
                search_req = self.service.search().list(
                    part="snippet",
                    channelId=channel["id"],
                    maxResults=max_results,
                    order="date",
                    type="video"
                )
                search_res = search_req.execute()
                items = search_res.get("items", [])
                videos = []
                for item in items:
                    snippet = item.get("snippet", {})
                    videos.append({
                        "videoId": item["id"]["videoId"],
                        "title": snippet.get("title"),
                        "publishedAt": snippet.get("publishedAt"),
                        "viewCount": None,
                        "likeCount": None
                    })
                return videos

            # Caso padrão: playlist "uploads"
            playlist_items_req = self.service.playlistItems().list(
                part="contentDetails,snippet",
                playlistId=uploads_playlist_id,
                maxResults=max_results
            )
            playlist_items_res = playlist_items_req.execute()
            items = playlist_items_res.get("items", [])

            video_ids = [it["contentDetails"]["videoId"] for it in items if it.get("contentDetails")]
            if not video_ids:
                return []

            videos_req = self.service.videos().list(
                part="snippet,statistics",
                id=",".join(video_ids)
            )
            videos_res = videos_req.execute()
            videos = []
            for item in videos_res.get("items", []):
                snippet = item.get("snippet", {})
                stats = item.get("statistics", {})
                videos.append({
                    "videoId": item.get("id"),
                    "title": snippet.get("title"),
                    "publishedAt": snippet.get("publishedAt"),
                    "viewCount": int(stats.get("viewCount", 0)) if stats.get("viewCount") is not None else 0,
                    "likeCount": int(stats.get("likeCount", 0)) if stats.get("likeCount") is not None else 0,
                    "commentCount": int(stats.get("commentCount", 0)) if stats.get("commentCount") is not None else 0
                })
            # Ordena por views desc
            videos.sort(key=lambda v: v.get("viewCount", 0), reverse=True)
            return videos
        except Exception as e:
            print(f"[YouTubeService] Erro ao buscar performance de vídeos: {e}")
            return []

    def get_monetization_progress(self):
        """
        Retorna um resumo simples de progresso rumo à monetização (estimado),
        usando apenas stats básicos disponíveis sem Analytics.
        """
        stats = self.get_channel_stats()
        try:
            subscribers = int(stats.get("subscribers", 0) or 0)
        except Exception:
            subscribers = 0
        try:
            total_views = int(stats.get("views", 0) or 0)
        except Exception:
            total_views = 0

        # Estimativa bem simplificada de horas de exibição:
        # assumindo ~3min de watch médio por view em vídeos longos.
        # horas ≈ (views * 3min) / 60
        estimated_watch_hours = (total_views * 3) / 60.0

        data = {
            "subscribers": subscribers,
            "subscribers_target": 1000,
            "total_views": total_views,
            "estimated_watch_hours": round(estimated_watch_hours, 1),
            "watch_hours_target": 4000,
            # valores percentuais para exibição
            "subscribers_progress_pct": min(100, round(subscribers / 10.0, 1)) if subscribers else 0,
            "watch_hours_progress_pct": min(100, round((estimated_watch_hours / 4000.0) * 100, 1)) if estimated_watch_hours else 0,
            "raw_stats": stats,
        }
        return data

