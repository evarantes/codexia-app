from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Book
from pydantic import BaseModel
import shutil
import os
import uuid
from pathlib import Path

router = APIRouter(prefix="/books", tags=["Books"])

def get_safe_filename(original_filename: str) -> str:
    extension = Path(original_filename).suffix
    return f"{uuid.uuid4()}{extension}"

@router.post("/")
def create_book(
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
        with open(file_location, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        # Caminho relativo para acesso via web
        file_path = f"/static/books/{safe_filename}"

    cover_image_url = None
    if cover_file:
        cover_dir = "app/static/covers"
        os.makedirs(cover_dir, exist_ok=True)
        safe_covername = get_safe_filename(cover_file.filename)
        cover_location = os.path.join(cover_dir, safe_covername)
        with open(cover_location, "wb") as buffer:
            shutil.copyfileobj(cover_file.file, buffer)
        cover_image_url = f"/static/covers/{safe_covername}"

    db_book = Book(
        title=title,
        author=author,
        synopsis=synopsis,
        price=price,
        payment_link=payment_link,
        file_path=file_path,
        cover_image_url=cover_image_url
    )
    db.add(db_book)
    db.commit()
    db.refresh(db_book)
    return db_book

@router.get("/")
def list_books(db: Session = Depends(get_db)):
    return db.query(Book).all()

@router.put("/{book_id}")
def update_book(
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
        with open(file_location, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        db_book.file_path = f"/static/books/{safe_filename}"

    if cover_file:
        cover_dir = "app/static/covers"
        os.makedirs(cover_dir, exist_ok=True)
        safe_covername = get_safe_filename(cover_file.filename)
        cover_location = os.path.join(cover_dir, safe_covername)
        with open(cover_location, "wb") as buffer:
            shutil.copyfileobj(cover_file.file, buffer)
        db_book.cover_image_url = f"/static/covers/{safe_covername}"

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
