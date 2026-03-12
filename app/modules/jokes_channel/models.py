from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, Float, JSON, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class JokeAvatar(Base):
    """Avatar fixo que conta as piadas no canal."""
    __tablename__ = "joke_avatars"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=True, index=True)
    name = Column(String, nullable=False, default="Risadão")
    description = Column(Text, nullable=True)
    image_url = Column(String, nullable=True)
    image_base64 = Column(Text, nullable=True)
    voice_style = Column(String, default="human")
    voice_gender = Column(String, default="male")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class JokeTheme(Base):
    """Temas disponíveis para piadas."""
    __tablename__ = "joke_themes"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=True, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    icon = Column(String, nullable=True, default="fa-laugh")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Joke(Base):
    """Piada individual — pode ser criada pela IA ou inserida manualmente."""
    __tablename__ = "jokes"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=True, index=True)
    theme_id = Column(Integer, ForeignKey("joke_themes.id"), nullable=True)
    title = Column(String, nullable=True)
    content = Column(Text, nullable=False)
    source = Column(String, default="ai")  # ai | manual
    duration_sec = Column(Float, nullable=True)
    audio_url = Column(String, nullable=True)
    status = Column(String, default="draft")  # draft | approved | rejected | used
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    theme = relationship("JokeTheme", lazy="joined")


class JokeCompilation(Base):
    """Compilação de piadas para gerar um vídeo longo (10+ min)."""
    __tablename__ = "joke_compilations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    avatar_id = Column(Integer, ForeignKey("joke_avatars.id"), nullable=True)
    theme_id = Column(Integer, ForeignKey("joke_themes.id"), nullable=True)

    jokes_json = Column(JSON, nullable=True)
    total_jokes = Column(Integer, default=0)
    target_duration_min = Column(Integer, default=10)
    actual_duration_sec = Column(Float, nullable=True)

    video_url = Column(String, nullable=True)
    thumbnail_url = Column(String, nullable=True)

    status = Column(String, default="draft")
    # draft -> generating -> review -> approved -> publishing -> published | failed
    progress = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)

    youtube_video_id = Column(String, nullable=True)
    published_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    avatar = relationship("JokeAvatar", lazy="joined")
    theme = relationship("JokeTheme", lazy="joined")
