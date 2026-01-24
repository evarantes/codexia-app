from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
import bcrypt
from sqlalchemy.orm import Session
from pydantic import BaseModel
import secrets
import os
from app.database import get_db
from app.models import User
from app.services.email_service import EmailService

# Configurações de Segurança
# Em produção, usar env var
SECRET_KEY = os.getenv("SECRET_KEY", "sua_secret_key_super_secreta_codexia_2025") 
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 # 24 horas

router = APIRouter(tags=["Authentication"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

class Token(BaseModel):
    access_token: str
    token_type: str
    must_change_password: bool = False

class TokenData(BaseModel):
    username: Optional[str] = None

class PasswordReset(BaseModel):
    email: str

class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str

class PasswordChange(BaseModel):
    current_password: str
    new_password: str

# Helpers
def verify_password(plain_password, hashed_password):
    if isinstance(plain_password, str):
        plain_password = plain_password.encode('utf-8')
    if isinstance(hashed_password, str):
        hashed_password = hashed_password.encode('utf-8')
    
    # bcrypt.checkpw expects bytes
    try:
        return bcrypt.checkpw(plain_password, hashed_password)
    except ValueError:
        return False

def get_password_hash(password):
    if isinstance(password, str):
        password = password.encode('utf-8')
    
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password, salt)
    return hashed.decode('utf-8')

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        token_data = TokenData(username=username)
    except JWTError:
        raise credentials_exception
    user = db.query(User).filter(User.email == token_data.username).first()
    if user is None:
        raise credentials_exception
    return user

# Endpoints
@router.post("/token", response_model=Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.email}, expires_delta=access_token_expires
    )
    return {
        "access_token": access_token, 
        "token_type": "bearer",
        "must_change_password": user.must_change_password
    }

@router.post("/auth/forgot-password")
def forgot_password(request: PasswordReset, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == request.email).first()
    if not user:
        # Não revelar que o usuário não existe
        return {"message": "If the email exists, a reset link will be sent."}
    
    token = secrets.token_urlsafe(32)
    user.reset_token = token
    user.reset_token_expire = datetime.utcnow() + timedelta(hours=1)
    db.commit()
    
    email_service = EmailService()
    email_service.send_password_reset_email(user.email, token)
    
    return {"message": "If the email exists, a reset link will be sent."}

@router.post("/auth/reset-password")
def reset_password(request: PasswordResetConfirm, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.reset_token == request.token).first()
    if not user:
        raise HTTPException(status_code=400, detail="Invalid token")
    
    if user.reset_token_expire < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Token expired")
        
    user.hashed_password = get_password_hash(request.new_password)
    user.reset_token = None
    user.reset_token_expire = None
    db.commit()
    
    return {"message": "Password updated successfully"}

@router.post("/auth/change-password")
def change_password(request: PasswordChange, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not verify_password(request.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect current password")
    
    current_user.hashed_password = get_password_hash(request.new_password)
    current_user.must_change_password = False
    db.commit()
    
    return {"message": "Password updated successfully"}

@router.get("/auth/me")
def read_users_me(current_user: User = Depends(get_current_user)):
    return {
        "email": current_user.email, 
        "id": current_user.id,
        "must_change_password": current_user.must_change_password
    }
