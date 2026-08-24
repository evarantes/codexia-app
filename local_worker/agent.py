from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

try:
    import psutil  # type: ignore
except Exception:
    psutil = None


class WorkerError(RuntimeError):
    pass


class LocalRenderAgent:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        worker_id: Optional[str] = None,
        poll_seconds: int = 15,
        heartbeat_seconds: int = 30,
        work_root: Optional[str] = None,
        max_ram_percent: float = 85.0,
        min_free_disk_gb: float = 8.0,
        ffmpeg_threads: int = 2,
    ):
        self.base_url = base_url.rstrip("/")
        if not self.base_url.lower().startswith("https://"):
            raise WorkerError("O worker exige HTTPS; nenhuma conexão HTTP é permitida.")
        self.token = token.strip()
        if len(self.token) < 24:
            raise WorkerError("Token do worker ausente ou curto demais.")
        self.worker_id = worker_id or f"win-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.poll_seconds = max(5, int(poll_seconds))
        self.heartbeat_seconds = max(10, int(heartbeat_seconds))
        self.work_root = Path(work_root or (Path(tempfile.gettempdir()) / "codexia-local-worker")).resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.max_ram_percent = max(50.0, min(95.0, float(max_ram_percent)))
        self.min_free_disk_gb = max(2.0, float(min_free_disk_gb))
        self.ffmpeg_threads = max(1, min(2, int(ffmpeg_threads)))
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.token}", "User-Agent": "CodexiaLocalWorker/phase1"})
        self._busy_task_id: Optional[str] = None
        self._stop = threading.Event()
        self._single_job_lock = threading.Lock()

    def api(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}{path}"
        response = self.session.request(method, url, timeout=60, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except Exception:
                detail = response.text[:500]
            raise WorkerError(f"API {response.status_code}: {detail}")
        return response

    def inventory(self) -> Dict[str, Any]:
        disk = shutil.disk_usage(self.work_root)
        inv: Dict[str, Any] = {
            "hostname": socket.gethostname(),
            "os": platform.platform(),
            "python": sys.version.split()[0],
            "cpu_logical": os.cpu_count() or 1,
            "disk_total_gb": round(disk.total / (1024**3), 2),
            "disk_free_gb": round(disk.free / (1024**3), 2),
            "ffmpeg": self._ffmpeg_version(),
            "qsv_h264": self._ffmpeg_has_encoder("h264_qsv"),
            "gpu": self._gpu_inventory(),
        }
        if psutil is not None:
            try:
                vm = psutil.virtual_memory()
                inv.update({
                    "ram_total_gb": round(vm.total / (1024**3), 2),
                    "ram_available_gb": round(vm.available / (1024**3), 2),
                    "ram_percent": float(vm.percent),
                    "cpu_percent": float(psutil.cpu_percent(interval=0.2)),
                })
            except Exception:
                pass
        return inv

    def _ffmpeg_version(self) -> Optional[str]:
        try:
            cp = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10, check=False)
            return (cp.stdout.splitlines() or [None])[0]
        except Exception:
            return None

    def _ffmpeg_has_encoder(self, encoder: str) -> bool:
        try:
            cp = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=15, check=False)
            return encoder in cp.stdout
        except Exception:
            return False

    def _gpu_inventory(self) -> List[str]:
        if os.name != "nt":
            return []
        commands = [
            ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
            ["wmic", "path", "win32_VideoController", "get", "name"],
        ]
        for cmd in commands:
            try:
                cp = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
                values = [line.strip() for line in cp.stdout.splitlines() if line.strip() and line.strip().lower() != "name"]
                if values:
                    return values
            except Exception:
                continue
        return []

    def heartbeat(self) -> None:
        self.api("POST", "/local-worker/v1/heartbeat", json={
            "worker_id": self.worker_id,
            "version": "phase1",
            "inventory": self.inventory(),
            "busy_task_id": self._busy_task_id,
        })

    def preflight(self) -> None:
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            raise WorkerError("FFmpeg/ffprobe não encontrados no PATH.")
        disk = shutil.disk_usage(self.work_root)
        free_gb = disk.free / (1024**3)
        if free_gb < self.min_free_disk_gb:
            raise WorkerError(f"Disco livre insuficiente: {free_gb:.1f} GiB.")
        if psutil is not None:
            ram = psutil.virtual_memory()
            if float(ram.percent) > self.max_ram_percent:
                raise WorkerError(f"RAM já está em {ram.percent:.1f}%; limite configurado {self.max_ram_percent:.1f}%.")

    def run_forever(self) -> None:
        self.preflight()
        next_heartbeat = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now >= next_heartbeat:
                try:
                    self.heartbeat()
                except Exception as exc:
                    print(f"heartbeat warning: {exc}", flush=True)
                next_heartbeat = now + self.heartbeat_seconds
            if self._busy_task_id is None:
                try:
                    lease = self.api("POST", "/local-worker/v1/lease", json={"worker_id": self.worker_id}).json()
                    if lease.get("leased"):
                        self.process_manifest(lease["manifest"])
                except Exception as exc:
                    print(f"lease warning: {exc}", flush=True)
            self._stop.wait(self.poll_seconds)

    def process_manifest(self, manifest: Dict[str, Any]) -> None:
        if not self._single_job_lock.acquire(blocking=False):
            raise WorkerError("Já existe um render em execução neste worker.")
        task_id = str(manifest.get("task_id") or "").strip()
        if not task_id:
            self._single_job_lock.release()
            raise WorkerError("Manifesto sem task_id.")
        self._busy_task_id = task_id
        task_dir = self.work_root / task_id
        try:
            self.preflight()
            if task_dir.exists():
                shutil.rmtree(task_dir, ignore_errors=True)
            task_dir.mkdir(parents=True, exist_ok=True)
            assets = self.download_assets(manifest, task_dir)
            output = self.render(manifest, assets, task_dir)
            self.upload(task_id, output)
        except Exception as exc:
            try:
                self.api("POST", f"/local-worker/v1/tasks/{task_id}/failed", json={"worker_id": self.worker_id, "error": str(exc)[:2000]})
            except Exception:
                pass
            raise
        finally:
            self._busy_task_id = None
            shutil.rmtree(task_dir, ignore_errors=True)
            self._single_job_lock.release()

    def download_assets(self, manifest: Dict[str, Any], task_dir: Path) -> List[Dict[str, Any]]:
        downloaded: List[Dict[str, Any]] = []
        for item in manifest.get("assets") or []:
            index = int(item["index"])
            filename = os.path.basename(str(item.get("filename") or f"asset-{index}"))
            path = task_dir / f"{index:03d}-{filename}"
            headers = {"X-Worker-Id": self.worker_id}
            response = self.api("GET", str(item["download_url"]), headers=headers, stream=True)
            digest = hashlib.sha256()
            total = 0
            with open(path, "wb") as fh:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
            if total != int(item.get("bytes") or total):
                raise WorkerError(f"Tamanho divergente no ativo {index}.")
            expected_hash = str(item.get("sha256") or "")
            if expected_hash and digest.hexdigest() != expected_hash:
                raise WorkerError(f"SHA-256 divergente no ativo {index}.")
            copied = dict(item)
            copied["local_path"] = str(path)
            downloaded.append(copied)
        return downloaded

    def _audio_duration(self, audio_path: str) -> float:
        cp = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True, timeout=30, check=False,
        )
        try:
            return max(0.1, float(cp.stdout.strip()))
        except Exception as exc:
            raise WorkerError("Não foi possível medir a duração do áudio preservado.") from exc

    def render(self, manifest: Dict[str, Any], assets: List[Dict[str, Any]], task_dir: Path) -> Path:
        images = [a for a in assets if a.get("kind") == "image"]
        audios = [a for a in assets if a.get("kind") == "audio"]
        captions = [a for a in assets if a.get("kind") == "captions"]
        if not images or not audios:
            raise WorkerError("Manifesto sem imagens/áudio preservados.")
        audio_path = str(audios[0]["local_path"])
        duration = self._audio_duration(audio_path)
        per_image = max(0.25, duration / len(images))
        concat = task_dir / "images.txt"
        lines: List[str] = []
        for item in images:
            safe = str(item["local_path"]).replace("'", "'\\''")
            lines.append(f"file '{safe}'")
            lines.append(f"duration {per_image:.6f}")
        safe_last = str(images[-1]["local_path"]).replace("'", "'\\''")
        lines.append(f"file '{safe_last}'")
        concat.write_text("\n".join(lines) + "\n", encoding="utf-8")

        render_cfg = manifest.get("render") or {}
        width = max(640, min(3840, int(render_cfg.get("width") or 1280)))
        height = max(360, min(2160, int(render_cfg.get("height") or 720)))
        fps = max(24, min(60, int(render_cfg.get("fps") or 30)))
        codec = "h264_qsv" if self._ffmpeg_has_encoder("h264_qsv") else "libx264"
        output = task_dir / "final.mp4"
        vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps},format=yuv420p"
        if captions:
            subtitle_path = str(captions[0]["local_path"]).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
            vf += f",subtitles='{subtitle_path}'"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-f", "concat", "-safe", "0", "-i", str(concat),
            "-i", audio_path,
            "-vf", vf,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", codec, "-threads", str(self.ffmpeg_threads),
            "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
            str(output),
        ]
        if codec == "h264_qsv":
            cmd[cmd.index("-c:a"):cmd.index("-c:a")] = ["-global_quality", "23"]
        else:
            cmd[cmd.index("-c:a"):cmd.index("-c:a")] = ["-preset", "veryfast", "-crf", "21"]
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=max(300, int(duration * 4)), check=False)
        if cp.returncode != 0 or not output.is_file() or output.stat().st_size < 1024:
            raise WorkerError(f"FFmpeg falhou: {cp.stderr[-1200:]}")
        return output

    def upload(self, task_id: str, output: Path) -> None:
        with open(output, "rb") as fh:
            response = self.api(
                "POST",
                f"/local-worker/v1/tasks/{task_id}/complete",
                params={"worker_id": self.worker_id},
                files={"file": (output.name, fh, "video/mp4")},
                timeout=600,
            )
        data = response.json()
        if not data.get("ok"):
            raise WorkerError("Servidor não confirmou o MP4 final.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codexia Local Render Worker - Phase 1")
    parser.add_argument("--base-url", default=os.getenv("CODEXIA_LOCAL_WORKER_BASE_URL", ""))
    parser.add_argument("--token", default=os.getenv("CODEXIA_LOCAL_WORKER_TOKEN", ""))
    parser.add_argument("--worker-id", default=os.getenv("CODEXIA_LOCAL_WORKER_ID", ""))
    parser.add_argument("--work-root", default=os.getenv("CODEXIA_LOCAL_WORKER_ROOT", ""))
    parser.add_argument("--poll-seconds", type=int, default=int(os.getenv("CODEXIA_LOCAL_WORKER_POLL_SECONDS", "15")))
    parser.add_argument("--max-ram-percent", type=float, default=float(os.getenv("CODEXIA_LOCAL_WORKER_MAX_RAM_PERCENT", "85")))
    parser.add_argument("--min-free-disk-gb", type=float, default=float(os.getenv("CODEXIA_LOCAL_WORKER_MIN_FREE_DISK_GB", "8")))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.base_url or not args.token:
        print("Defina CODEXIA_LOCAL_WORKER_BASE_URL e CODEXIA_LOCAL_WORKER_TOKEN.", file=sys.stderr)
        return 2
    agent = LocalRenderAgent(
        base_url=args.base_url,
        token=args.token,
        worker_id=args.worker_id or None,
        work_root=args.work_root or None,
        poll_seconds=args.poll_seconds,
        max_ram_percent=args.max_ram_percent,
        min_free_disk_gb=args.min_free_disk_gb,
    )
    print(json.dumps({"worker_id": agent.worker_id, "inventory": agent.inventory()}, ensure_ascii=False, indent=2))
    agent.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
