from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Book
from pydantic import BaseModel
import shutil
import os
import uuid
from pathlib import Path
import json
from fastapi.responses import FileResponse, Response
from app.services.book_assembler import BookAssembler

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
    try:
        books = db.query(Book).all()
        print(f"Listing {len(books)} books from DB")
        return books
    except Exception as e:
        print(f"Error listing books: {e}")
        raise HTTPException(status_code=500, detail="Error fetching books")


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

@router.get("/{book_id}/cover")
def get_book_cover(book_id: int, db: Session = Depends(get_db)):
    """
    Retorna a capa do livro.
    - Se o arquivo existir no disco, retorna direto.
    - Se não existir, retorna usando base64 salvo no banco.
    """
    db_book = db.query(Book).filter(Book.id == book_id).first()
    if not db_book:
        raise HTTPException(status_code=404, detail="Book not found")

    # Prioridade 1: Se temos base64, retorna direto (sempre funciona)
    if db_book.cover_image_base64:
        # Extrai o tipo MIME e os dados base64
        if db_book.cover_image_base64.startswith('data:'):
            # Formato: data:image/jpeg;base64,/9j/4AAQ...
            parts = db_book.cover_image_base64.split(',', 1)
            if len(parts) == 2:
                mime_type = parts[0].split(';')[0].split(':')[1]
                base64_data = parts[1]
                image_bytes = base64.b64decode(base64_data)
                return Response(content=image_bytes, media_type=mime_type)
        else:
            # Assume que é só base64 puro
            try:
                image_bytes = base64.b64decode(db_book.cover_image_base64)
                return Response(content=image_bytes, media_type="image/jpeg")
            except:
                pass

    # Prioridade 2: Tenta arquivo no disco
    if db_book.cover_image_url:
        cover_rel = db_book.cover_image_url.lstrip("/")
        cover_path = os.path.join("app", cover_rel) if cover_rel else None
        
        if cover_path and os.path.exists(cover_path):
            # Determina tipo MIME pela extensão
            ext = os.path.splitext(cover_path)[1].lower()
            media_type = {
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.png': 'image/png',
                '.gif': 'image/gif',
                '.webp': 'image/webp'
            }.get(ext, 'image/jpeg')
            
            return FileResponse(cover_path, media_type=media_type)

    # Se não encontrou nada, retorna 404
    raise HTTPException(status_code=404, detail="Book cover not found")

@router.get("/{book_id}/download")
def download_book(book_id: int, db: Session = Depends(get_db)):
    """
    Faz o download do PDF do livro.
    - Se o arquivo existir no disco, retorna direto.
    - Se não existir (ex: Render reiniciou), tenta REGERAR o PDF a partir do conteúdo salvo em full_text.
    """
    db_book = db.query(Book).filter(Book.id == book_id).first()
    if not db_book:
        raise HTTPException(status_code=404, detail="Book not found")

    # Caminho atual salvo no banco (ex: /static/generated/arquivo.pdf ou /static/books/arquivo.pdf)
    rel_path = (db_book.file_path or "").lstrip("/")
    abs_path = os.path.join("app", rel_path) if rel_path else None

    # Se o arquivo ainda existir, devolve direto
    if abs_path and os.path.exists(abs_path):
        filename = os.path.basename(abs_path)
        return FileResponse(abs_path, media_type="application/pdf", filename=filename)

    # Se não existe arquivo, mas temos conteúdo salvo, tenta regerar
    if not db_book.full_text:
        raise HTTPException(status_code=404, detail="Book file not found and no stored content to regenerate.")

    try:
        sections = json.loads(db_book.full_text)
    except Exception:
        sections = {}

    # Resolve caminho da capa, se existir
    cover_image = None
    if db_book.cover_image_url:
        cover_rel = db_book.cover_image_url.lstrip("/")
        cover_image = os.path.join("app", cover_rel) if cover_rel else None

    # Garante diretório de saída
    output_dir = os.path.join("app", "static", "generated")
    os.makedirs(output_dir, exist_ok=True)

    safe_title = f"book_{db_book.id}"
    output_path = os.path.join(output_dir, f"{safe_title}.pdf")

    assembler = BookAssembler(output_path=output_path)
    book_data = {
        "metadata": {
            "title": db_book.title,
            "author": db_book.author,
        },
        "cover_image": cover_image,
        "sections": sections
    }
    try:
        final_path = assembler.create_book(book_data)
    except Exception as e:
        print(f"Erro ao regerar livro {book_id}: {e}")
        raise HTTPException(status_code=500, detail="Erro ao regerar o PDF do livro.")

    # Atualiza caminho salvo no banco para futuras chamadas
    db_book.file_path = f"/static/generated/{os.path.basename(final_path)}"
    db.commit()

    return FileResponse(final_path, media_type="application/pdf", filename=os.path.basename(final_path))
