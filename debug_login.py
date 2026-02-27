import os
import sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base, SessionLocal
from app.models import User
from app.routers.auth import verify_password, create_access_token_for_user
from datetime import timedelta

# Configura conexão
db = SessionLocal()

print(f"Checking user evarantes2@gmail.com in DB...")

try:
    user = db.query(User).filter(User.email == "evarantes2@gmail.com").first()
    
    if not user:
        print("ERROR: User not found!")
    else:
        print(f"User found: ID={user.id}, Email={user.email}, Role={user.role}, Tenant={user.tenant_id}")
        
        # Check password
        password = "123456"
        is_valid = verify_password(password, user.hashed_password)
        print(f"Password '{password}' valid? {is_valid}")
        
        if is_valid:
            try:
                token = create_access_token_for_user(user, timedelta(minutes=60))
                print(f"Token generated successfully: {token[:20]}...")
            except Exception as e:
                print(f"ERROR generating token: {e}")
                import traceback
                traceback.print_exc()
        else:
            print(f"Hash stored: {user.hashed_password}")

except Exception as e:
    print(f"Database error: {e}")
    import traceback
    traceback.print_exc()
finally:
    db.close()
