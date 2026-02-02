from app.database import SessionLocal, engine
from app.models import Settings
from sqlalchemy import text, inspect

def run_migrations():
    inspector = inspect(engine)
    if "settings" in inspector.get_table_names():
        columns = [c["name"] for c in inspector.get_columns("settings")]
        with engine.connect() as conn:
            if "suno_api_key" not in columns:
                print("Migrating: Adding suno_api_key to settings...")
                conn.execute(text("ALTER TABLE settings ADD COLUMN suno_api_key TEXT"))
                conn.commit()
            
            # Check for other potential missing columns
            if "gemini_api_key" not in columns:
                conn.execute(text("ALTER TABLE settings ADD COLUMN gemini_api_key TEXT"))
            if "deepseek_api_key" not in columns:
                conn.execute(text("ALTER TABLE settings ADD COLUMN deepseek_api_key TEXT"))
            if "groq_api_key" not in columns:
                conn.execute(text("ALTER TABLE settings ADD COLUMN groq_api_key TEXT"))
            if "anthropic_api_key" not in columns:
                conn.execute(text("ALTER TABLE settings ADD COLUMN anthropic_api_key TEXT"))
            if "mistral_api_key" not in columns:
                conn.execute(text("ALTER TABLE settings ADD COLUMN mistral_api_key TEXT"))
            if "openrouter_api_key" not in columns:
                conn.execute(text("ALTER TABLE settings ADD COLUMN openrouter_api_key TEXT"))
            if "hotmart_client_id" not in columns:
                conn.execute(text("ALTER TABLE settings ADD COLUMN hotmart_client_id TEXT"))
            if "hotmart_client_secret" not in columns:
                conn.execute(text("ALTER TABLE settings ADD COLUMN hotmart_client_secret TEXT"))
            if "hotmart_access_token" not in columns:
                conn.execute(text("ALTER TABLE settings ADD COLUMN hotmart_access_token TEXT"))
            if "hotmart_token_expires_at" not in columns:
                conn.execute(text("ALTER TABLE settings ADD COLUMN hotmart_token_expires_at TIMESTAMP"))
            conn.commit()

def check_creds():
    run_migrations()
    db = SessionLocal()
    try:
        settings = db.query(Settings).first()
        if not settings:
            print("No settings record found!")
            return

        print("-" * 30)
        print("SETTINGS CHECK")
        print("-" * 30)
        print(f"Client ID:      {'[PRESENT]' if settings.youtube_client_id else '[MISSING]'}")
        if settings.youtube_client_id:
            print(f"  Value (prefix): {settings.youtube_client_id[:10]}...")
            
        print(f"Client Secret:  {'[PRESENT]' if settings.youtube_client_secret else '[MISSING]'}")
        
        print(f"Refresh Token:  {'[PRESENT]' if settings.youtube_refresh_token else '[MISSING]'}")
        if settings.youtube_refresh_token:
            print(f"  Value (prefix): {settings.youtube_refresh_token[:10]}...")
            print(f"  Length: {len(settings.youtube_refresh_token)}")
            
        print("-" * 30)
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    check_creds()
