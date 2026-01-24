import mercadopago
import os
from app.database import SessionLocal
from app.models import Settings

class PaymentService:
    def __init__(self):
        # Configuração será carregada sob demanda
        self.access_token = None
        self.sdk = None

    def _load_config(self):
        db = SessionLocal()
        settings = db.query(Settings).first()
        db.close()

        self.access_token = None
        if settings:
            self.access_token = settings.mercadopago_access_token
        
        if not self.access_token:
            self.access_token = os.getenv("MERCADO_PAGO_ACCESS_TOKEN")

        if self.access_token and self.access_token != "seu_token_mercado_pago":
            self.sdk = mercadopago.SDK(self.access_token)
        else:
            self.sdk = None

    def create_payment_link(self, title, price, description="Livro"):
        """
        Gera um link de pagamento real no Mercado Pago ou um mock.
        """
        self._load_config() # Reload config

        if not self.sdk:
            print("[MERCADO PAGO MOCK] Token não configurado. Gerando link simulado.")
            return f"https://www.mercadopago.com.br/checkout/mock/{title.replace(' ', '_')}"

        preference_data = {
            "items": [
                {
                    "title": title,
                    "quantity": 1,
                    "unit_price": float(price),
                    "currency_id": "BRL",
                    "description": description
                }
            ],
            "back_urls": {
                "success": "http://localhost:8000/success",
                "failure": "http://localhost:8000/failure",
                "pending": "http://localhost:8000/pending"
            },
            "auto_return": "approved"
        }

        try:
            preference_response = self.sdk.preference().create(preference_data)
            preference = preference_response["response"]
            return preference["init_point"] # Link de pagamento (sandbox ou produção)
        except Exception as e:
            print(f"Erro ao criar preferência no Mercado Pago: {e}")
            return None
