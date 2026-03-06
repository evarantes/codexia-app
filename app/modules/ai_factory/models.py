from sqlalchemy import Column, Integer, String, Text, DateTime, JSON
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
