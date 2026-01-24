from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Lead, Book, Customer, Sale
from pydantic import BaseModel
from typing import List
from datetime import datetime

router = APIRouter(prefix="/crm", tags=["CRM"])

# Endpoints reais consultando o banco de dados.

class CustomerOut(BaseModel):
    id: int
    name: str
    email: str
    created_at: datetime

class SaleOut(BaseModel):
    id: int
    book_title: str
    customer_name: str
    amount: float
    date: datetime

@router.get("/customers", response_model=List[CustomerOut])
def get_customers(db: Session = Depends(get_db)):
    return db.query(Customer).all()

@router.get("/sales", response_model=List[SaleOut])
def get_sales(db: Session = Depends(get_db)):
    sales = db.query(Sale).all()
    result = []
    for sale in sales:
        result.append({
            "id": sale.id,
            "book_title": sale.book.title if sale.book else "Livro Removido",
            "customer_name": sale.customer.name if sale.customer else "Cliente Desconhecido",
            "amount": sale.amount,
            "date": sale.created_at
        })
    return result

@router.post("/remarketing")
def create_remarketing_campaign():
    # Simula envio de emails
    return {"message": "Campanha de Remarketing enviada para 2 clientes com sucesso!", "count": 2}

@router.get("/history/{customer_id}")
def get_customer_history(customer_id: int):
    # Simula histórico
    return {
        "customer_id": customer_id,
        "history": [
            {"date": "2023-10-01", "action": "Comprou 'A Hora da Virada'"},
            {"date": "2023-10-05", "action": "Abriu email de boas-vindas"}
        ]
    }

@router.post("/send-offer/{customer_id}")
def send_offer(customer_id: int):
    return {"message": f"Cupom de 20% enviado para o cliente {customer_id}!"}
