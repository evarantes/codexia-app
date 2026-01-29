"""
Rotas para integração com Hotmart
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Book, Settings
from app.services.hotmart_service import HotmartService
from app.services.ai_generator import AIContentGenerator
from pydantic import BaseModel
from typing import Optional
import json

router = APIRouter(prefix="/hotmart", tags=["Hotmart"])

class HotmartProductRequest(BaseModel):
    book_id: int
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    category: Optional[str] = None
    tags: Optional[list] = None
    custom_fields: Optional[dict] = None

class SyncHotmartFieldsRequest(BaseModel):
    book_id: int
    changed_field: str  # 'name', 'description', 'subtitle', 'price', 'category', 'tags'
    new_value: str
    current_form: dict  # Estado atual do formulário completo

@router.get("/test-connection")
def test_hotmart_connection(db: Session = Depends(get_db)):
    """Testa a conexão com a Hotmart"""
    try:
        service = HotmartService(db=db)
        result = service.test_connection()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/analyze-book/{book_id}")
def analyze_book_for_hotmart(book_id: int, db: Session = Depends(get_db)):
    """
    Analisa um livro e gera sugestões de configuração para publicação na Hotmart
    usando IA para otimizar título, descrição, preço, categoria, tags, etc.
    """
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Livro não encontrado")
    
    try:
        ai = AIContentGenerator()
        
        # Prepara dados do livro para análise
        book_data = {
            "title": book.title,
            "author": book.author,
            "synopsis": book.synopsis or "",
            "price": book.price or 0,
        }
        
        # Se tiver conteúdo completo, inclui informações dos capítulos
        if book.full_text:
            try:
                sections = json.loads(book.full_text)
                if isinstance(sections, dict):
                    book_data["chapters"] = list(sections.keys())
                    book_data["content_summary"] = str(sections)[:500]  # Primeiros 500 chars
            except:
                pass
        
        # Gera sugestões com IA
        suggestions = ai.generate_hotmart_suggestions(book_data)
        
        return {
            "book": {
                "id": book.id,
                "title": book.title,
                "author": book.author,
                "synopsis": book.synopsis,
                "price": book.price,
            },
            "suggestions": suggestions
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao analisar livro: {str(e)}")

@router.post("/publish")
def publish_book_to_hotmart(request: HotmartProductRequest, db: Session = Depends(get_db)):
    """
    Publica um livro na Hotmart como produto
    """
    book = db.query(Book).filter(Book.id == request.book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Livro não encontrado")
    
    try:
        service = HotmartService(db=db)
        
        # Prepara dados do produto para a Hotmart
        product_data = {
            "name": request.name or book.title,
            "description": request.description or book.synopsis or "",
            "price": request.price or book.price or 0,
        }
        
        # Adiciona campos opcionais se fornecidos
        if request.category:
            product_data["category"] = request.category
        if request.tags:
            product_data["tags"] = request.tags
        if request.custom_fields:
            product_data.update(request.custom_fields)
        
        # Cria o produto na Hotmart
        result = service.create_product(product_data)
        
        # Atualiza o livro com o link da Hotmart (API pode retornar "id" ou "product_id")
        product_id = result.get("product_id") or result.get("id")
        if product_id:
            book.payment_link = f"https://hotmart.com/pt-br/marketplace/produtos/{product_id}"
            db.commit()
        
        payment_link = book.payment_link if product_id else None
        return {
            "success": True,
            "message": "Livro publicado na Hotmart com sucesso!",
            "product": result,
            "payment_link": payment_link
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/sync-fields")
def sync_hotmart_fields(request: SyncHotmartFieldsRequest, db: Session = Depends(get_db)):
    """
    Sincroniza campos relacionados quando o usuário altera manualmente um campo.
    Regenera campos dependentes usando IA para manter consistência.
    """
    book = db.query(Book).filter(Book.id == request.book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Livro não encontrado")
    
    try:
        ai = AIContentGenerator()
        
        # Prepara dados do livro + alteração manual
        book_data = {
            "title": request.current_form.get("name") or book.title,
            "author": book.author,
            "synopsis": request.current_form.get("description") or book.synopsis or "",
            "price": request.current_form.get("price") or book.price or 0,
        }
        
        # Gera sugestões atualizadas com base na alteração manual
        # A IA vai usar o valor alterado como referência principal
        updated_suggestions = ai.generate_hotmart_suggestions_sync(
            book_data=book_data,
            changed_field=request.changed_field,
            new_value=request.new_value,
            current_form=request.current_form
        )
        
        return {
            "success": True,
            "updated_fields": updated_suggestions
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao sincronizar campos: {str(e)}")

@router.get("/products")
def list_hotmart_products(db: Session = Depends(get_db)):
    """
    Lista produtos criados na Hotmart (se a API suportar)
    """
    try:
        service = HotmartService(db=db)
        # Nota: A API da Hotmart pode não ter endpoint de listagem
        # Isso depende da documentação oficial
        return {"message": "Funcionalidade de listagem ainda não implementada"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
