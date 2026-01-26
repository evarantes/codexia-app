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
            
        conn.commit()

if __name__ == "__main__":
    migrate()
