import sqlite3

def migrate():
    conn = sqlite3.connect('vibraface.db')
    cursor = conn.cursor()
    
    try:
        cursor.execute("ALTER TABLE scheduled_videos ADD COLUMN video_url VARCHAR")
        print("Coluna video_url adicionada com sucesso.")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            print("Coluna video_url já existe.")
        else:
            print(f"Erro: {e}")
            
    conn.commit()
    conn.close()

if __name__ == "__main__":
    migrate()
