from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Book
from pydantic import BaseModel
import shutil
import os
import uuid
from pathlib import Path

import base64

router = APIRouter(prefix="/books", tags=["Books"])

def get_safe_filename(original_filename: str) -> str:
    extension = Path(original_filename).suffix
    return f"{uuid.uuid4()}{extension}"

@router.post("/")
async def create_book(
    title: str = Form(...),
    author: str = Form(...),
    synopsis: str = Form(...),
    price: float = Form(...),
    payment_link: str = Form("http://link_padrao"),
    file: UploadFile = File(None),
    cover_file: UploadFile = File(None),
    db: Session = Depends(get_db)
):
    file_path = None
    if file:
        upload_dir = "app/static/books"
        os.makedirs(upload_dir, exist_ok=True)
        safe_filename = get_safe_filename(file.filename)
        file_location = os.path.join(upload_dir, safe_filename)
        
        content = await file.read()
        with open(file_location, "wb") as buffer:
            buffer.write(content)
        
        # Caminho relativo para acesso via web
        file_path = f"/static/books/{safe_filename}"

    cover_image_url = None
    cover_image_base64 = None
    if cover_file:
        cover_dir = "app/static/covers"
        os.makedirs(cover_dir, exist_ok=True)
        safe_covername = get_safe_filename(cover_file.filename)
        cover_location = os.path.join(cover_dir, safe_covername)
        
        content = await cover_file.read()
        
        # Save to disk
        with open(cover_location, "wb") as buffer:
            buffer.write(content)
        cover_image_url = f"/static/covers/{safe_covername}"
        
        # Save to Base64
        encoded = base64.b64encode(content).decode("utf-8")
        mime_type = cover_file.content_type or "image/jpeg"
        cover_image_base64 = f"data:{mime_type};base64,{encoded}"
        
        print(f"Cover saved at: {cover_location}, URL: {cover_image_url}")

    db_book = Book(
        title=title,
        author=author,
        synopsis=synopsis,
        price=price,
        payment_link=payment_link,
        file_path=file_path,
        cover_image_url=cover_image_url,
        cover_image_base64=cover_image_base64
    )
    db.add(db_book)
    db.commit()
    db.refresh(db_book)
    print(f"Book created: {db_book.id} - {db_book.title}")
    return db_book

@router.get("/")
def list_books(db: Session = Depends(get_db)):
    books = db.query(Book).all()
    print(f"Listing {len(books)} books")
    return books

@router.put("/{book_id}")
async def update_book(
    book_id: int,
    title: str = Form(...),
    author: str = Form(...),
    synopsis: str = Form(...),
    price: float = Form(...),
    payment_link: str = Form("http://link_padrao"),
    file: UploadFile = File(None),
    cover_file: UploadFile = File(None),
    db: Session = Depends(get_db)
):
    db_book = db.query(Book).filter(Book.id == book_id).first()
    if not db_book:
        raise HTTPException(status_code=404, detail="Book not found")

    db_book.title = title
    db_book.author = author
    db_book.synopsis = synopsis
    db_book.price = price
    db_book.payment_link = payment_link

    if file:
        upload_dir = "app/static/books"
        os.makedirs(upload_dir, exist_ok=True)
        safe_filename = get_safe_filename(file.filename)
        file_location = os.path.join(upload_dir, safe_filename)
        
        content = await file.read()
        with open(file_location, "wb") as buffer:
            buffer.write(content)
        db_book.file_path = f"/static/books/{safe_filename}"

    if cover_file:
        cover_dir = "app/static/covers"
        os.makedirs(cover_dir, exist_ok=True)
        safe_covername = get_safe_filename(cover_file.filename)
        cover_location = os.path.join(cover_dir, safe_covername)
        
        content = await cover_file.read()
        
        with open(cover_location, "wb") as buffer:
            buffer.write(content)
        db_book.cover_image_url = f"/static/covers/{safe_covername}"
        
        # Save to Base64
        encoded = base64.b64encode(content).decode("utf-8")
        mime_type = cover_file.content_type or "image/jpeg"
        db_book.cover_image_base64 = f"data:{mime_type};base64,{encoded}"

    db.commit()
    db.refresh(db_book)
    return db_book

@router.delete("/{book_id}")
def delete_book(book_id: int, db: Session = Depends(get_db)):
    db_book = db.query(Book).filter(Book.id == book_id).first()
    if not db_book:
        raise HTTPException(status_code=404, detail="Book not found")
    
    db.delete(db_book)
    db.commit()
    return {"message": "Book deleted successfully"}
