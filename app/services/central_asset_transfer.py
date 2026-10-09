"""Verified, task-scoped transfer; API is archive, worker is cache.

Disabled unless configured. Never deletes a file on an unverified transfer.
"""
import hashlib
import os
import re
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

    @property
    def enabled(self):
        return bool(self.url)

    def endpoint(self, task, suffix=''):
        return f'{self.url}/internal/production-assets/{task_key(task)}{suffix}'

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
        with p.open('rb') as stream:
            response = self.session.put(self.endpoint(task, f'/{kind}/{filename}'), data=stream,
                headers={'X-Content-SHA256': sha}, timeout=(10, 300), allow_redirects=False)
        response.raise_for_status()
        result = response.json()
        if result.get('sha256') != sha or int(result.get('size', -1)) != p.stat().st_size:
            raise RuntimeError('Arquivo central não confirmou integridade; cópia local preservada.')
        _ACK[key] = stamp

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

    def publish_manifest(self, task):
        if not self.enabled:
            return
        from app.services.production_manifest import load_manifest, manifest_dir
        for entry in load_manifest(task).get('artifacts') or []:
            p = Path(entry.get('durable_path') or entry.get('original_path') or '')
            if entry.get('kind') in EXTENSIONS and p.is_file():
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
        candidates = set((manifest_dir(task) / 'central-cache').glob('*'))
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


def map_cached_references(value, mapping):
    if isinstance(value, dict):
        return {k: map_cached_references(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [map_cached_references(v, mapping) for v in value]
    if isinstance(value, str):
        filename = Path(value.split('?', 1)[0]).name
        return mapping.get(filename, value)
    return value
