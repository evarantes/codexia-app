from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Book, Post
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/marketing", tags=["Marketing"])

class GenerateAdRequest(BaseModel):
    book_id: int
    style: str = "cliffhanger"

class PostToFacebookRequest(BaseModel):
    content: str
    book_id: Optional[int] = None

@router.post("/generate-ad")
def generate_ad(request: GenerateAdRequest, db: Session = Depends(get_db)):
    book = db.query(Book).filter(Book.id == request.book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    
    from app.services.ai_generator import AIContentGenerator
    ai_service = AIContentGenerator()
    ad_content = ai_service.generate_ad_copy(book.title, book.synopsis, request.style)
    
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


@router.post("/post-to-facebook")
def post_to_facebook(request: PostToFacebookRequest, db: Session = Depends(get_db)):
    """Publica conteúdo de marketing na fanpage do Facebook."""
    from app.services.facebook_api import FacebookService

    link = None
    if request.book_id:
        book = db.query(Book).filter(Book.id == request.book_id).first()
        if book and book.payment_link:
            link = book.payment_link

    fb = FacebookService()
    result = fb.post_to_feed(request.content, link=link)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result
