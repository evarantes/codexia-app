"""Verified, task-scoped transfer; API is archive, worker is cache.

Disabled unless configured. Never deletes a file on an unverified transfer.
"""
import hashlib
import os
import re
import shutil
import threading
from pathlib import Path
from urllib.parse import urlparse
import requests

LOCK = threading.RLock()
_ACK = {}
EXTENSIONS = {'image': {'.png', '.jpg', '.jpeg', '.webp'},
              'audio': {'.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac'},
              'video': {'.mp4', '.mov', '.mkv', '.webm'},
              'caption': {'.srt', '.vtt', '.ass'}, 'script': {'.json'}}


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def roots():
    from app.config import IMAGES_OUTPUT_DIR, AUDIO_OUTPUT_DIR, VIDEO_OUTPUT_DIR
    from app.services.production_manifest import _root_dir
    return {'image': Path(IMAGES_OUTPUT_DIR), 'audio': Path(AUDIO_OUTPUT_DIR),
            'video': Path(VIDEO_OUTPUT_DIR), 'caption': Path(VIDEO_OUTPUT_DIR),
            'script': _root_dir()}


def task_key(task):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', str(task)):
        raise ValueError('ID de produção inválido')
    return str(task)


def archive_path(kind, filename, task=None):
    """Resolve new archive writes inside the task namespace.

    Without a task this remains a filename validator/legacy path resolver
    for callers that only inspect downloaded metadata.
    """
    if kind not in EXTENSIONS or Path(filename).name != filename or not re.fullmatch(r'[A-Za-z0-9_.-]{1,240}', filename):
        raise ValueError('Ativo inválido')
    if Path(filename).suffix.lower() not in EXTENSIONS[kind]:
        raise ValueError('Extensão inválida')
    root = roots()[kind].resolve()
    if task is not None:
        root = root / task_key(task)
    path = (root / filename).resolve()
    if path.parent != root:
        raise ValueError('Caminho inválido')
    return path


def index_path(task):
    from app.services.production_manifest import manifest_dir
    return manifest_dir(task_key(task)) / 'central-index.json'


def central_index(task, verify=True):
    from app.services.production_manifest import load_manifest, _read_json
    entries = list((_read_json(index_path(task)).get('files') or []))
    for item in load_manifest(task).get('artifacts') or []:
        kind = item.get('kind')
        raw = item.get('durable_path') or item.get('original_path')
        if kind not in EXTENSIONS or not raw:
            continue
        p = Path(raw)
        allowed = list(roots().values())
        if p.is_file() and any(p.resolve().is_relative_to(r.resolve()) for r in allowed):
            entries.append({'kind': kind, 'filename': Path(item.get('original_path') or p).name, 'path': str(p),
                            'sha256': digest(p) if verify else '', 'size': p.stat().st_size})
    unique = {}
    for item in entries:
        p = Path(item.get('path') or '')
        if p.is_file() and any(p.resolve().is_relative_to(r.resolve()) for r in roots().values()):
            actual = dict(item)
            if verify:
                actual['sha256'] = digest(p)
                actual['size'] = p.stat().st_size
            unique[(item['kind'], item['filename'])] = actual
    return list(unique.values())


def register(task, entry):
    from app.services.production_manifest import _atomic_write_json, _read_json
    with LOCK:
        path = index_path(task)
        data = _read_json(path)
        entries = {(e['kind'], e['filename']): e for e in data.get('files') or []}
        entries[(entry['kind'], entry['filename'])] = entry
        _atomic_write_json(path, {'files': list(entries.values())})


