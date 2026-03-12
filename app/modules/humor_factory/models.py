from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func

from app.database import Base


class HumorChannel(Base):
    __tablename__ = "codexia_humor_channels"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, default="Canal de Humor")
    description = Column(Text, nullable=True)
    avatar_path = Column(String, nullable=True)
    default_voice_gender = Column(String, nullable=False, default="male")
    allowed_themes = Column(Text, nullable=True)  # JSON array
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class HumorProject(Base):
    __tablename__ = "codexia_humor_projects"

    id = Column(Integer, primary_key=True, index=True)
    channel_id = Column(Integer, ForeignKey("codexia_humor_channels.id"), nullable=True, index=True)

    title = Column(String, nullable=True)
    theme = Column(String, nullable=False)
    joke_source = Column(String, nullable=False, default="ai")  # ai | manual | mixed
    manual_jokes_text = Column(Text, nullable=True)
    jokes_json = Column(Text, nullable=True)  # JSON array
    avatar_override_path = Column(String, nullable=True)
    opening_message = Column(Text, nullable=True)
    catchphrase_message = Column(Text, nullable=True)
    closing_message = Column(Text, nullable=True)

    target_minutes = Column(Integer, nullable=False, default=10)
    auto_publish_after_review = Column(Boolean, nullable=False, default=False)

    status = Column(String, nullable=False, default="queued")
    progress = Column(Integer, nullable=False, default=0)
    status_message = Column(String, nullable=True)
    logs = Column(Text, nullable=True)
    review_notes = Column(Text, nullable=True)

    video_path = Column(String, nullable=True)
    scheduled_video_id = Column(Integer, nullable=True, index=True)
    youtube_video_id = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
