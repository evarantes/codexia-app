from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey, Boolean, UniqueConstraint
from sqlalchemy.orm import relationship
from app.database import Base
from datetime import datetime

class CommunityComment(Base):
    __tablename__ = "community_comments"

    id = Column(Integer, primary_key=True, index=True)
    youtube_comment_id = Column(String, unique=True, index=True)
    youtube_parent_id = Column(String, nullable=True, index=True)
    youtube_video_id = Column(String, index=True)
    scheduled_video_id = Column(Integer, ForeignKey("scheduled_videos.id"), nullable=True)
    author = Column(String, nullable=True)
    text = Column(Text)
    like_count = Column(Integer, default=0)
    published_at = Column(DateTime, nullable=True)
    status = Column(String, default="new")
    sentiment = Column(String, nullable=True)
    label = Column(String, nullable=True)
    urgency = Column(String, nullable=True)
    reply_draft = Column(Text, nullable=True)
    reply_text = Column(Text, nullable=True)
    reply_sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CommunityPost(Base):
    """Posts da comunidade: posters e enquetes gerados por IA."""
    __tablename__ = "community_posts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    post_type = Column(String, default="poster")  # poster | poll
    title = Column(String, nullable=False)
    content = Column(Text, nullable=True)
    image_prompt = Column(Text, nullable=True)
    image_url = Column(String, nullable=True)
    poll_options_json = Column(Text, nullable=True)  # JSON: [{"text": "...", "votes": 0}]
    poll_votes_json = Column(Text, nullable=True)  # JSON: {"option_idx": count}
    status = Column(String, default="draft")  # draft | published
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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
    file_base64 = Column(Text, nullable=True) # Backup persistente do arquivo do livro
    file_original_name = Column(String, nullable=True)
    file_mime_type = Column(String, nullable=True)
    status_amazon = Column(String, nullable=True)
    amazon_task_id = Column(String, nullable=True)
    amazon_last_error = Column(Text, nullable=True)
    amazon_updated_at = Column(DateTime, nullable=True)
    amazon_asin = Column(String, nullable=True)
    amazon_product_url = Column(String, nullable=True)
    amazon_listing_status = Column(String, nullable=True)
    amazon_format = Column(String, nullable=True)
    amazon_last_synced_at = Column(DateTime, nullable=True)
    
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

