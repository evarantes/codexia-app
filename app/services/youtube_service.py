import os
import json
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from app.database import SessionLocal
from app.models import Settings

# Escopos necessários
SCOPES = [
    'https://www.googleapis.com/auth/youtube.upload',
    'https://www.googleapis.com/auth/youtube.force-ssl',
    'https://www.googleapis.com/auth/youtube.readonly',
    'https://www.googleapis.com/auth/yt-analytics.readonly'
]

class YouTubeService:
    def __init__(self):
        self.credentials = None
        self.service = None
        self.auth_source = None
        self.auth_error = None
        self._load_credentials()

    def _read_env_youtube_creds(self):
        client_id = (os.getenv("YOUTUBE_CLIENT_ID") or "").strip()
        client_secret = (os.getenv("YOUTUBE_CLIENT_SECRET") or "").strip()
        refresh_token = (os.getenv("YOUTUBE_REFRESH_TOKEN") or "").strip()
        return {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }

    def _read_db_youtube_creds(self, settings):
        return {
            "client_id": (getattr(settings, "youtube_client_id", None) or "").strip() if settings else "",
            "client_secret": (getattr(settings, "youtube_client_secret", None) or "").strip() if settings else "",
            "refresh_token": (getattr(settings, "youtube_refresh_token", None) or "").strip() if settings else "",
        }

    def _compose_info(self, base):
        return {
            "client_id": base.get("client_id"),
            "client_secret": base.get("client_secret"),
            "refresh_token": base.get("refresh_token"),
            "token_uri": "https://oauth2.googleapis.com/token",
        }

    def _has_full_creds(self, data):
        return bool((data.get("client_id") or "").strip() and (data.get("client_secret") or "").strip() and (data.get("refresh_token") or "").strip())

    def _oauth_client_id_secret(self, settings=None):
        db_creds = self._read_db_youtube_creds(settings)
        env_creds = self._read_env_youtube_creds()
        client_id = db_creds.get("client_id") or env_creds.get("client_id")
        client_secret = db_creds.get("client_secret") or env_creds.get("client_secret")
        return (client_id or "").strip(), (client_secret or "").strip()

    def _load_credentials(self):
        """Carrega credenciais do banco ou arquivo"""
        settings = None

        # 1. Tentar carregar do Banco de Dados
        try:
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
                    self.auth_source = "database"
                except Exception as e:
                    print(f"Erro ao carregar credenciais do YouTube do banco: {e}")
                    self.auth_error = f"Erro ao ler credenciais do banco: {e}"
        except Exception as e:
            print(f"Erro ao acessar banco de dados para credenciais: {e}")
            self.auth_error = f"Erro ao acessar banco para credenciais: {e}"
            settings = None

        db_creds = self._read_db_youtube_creds(settings)
        env_creds = self._read_env_youtube_creds()
        mixed_db_env = {
            "client_id": db_creds.get("client_id") or env_creds.get("client_id"),
            "client_secret": db_creds.get("client_secret") or env_creds.get("client_secret"),
            "refresh_token": db_creds.get("refresh_token") or env_creds.get("refresh_token"),
        }
        mixed_env_db = {
            "client_id": env_creds.get("client_id") or db_creds.get("client_id"),
            "client_secret": env_creds.get("client_secret") or db_creds.get("client_secret"),
            "refresh_token": env_creds.get("refresh_token") or db_creds.get("refresh_token"),
        }

        candidates = []
        if self._has_full_creds(db_creds):
            candidates.append(("database", db_creds))
        if self._has_full_creds(env_creds):
            candidates.append(("environment", env_creds))
        if self._has_full_creds(mixed_db_env):
            candidates.append(("mixed_db_env", mixed_db_env))
        if self._has_full_creds(mixed_env_db):
            candidates.append(("mixed_env_db", mixed_env_db))

        last_error = None
        for source, raw in candidates:
            try:
                creds = Credentials.from_authorized_user_info(self._compose_info(raw), scopes=None)
                if creds.refresh_token and (not creds.valid or creds.expired):
                    creds.refresh(Request())
                service = build('youtube', 'v3', credentials=creds)
                self.credentials = creds
                self.service = service
                self.auth_source = source
                self.auth_error = None
                return
            except Exception as e:
                last_error = f"{source}: {e}"
                print(f"Erro ao validar credenciais YouTube ({source}): {e}")

        # Fallback para arquivo local (desenvolvimento)
        if os.path.exists('token.json'):
            try:
                creds = Credentials.from_authorized_user_file('token.json', SCOPES)
                if creds.refresh_token and (not creds.valid or creds.expired):
                    creds.refresh(Request())
                self.credentials = creds
                self.service = build('youtube', 'v3', credentials=creds)
                self.auth_source = "token_file"
                self.auth_error = None
                return
            except Exception as e:
                print(f"Erro ao carregar token.json: {e}")
                last_error = f"token_file: {e}"

        self.credentials = None
        self.service = None
        self.auth_error = last_error or "Credenciais do YouTube ausentes (banco/ENV/token.json)."

    def list_video_comments(self, youtube_video_id: str, max_results: int = 100):
        """Lista comentários (threads) de um vídeo. Retorna lista achatada de comentários (top-level e replies)."""
        if not self.service:
            raise RuntimeError(self.auth_error or "Serviço do YouTube não inicializado.")
        items = []
        try:
            page_token = None
            while True:
                req = self.service.commentThreads().list(
                    part="snippet,replies",
                    videoId=youtube_video_id,
                    maxResults=min(max_results, 100),
                    order="time",
                    textFormat="plainText",
                    pageToken=page_token
                )
                resp = req.execute()
                for th in resp.get("items", []):
                    top = th.get("snippet", {}).get("topLevelComment", {})
                    s = top.get("snippet", {}) if top else {}
                    if top:
                        items.append({
                            "youtube_comment_id": top.get("id"),
                            "youtube_parent_id": None,
                            "youtube_video_id": youtube_video_id,
                            "author": s.get("authorDisplayName"),
                            "text": s.get("textDisplay") or s.get("textOriginal"),
                            "like_count": s.get("likeCount", 0),
                            "published_at": s.get("publishedAt"),
                        })
                    for rep in (th.get("replies", {}) or {}).get("comments", []) or []:
                        rs = rep.get("snippet", {}) if rep else {}
                        items.append({
                            "youtube_comment_id": rep.get("id"),
                            "youtube_parent_id": top.get("id") if top else None,
                            "youtube_video_id": youtube_video_id,
                            "author": rs.get("authorDisplayName"),
                            "text": rs.get("textDisplay") or rs.get("textOriginal"),
                            "like_count": rs.get("likeCount", 0),
                            "published_at": rs.get("publishedAt"),
                        })
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as e:
            raise RuntimeError(f"Erro YouTube API (comments): {e}")
        return items

    def reply_to_comment(self, parent_comment_id: str, text: str):
        """Responde a um comentário (parentId = comentário top-level)."""
        if not self.service:
            raise RuntimeError(self.auth_error or "Serviço do YouTube não inicializado.")
        try:
            body = {
                "snippet": {
                    "parentId": parent_comment_id,
                    "textOriginal": text
                }
            }
            req = self.service.comments().insert(part="snippet", body=body)
            resp = req.execute()
            return resp
        except HttpError as e:
            raise RuntimeError(f"Erro ao responder comentário: {e}")

    def get_auth_url(self):
        """Gera URL para o usuário autorizar (Fluxo simplificado)"""
        # 1. Tentar credenciais do banco/ENV (Client ID + Secret) para montar client_config
        db = SessionLocal()
        settings = db.query(Settings).first()
        db.close()
        client_id, client_secret = self._oauth_client_id_secret(settings)
        if client_id and client_secret:
            try:
                client_config = {
                    "installed": {
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob"]
                    }
                }
                flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
                flow.redirect_uri = 'urn:ietf:wg:oauth:2.0:oob'
                auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline', include_granted_scopes='true')
                
                # Persist code_verifier for PKCE (required by Google for OOB/Desktop)
                if getattr(flow, "code_verifier", None):
                    try:
                        with open("youtube_pkce.txt", "w") as f:
                            f.write(flow.code_verifier)
                    except Exception as e:
                        print(f"Warning: Failed to save PKCE verifier: {e}")

                return auth_url
            except Exception as e:
                raise RuntimeError(f"Credenciais do YouTube (banco/ENV) inválidas: {e}")
        # 2. Fallback: arquivo client_secret.json (desenvolvimento)
        if not os.path.exists('client_secret.json'):
            raise FileNotFoundError("client_secret.json não encontrado e credenciais do YouTube não configuradas (Configurações/ENV).")
        flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', SCOPES)
        flow.redirect_uri = 'urn:ietf:wg:oauth:2.0:oob'
        auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline', include_granted_scopes='true')
        
        # Persist code_verifier for PKCE (fallback flow)
        if getattr(flow, "code_verifier", None):
            try:
                with open("youtube_pkce.txt", "w") as f:
                    f.write(flow.code_verifier)
            except Exception as e:
                print(f"Warning: Failed to save PKCE verifier (file): {e}")

        return auth_url

    def exchange_code_for_token(self, code):
        """Troca o código de autorização por tokens e salva no banco. Retorna (success, message)."""
        try:
            # 1. Tentar configurar Flow via Banco/ENV
            db = SessionLocal()
            settings = db.query(Settings).first()
            db.close()

            flow = None
            used_json_file = False
            
            client_id, client_secret = self._oauth_client_id_secret(settings)
            if client_id and client_secret:
                try:
                    client_config = {
                        "installed": {
                            "client_id": client_id,
                            "client_secret": client_secret,
                            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                            "token_uri": "https://oauth2.googleapis.com/token",
                            "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob"]
                        }
                    }
                    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
                except Exception as e:
                    print(f"Erro ao criar Flow com credenciais do banco: {e}")

            # 2. Fallback para arquivo se não conseguiu via banco
            if not flow:
                if os.path.exists('client_secret.json'):
                    flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', SCOPES)
                    used_json_file = True
                else:
                    return False, "Credenciais não encontradas (Banco ou arquivo json)"

            flow.redirect_uri = 'urn:ietf:wg:oauth:2.0:oob'
            
            # Restore PKCE verifier if available
            if os.path.exists("youtube_pkce.txt"):
                try:
                    with open("youtube_pkce.txt", "r") as f:
                        verifier = f.read().strip()
                        if verifier:
                            flow.code_verifier = verifier
                    # Cleanup
                    try:
                        os.remove("youtube_pkce.txt")
                    except:
                        pass
                except Exception as e:
                    print(f"Warning: Failed to load PKCE verifier: {e}")

            flow.fetch_token(code=code)
            self.credentials = flow.credentials
            
            # Se usou JSON, extrair client_id/secret para salvar no banco
            client_id_override = None
            client_secret_override = None
            
            if used_json_file:
                try:
                    with open('client_secret.json', 'r') as f:
                        data = json.load(f)
                        installed = data.get('installed', data.get('web', {}))
                        client_id_override = installed.get('client_id')
                        client_secret_override = installed.get('client_secret')
                except Exception as e:
                    print(f"Erro ao ler client_secret.json para persistência: {e}")

            # Salvar no banco
            self._save_credentials_to_db(client_id_override, client_secret_override)
            return True, "Autenticação realizada com sucesso!"
        except Exception as e:
            print(f"Erro ao trocar código por token: {e}")
            return False, str(e)

    def _save_credentials_to_db(self, client_id=None, client_secret=None):
        """Salva as credenciais atuais no banco de dados"""
        if not self.credentials:
            return

        db = SessionLocal()
        try:
            settings = db.query(Settings).first()
            if not settings:
                settings = Settings()
                db.add(settings)
            
            # Garantir que temos refresh_token
            if not self.credentials.refresh_token:
                print("AVISO: Google não retornou refresh_token. Mantendo o anterior se existir.")
            else:
                settings.youtube_refresh_token = self.credentials.refresh_token.strip()
            
            # Atualizar client_id/secret se fornecidos ou presentes nas credenciais
            current_client_id = getattr(self.credentials, 'client_id', None) or client_id
            if current_client_id:
                settings.youtube_client_id = current_client_id.strip()
            
            current_client_secret = getattr(self.credentials, 'client_secret', None) or client_secret
            if current_client_secret:
                settings.youtube_client_secret = current_client_secret.strip()

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
        """Retorna estatísticas do canal conectado"""
        if not self.service:
            # Tentar descobrir POR QUE falhou
            reason = "Autenticação falhou"
            if not self.credentials:
                reason = "Credenciais não encontradas no banco"
            elif not self.credentials.valid:
                reason = "Credenciais inválidas ou expiradas"
                
            return {"connected": False, "error": reason}
        
        try:
            channel = self._get_my_channel()
            if channel:
                stats = channel['statistics']
                snippet = channel['snippet']
                return {
                    "connected": True,
                    "title": snippet['title'],
                    "subscribers": stats['subscriberCount'],
                    "views": stats['viewCount'],
                    "videos": stats['videoCount'],
                    "thumbnail": snippet['thumbnails']['default']['url']
                }
            return {"connected": False, "error": "Nenhum canal encontrado"}
        except HttpError as e:
            if e.resp.status == 403 and "accessNotConfigured" in str(e):
                return {
                    "connected": False, 
                    "error": "A API 'YouTube Data API v3' não está ativada no Google Cloud. Ative-a no Console do Google Cloud."
                }
            return {"connected": False, "error": f"Erro do Google: {str(e)}"}
        except Exception as e:
            return {"connected": False, "error": f"Erro ao buscar canal: {str(e)}"}

    def upload_video(self, file_path, title, description, tags=None, category_id="27"):  # 27 = Education
        """Faz upload de um vídeo para o YouTube"""
        if tags is None:
            tags = []
        if not self.service:
            reason = self.auth_error or "Canal não conectado ao YouTube."
            print(f"[YOUTUBE_DISCONNECTED] Upload não iniciado: {reason}")
            return {"error": reason, "status": "not_connected"}
            
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

    def get_subscriber_insights(self, days: int = 14, max_results: int = 20):
        if not self.credentials:
            return {
                "days": days,
                "subscriber_sources": [],
                "best_videos": [],
                "totals": {"subscribersGained": 0, "subscribersLost": 0},
                "error": self.auth_error or "Credenciais não disponíveis."
            }

        if not self.service:
            return {
                "days": days,
                "subscriber_sources": [],
                "best_videos": [],
                "totals": {"subscribersGained": 0, "subscribersLost": 0},
                "error": self.auth_error or "Serviço do YouTube não inicializado."
            }

        from datetime import datetime, timedelta
        end_date = datetime.utcnow().date()
        start_date = end_date - timedelta(days=max(1, int(days)))

        try:
            analytics = build("youtubeAnalytics", "v2", credentials=self.credentials)

            totals_resp = analytics.reports().query(
                ids="channel==MINE",
                startDate=start_date.isoformat(),
                endDate=end_date.isoformat(),
                metrics="subscribersGained,subscribersLost"
            ).execute()

            totals = {"subscribersGained": 0, "subscribersLost": 0}
            try:
                row = (totals_resp.get("rows") or [[0, 0]])[0]
                totals["subscribersGained"] = int(row[0] or 0)
                totals["subscribersLost"] = int(row[1] or 0)
            except Exception:
                pass

            sources_resp = analytics.reports().query(
                ids="channel==MINE",
                startDate=start_date.isoformat(),
                endDate=end_date.isoformat(),
                metrics="subscribersGained",
                dimensions="trafficSourceType",
                sort="-subscribersGained",
                maxResults=min(int(max_results), 25)
            ).execute()

            subscriber_sources = []
            for r in (sources_resp.get("rows") or []):
                try:
                    subscriber_sources.append({"source": r[0], "subscribersGained": int(r[1] or 0)})
                except Exception:
                    continue

            best_videos_resp = analytics.reports().query(
                ids="channel==MINE",
                startDate=start_date.isoformat(),
                endDate=end_date.isoformat(),
                metrics="subscribersGained,views,likes,comments",
                dimensions="video",
                sort="-subscribersGained",
                maxResults=min(int(max_results), 50)
            ).execute()

            rows = best_videos_resp.get("rows") or []
            video_ids = [r[0] for r in rows if r and r[0]]

            meta_by_id = {}
            if video_ids:
                try:
                    meta_resp = self.service.videos().list(
                        part="snippet,contentDetails",
                        id=",".join(video_ids[:50])
                    ).execute()
                    for it in (meta_resp.get("items") or []):
                        vid = it.get("id")
                        snippet = it.get("snippet") or {}
                        cd = it.get("contentDetails") or {}
                        meta_by_id[vid] = {
                            "title": snippet.get("title"),
                            "publishedAt": snippet.get("publishedAt"),
                            "duration": cd.get("duration"),
                        }
                except Exception:
                    meta_by_id = {}

            best_videos = []
            for r in rows:
                if not r or len(r) < 5:
                    continue
                vid = r[0]
                best_videos.append({
                    "videoId": vid,
                    "title": meta_by_id.get(vid, {}).get("title"),
                    "publishedAt": meta_by_id.get(vid, {}).get("publishedAt"),
                    "duration": meta_by_id.get(vid, {}).get("duration"),
                    "subscribersGained": int(r[1] or 0),
                    "views": int(r[2] or 0),
                    "likes": int(r[3] or 0),
                    "comments": int(r[4] or 0),
                })

            return {
                "days": days,
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "totals": totals,
                "subscriber_sources": subscriber_sources,
                "best_videos": best_videos,
            }
        except HttpError as e:
            return {
                "days": days,
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "subscriber_sources": [],
                "best_videos": [],
                "totals": {"subscribersGained": 0, "subscribersLost": 0},
                "error": str(e),
            }
        except Exception as e:
            return {
                "days": days,
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "subscriber_sources": [],
                "best_videos": [],
                "totals": {"subscribersGained": 0, "subscribersLost": 0},
                "error": str(e),
            }

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
