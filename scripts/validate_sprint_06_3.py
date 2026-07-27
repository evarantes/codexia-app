import concurrent.futures
import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/codexia")

from app.routers.youtube import _build_scheduled_video_equivalence_key
from app.services.video_generator import (
    DEFAULT_SCENE_AUDIO_MARGIN_SEC,
    DEFAULT_SCENE_CAPTION_LEAD_SEC,
    DEFAULT_SCENE_IMAGE_LEAD_SEC,
    VideoGenerator,
)


class InMemoryScheduledStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_id = 1
        self._by_key: Dict[str, Dict[str, Any]] = {}

    def create_or_get(self, *, theme: str, scheduled_for: datetime, video_type: str) -> Dict[str, Any]:
        key = _build_scheduled_video_equivalence_key(
            user_id=None,
            theme=theme,
            scheduled_for=scheduled_for,
            video_type=video_type,
        )
        with self._lock:
            existing = self._by_key.get(key)
            if existing is not None:
                return dict(existing)
            item = {
                "id": self._next_id,
                "key": key,
                "theme": theme,
                "scheduled_for": scheduled_for.isoformat(),
                "video_type": video_type,
                "status": "queued",
                "dispatch_claims": 0,
                "processing_claims": 0,
            }
            self._by_key[key] = item
            self._next_id += 1
            return dict(item)

    def claim_dispatch(self, key: str) -> bool:
        with self._lock:
            item = self._by_key.get(key)
            if item is None or item["status"] != "queued":
                return False
            item["status"] = "dispatching"
            item["dispatch_claims"] += 1
            return True

    def claim_processing(self, key: str) -> bool:
        with self._lock:
            item = self._by_key.get(key)
            if item is None or item["status"] not in {"queued", "dispatching"}:
                return False
            item["status"] = "processing"
            item["processing_claims"] += 1
            return True

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._by_key.values()]


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_concurrency_validation() -> Dict[str, Any]:
    theme = "Tema unico da sprint 06.3"
    scheduled_for = datetime(2026, 7, 26, 12, 0)
    video_type = "video"
    store = InMemoryScheduledStore()

    def _create_same_video() -> Dict[str, Any]:
        return store.create_or_get(theme=theme, scheduled_for=scheduled_for, video_type=video_type)

    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
        created = list(executor.map(lambda _: _create_same_video(), range(100)))

    snapshot = store.snapshot()
    _assert(len(snapshot) == 1, f"Esperado 1 ScheduledVideo; encontrado {len(snapshot)}.")
    key = snapshot[0]["key"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
        dispatch_results = list(executor.map(lambda _: store.claim_dispatch(key), range(100)))
    _assert(sum(1 for item in dispatch_results if item) == 1, "Dispatch deveria ser aceito apenas uma vez.")

    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
        processing_results = list(executor.map(lambda _: store.claim_processing(key), range(100)))
    _assert(sum(1 for item in processing_results if item) == 1, "Processing claim deveria ser aceito apenas uma vez.")

    for _ in range(100):
        store.create_or_get(theme=theme, scheduled_for=scheduled_for, video_type=video_type)

    final_snapshot = store.snapshot()
    _assert(len(final_snapshot) == 1, "Scheduler idempotente falhou após 100 ciclos.")

    return {
        "concurrent_create_attempts": len(created),
        "scheduled_videos_created": len(snapshot),
        "dispatch_claim_successes": sum(1 for item in dispatch_results if item),
        "processing_claim_successes": sum(1 for item in processing_results if item),
        "scheduler_cycles_after_creation": 100,
        "scheduled_videos_after_stress": len(final_snapshot),
        "status_after_stress": final_snapshot[0]["status"],
    }


def run_scene_timeline_validation() -> Dict[str, Any]:
    generator = VideoGenerator()
    scenes = [
        {"text": "A primeira cena apresenta a abertura do tema."},
        {"text": "A segunda cena conclui a mensagem principal."},
    ]
    sync_map = {
        "scene_timelines": [
            [
                {
                    "block_index": 0,
                    "caption": "A primeira cena apresenta a abertura do tema.",
                    "global_start": 3.50,
                    "global_end": 8.42,
                }
            ],
            [
                {
                    "block_index": 1,
                    "caption": "A segunda cena conclui a mensagem principal.",
                    "global_start": 8.72,
                    "global_end": 13.10,
                }
            ],
        ]
    }
    timeline = generator._build_official_scene_timeline(
        scenes=scenes,
        scene_caption_sync=sync_map,
        planned_scene_durations=[4.92, 4.38],
        opening_text="Abertura oficial do video.",
        opening_image="opening.png",
        title_duration=3.0,
        initial_opening_silence_sec=3.0,
        cta_text="Encerramento oficial do video.",
        closing_image="closing.png",
        pause_before_cta_sec=1.0,
        cta_duration=2.0,
        end_duration=4.0,
        timeline_source="audio_segments",
        transition_name="fade",
    )

    story_items = [item for item in timeline if item.get("kind") == "story"]
    _assert(len(timeline) == 5, "SceneTimeline deveria conter abertura, 2 cenas, fechamento e endcard.")
    _assert(len(story_items) == 2, "SceneTimeline deveria conter 2 cenas principais.")
    first, second = story_items
    _assert(first["audio_start"] == 3.5, "Audio start da primeira cena divergente.")
    _assert(first["scene_start"] <= first["caption_start"] <= first["audio_start"], "Sequencia imagem > legenda > audio inválida na primeira cena.")
    _assert(abs((first["audio_end"] + DEFAULT_SCENE_AUDIO_MARGIN_SEC) - first["scene_end"]) <= 0.001, "Margem pós-audio da primeira cena inválida.")
    _assert(second["scene_start"] >= first["scene_end"], "SceneTimeline não pode sobrepor cenas.")
    _assert(first["transition"] == "fade" and second["transition"] == "fade", "Transição oficial deveria ser fade.")
    _assert(first["caption_blocks"], "Primeira cena precisa de blocos de legenda.")
    _assert(timeline[0]["kind"] == "opening", "Primeira entrada da SceneTimeline deveria ser a abertura.")
    _assert(timeline[-2]["kind"] == "closing" and timeline[-1]["kind"] == "endcard", "Timeline deveria terminar com fechamento e endcard.")

    return {
        "scene_count": len(timeline),
        "opening_scene": timeline[0],
        "first_scene": story_items[0],
        "second_scene": story_items[1],
        "closing_scene": timeline[-2],
        "endcard_scene": timeline[-1],
        "scene_image_lead_sec": DEFAULT_SCENE_IMAGE_LEAD_SEC,
        "scene_caption_lead_sec": DEFAULT_SCENE_CAPTION_LEAD_SEC,
        "scene_audio_margin_sec": DEFAULT_SCENE_AUDIO_MARGIN_SEC,
    }


def main() -> int:
    report = {
        "concurrency_validation": run_concurrency_validation(),
        "scene_timeline_validation": run_scene_timeline_validation(),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
