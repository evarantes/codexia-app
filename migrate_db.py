import os
from sqlalchemy import text
from app.database import engine

def migrate():
    with engine.connect() as conn:
        # Add voice_style column
        try:
            conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN voice_style VARCHAR DEFAULT 'human'"))
            print("Coluna voice_style adicionada.")
        except Exception as e:
            print(f"Erro ao adicionar voice_style (pode já existir): {e}")

        # Add voice_gender column
        try:
            conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN voice_gender VARCHAR DEFAULT 'female'"))
            print("Coluna voice_gender adicionada.")
        except Exception as e:
            print(f"Erro ao adicionar voice_gender (pode já existir): {e}")

        # Add Hotmart columns to settings
        hotmart_columns = [
            ("hotmart_client_id", "VARCHAR"),
            ("hotmart_client_secret", "VARCHAR"),
            ("hotmart_access_token", "VARCHAR"),
            ("hotmart_token_expires_at", "TIMESTAMP") # Use TIMESTAMP for Postgres compatibility
        ]
        
        for col_name, col_type in hotmart_columns:
            try:
                # Check if column exists (PostgreSQL specific way, but simple try/catch works for both usually if we just try to add)
                # For better cross-db compatibility (SQLite/Postgres), simple ALTER TABLE is safest in try/catch
                conn.execute(text(f"ALTER TABLE settings ADD COLUMN {col_name} {col_type}"))
                print(f"Coluna {col_name} adicionada.")
            except Exception as e:
                # PostgreSQL error code for duplicate column is 42701, but we catch generic
                print(f"Nota: Coluna {col_name} não adicionada (provavelmente já existe). Erro: {e}")
            
        conn.commit()

if __name__ == "__main__":
    migrate()
