from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func

from app.database import Base


class BibleVideoSeries(Base):
    __tablename__ = "codexia_bible_video_series"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    name = Column(String, nullable=False, index=True)
    bible_book = Column(String, nullable=True, index=True)
    main_character = Column(String, nullable=True, index=True)
    target_audience = Column(String, nullable=True)
    production_profile = Column(String, nullable=True, index=True)
    production_profile_json = Column(Text, nullable=True)
    visual_style = Column(String, nullable=True)
    narrative_tone = Column(String, nullable=True)
    planned_episodes = Column(Integer, nullable=False, default=10)
    episode_duration_minutes = Column(Integer, nullable=False, default=5)
    language = Column(String, nullable=False, default="pt-BR")
    linked_channel = Column(String, nullable=True)
    status = Column(String, nullable=False, default="draft")
    bible_story_text = Column(Text, nullable=True)
    series_summary = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BibleVideoEpisode(Base):
    __tablename__ = "codexia_bible_video_episodes"

    id = Column(Integer, primary_key=True, index=True)
    series_id = Column(Integer, ForeignKey("codexia_bible_video_series.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    episode_number = Column(Integer, nullable=False, index=True)
    title = Column(String, nullable=False)
    summary = Column(Text, nullable=True)
    biblical_basis = Column(Text, nullable=True)
    opening_hook = Column(Text, nullable=True)
    development_text = Column(Text, nullable=True)
    tension_moment = Column(Text, nullable=True)
    impact_phrase = Column(Text, nullable=True)
    ending_hook = Column(Text, nullable=True)
    short_suggestion = Column(Text, nullable=True)
    thumbnail_suggestion = Column(Text, nullable=True)
    youtube_title_suggestion = Column(Text, nullable=True)
    estimated_minutes = Column(Integer, nullable=False, default=5)
    status = Column(String, nullable=False, default="idea")
    approval_status = Column(String, nullable=False, default="pending")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BibleVideoScript(Base):
    __tablename__ = "codexia_bible_video_scripts"

    id = Column(Integer, primary_key=True, index=True)
    series_id = Column(Integer, ForeignKey("codexia_bible_video_series.id"), nullable=False, index=True)
    episode_id = Column(Integer, ForeignKey("codexia_bible_video_episodes.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    desired_duration_minutes = Column(Integer, nullable=False, default=5)
    narrative_style = Column(String, nullable=True)
    drama_level = Column(Integer, nullable=False, default=7)
    biblical_fidelity_level = Column(Integer, nullable=False, default=9)
    target_audience = Column(String, nullable=True)
    subscribe_cta = Column(Text, nullable=True)
    next_episode_cta = Column(Text, nullable=True)
    full_narration = Column(Text, nullable=True)
    scenes_json = Column(Text, nullable=True)
    optional_dialogues_json = Column(Text, nullable=True)
    voice_emotion_notes = Column(Text, nullable=True)
    soundtrack_notes = Column(Text, nullable=True)
    sound_effects_notes = Column(Text, nullable=True)
    retention_hooks_json = Column(Text, nullable=True)
    thumbnail_json = Column(Text, nullable=True)
    shorts_json = Column(Text, nullable=True)
    validation_status = Column(String, nullable=False, default="pending")
    validation_notes = Column(Text, nullable=True)
    validation_flags_json = Column(Text, nullable=True)
    disclaimer_required = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BibleVideoScene(Base):
    __tablename__ = "codexia_bible_video_scenes"

    id = Column(Integer, primary_key=True, index=True)
    script_id = Column(Integer, ForeignKey("codexia_bible_video_scripts.id"), nullable=False, index=True)
    series_id = Column(Integer, ForeignKey("codexia_bible_video_series.id"), nullable=False, index=True)
    episode_id = Column(Integer, ForeignKey("codexia_bible_video_episodes.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    scene_number = Column(Integer, nullable=False)
    narration_text = Column(Text, nullable=True)
    visual_description = Column(Text, nullable=True)
    characters_json = Column(Text, nullable=True)
    scenario_name = Column(String, nullable=True)
    emotion = Column(String, nullable=True)
    prompt_image = Column(Text, nullable=True)
    prompt_animation = Column(Text, nullable=True)
    duration_seconds = Column(Float, nullable=False, default=8.0)
    camera_type = Column(String, nullable=True)
    effects_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BibleVideoCharacter(Base):
    __tablename__ = "codexia_bible_video_characters"

    id = Column(Integer, primary_key=True, index=True)
    series_id = Column(Integer, ForeignKey("codexia_bible_video_series.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    name = Column(String, nullable=False, index=True)
    description = Column(Text, nullable=True)
    approximate_age = Column(String, nullable=True)
    clothing = Column(Text, nullable=True)
    hair = Column(Text, nullable=True)
    default_expression = Column(String, nullable=True)
    visual_style = Column(String, nullable=True)
    base_prompt = Column(Text, nullable=True)
    reference_image_url = Column(String, nullable=True)
    emotions_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BibleVideoScenario(Base):
    __tablename__ = "codexia_bible_video_scenarios"

    id = Column(Integer, primary_key=True, index=True)
    series_id = Column(Integer, ForeignKey("codexia_bible_video_series.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    name = Column(String, nullable=False, index=True)
    description = Column(Text, nullable=True)
    base_prompt = Column(Text, nullable=True)
    visual_style = Column(String, nullable=True)
    reference_image_url = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BibleVideoPrompt(Base):
    __tablename__ = "codexia_bible_video_prompts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    category = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BibleVideoConfig(Base):
    __tablename__ = "codexia_bible_video_configs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    text_provider = Column(String, nullable=False, default="openai")
    voice_provider = Column(String, nullable=False, default="elevenlabs")
    image_provider = Column(String, nullable=False, default="openai")
    video_provider = Column(String, nullable=False, default="luma")
    music_provider = Column(String, nullable=False, default="musicgen")
    caption_provider = Column(String, nullable=False, default="native")
    thumbnail_provider = Column(String, nullable=False, default="openai")
    text_api_key = Column(String, nullable=True)
    voice_api_key = Column(String, nullable=True)
    image_api_key = Column(String, nullable=True)
    video_api_key = Column(String, nullable=True)
    youtube_api_key = Column(String, nullable=True)
    tiktok_api_key = Column(String, nullable=True)
    instagram_api_key = Column(String, nullable=True)
    default_voice = Column(String, nullable=True)
    default_voice_speed = Column(Float, nullable=False, default=1.0)
    default_voice_emotion = Column(String, nullable=True)
    default_voice_intensity = Column(Float, nullable=False, default=0.7)
    default_language = Column(String, nullable=False, default="pt-BR")
    default_cta = Column(Text, nullable=True)
    default_next_episode_cta = Column(Text, nullable=True)
    default_playlist = Column(String, nullable=True)
    made_for_kids_default = Column(Boolean, nullable=False, default=False)
    daily_spend_limit = Column(Float, nullable=False, default=0.0)
    monthly_spend_limit = Column(Float, nullable=False, default=0.0)
    text_cost_unit = Column(Float, nullable=False, default=0.0)
    voice_cost_unit = Column(Float, nullable=False, default=0.0)
    image_cost_unit = Column(Float, nullable=False, default=0.0)
    video_cost_unit = Column(Float, nullable=False, default=0.0)
    music_cost_unit = Column(Float, nullable=False, default=0.0)
    caption_cost_unit = Column(Float, nullable=False, default=0.0)
    thumbnail_cost_unit = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BibleVideoJob(Base):
    __tablename__ = "codexia_bible_video_jobs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    series_id = Column(Integer, ForeignKey("codexia_bible_video_series.id"), nullable=True, index=True)
    episode_id = Column(Integer, ForeignKey("codexia_bible_video_episodes.id"), nullable=True, index=True)
    script_id = Column(Integer, ForeignKey("codexia_bible_video_scripts.id"), nullable=True, index=True)
    parent_job_id = Column(Integer, ForeignKey("codexia_bible_video_jobs.id"), nullable=True, index=True)
    title = Column(String, nullable=False)
    job_type = Column(String, nullable=False, default="episode")
    platform = Column(String, nullable=False, default="youtube")
    aspect_ratio = Column(String, nullable=False, default="16:9")
    kanban_stage = Column(String, nullable=False, default="idea")
    status = Column(String, nullable=False, default="queued")
    approval_status = Column(String, nullable=False, default="pending")
    progress = Column(Integer, nullable=False, default=0)
    status_message = Column(Text, nullable=True)
    task_id = Column(String, nullable=True, index=True)
    scheduled_for = Column(DateTime(timezone=True), nullable=True)
    tags_json = Column(Text, nullable=True)
    description_text = Column(Text, nullable=True)
    pinned_comment = Column(Text, nullable=True)
    playlist_name = Column(String, nullable=True)
    publish_platforms_json = Column(Text, nullable=True)
    plan_json = Column(Text, nullable=True)
    result_json = Column(Text, nullable=True)
    output_video_url = Column(String, nullable=True)
    output_thumbnail_url = Column(String, nullable=True)
    published_video_id = Column(String, nullable=True)
    estimated_cost = Column(Float, nullable=False, default=0.0)
    actual_cost = Column(Float, nullable=False, default=0.0)
    error_log = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BibleVideoMetric(Base):
    __tablename__ = "codexia_bible_video_metrics"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    series_id = Column(Integer, ForeignKey("codexia_bible_video_series.id"), nullable=True, index=True)
    episode_id = Column(Integer, ForeignKey("codexia_bible_video_episodes.id"), nullable=True, index=True)
    job_id = Column(Integer, ForeignKey("codexia_bible_video_jobs.id"), nullable=True, index=True)
    platform = Column(String, nullable=False, default="youtube")
    video_id = Column(String, nullable=True)
    view_count = Column(Integer, nullable=False, default=0)
    ctr = Column(Float, nullable=False, default=0.0)
    retention = Column(Float, nullable=False, default=0.0)
    subscribers_gained = Column(Integer, nullable=False, default=0)
    likes = Column(Integer, nullable=False, default=0)
    comments = Column(Integer, nullable=False, default=0)
    extra_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