class CentralAssetTransfer:
    def __init__(self):
        self.url = (os.getenv('CODEXIA_ASSET_ARCHIVE_URL') or '').rstrip('/')
        self.token = os.getenv('CODEXIA_ASSET_TRANSFER_TOKEN') or ''
        if self.url:
            parsed = urlparse(self.url)
            if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or parsed.fragment or not self.token:
                raise RuntimeError('Arquivo central exige URL HTTPS e token de transferência configurado.')
        self.session = requests.Session()
        self.session.headers['X-Codexia-Asset-Token'] = self.token
        self._remote_index_cache = {}

    @property
    def enabled(self):
        return bool(self.url)

    def endpoint(self, task, suffix=''):
        return f'{self.url}/internal/production-assets/{task_key(task)}{suffix}'

    def _remote_index(self, task):
        task = task_key(task)
        if task in self._remote_index_cache:
            return self._remote_index_cache[task]
        response = self.session.get(self.endpoint(task), timeout=(10, 60), allow_redirects=False)
        response.raise_for_status()
        entries = {}
        for item in response.json().get('files') or []:
            kind = str(item.get('kind') or '')
            filename = str(item.get('filename') or '')
            if kind in EXTENSIONS and Path(filename).name == filename:
                entries[(kind, filename)] = {
                    'sha256': str(item.get('sha256') or ''),
                    'size': int(item.get('size') or 0),
                }
        self._remote_index_cache[task] = entries
        return entries

    def publish(self, task, path, kind, filename=None):
        if not self.enabled:
            return
        p = Path(path)
        filename = filename or p.name
        key = (self.url, str(task), str(p), kind, filename)
        stamp = (p.stat().st_size, p.stat().st_mtime_ns)
        if _ACK.get(key) == stamp:
            return
        sha = digest(p)
        size = p.stat().st_size
        try:
            existing = self._remote_index(task).get((kind, filename))
        except Exception:
            existing = None
        if (
            existing
            and existing.get('sha256') == sha
            and int(existing.get('size') or -1) == size
        ):
            _ACK[key] = stamp
            return
        expected_previous_sha = ''
        response = None
        for _attempt in range(3):
            headers = {'X-Content-SHA256': sha}
            if expected_previous_sha:
                headers['X-Expected-Previous-SHA256'] = expected_previous_sha
            with p.open('rb') as stream:
                response = self.session.put(self.endpoint(task, f'/{kind}/{filename}'), data=stream,
                    headers=headers, timeout=(10, 300), allow_redirects=False)
            if response.status_code != 409:
                response.raise_for_status()
                break

            # A renderer can publish an early checkpoint while an MP4 is still
            # being finalized. If the same task/name now contains those earlier
            # bytes, replace them only with a compare-and-swap against the hash
            # returned by the archive. This also recovers retries racing each
            # other without clobbering a newer upload.
            try:
                detail = response.json().get('detail') or {}
                existing_sha = str(detail.get('existing_sha256') or '').strip().lower() if isinstance(detail, dict) else ''
            except Exception:
                existing_sha = ''
            if not re.fullmatch(r'[0-9a-f]{64}', existing_sha):
                response.raise_for_status()
            if existing_sha == sha:
                # O arquivo central já contém exatamente estes bytes: trata como
                # sucesso idempotente para permitir reenvios após reinício do worker.
                _ACK[key] = stamp
                self._remote_index_cache.setdefault(task_key(task), {})[(kind, filename)] = {
                    'sha256': sha,
                    'size': size,
                }
                return
            expected_previous_sha = existing_sha
        else:
            response.raise_for_status()
        result = response.json()
        if result.get('sha256') != sha or int(result.get('size', -1)) != p.stat().st_size:
            raise RuntimeError('Arquivo central não confirmou integridade; cópia local preservada.')
        _ACK[key] = stamp
        self._remote_index_cache.setdefault(task_key(task), {})[(kind, filename)] = {
            'sha256': sha,
            'size': size,
        }

    def hydrate(self, task):
        if not self.enabled:
            return {}
        response = self.session.get(self.endpoint(task), timeout=(10, 60), allow_redirects=False)
        response.raise_for_status()
        from app.services.production_manifest import manifest_dir, record_artifact
        cache = manifest_dir(task) / 'central-cache'
        cache.mkdir(exist_ok=True)
        mapping = {}
        for entry in response.json().get('files') or []:
            kind, filename = entry['kind'], entry['filename']
            archive_path(kind, filename)  # validate server metadata before constructing a URL/path
            dest = cache / filename
            if dest.is_file() and digest(dest) == entry['sha256']:
                mapping[filename] = str(dest)
                continue
            tmp = dest.with_suffix(dest.suffix + '.part')
            try:
                with self.session.get(self.endpoint(task, f'/{kind}/{filename}'), stream=True,
                        timeout=(10, 300), allow_redirects=False) as download:
                    download.raise_for_status()
                    with tmp.open('wb') as stream:
                        for chunk in download.iter_content(1024 * 1024):
                            stream.write(chunk)
                if digest(tmp) != entry['sha256'] or tmp.stat().st_size != entry['size']:
                    raise RuntimeError('Ativo recebido incompleto; render não iniciado.')
                os.replace(tmp, dest)
                record_artifact(task, str(dest), kind=kind, source='central_archive')
                mapping[filename] = str(dest)
            finally:
                tmp.unlink(missing_ok=True)
        return mapping

    def publish_manifest(self, task, *, include_video=True):
        if not self.enabled:
            return
        from app.services.production_manifest import load_manifest, manifest_dir
        for entry in load_manifest(task).get('artifacts') or []:
            p = Path(entry.get('durable_path') or entry.get('original_path') or '')
            if (entry.get('kind') in EXTENSIONS
                    and (include_video or entry.get('kind') != 'video')
                    and p.is_file()):
                self.publish(task, p, entry['kind'], Path(entry.get('original_path') or p).name)
        script = manifest_dir(task) / 'script.json'
        if script.is_file():
            # Distinct task filename prevents scripts from overwriting each other.
            named = script.with_name(f'{task_key(task)}-{digest(script)[:16]}-script.json')
            named.write_bytes(script.read_bytes())
            self.publish(task, named, 'script')

    def clean_cache(self, task):
        """Delete verified task media only; preserve files referenced by another task."""
        if not self.enabled:
            return
        from app.services.production_manifest import manifest_dir, load_manifest, _root_dir, _read_json
        response = self.session.get(self.endpoint(task), timeout=(10, 60), allow_redirects=False)
        response.raise_for_status()
        hashes = {e['sha256'] for e in response.json().get('files') or []}
        shared = set()
        for manifest in _root_dir().glob('*/manifest.json'):
            if manifest.parent.name == str(task):
                continue
            for item in _read_json(manifest).get('artifacts') or []:
                for key in ('original_path', 'durable_path'):
                    if item.get(key):
                        shared.add(str(Path(item[key]).resolve()))
        manifest_root = manifest_dir(task)
        candidates = set((manifest_root / 'central-cache').glob('*'))
        candidates.update(manifest_root.glob(f'{task_key(task)}-*-script.json'))
        local_script = manifest_root / 'script.json'
        if local_script.is_file():
            candidates.add(local_script)
        for item in load_manifest(task).get('artifacts') or []:
            if item.get('source') == 'filesystem_checkpoint':
                continue
            for key in ('original_path', 'durable_path'):
                if item.get(key):
                    candidates.add(Path(item[key]))
        allowed = [r.resolve() for r in roots().values()]
        for p in candidates:
            resolved = p.resolve()
            if str(resolved) in shared or not any(resolved.is_relative_to(r) for r in allowed):
                continue
            if p.is_file() and digest(p) in hashes:
                p.unlink()


    def cleanup_archived_worker_cache(
        self,
        current_task,
        *,
        target_free_gb=5.0,
        max_tasks=12,
    ):
        """Recupera espaço temporário do worker sem apagar dados não arquivados.

        Tarefas em execução permanecem intocadas. Cópias locais de tarefas pausadas,
        falhas ou concluídas só são removidas quando o arquivo central confirma o
        mesmo SHA-256; os ativos centrais e o manifesto permanecem intactos.
        """
        if not self.enabled:
            return {
                "enabled": False,
                "free_before_gb": 0.0,
                "free_after_gb": 0.0,
                "reclaimed_gb": 0.0,
                "tasks_checked": 0,
                "errors": 0,
            }

        from app.services.production_manifest import _root_dir, load_manifest, manifest_dir
        from app.services.task_manager import get_task

        storage_path = "/data" if os.path.isdir("/data") else "/"
        try:
            target_gb = max(1.0, min(50.0, float(target_free_gb)))
        except Exception:
            target_gb = 5.0
        try:
            task_limit = max(1, min(50, int(max_tasks)))
        except Exception:
            task_limit = 12

        def _free_bytes():
            return int(shutil.disk_usage(storage_path).free)

        before = _free_bytes()
        target_bytes = int(target_gb * (1024 ** 3))
        checked = 0
        errors = 0
        if before < target_bytes:
            manifests = []
            try:
                manifests = list(_root_dir().glob("*/manifest.json"))
                manifests.sort(key=lambda item: item.stat().st_mtime)
            except Exception:
                manifests = []

            allowed_roots = [root.resolve() for root in roots().values()]

            def _is_tracked_local_file(candidate):
                try:
                    resolved = Path(candidate).resolve()
                    return (
                        resolved.is_file()
                        and any(resolved.is_relative_to(root) for root in allowed_roots)
                    )
                except Exception:
                    return False

            for manifest_path in manifests:
                task_id = manifest_path.parent.name
                if task_id == str(current_task or ""):
                    continue
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
                    continue
                try:
                    task = get_task(task_id) or {}
                    status = str(task.get("status") or "").strip().lower()
                    if status not in {
                        "completed",
                        "awaiting_review",
                        "approved",
                        "rendered_upload_failed",
                        "failed",
                        "paused",
                    }:
                        continue

                    task_manifest_dir = manifest_dir(task_id)
                    local_candidates = list((task_manifest_dir / "central-cache").glob("*"))
                    local_candidates.extend(task_manifest_dir.glob(f"{task_id}-*-script.json"))
                    local_script = task_manifest_dir / "script.json"
                    if local_script.is_file():
                        local_candidates.append(local_script)
                    for artifact in load_manifest(task_id).get("artifacts") or []:
                        if artifact.get("source") == "filesystem_checkpoint":
                            continue
                        for key in ("original_path", "durable_path"):
                            raw_path = artifact.get(key)
                            if raw_path:
                                local_candidates.append(Path(raw_path))
                    if not any(_is_tracked_local_file(path) for path in local_candidates):
                        continue

                    checked += 1
                    # Arquiva primeiro os ativos antigos que ainda não chegaram ao
                    # servidor principal; só depois remove cópias confirmadas.
                    self.publish_manifest(task_id)
                    self.clean_cache(task_id)
                except Exception:
                    errors += 1
                if checked >= task_limit or _free_bytes() >= target_bytes:
                    break

        after = _free_bytes()
        return {
            "enabled": True,
            "storage_path": storage_path,
            "target_free_gb": round(target_gb, 2),
            "free_before_gb": round(before / (1024 ** 3), 2),
            "free_after_gb": round(after / (1024 ** 3), 2),
            "reclaimed_gb": round(max(0, after - before) / (1024 ** 3), 2),
            "tasks_checked": checked,
            "errors": errors,
        }


def map_cached_references(value, mapping):
    if isinstance(value, dict):
        return {k: map_cached_references(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [map_cached_references(v, mapping) for v in value]
    if isinstance(value, str):
        filename = Path(value.split('?', 1)[0]).name
        return mapping.get(filename, value)
    return value
