from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Book, Post
from app.services.ai_generator import AIContentGenerator
from pydantic import BaseModel

router = APIRouter(prefix="/marketing", tags=["Marketing"])

class GenerateAdRequest(BaseModel):
    book_id: int
    style: str = "cliffhanger" # cliffhanger, storytelling, short_video

@router.post("/generate-ad")
def generate_ad(request: GenerateAdRequest, db: Session = Depends(get_db)):
    book = db.query(Book).filter(Book.id == request.book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    
    # Gerar conteúdo usando a IA
    # Instanciar aqui para evitar problemas de inicialização do DB no import
    ai_service = AIContentGenerator()
    ad_content = ai_service.generate_ad_copy(book.title, book.synopsis, request.style)
    
    # Salvar o post como rascunho
    post = Post(
        book_id=book.id,
        content=ad_content,
        post_type=request.style,
        status="draft"
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    
    return post
