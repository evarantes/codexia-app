from app.database import SessionLocal
from app.models import Settings

db = SessionLocal()
settings = db.query(Settings).first()

if settings:
    print(f"ID: {settings.id}")
    print(f"Client ID: {settings.youtube_client_id}")
    print(f"Client Secret: {settings.youtube_client_secret}")
    print(f"Refresh Token: {settings.youtube_refresh_token}")
else:
    print("No settings found.")
db.close()