class StoryDraft(Base):
    __tablename__ = "story_drafts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    title = Column(String, index=True)
    kind = Column(String, default="story")
    content = Column(Text)
    metadata_json = Column(Text)
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
    openai_image_model = Column(String, nullable=True)
    openai_allow_text = Column(Boolean, default=False)
    openai_allow_script = Column(Boolean, default=False)
    openai_allow_editorial = Column(Boolean, default=False)
    openai_allow_analysis = Column(Boolean, default=False)
    openai_allow_images = Column(Boolean, default=True)
    openai_allow_thumbnail = Column(Boolean, default=True)
    openai_allow_transcription = Column(Boolean, default=False)
    openai_allow_tts = Column(Boolean, default=False)
    openai_allow_embeddings = Column(Boolean, default=False)
    openai_allow_other = Column(Boolean, default=False)
    openai_no_credit = Column(Boolean, default=False)
    ai_cb_failure_threshold = Column(Integer, nullable=True)
    ai_cb_cooldown_seconds = Column(Integer, nullable=True)
    ai_cb_half_open_max_attempts = Column(Integer, nullable=True)
    leonardo_api_key = Column(String, nullable=True)
    leonardo_model_id = Column(String, nullable=True)
    gemini_api_key = Column(String, nullable=True)
    gemini_script_model = Column(String, nullable=True)
    gemini_text_model = Column(String, nullable=True)
    gemini_editorial_model = Column(String, nullable=True)
    gemini_analysis_model = Column(String, nullable=True)
    deepseek_api_key = Column(String, nullable=True)
    groq_api_key = Column(String, nullable=True)
    groq_transcription_model = Column(String, nullable=True)
    groq_text_model = Column(String, nullable=True)
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
    official_channel_logo_path = Column(String, nullable=True)
    official_channel_logo_url = Column(String, nullable=True)
    youtube_comments_last_sync_at = Column(DateTime, nullable=True)
    youtube_auto_thanks_enabled = Column(Boolean, default=False)
    youtube_auto_thanks_template = Column(Text, nullable=True)
    youtube_auto_thanks_max_per_run = Column(Integer, default=15)
    youtube_auto_thanks_cooldown_hours = Column(Integer, default=72)
    # Hotmart Integration
    hotmart_client_id = Column(String, nullable=True)
    hotmart_client_secret = Column(String, nullable=True)
    hotmart_basic = Column(String, nullable=True)
    hotmart_access_token = Column(String, nullable=True)
    hotmart_token_expires_at = Column(DateTime, nullable=True)
    # Amazon KDP Integration
    amazon_kdp_email = Column(String, nullable=True)
    amazon_kdp_password = Column(String, nullable=True)
    amazon_kdp_login_url = Column(String, nullable=True)
    amazon_kdp_bookshelf_url = Column(String, nullable=True)
    amazon_kdp_timeout_ms = Column(Integer, nullable=True)
    amazon_kdp_email_selector = Column(String, nullable=True)
    amazon_kdp_password_selector = Column(String, nullable=True)
    amazon_kdp_submit_selector = Column(String, nullable=True)
    amazon_kdp_new_ebook_url = Column(String, nullable=True)
    amazon_kdp_new_ebook_button_selector = Column(String, nullable=True)
    amazon_kdp_title_selector = Column(String, nullable=True)
    amazon_kdp_subtitle_selector = Column(String, nullable=True)
    amazon_kdp_author_selector = Column(String, nullable=True)
    amazon_kdp_description_selector = Column(String, nullable=True)
    amazon_kdp_keywords_selector = Column(String, nullable=True)
    amazon_kdp_book_file_input_selector = Column(String, nullable=True)
    amazon_kdp_cover_file_input_selector = Column(String, nullable=True)
    amazon_kdp_price_selector = Column(String, nullable=True)
    amazon_kdp_publish_selector = Column(String, nullable=True)
    # Suno (música com voz)
    suno_api_key = Column(String, nullable=True)
    # Stock Media & TTS
    pexels_api_key = Column(String, nullable=True)
    pixabay_api_key = Column(String, nullable=True)
    edenai_api_key = Column(String, nullable=True)
    elevenlabs_api_key = Column(String, nullable=True)
    elevenlabs_voice_id = Column(String, nullable=True)
    elevenlabs_voice_name = Column(String, nullable=True)
    text_provider = Column(String, nullable=True)
    voice_provider = Column(String, nullable=True)
    image_provider = Column(String, nullable=True)
    video_provider = Column(String, nullable=True)
    music_provider = Column(String, nullable=True)
    caption_provider = Column(String, nullable=True)
    thumbnail_provider = Column(String, nullable=True)
    default_voice = Column(String, nullable=True)
    default_voice_speed = Column(Float, nullable=True)
    default_voice_emotion = Column(String, nullable=True)
    default_voice_intensity = Column(Float, nullable=True)
    default_language = Column(String, nullable=True)
    default_cta = Column(Text, nullable=True)
    default_next_episode_cta = Column(Text, nullable=True)
    default_playlist = Column(String, nullable=True)
    made_for_kids_default = Column(Boolean, nullable=True)
    daily_spend_limit = Column(Float, nullable=True)
    monthly_spend_limit = Column(Float, nullable=True)
    per_video_spend_limit = Column(Float, nullable=True)
    text_cost_unit = Column(Float, nullable=True)
    voice_cost_unit = Column(Float, nullable=True)
    image_cost_unit = Column(Float, nullable=True)
    video_cost_unit = Column(Float, nullable=True)
    music_cost_unit = Column(Float, nullable=True)
    caption_cost_unit = Column(Float, nullable=True)
    thumbnail_cost_unit = Column(Float, nullable=True)
    max_quality_recovery_attempts = Column(Integer, nullable=True)
    min_quality_recovery_score_delta = Column(Float, nullable=True)
    editorial_intelligence_enabled = Column(Boolean, nullable=True)
    editorial_intelligence_fail_open = Column(Boolean, nullable=True)
    editorial_intelligence_mode = Column(String, nullable=True)
    editorial_intelligence_provider = Column(String, nullable=True)
    primary_provider = Column(String, nullable=True)
    fallback_provider = Column(String, nullable=True)
    editorial_provider = Column(String, nullable=True)
    editorial_fallback_provider = Column(String, nullable=True)
    provider_priority = Column(Text, nullable=True)
    approved_models = Column(Text, nullable=True)
    # Instagram Integration (via Graph API — usa token do Facebook)
    instagram_user_id = Column(String, nullable=True)
    instagram_access_token = Column(String, nullable=True)
    # TikTok Integration (Content Posting API)
    tiktok_access_token = Column(String, nullable=True)
    
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


class SeriesPlan(Base):
    __tablename__ = "series_plans"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    channel_id = Column(String, nullable=True, index=True)
    name = Column(String, nullable=False, index=True)
    main_theme = Column(String, nullable=False, index=True)
    objective = Column(Text, nullable=True)
    target_audience = Column(String, nullable=True)
    content_type = Column(String, nullable=False, default="reflection")
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)
    publication_time = Column(String, nullable=False, default="19:00")
    timezone = Column(String, nullable=False, default="UTC")
    production_lead_days = Column(Integer, nullable=False, default=1)
    production_time = Column(String, nullable=False, default="06:00")
    duration_minutes = Column(Integer, nullable=True)
    visibility = Column(String, nullable=False, default="unlisted")
    tone = Column(String, nullable=True)
    narration_style = Column(String, nullable=True)
    continuity_level = Column(String, nullable=True)
    hook_intensity = Column(String, nullable=True)
    use_biblical_references = Column(Boolean, default=True)
    cta_subscribe = Column(Boolean, default=True)
    cta_next_episode = Column(Boolean, default=True)
    auto_approval = Column(Boolean, default=False)
    status = Column(String, nullable=False, default="draft", index=True)
    total_episodes = Column(Integer, nullable=False, default=0)
    current_episode = Column(Integer, nullable=False, default=0)
    editorial_plan_json = Column(Text, nullable=True)
    editorial_memory_json = Column(Text, nullable=True)
    archived_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    episodes = relationship("SeriesEpisode", back_populates="series", cascade="all, delete-orphan")


