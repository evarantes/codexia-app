from sqlalchemy import Boolean, Column, Integer, String, Text, DateTime, JSON
from sqlalchemy.sql import func
from app.database import Base

class AIStory(Base):
    __tablename__ = "codexia_ai_stories"
    id = Column(Integer, primary_key=True, index=True)
    theme = Column(String, nullable=False)
    style = Column(String, nullable=True)
    audience = Column(String, nullable=True)
    length = Column(String, nullable=True)
    title = Column(String, nullable=True)
    synopsis = Column(Text, nullable=True)
    content = Column(JSON, nullable=True)  # chapters, structure
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class AICover(Base):
    __tablename__ = "codexia_ai_covers"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    subtitle = Column(String, nullable=True)
    author = Column(String, nullable=True)
    style = Column(String, nullable=True)
    prompt = Column(Text, nullable=True)
    image_url = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class AIImage(Base):
    __tablename__ = "codexia_ai_images"
    id = Column(Integer, primary_key=True, index=True)
    theme = Column(String, nullable=False)
    style = Column(String, nullable=True)
    prompt = Column(Text, nullable=True)
    image_url = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class AIScript(Base):
    __tablename__ = "codexia_ai_scripts"
    id = Column(Integer, primary_key=True, index=True)
    theme = Column(String, nullable=False)
    duration = Column(String, nullable=True)
    narrative_type = Column(String, nullable=True)
    script_content = Column(Text, nullable=True)
    scenes = Column(JSON, nullable=True)
    shorts = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AIJokeChannelProject(Base):
    __tablename__ = "codexia_ai_joke_channel_projects"

    id = Column(Integer, primary_key=True, index=True)
    channel_name = Column(String, nullable=False)
    theme = Column(String, nullable=False)
    category = Column(String, nullable=True)
    tone = Column(String, nullable=True)
    source_mode = Column(String, nullable=False, default="ai")  # ai | manual | mixed
    duration_minutes = Column(Integer, nullable=False, default=10)
    jokes_count = Column(Integer, nullable=False, default=24)
    manual_jokes = Column(Text, nullable=True)
    avatar_name = Column(String, nullable=True)
    avatar_style = Column(String, nullable=True)
    avatar_description = Column(Text, nullable=True)
    auto_publish = Column(Boolean, nullable=False, default=False)
    status = Column(String, nullable=False, default="ready_for_review")
    review_notes = Column(Text, nullable=True)
    generated_payload = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
