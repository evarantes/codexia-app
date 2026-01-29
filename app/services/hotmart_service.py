"""
Serviço para integração com a API da Hotmart
"""
import requests
import json
import os
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.models import Settings

class HotmartService:
    def __init__(self, db: Session = None):
        self.db = db
        self.base_url = "https://api-sec-vlc.hotmart.com"
        self.token_url = f"{self.base_url}/security/oauth/token"
        self.products_url = "https://developers.hotmart.com/payments/api/v1/products"
        self.access_token = None
        self.token_expires_at = None
        
    def _get_settings(self):
        """Busca configurações do banco de dados"""
        if not self.db:
            return None
        return self.db.query(Settings).first()
    
    def _get_client_credentials(self):
        """Obtém credenciais da Hotmart do banco de dados"""
        settings = self._get_settings()
        if not settings:
            return None, None
        
        client_id = settings.hotmart_client_id
        client_secret = settings.hotmart_client_secret
        
        # Verifica se são strings vazias ou None
        if not client_id or not client_id.strip():
            return None, None
        if not client_secret or not client_secret.strip():
            return None, None
            
        return client_id.strip(), client_secret.strip()
    
    def authenticate(self):
        """
        Autentica na API da Hotmart usando OAuth 2.0 Client Credentials
        Retorna True se autenticado com sucesso
        """
        client_id, client_secret = self._get_client_credentials()
        
        if not client_id or not client_secret:
            raise Exception("Credenciais Hotmart não configuradas. Configure em Configurações.")
        
        # Verifica se já temos um token válido
        settings = self._get_settings()
        if settings and settings.hotmart_access_token and settings.hotmart_token_expires_at:
            expires_at = settings.hotmart_token_expires_at
            if isinstance(expires_at, str):
                try:
                    expires_at = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
                except:
                    expires_at = None
            
            if expires_at and expires_at > datetime.utcnow():
                self.access_token = settings.hotmart_access_token
                self.token_expires_at = expires_at
                return True
        
        # Solicita novo token
        try:
            import base64
            
            # Tenta primeiro com Basic Auth no header (método mais comum)
            auth_string = f"{client_id}:{client_secret}"
            auth_bytes = auth_string.encode('utf-8')
            auth_b64 = base64.b64encode(auth_bytes).decode('utf-8')
            
            headers = {
                "Authorization": f"Basic {auth_b64}",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            response = requests.post(
                self.token_url,
                data={"grant_type": "client_credentials"},
                headers=headers,
                timeout=10
            )
            
            # Se falhar com Basic Auth, tenta com credenciais no body (fallback)
            if response.status_code == 401:
                response = requests.post(
                    self.token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=10
                )
            
            if response.status_code != 200:
                error_detail = response.text
                try:
                    error_json = response.json()
                    error_msg = error_json.get("error_description") or error_json.get("error") or error_detail
                except:
                    error_msg = error_detail
                raise Exception(f"Erro ao autenticar na Hotmart: {response.status_code} - {error_msg}")
            
            data = response.json()
            self.access_token = data.get("access_token")
            expires_in = data.get("expires_in", 3600)  # Default 1 hora
            
            # Calcula data de expiração
            self.token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in - 60)  # -60 para margem
            
            # Salva no banco de dados
            if settings and self.db:
                settings.hotmart_access_token = self.access_token
                settings.hotmart_token_expires_at = self.token_expires_at
                self.db.commit()
            
            return True
            
        except requests.exceptions.RequestException as e:
            raise Exception(f"Erro de conexão com Hotmart: {str(e)}")
    
    def _ensure_authenticated(self):
        """Garante que estamos autenticados"""
        if not self.access_token or (self.token_expires_at and self.token_expires_at <= datetime.utcnow()):
            self.authenticate()
    
    def create_product(self, product_data: dict):
        """
        Cria um produto na Hotmart
        
        product_data deve conter:
        - name: Nome do produto
        - description: Descrição
        - price: Preço
        - category: Categoria
        - etc.
        """
        self._ensure_authenticated()
        
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        
        try:
            response = requests.post(
                self.products_url,
                json=product_data,
                headers=headers,
                timeout=30
            )
            
            if response.status_code not in [200, 201]:
                error_detail = response.text[:500] if response.text else response.reason
                raise Exception(f"Erro ao criar produto na Hotmart: {response.status_code} - {error_detail}")
            
            # Resposta pode ser vazia ou não-JSON (ex.: developers.hotmart.com retorna HTML ou vazio)
            body = response.text.strip()
            if not body:
                location = response.headers.get("Location") or ""
                product_id = location.rstrip("/").split("/")[-1] if location else None
                if product_id:
                    return {"id": product_id, "product_id": product_id}
                raise Exception(
                    "A API da Hotmart retornou resposta vazia (status %s). "
                    "O endpoint de produtos pode não estar disponível ou as credenciais não têm permissão. "
                    "Crie o produto manualmente em app.hotmart.com e use o link no livro."
                % response.status_code)
            try:
                return response.json()
            except (json.JSONDecodeError, ValueError):
                raise Exception(
                    f"A API da Hotmart retornou resposta inválida (não é JSON). "
                    f"Status: {response.status_code}. Verifique se o endpoint de produtos está correto nas credenciais."
                )
            
        except requests.exceptions.RequestException as e:
            raise Exception(f"Erro de conexão ao criar produto: {str(e)}")
    
    def get_product(self, product_id: str):
        """Busca informações de um produto"""
        self._ensure_authenticated()
        
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        
        try:
            url = f"{self.products_url}/{product_id}"
            response = requests.get(url, headers=headers, timeout=10)
            
            if response.status_code != 200:
                error_detail = response.text
                raise Exception(f"Erro ao buscar produto: {response.status_code} - {error_detail}")
            
            return response.json()
            
        except requests.exceptions.RequestException as e:
            raise Exception(f"Erro de conexão ao buscar produto: {str(e)}")
    
    def test_connection(self):
        """Testa a conexão com a Hotmart"""
        try:
            self.authenticate()
            return {"success": True, "message": "Conexão com Hotmart estabelecida com sucesso!"}
        except Exception as e:
            return {"success": False, "message": str(e)}
