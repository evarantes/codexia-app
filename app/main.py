from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.database import engine, Base
from app.routers import books, marketing, settings, video, crm, webhook, youtube, book_factory
from dotenv import load_dotenv
import os

# Carregar variáveis de ambiente
load_dotenv()

# Create tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Codexia API", description="Sua fábrica de conteúdo movida a inteligência")

# Montar arquivos estáticos
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Routers
app.include_router(books.router)
app.include_router(marketing.router)
app.include_router(settings.router)
app.include_router(video.router)
app.include_router(crm.router)
app.include_router(webhook.router)
app.include_router(youtube.router)
app.include_router(book_factory.router)

@app.get("/")
def read_root():
    return FileResponse('app/static/index.html')

@app.get("/success")
def payment_success():
    return {"status": "Pagamento Aprovado! Envie o livro."}

@app.get("/failure")
def payment_failure():
    return {"status": "Pagamento Falhou."}

@app.get("/pending")
def payment_pending():
    return {"status": "Pagamento Pendente."}
