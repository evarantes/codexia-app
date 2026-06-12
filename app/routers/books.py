from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form
from sqlalchemy.orm import Session
from app.database import get_db
from app.database import SessionLocal
from app.models import Book, User
from pydantic import BaseModel
import shutil
import os
import uuid
from pathlib import Path
import json
from fastapi.responses import FileResponse, Response
from app.services.book_assembler import BookAssembler
from app.config import BOOKS_OUTPUT_DIR, COVERS_OUTPUT_DIR, STATIC_DIR

import base64
import threading
from datetime import datetime

from app.routers.auth import get_current_user
from app.services.task_manager import create_task, is_task_cancel_requested, update_task

router = APIRouter(prefix="/books", tags=["Books"])

def get_safe_filename(original_filename: str) -> str:
    extension = Path(original_filename).suffix
    return f"{uuid.uuid4()}{extension}"

def _parse_price(value) -> float:
    if value is None:
        raise HTTPException(status_code=422, detail="Preço é obrigatório.")
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        raise HTTPException(status_code=422, detail="Preço é obrigatório.")
    s = s.replace("R$", "").replace(" ", "").replace("\u00a0", "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        raise HTTPException(status_code=422, detail=f"Preço inválido: {value}")

def _resolve_book_file_path(url_path: str) -> str:
    raw = (url_path or "").strip().split("?", 1)[0].split("#", 1)[0]
    clean = raw.lstrip("/")
    if not clean:
        return ""
    if clean.startswith("static/books/"):
        safe_name = os.path.basename(clean).strip()
        if not safe_name:
            return ""
        for directory in (BOOKS_OUTPUT_DIR, str(STATIC_DIR / "books"), os.path.join("app", "static", "books")):
            if directory:
                candidate = os.path.join(directory, safe_name)
                if os.path.isfile(candidate):
                    return candidate
        return ""
    candidate = os.path.join("app", clean.replace("/", os.sep))
    if os.path.exists(candidate):
        return candidate
    return ""

def _guess_book_media_type(filename: str) -> str:
    ext = (Path(filename or "").suffix or "").lower()
    if ext == ".pdf":
        return "application/pdf"
    if ext == ".epub":
        return "application/epub+zip"
    if ext == ".mobi":
        return "application/x-mobipocket-ebook"
    if ext == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if ext == ".doc":
        return "application/msword"
    return "application/octet-stream"

def _book_file_status(book: Book) -> tuple[bool, bool, str, str]:
    has_disk_file = bool(_resolve_book_file_path(str(getattr(book, "file_path", "") or "")))
    has_backup = bool(str(getattr(book, "file_base64", "") or "").strip())
    if has_disk_file:
        return True, has_backup, "stored", "Arquivo salvo no servidor"
    if has_backup:
        return False, True, "backup_only", "Arquivo salvo em backup"
    return False, False, "missing", "Arquivo ausente"

def _normalized_amazon_error(book: Book, has_disk_file: bool, has_backup: bool) -> str | None:
    err = str(getattr(book, "amazon_last_error", "") or "").strip()
    if not err:
        return None
    lower_err = err.lower()
    if (has_disk_file or has_backup) and "arquivo do livro não encontrado no servidor" in lower_err:
        return None
    return err

def _serialize_book(book: Book) -> dict:
    has_disk_file, has_backup, storage_status, storage_label = _book_file_status(book)
    amazon_error = _normalized_amazon_error(book, has_disk_file, has_backup)
    return {
        "id": book.id,
        "user_id": book.user_id,
        "title": book.title,
        "author": book.author,
        "synopsis": book.synopsis,
        "full_text": book.full_text,
        "price": book.price,
        "payment_link": book.payment_link,
        "cover_image_url": book.cover_image_url,
        "cover_image_base64": book.cover_image_base64,
        "file_path": book.file_path,
        "file_original_name": getattr(book, "file_original_name", None),
        "file_mime_type": getattr(book, "file_mime_type", None),
        "has_file_on_disk": has_disk_file,
        "has_file_backup": has_backup,
        "file_storage_status": storage_status,
        "file_storage_label": storage_label,
        "status_amazon": book.status_amazon,
        "amazon_task_id": book.amazon_task_id,
        "amazon_last_error": amazon_error,
        "amazon_updated_at": book.amazon_updated_at.isoformat() if book.amazon_updated_at else None,
    }

def _store_book_file_content(content: bytes, original_name: str, content_type: str | None) -> tuple[str, str]:
    safe_filename = get_safe_filename(original_name or "livro.pdf")
    file_location = os.path.join(BOOKS_OUTPUT_DIR, safe_filename)
    try:
        os.makedirs(BOOKS_OUTPUT_DIR, exist_ok=True)
        with open(file_location, "wb") as buffer:
            buffer.write(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha ao salvar arquivo do livro no servidor: {e}")
    return safe_filename, file_location

def _materialize_book_file(book: Book, preferred_dir: str | None = None, preferred_name: str | None = None) -> str:
    existing = _resolve_book_file_path(str(getattr(book, "file_path", "") or ""))
    if existing and os.path.isfile(existing):
        return existing
    raw_b64 = str(getattr(book, "file_base64", "") or "").strip()
    if not raw_b64:
        return ""
    file_name = os.path.basename(str(preferred_name or getattr(book, "file_original_name", "") or getattr(book, "title", "livro")).strip()) or f"book_{int(getattr(book, 'id', 0) or 0)}.pdf"
    out_dir = preferred_dir or BOOKS_OUTPUT_DIR
    out_path = os.path.join(out_dir, file_name)
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(base64.b64decode(raw_b64))
        return out_path if os.path.isfile(out_path) else ""
    except Exception:
        return ""

@router.post("/")
async def create_book(
    title: str = Form(...),
    author: str = Form(...),
    synopsis: str = Form(""),
    price: str = Form(...),
    payment_link: str = Form("http://link_padrao"),
    file: UploadFile = File(None),
    cover_file: UploadFile = File(None),
    db: Session = Depends(get_db)
):
    price_value = _parse_price(price)
    file_path = None
    file_base64 = None
    file_original_name = None
    file_mime_type = None
    if file:
        content = await file.read()
        safe_filename, _ = _store_book_file_content(content, file.filename, file.content_type)
        file_path = f"/static/books/{safe_filename}"
        file_base64 = base64.b64encode(content).decode("utf-8")
        file_original_name = (file.filename or "").strip() or safe_filename
        file_mime_type = file.content_type or _guess_book_media_type(file_original_name)

    cover_image_url = None
    cover_image_base64 = None
    if cover_file:
        cover_dir = COVERS_OUTPUT_DIR
        try:
            os.makedirs(cover_dir, exist_ok=True)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Falha ao preparar diretório de upload da capa: {e}")
        safe_covername = get_safe_filename(cover_file.filename)
        cover_location = os.path.join(cover_dir, safe_covername)
        
        content = await cover_file.read()
        
        # Save to disk
        try:
            with open(cover_location, "wb") as buffer:
                buffer.write(content)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Falha ao salvar capa no servidor: {e}")
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
        price=price_value,
        payment_link=payment_link,
        file_path=file_path,
        file_base64=file_base64,
        file_original_name=file_original_name,
        file_mime_type=file_mime_type,
        cover_image_url=cover_image_url,
        cover_image_base64=cover_image_base64
    )
    try:
        db.add(db_book)
        db.commit()
        db.refresh(db_book)
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Erro ao salvar livro no banco: {e}")
    print(f"Book created: {db_book.id} - {db_book.title}")
    return db_book

@router.get("/")
def list_books(db: Session = Depends(get_db)):
    try:
        books = db.query(Book).all()
        print(f"Listing {len(books)} books from DB")
        return [_serialize_book(book) for book in books]
    except Exception as e:
        print(f"Error listing books: {e}")
        raise HTTPException(status_code=500, detail="Error fetching books")


@router.put("/{book_id}")
async def update_book(
    book_id: int,
    title: str = Form(...),
    author: str = Form(...),
    synopsis: str = Form(""),
    price: str = Form(...),
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
    db_book.price = _parse_price(price)
    db_book.payment_link = payment_link

    if file:
        content = await file.read()
        safe_filename, _ = _store_book_file_content(content, file.filename, file.content_type)
        db_book.file_path = f"/static/books/{safe_filename}"
        db_book.file_base64 = base64.b64encode(content).decode("utf-8")
        db_book.file_original_name = (file.filename or "").strip() or safe_filename
        db_book.file_mime_type = file.content_type or _guess_book_media_type(db_book.file_original_name)
        if "arquivo do livro não encontrado no servidor" in str(getattr(db_book, "amazon_last_error", "") or "").lower():
            db_book.amazon_last_error = None
            if str(getattr(db_book, "status_amazon", "") or "").lower() == "failed":
                db_book.status_amazon = None

    if cover_file:
        cover_dir = COVERS_OUTPUT_DIR
        try:
            os.makedirs(cover_dir, exist_ok=True)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Falha ao preparar diretório de upload da capa: {e}")
        safe_covername = get_safe_filename(cover_file.filename)
        cover_location = os.path.join(cover_dir, safe_covername)
        
        content = await cover_file.read()
        
        try:
            with open(cover_location, "wb") as buffer:
                buffer.write(content)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Falha ao salvar capa no servidor: {e}")
        db_book.cover_image_url = f"/static/covers/{safe_covername}"
        
        # Save to Base64
        encoded = base64.b64encode(content).decode("utf-8")
        mime_type = cover_file.content_type or "image/jpeg"
        db_book.cover_image_base64 = f"data:{mime_type};base64,{encoded}"

    try:
        db.commit()
        db.refresh(db_book)
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Erro ao atualizar livro no banco: {e}")
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
    abs_path = _materialize_book_file(db_book)

    # Se o arquivo ainda existir, devolve direto
    if abs_path and os.path.exists(abs_path):
        filename = os.path.basename(abs_path)
        media_type = getattr(db_book, "file_mime_type", None) or _guess_book_media_type(filename)
        download_name = getattr(db_book, "file_original_name", None) or filename
        return FileResponse(abs_path, media_type=media_type, filename=download_name)

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


class PublishKdpRequest(BaseModel):
    subtitle: str = ""
    author: str = "E.MA"
    description: str = ""
    keywords: str = ""
    price: str = ""
    headless: bool = True


def _resolve_local_path(p: str) -> str:
    raw = (p or "").strip()
    if not raw:
        return ""
    if os.path.isabs(raw) and os.path.exists(raw):
        return raw
    if (raw.startswith("app" + os.sep) or raw.startswith("app/") or raw.startswith("app\\")) and os.path.exists(raw):
        return raw
    clean = raw.lstrip("/")
    norm = clean.replace("\\", "/")
    if norm.startswith("app/"):
        candidate = os.path.join(*norm.split("/"))
        if os.path.exists(candidate):
            return candidate
    if norm.startswith("static/") or norm.startswith("uploads/"):
        candidate = os.path.join("app", *norm.split("/"))
        if os.path.exists(candidate):
            return candidate
    candidate = os.path.join("app", *norm.split("/"))
    if os.path.exists(candidate):
        return candidate
    return ""


def _safe_keywords_from_text(title: str, synopsis: str) -> str:
    base = f"{title or ''} {synopsis or ''}".strip().lower()
    if not base:
        return ""
    words = []
    for token in base.replace("\n", " ").replace("\t", " ").split(" "):
        w = "".join([c for c in token if c.isalnum() or c in ("-", "_")]).strip()
        if len(w) < 3:
            continue
        if w not in words:
            words.append(w)
        if len(words) >= 7:
            break
    return ", ".join(words)


def _write_base64_image_to_file(data_url: str, out_path: str) -> str:
    raw = (data_url or "").strip()
    if not raw:
        return ""
    mime = "image/png"
    b64 = raw
    if raw.startswith("data:"):
        try:
            header, b64 = raw.split(",", 1)
            if ";" in header and ":" in header:
                mime = header.split(";", 1)[0].split(":", 1)[1] or mime
        except Exception:
            b64 = raw
    try:
        payload = base64.b64decode(b64)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(payload)
        if os.path.exists(out_path):
            return out_path
    except Exception:
        return ""
    return ""


def _ensure_pdf_for_book(book: Book, out_path: str) -> str:
    if not book or not book.full_text:
        return ""
    try:
        sections = json.loads(book.full_text)
    except Exception:
        sections = {}
    cover_image = ""
    if getattr(book, "cover_image_url", None):
        cover_image = _resolve_local_path(str(book.cover_image_url))
    assembler = BookAssembler(output_path=out_path)
    book_data = {
        "metadata": {"title": book.title, "author": book.author},
        "cover_image": cover_image or None,
        "sections": sections,
    }
    try:
        final_path = assembler.create_book(book_data)
        return final_path if os.path.exists(final_path) else ""
    except Exception:
        return ""


@router.post("/{book_id}/publish/kdp")
def publish_book_kdp(
    book_id: int,
    request: PublishKdpRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    book = db.query(Book).filter(Book.id == int(book_id)).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    if book.user_id and book.user_id != user.id and not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Sem permissão para publicar este livro.")

    task_id = create_task(user_id=user.id)
    now = datetime.utcnow()
    try:
        book.status_amazon = "processing"
        book.amazon_task_id = task_id
        book.amazon_last_error = None
        book.amazon_updated_at = now
        db.commit()
    except Exception:
        db.rollback()

    update_task(
        task_id,
        status="processing",
        progress=1,
        message="Preparando arquivos para publicação...",
        result={"kind": "ebook_publish", "book_id": int(book.id), "target": "kdp"},
    )

    subtitle = (request.subtitle or "").strip()
    author = (request.author or "").strip() or "E.MA"
    description = (request.description or "").strip() or (book.synopsis or "").strip()
    keywords = (request.keywords or "").strip() or _safe_keywords_from_text(book.title or "", book.synopsis or "")
    price = (request.price or "").strip()
    if not price and getattr(book, "price", None) is not None:
        try:
            price = f"{float(book.price):.2f}"
        except Exception:
            price = ""
    headless = bool(request.headless)

    def _run():
        dbx = SessionLocal()
        try:
            b = dbx.query(Book).filter(Book.id == int(book_id)).first()
            if not b:
                update_task(task_id, status="failed", progress=0, message="Livro não encontrado no servidor.")
                return
            if b.user_id and b.user_id != user.id and not getattr(user, "is_admin", False):
                update_task(task_id, status="failed", progress=0, message="Sem permissão para publicar este livro.")
                return
            if is_task_cancel_requested(task_id):
                update_task(task_id, status="cancelled", progress=0, message="Cancelado pelo usuário.")
                return

            update_task(task_id, status="processing", progress=10, message="Resolvendo arquivo do livro e capa...")
            book_abs = _resolve_local_path(str(getattr(b, "file_path", "") or ""))
            cover_abs = _resolve_local_path(str(getattr(b, "cover_image_url", "") or ""))

            if not cover_abs and getattr(b, "cover_image_base64", None):
                img_out = os.path.join("generated_assets", "distribution_logs", str(task_id), "cover.png")
                cover_abs = _write_base64_image_to_file(str(b.cover_image_base64), img_out)

            if not book_abs:
                preferred_name = os.path.basename(str(getattr(b, "file_original_name", "") or "")) or f"book_{int(b.id)}.pdf"
                book_abs = _materialize_book_file(
                    b,
                    preferred_dir=os.path.join("generated_assets", "distribution_logs", str(task_id)),
                    preferred_name=preferred_name,
                )

            if not book_abs:
                pdf_out = os.path.join("generated_assets", "distribution_logs", str(task_id), f"book_{int(b.id)}.pdf")
                book_abs = _ensure_pdf_for_book(b, pdf_out)
                if book_abs:
                    try:
                        b.file_path = f"/generated_assets/distribution_logs/{str(task_id)}/{os.path.basename(book_abs)}"
                        dbx.commit()
                    except Exception:
                        dbx.rollback()

            if not book_abs or not os.path.isfile(book_abs):
                raise Exception("Arquivo do livro não encontrado no servidor.")
            if not cover_abs or not os.path.isfile(cover_abs):
                raise Exception("Arquivo da capa não encontrado no servidor.")

            update_task(task_id, status="processing", progress=25, message="Iniciando automação no navegador...")
            from app.services.distribution_automation import publish_ebook_kdp_via_browser

            res = publish_ebook_kdp_via_browser(
                task_id=task_id,
                book_file_path=book_abs,
                cover_file_path=cover_abs,
                title=(b.title or "Livro"),
                subtitle=subtitle,
                author=author,
                description=description,
                keywords=keywords,
                price=(price or None),
                headless=headless,
            )

            try:
                b.status_amazon = "published"
                b.amazon_last_error = None
                b.amazon_updated_at = datetime.utcnow()
                dbx.commit()
            except Exception:
                dbx.rollback()

            update_task(
                task_id,
                status="completed",
                progress=100,
                message="Publicação enviada.",
                result={
                    "kind": "ebook_publish",
                    "book_id": int(b.id),
                    "target": "kdp",
                    "automation_result": res,
                    "logs_url": f"/generated_assets/distribution_logs/{str(task_id)}/",
                },
            )
        except Exception as e:
            try:
                b2 = dbx.query(Book).filter(Book.id == int(book_id)).first()
                if b2:
                    b2.status_amazon = "failed"
                    b2.amazon_last_error = str(e)
                    b2.amazon_updated_at = datetime.utcnow()
                    dbx.commit()
            except Exception:
                dbx.rollback()
            update_task(
                task_id,
                status="failed",
                progress=0,
                message=f"Falha ao publicar: {e}",
                result={"kind": "ebook_publish", "book_id": int(book_id), "target": "kdp"},
            )
        finally:
            try:
                dbx.close()
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()
    return {"task_id": task_id, "status": "processing"}
