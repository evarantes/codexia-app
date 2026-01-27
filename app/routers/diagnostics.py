from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import get_db
from app.models import Settings
from app.services.ai_generator import AIContentGenerator
import os
import requests

router = APIRouter(prefix="/diagnostics", tags=["System Diagnostics"])

@router.get("/run")
def run_diagnostics(db: Session = Depends(get_db)):
    report = {
        "status": "healthy",
        "checks": []
    }
    
    # 1. Database Check
    try:
        db.execute(text("SELECT 1"))
        report["checks"].append({"name": "Database", "status": "OK", "message": "Connected successfully"})
    except Exception as e:
        report["status"] = "degraded"
        report["checks"].append({"name": "Database", "status": "FAIL", "message": str(e)})

    # 2. File System Permissions
    directories = [
        "app/static/generated",
        "app/static/covers",
        "app/static/videos",
        "app/static/temp_uploads"
    ]
    for directory in directories:
        if not os.path.exists(directory):
            try:
                os.makedirs(directory, exist_ok=True)
                report["checks"].append({"name": f"Dir: {directory}", "status": "OK", "message": "Created successfully"})
            except Exception as e:
                report["status"] = "degraded"
                report["checks"].append({"name": f"Dir: {directory}", "status": "FAIL", "message": f"Missing and cannot create: {e}"})
        else:
            if os.access(directory, os.W_OK):
                report["checks"].append({"name": f"Dir: {directory}", "status": "OK", "message": "Writable"})
            else:
                report["status"] = "degraded"
                report["checks"].append({"name": f"Dir: {directory}", "status": "FAIL", "message": "Not writable"})

    # 3. AI Service Configuration
    settings = db.query(Settings).first()
    
    # Determine provider
    provider = "openai" # Default
    if settings and settings.ai_provider:
        provider = settings.ai_provider
        
    # Check keys based on provider
    key_found = False
    details = ""
    
    known_providers = {
        "openai": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "groq": "GROQ_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "openrouter": "OPENROUTER_API_KEY"
    }
    
    if provider == "hybrid":
        # Check if ANY key is present
        found_providers = []
        for p, env_var in known_providers.items():
            # Check DB
            db_key = getattr(settings, f"{p}_api_key", None) if settings else None
            # Check Env
            env_key = os.getenv(env_var)
            
            if db_key or env_key:
                found_providers.append(p)
                
        if found_providers:
            key_found = True
            details = f"Hybrid (Found: {', '.join(found_providers)})"
        else:
            details = "Hybrid (No keys found)"
            
    else:
        # Check specific provider key
        env_var = known_providers.get(provider, f"{provider.upper()}_API_KEY")
        
        # Check DB
        db_key = getattr(settings, f"{provider}_api_key", None) if settings else None
        # Check Env
        env_key = os.getenv(env_var)
        
        if db_key or env_key:
            key_found = True
            details = f"Provider: {provider}, Key present"
        else:
            details = f"Provider: {provider}, Key missing"

    if key_found:
        report["checks"].append({"name": "AI Configuration", "status": "OK", "message": details})
    else:
        report["status"] = "degraded"
        report["checks"].append({"name": "AI Configuration", "status": "FAIL", "message": details})

    # 4. Critical Dependencies (FFmpeg)
    # This is a bit OS specific, but we can try running it
    import shutil
    if shutil.which("ffmpeg"):
        report["checks"].append({"name": "FFmpeg", "status": "OK", "message": "Installed"})
    else:
        report["status"] = "degraded"
        report["checks"].append({"name": "FFmpeg", "status": "FAIL", "message": "Not found in PATH (Video generation will fail)"})

    return report

@router.post("/test-ai-connection")
def test_ai_connection(db: Session = Depends(get_db)):
    """
    Tests the AI connection by generating a simple text.
    """
    try:
        ai_service = AIContentGenerator()
        response = ai_service.generate_completion("Say 'Hello World'", system_message="You are a test bot.")
        if "Hello" in response or "World" in response:
             return {"status": "success", "response": response}
        else:
             raise HTTPException(status_code=500, detail="AI returned unexpected response")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI Connection Failed: {str(e)}")

@router.post("/test-pdf-generation")
def test_pdf_generation():
    """
    Tests the PDF generation capability.
    """
    try:
        from app.services.book_assembler import BookAssembler
        output_path = os.path.join("app", "static", "generated", "test_diagnostic.pdf")
        assembler = BookAssembler(output_path=output_path)
        
        book_data = {
            "metadata": {"title": "Test Book", "author": "System"},
            "sections": {
                "pre_textual": {"title_page": True},
                "textual": [{"title": "Chapter 1", "content": "This is a test."}],
                "post_textual": {}
            }
        }
        assembler.create_book(book_data)
        
        if os.path.exists(output_path):
             return {"status": "success", "url": "/static/generated/test_diagnostic.pdf"}
        else:
             raise HTTPException(status_code=500, detail="PDF file was not created.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF Generation Failed: {str(e)}")
