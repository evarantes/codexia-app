from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from app.database import Base
from datetime import datetime


class Tenant(Base):
    """Tenant para multi-tenant SaaS."""
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Book(Base):
    __tablename__ = "books"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
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


class BookDraft(Base):
    """Rascunhos da Fábrica de Livros: análise e estrutura antes da geração do PDF."""
    __tablename__ = "book_drafts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    title = Column(String, index=True)
    author = Column(String, nullable=True)
    metadata_json = Column(Text)  # JSON: title, author, subtitle, style, etc.
    sections_json = Column(Text)  # JSON: pre_textual, textual, post_textual
    cover_filename = Column(String, nullable=True)
    cover_base64 = Column(Text, nullable=True)  # Imagem em base64 para persistir (Render sem disco)
    manuscript_filename = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    name = Column(String)
    contact_info = Column(String)
    interest_level = Column(String)
    source = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

class Settings(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    openai_api_key = Column(String, nullable=True)
    leonardo_api_key = Column(String, nullable=True)
    leonardo_model_id = Column(String, nullable=True)
    gemini_api_key = Column(String, nullable=True)
    deepseek_api_key = Column(String, nullable=True)
    groq_api_key = Column(String, nullable=True)
    anthropic_api_key = Column(String, nullable=True)
    mistral_api_key = Column(String, nullable=True)
    openrouter_api_key = Column(String, nullable=True)
    openrouter_model = Column(String, nullable=True)
    ai_provider = Column(String, default="openai") # openai | gemini | deepseek | groq | anthropic | mistral | openrouter | hybrid
    facebook_page_id = Column(String, nullable=True)
    facebook_access_token = Column(String, nullable=True)
    whatsapp_phone_number_id = Column(String, nullable=True)
    whatsapp_access_token = Column(String, nullable=True)
    whatsapp_verify_token = Column(String, nullable=True)
    whatsapp_allowed_numbers = Column(String, nullable=True)
    telegram_bot_token = Column(String, nullable=True)
    telegram_allowed_chat_ids = Column(String, nullable=True)
    mercadopago_access_token = Column(String, nullable=True)
    # YouTube Integration
    youtube_client_id = Column(String, nullable=True)
    youtube_client_secret = Column(String, nullable=True)
    youtube_refresh_token = Column(String, nullable=True)
    # Hotmart Integration
    hotmart_client_id = Column(String, nullable=True)
    hotmart_client_secret = Column(String, nullable=True)
    hotmart_access_token = Column(String, nullable=True)
    hotmart_token_expires_at = Column(DateTime, nullable=True)
    # Suno (música com voz)
    suno_api_key = Column(String, nullable=True)
    # Stock Media & TTS
    pexels_api_key = Column(String, nullable=True)
    pixabay_api_key = Column(String, nullable=True)
    edenai_api_key = Column(String, nullable=True)
    elevenlabs_api_key = Column(String, nullable=True)
    elevenlabs_voice_id = Column(String, nullable=True)
    elevenlabs_voice_name = Column(String, nullable=True)
    
    is_active = Column(Boolean, default=True)

class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
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
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
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
    voice_style = Column(String, default="human")
    voice_gender = Column(String, default="female")
    music_file_path = Column(String, nullable=True) # Caminho do arquivo de música para videoclipes
    youtube_video_id = Column(String, nullable=True)
    uploaded_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    email = Column(String, unique=True, index=True)
    name = Column(String, nullable=True)
    hashed_password = Column(String)
    is_active = Column(Boolean, default=True)
    must_change_password = Column(Boolean, default=False)
    is_admin = Column(Boolean, default=False)
    role = Column(String, default="cliente")  # admin | cliente | colaborador
    reset_token = Column(String, nullable=True)
    reset_token_expire = Column(DateTime, nullable=True)

class ChannelReport(Base):
    __tablename__ = "channel_reports"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
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

class ContentPlan(Base):
    __tablename__ = "content_plans"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    mode = Column(String) # theme, music
    theme = Column(String, nullable=True)
    start_date = Column(DateTime)
    days = Column(Integer)
    videos_per_day = Column(Integer)
    shorts_per_day = Column(Integer)
    duration_min = Column(Integer)
    voice_style = Column(String)
    voice_gender = Column(String)
    music_file = Column(String, nullable=True) # Caminho do arquivo de música (Modo Música)
    status = Column(String, default="draft") # draft, confirmed, processing, completed
    created_at = Column(DateTime, default=datetime.utcnow)

    videos = relationship("Video", back_populates="plan")

class Video(Base):
    __tablename__ = "videos"

    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("content_plans.id"), nullable=True)
    type = Column(String) # LONG, SHORT, TIKTOK
    title = Column(String)
    description = Column(Text, nullable=True)
    tags = Column(String, nullable=True)
    hashtags = Column(String, nullable=True)
    duration_sec = Column(Float, nullable=True)
    status = Column(String, default="queued") # QUEUED, SCRIPT, TTS, VISUALS, RENDER, READY, PUBLISHED, ERROR
    scheduled_at = Column(DateTime, nullable=True)
    published_at = Column(DateTime, nullable=True)
    youtube_video_id = Column(String, nullable=True)
    parent_video_id = Column(Integer, ForeignKey("videos.id"), nullable=True) # For shorts derived from long
    created_at = Column(DateTime, default=datetime.utcnow)
    
    plan = relationship("ContentPlan", back_populates="videos")
    scenes = relationship("Scene", back_populates="video", cascade="all, delete-orphan")
    assets = relationship("Asset", back_populates="video", cascade="all, delete-orphan")
    jobs = relationship("Job", back_populates="video", cascade="all, delete-orphan")
    
    # Self-referential relationship for shorts
    parent_video = relationship("Video", remote_side=[id], backref="derived_videos")

class Scene(Base):
    __tablename__ = "scenes"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"))
    idx = Column(Integer)
    narration_text = Column(Text)
    visual_prompt = Column(Text)
    keywords = Column(String)
    duration_sec = Column(Float)
    
    video = relationship("Video", back_populates="scenes")

class Asset(Base):
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"))
    kind = Column(String) # AUDIO, IMAGE, CLIP, THUMB, SRT, FINAL
    storage_key = Column(String) # Path or S3 key
    meta_json = Column(Text, nullable=True) # JSON with details
    created_at = Column(DateTime, default=datetime.utcnow)

    video = relationship("Video", back_populates="assets")

class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"))
    step = Column(String) # script, tts, visuals, render, shorts, metadata
    status = Column(String, default="pending") # pending, processing, completed, failed
    progress = Column(Integer, default=0)
    logs = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    video = relationship("Video", back_populates="jobs")
