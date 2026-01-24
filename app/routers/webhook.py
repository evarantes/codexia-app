from fastapi import APIRouter, Request, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Sale, Customer, Book
from app.services.email_service import EmailService
from app.services.payment import PaymentService
from pydantic import BaseModel
import datetime

router = APIRouter(prefix="/webhook", tags=["Webhook"])

class WebhookPayload(BaseModel):
    action: str
    data: dict

@router.post("/mercadopago")
async def mercadopago_webhook(payload: dict, db: Session = Depends(get_db)):
    """
    Recebe notificações do Mercado Pago.
    Em produção, o MP envia um JSON com 'action' e 'data'.
    """
    print(f"[WEBHOOK] Payload recebido: {payload}")

    # Verifica se é uma notificação de pagamento
    if payload.get("type") == "payment":
        payment_id = payload.get("data", {}).get("id")
        
        # Aqui deveríamos consultar a API do Mercado Pago para pegar os detalhes reais
        # Como estamos simulando ou usando sandbox, vamos simular os dados do cliente
        # se não conseguirmos pegar da API.
        
        # Simulação de dados obtidos do pagamento (Mock)
        # Em produção: payment_info = payment_service.get_payment_info(payment_id)
        
        # MOCK DATA para fins de demonstração imediata
        customer_email = "cliente@exemplo.com"
        customer_name = "Cliente Codexia"
        book_id = 1 # Assumindo o primeiro livro por enquanto
        amount = 29.90
        status = "approved"

        # Verificar se o cliente já existe
        customer = db.query(Customer).filter(Customer.email == customer_email).first()
        if not customer:
            customer = Customer(name=customer_name, email=customer_email)
            db.add(customer)
            db.commit()
            db.refresh(customer)
        
        # Registrar a venda
        existing_sale = db.query(Sale).filter(Sale.payment_id == str(payment_id)).first()
        if not existing_sale:
            sale = Sale(
                customer_id=customer.id,
                book_id=book_id,
                amount=amount,
                status=status,
                payment_id=str(payment_id)
            )
            db.add(sale)
            db.commit()
            
            # Disparar Entrega
            book = db.query(Book).filter(Book.id == book_id).first()
            if book and status == "approved":
                # Gerar link de download
                if book.file_path:
                     download_link = f"http://localhost:8000{book.file_path}"
                else:
                     download_link = f"http://codexia.com/download/{book.id}/secure-token-123"
                
                # Instancia serviço de email sob demanda
                email_service = EmailService()
                email_service.send_delivery_email(customer.email, customer.name, book.title, download_link)

    return {"status": "received"}

@router.post("/simulate-sale")
def simulate_sale(db: Session = Depends(get_db)):
    """
    Endpoint auxiliar para testar o fluxo sem precisar fazer uma compra real no MP.
    """
    # Criar cliente teste
    customer = db.query(Customer).filter(Customer.email == "teste@codexia.com").first()
    if not customer:
        customer = Customer(name="João Leitor", email="teste@codexia.com", phone="11999999999")
        db.add(customer)
        db.commit()
        db.refresh(customer)
    
    # Pegar um livro
    book = db.query(Book).first()
    if not book:
        return {"error": "Nenhum livro cadastrado para simular venda"}

    # Criar venda
    import uuid
    payment_id = str(uuid.uuid4())
    
    sale = Sale(
        customer_id=customer.id,
        book_id=book.id,
        amount=book.price,
        status="approved",
        payment_id=payment_id
    )
    db.add(sale)
    db.commit()

    # Enviar email
    if book.file_path:
         download_link = f"http://localhost:8000{book.file_path}"
    else:
         download_link = f"http://localhost:8000/download/{book.id}"
    email_service.send_delivery_email(customer.email, customer.name, book.title, download_link)

    return {"status": "Venda simulada com sucesso", "customer": customer.name, "book": book.title}
