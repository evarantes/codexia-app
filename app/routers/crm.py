from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Lead, Book
from pydantic import BaseModel
from typing import List
from datetime import datetime

router = APIRouter(prefix="/crm", tags=["CRM"])

# Mocks para demonstração, já que não temos tabelas de Vendas/Clientes completas ainda
# Vamos usar a tabela 'Lead' como base de clientes por enquanto, ou simular.
# Na verdade, o frontend espera 'customers' e 'sales'. 
# Vamos criar endpoints que retornam dados simulados baseados no que temos ou dados hardcoded se o DB estiver vazio.

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
    # Simulação: retornar leads convertidos ou dados fictícios se vazio
    # Para demonstração rápida, vamos criar alguns dados na memória se o DB estiver vazio
    customers = []
    # Aqui idealmente buscaríamos de uma tabela 'Customer' ou 'Order'
    # Como não criamos tabela Customer explícita (temos Lead), vamos mockar um pouco para a UI funcionar
    
    # Mock data para teste imediato do usuário
    if not customers:
        customers = [
            {"id": 1, "name": "Maria Silva", "email": "maria@email.com", "created_at": datetime.now()},
            {"id": 2, "name": "João Souza", "email": "joao@email.com", "created_at": datetime.now()},
        ]
    return customers

@router.get("/sales", response_model=List[SaleOut])
def get_sales(db: Session = Depends(get_db)):
    # Mock data
    return [
        {"id": 1, "book_title": "A Hora da Virada", "customer_name": "Maria Silva", "amount": 29.90, "date": datetime.now()},
        {"id": 2, "book_title": "A Virada de ANA", "customer_name": "João Souza", "amount": 19.90, "date": datetime.now()},
    ]

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