class SeriesEpisode(Base):
    __tablename__ = "series_episodes"
    __table_args__ = (
        UniqueConstraint("series_id", "episode_number", name="uq_series_episodes_series_episode_number"),
    )

    id = Column(Integer, primary_key=True, index=True)
    series_id = Column(Integer, ForeignKey("series_plans.id"), nullable=False, index=True)
    episode_number = Column(Integer, nullable=False, index=True)
    planned_title = Column(String, nullable=False)
    narrated_title = Column(String, nullable=True)
    summary = Column(Text, nullable=True)
    previous_episode_hook = Column(Text, nullable=True)
    next_episode_hook = Column(Text, nullable=True)
    publication_datetime = Column(DateTime, nullable=False, index=True)
    production_datetime = Column(DateTime, nullable=False, index=True)
    duration_minutes = Column(Integer, nullable=True)
    status = Column(String, nullable=False, default="planned", index=True)
    task_id = Column(String, ForeignKey("video_tasks.id"), nullable=True, index=True)
    scheduled_video_id = Column(Integer, ForeignKey("scheduled_videos.id"), nullable=True, index=True)
    content_fingerprint = Column(String, nullable=True, index=True)
    approved_at = Column(DateTime, nullable=True)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    published_at = Column(DateTime, nullable=True)
    youtube_video_id = Column(String, nullable=True)
    youtube_url = Column(String, nullable=True)
    current_version = Column(Integer, nullable=False, default=1)
    correction_plan_json = Column(Text, nullable=True)
    approved_snapshot_json = Column(Text, nullable=True)
    metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    series = relationship("SeriesPlan", back_populates="episodes")
    reviews = relationship("EpisodeReview", back_populates="episode", cascade="all, delete-orphan")


class EpisodeReview(Base):
    __tablename__ = "episode_reviews"

    id = Column(Integer, primary_key=True, index=True)
    episode_id = Column(Integer, ForeignKey("series_episodes.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False, default=1)
    decision = Column(String, nullable=False, index=True)
    reason_categories = Column(Text, nullable=True)
    feedback = Column(Text, nullable=True)
    affected_components = Column(Text, nullable=True)
    reused_components = Column(Text, nullable=True)
    regenerated_components = Column(Text, nullable=True)
    estimated_cost = Column(Float, nullable=True)
    actual_cost = Column(Float, nullable=True)
    result_summary = Column(Text, nullable=True)
    reviewed_at = Column(DateTime, default=datetime.utcnow)
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    episode = relationship("SeriesEpisode", back_populates="reviews")

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

class SystemNotification(Base):
    __tablename__ = "system_notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    kind = Column(String, index=True)
    title = Column(String)
    message = Column(Text)
    payload_json = Column(Text, nullable=True)
    status = Column(String, default="new", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    read_at = Column(DateTime, nullable=True)

class VideoTask(Base):
    __tablename__ = "video_tasks"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    status = Column(String, default="pending", index=True)
    progress = Column(Integer, default=0)
    message = Column(Text, nullable=True)
    result_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class SavedMusic(Base):
    __tablename__ = "saved_music"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    title = Column(String, default="Música")
    lyrics = Column(Text, nullable=True)
    genre = Column(String, nullable=True)
    vocal_gender = Column(String, nullable=True)
    with_vocals = Column(Boolean, default=False)
    music_url = Column(String, nullable=True)
    music_filename = Column(String, nullable=True)
    hq_wav_url = Column(String, nullable=True)
    hq_wav_filename = Column(String, nullable=True)
    cover_url = Column(String, nullable=True)
    cover_filename = Column(String, nullable=True)
    clip_url = Column(String, nullable=True)
    clip_filename = Column(String, nullable=True)
    status_spotify = Column(String, nullable=True)
    spotify_task_id = Column(String, nullable=True)
    spotify_last_error = Column(Text, nullable=True)
    spotify_updated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class SavedMusicShort(Base):
    __tablename__ = "saved_music_shorts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    parent_saved_music_id = Column(Integer, ForeignKey("saved_music.id"), nullable=False, index=True)
    title = Column(String, default="Short")
    clip_url = Column(String, nullable=False)
    clip_filename = Column(String, nullable=True)
    start_sec = Column(Float, nullable=True)
    end_sec = Column(Float, nullable=True)
    youtube_video_id = Column(String, nullable=True)
    uploaded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class ChannelInsight(Base):
    __tablename__ = "channel_insights"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    kind = Column(String, index=True)
    start_date = Column(DateTime, nullable=True)
    end_date = Column(DateTime, nullable=True)
    data_json = Column(Text, nullable=True)
    ai_summary = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

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
