from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from app.database import Base
from datetime import datetime

class Book(Base):
    __tablename__ = "books"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, index=True)
    author = Column(String, default="Você")
    synopsis = Column(Text)
    full_text = Column(Text)
    price = Column(Float)
    payment_link = Column(String)
    cover_image_url = Column(String)
    cover_image_base64 = Column(Text, nullable=True) # Armazena a imagem em Base64 para persistência no Render sem disco
    file_path = Column(String) # Caminho do arquivo do livro (PDF/EPUB)
    
    posts = relationship("Post", back_populates="book")
    sales = relationship("Sale", back_populates="book")

class Post(Base):
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, index=True)
    book_id = Column(Integer, ForeignKey("books.id"))
    content = Column(Text)
    post_type = Column(String)
    status = Column(String, default="draft")
    scheduled_for = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    media_url = Column(String, nullable=True) # URL do vídeo ou imagem gerada
    
    book = relationship("Book", back_populates="posts")

class Lead(Base):
    __tablename__ = "leads"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    contact_info = Column(String)
    interest_level = Column(String)
    source = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

class Settings(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    openai_api_key = Column(String, nullable=True)
    gemini_api_key = Column(String, nullable=True)
    deepseek_api_key = Column(String, nullable=True)
    groq_api_key = Column(String, nullable=True)
    anthropic_api_key = Column(String, nullable=True)
    mistral_api_key = Column(String, nullable=True)
    openrouter_api_key = Column(String, nullable=True)
    ai_provider = Column(String, default="openai") # openai | gemini | deepseek | groq | anthropic | mistral | openrouter | hybrid
    facebook_page_id = Column(String, nullable=True)
    facebook_access_token = Column(String, nullable=True)
    mercadopago_access_token = Column(String, nullable=True)
    # YouTube Integration
    youtube_client_id = Column(String, nullable=True)
    youtube_client_secret = Column(String, nullable=True)
    youtube_refresh_token = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)

class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    email = Column(String, index=True)
    phone = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    sales = relationship("Sale", back_populates="customer")

class Sale(Base):
    __tablename__ = "sales"

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"))
    book_id = Column(Integer, ForeignKey("books.id"))
    amount = Column(Float)
    status = Column(String) # approved, pending, rejected
    payment_id = Column(String, unique=True) # ID do Mercado Pago
    created_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", back_populates="sales")
    book = relationship("Book", back_populates="sales")

class ScheduledVideo(Base):
    __tablename__ = "scheduled_videos"

    id = Column(Integer, primary_key=True, index=True)
    theme = Column(String)
    title = Column(String)
    description = Column(String)
    scheduled_for = Column(DateTime)
    status = Column(String, default="pending") # pending, processing, published, failed
    video_type = Column(String, default="video") # video, short
    parent_video_id = Column(Integer, ForeignKey("scheduled_videos.id"), nullable=True) # For shorts derived from videos
    
    # Store the generated script/plan so we can execute it later
    script_data = Column(Text) # JSON string
    video_url = Column(String, nullable=True) # Caminho do vídeo gerado
    
    # New fields for progress and scheduling
    progress = Column(Integer, default=0)
    publish_at = Column(DateTime, nullable=True)
    auto_post = Column(Boolean, default=False)
    youtube_video_id = Column(String, nullable=True)
    uploaded_at = Column(DateTime, nullable=True)

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    is_active = Column(Boolean, default=True)
    must_change_password = Column(Boolean, default=False)
    reset_token = Column(String, nullable=True)
    reset_token_expire = Column(DateTime, nullable=True)

class ChannelReport(Base):
    __tablename__ = "channel_reports"
    
    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Snapshot stats
    subscribers = Column(Integer)
    views = Column(Integer)
    videos = Column(Integer)
    
    # Analysis
    analysis_text = Column(Text) # IA Analysis
    strategy_suggestion = Column(Text) # Sugestão de ação
    
    # Status
    status = Column(String, default="generated")

