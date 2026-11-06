"""Verified, task-scoped transfer; API is archive, worker is cache.

Disabled unless configured. Never deletes a file on an unverified transfer.
"""
import hashlib
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlparse
import requests

LOCK = threading.RLock()
_ACK = {}
_PUBLISH_LOCKS = {}
_PUBLISH_LOCKS_GUARD = threading.Lock()
EXTENSIONS = {'image': {'.png', '.jpg', '.jpeg', '.webp'},
              'audio': {'.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac'},
              'video': {'.mp4', '.mov', '.mkv', '.webm'},
              'caption': {'.srt', '.vtt', '.ass'}, 'script': {'.json'}}
_VIDEO_RETENTION_STATUSES = {
    'completed', 'awaiting_review', 'approved', 'rendered_upload_failed', 'published',
}


def preserves_final_video(status):
    return str(status or '').strip().lower() in _VIDEO_RETENTION_STATUSES


def digest(path, progress_callback=None):
    h = hashlib.sha256()
    total = 0
    try:
        total = int(os.path.getsize(path)) if progress_callback else 0
    except OSError:
        total = 0
    processed = 0
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
            processed += len(chunk)
            if progress_callback:
                try:
                    progress_callback(processed, total)
                except Exception:
                    # Progress reporting must never interrupt checksum work.
                    pass
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


@contextmanager
def task_index_lock(task):
    """Serialize project-index updates across API worker processes."""
    lock_path = index_path(task).with_suffix('.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+b') as stream:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - local Windows development
            yield
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _acquire_publish_lock(key):
    with _PUBLISH_LOCKS_GUARD:
        entry = _PUBLISH_LOCKS.get(key)
        if entry is None:
            entry = {'lock': threading.Lock(), 'references': 0}
            _PUBLISH_LOCKS[key] = entry
        entry['references'] += 1
        return entry


def _release_publish_lock(key, entry):
    with _PUBLISH_LOCKS_GUARD:
        entry['references'] -= 1
        if entry['references'] <= 0 and _PUBLISH_LOCKS.get(key) is entry:
            _PUBLISH_LOCKS.pop(key, None)


def task_drive_account(task):
    """Return the immutable Drive account pinned to a production, if any."""
    from app.services.production_manifest import _read_json
    data = _read_json(index_path(task_key(task)))
    account_id = data.get('drive_account_id')
    if account_id not in (None, ''):
        return int(account_id)
    for item in data.get('files') or []:
        if item.get('drive_file_id') and item.get('drive_account_id') not in (None, ''):
            return int(item['drive_account_id'])
    return None


def pin_task_drive_account(task, account_id):
    """Pin a project before upload sessions are issued; never move an archive."""
    from app.services.production_manifest import _atomic_write_json, _read_json
    task = task_key(task)
    account_id = int(account_id)
    with LOCK, task_index_lock(task):
        path = index_path(task)
        data = _read_json(path)
        current = data.get('drive_account_id')
        if current not in (None, '') and int(current) != account_id:
            raise ValueError('Este projeto já está vinculado a outra conta do Google Drive.')
        referenced = {
            int(item['drive_account_id'])
            for item in data.get('files') or []
            if item.get('drive_file_id') and item.get('drive_account_id') not in (None, '')
        }
        if referenced and referenced != {account_id}:
            raise ValueError('Os ativos deste projeto apontam para contas diferentes do Google Drive.')
        data['drive_account_id'] = account_id
        _atomic_write_json(path, data)
    return account_id


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
        p = Path(item.get('path') or '') if item.get('path') else None
        if p and p.is_file() and any(p.resolve().is_relative_to(r.resolve()) for r in roots().values()):
            actual = dict(item)
            if verify:
                actual['sha256'] = digest(p)
                actual['size'] = p.stat().st_size
            unique[(item['kind'], item['filename'])] = actual
        elif item.get('drive_file_id') and item.get('sha256') and item.get('size') is not None:
            # Drive-backed assets remain discoverable after all full-size local
            # copies have been removed from the production servers.
            unique[(item['kind'], item['filename'])] = dict(item)
    return list(unique.values())


def find_drive_asset(kind, filename):
    """Find the most recently indexed Drive copy for public media routes."""
    from app.services.production_manifest import _root_dir, _read_json
    if kind not in EXTENSIONS or Path(str(filename or '')).name != str(filename or ''):
        return None
    matches = []
    try:
        indexes = list(_root_dir().glob('*/central-index.json'))
        indexes.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        for path in indexes:
            for entry in (_read_json(path).get('files') or []):
                if (entry.get('kind') == kind and entry.get('filename') == filename
                        and entry.get('drive_file_id') and entry.get('sha256')):
                    matches.append(entry)
        return matches[0] if matches else None
    except Exception:
        return None


def register(task, entry, expected_previous_sha256=None):
    from app.services.production_manifest import _atomic_write_json, _read_json
    with LOCK, task_index_lock(task):
        path = index_path(task)
        data = _read_json(path)
        account_id = entry.get('drive_account_id')
        current_account_id = data.get('drive_account_id')
        if account_id not in (None, ''):
            account_id = int(account_id)
            if current_account_id not in (None, '') and int(current_account_id) != account_id:
                raise ValueError('O ativo foi enviado a uma conta diferente da conta vinculada ao projeto.')
            data['drive_account_id'] = account_id
        entries = {(e['kind'], e['filename']): e for e in data.get('files') or []}
        existing = entries.get((entry['kind'], entry['filename']))
        session_key = f"{entry['kind']}/{entry['filename']}"
        pending = (data.get('pending_uploads') or {}).get(session_key)
        if pending and (
            pending.get('sha256') != entry.get('sha256')
            or int(pending.get('size') or -1) != int(entry.get('size') or -2)
        ):
            raise ValueError('O arquivo concluído não corresponde à sessão de upload ativa.')
        if existing and existing.get('drive_file_id') and existing.get('sha256') != entry.get('sha256'):
            previous = str(
                expected_previous_sha256
                or (pending or {}).get('expected_previous_sha256')
                or ''
            ).strip().lower()
            if not previous or str(existing.get('sha256') or '') != previous:
                raise ValueError('Uma versão mais recente deste ativo já foi registrada no Google Drive.')
        if (existing and existing.get('drive_account_id') not in (None, '')
                and account_id not in (None, '')
                and int(existing['drive_account_id']) != account_id):
            raise ValueError('O nome do ativo já está vinculado a outra conta do Google Drive.')
        entries[(entry['kind'], entry['filename'])] = entry
        data['files'] = list(entries.values())
        pending = data.get('pending_uploads') or {}
        pending.pop(session_key, None)
        data['pending_uploads'] = pending
        _atomic_write_json(path, data)


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

    def drive_endpoint(self, task, suffix=''):
        return f'{self.url}/settings/google-drive/{task_key(task)}{suffix}'

    def _remote_index(self, task):
        task = task_key(task)
        if task in self._remote_index_cache:
            return self._remote_index_cache[task]
        response = self.session.get(self.endpoint(task), timeout=(10, 60), allow_redirects=False)
        response.raise_for_status()
        entries = {}
        for item in response.json().get('files') or []:
            if not item.get('drive_file_id'):
                continue
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
        lock_key = (self.url, task_key(task), str(kind), str(filename))
        entry = _acquire_publish_lock(lock_key)
        try:
            with entry['lock']:
                return self._publish_locked(task, p, kind, filename)
        finally:
            _release_publish_lock(lock_key, entry)

    def _publish_locked(self, task, p, kind, filename):
        if not self.enabled:
            return
        key = (self.url, str(task), str(p), kind, filename)
        stamp = (p.stat().st_size, p.stat().st_mtime_ns)
        if _ACK.get(key) == stamp:
            return
        size = stamp[0]
        sha_hasher = hashlib.sha256()
        expected_md5 = hashlib.md5()
        hashed_size = 0
        with p.open('rb') as stream:
            for hash_chunk in iter(lambda: stream.read(1024 * 1024), b''):
                hashed_size += len(hash_chunk)
                sha_hasher.update(hash_chunk)
                expected_md5.update(hash_chunk)
        current_stat = p.stat()
        if (hashed_size != size or current_stat.st_size != stamp[0]
                or current_stat.st_mtime_ns != stamp[1]):
            raise RuntimeError('O arquivo local mudou durante a preparação; nova tentativa será necessária.')
        sha = sha_hasher.hexdigest()
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
        for _attempt in range(3):
            response = self.session.post(
                self.drive_endpoint(task, '/upload-session'),
                json={
                    'kind': kind,
                    'filename': filename,
                    'sha256': sha,
                    'size': size,
                    'expected_previous_sha256': expected_previous_sha,
                },
                timeout=(10, 90),
                allow_redirects=False,
            )
            if response.status_code == 409:
                # A renderer can publish an early checkpoint while an MP4 is
                # still being finalized. Replace it only against the previous
                # hash returned by Drive's compare-and-swap check.
                try:
                    detail = response.json().get('detail') or {}
                    existing_sha = str(detail.get('existing_sha256') or '').strip().lower() if isinstance(detail, dict) else ''
                except Exception:
                    existing_sha = ''
                if not re.fullmatch(r'[0-9a-f]{64}', existing_sha):
                    response.raise_for_status()
                if existing_sha == sha:
                    self._remote_index_cache.setdefault(task_key(task), {})[(kind, filename)] = {
                        'sha256': sha,
                        'size': size,
                    }
                    _ACK[key] = stamp
                    return
                expected_previous_sha = existing_sha
                continue
            response.raise_for_status()
            session = response.json()
            if session.get('already_present'):
                entry = session.get('entry') or {}
                if entry.get('sha256') != sha or int(entry.get('size') or -1) != size:
                    raise RuntimeError('Google Drive retornou um ativo diferente do arquivo local.')
                _ACK[key] = stamp
                self._remote_index_cache.setdefault(task_key(task), {})[(kind, filename)] = {
                    'sha256': sha,
                    'size': size,
                }
                return
            upload_url = str(session.get('upload_url') or '')
            if not upload_url.startswith('https://www.googleapis.com/upload/drive/v3/files'):
                raise RuntimeError('O servidor não retornou uma sessão direta válida do Google Drive.')

            def query_drive_session():
                try:
                    response = requests.put(
                        upload_url,
                        data=b'',
                        headers={
                            'Content-Length': '0',
                            'Content-Range': f'bytes */{size}',
                        },
                        timeout=(15, 60),
                        allow_redirects=False,
                    )
                except requests.RequestException as exc:
                    raise RuntimeError(
                        'Não foi possível consultar a sessão retomável do Google Drive; arquivo local preservado.'
                    ) from exc
                if response.status_code == 308:
                    server_range = str(response.headers.get('Range') or '')
                    match = re.search(r'bytes=0-(\d+)$', server_range)
                    return (int(match.group(1)) + 1 if match else 0), None
                if response.status_code in (200, 201):
                    return size, response.json()
                if response.status_code in (404, 410):
                    raise RuntimeError(
                        'A sessão de upload do Google Drive expirou; o arquivo local foi preservado.'
                    )
                response.raise_for_status()
                raise RuntimeError('Google Drive retornou um estado inesperado para a sessão retomável.')

            # The session URL may survive a CX33 restart. Query Drive before
            # sending anything so an already accepted range is never replayed.
            offset, final_metadata = query_drive_session()
            with p.open('rb') as stream:
                while offset < size and final_metadata is None:
                    stream.seek(offset)
                    chunk = stream.read(min(8 * 1024 * 1024, size - offset))
                    if not chunk:
                        raise RuntimeError('O arquivo local mudou durante o upload direto ao Google Drive.')
                    end = offset + len(chunk) - 1
                    upload_headers = {
                        'Content-Length': str(len(chunk)),
                        'Content-Range': f'bytes {offset}-{end}/{size}',
                    }
                    try:
                        uploaded = requests.put(
                            upload_url, data=chunk, headers=upload_headers,
                            timeout=(15, 1800), allow_redirects=False,
                        )
                    except requests.RequestException:
                        uploaded = None
                    if uploaded is None or uploaded.status_code == 503:
                        retry_after = int((uploaded.headers.get('Retry-After') if uploaded else 0) or 0)
                        if retry_after > 0:
                            time.sleep(min(60, retry_after))
                        # Query Drive's resumable session to recover an unknown
                        # response without retransferring bytes through Codexia.
                        offset, final_metadata = query_drive_session()
                        if final_metadata is None:
                            continue
                        break
                    if uploaded.status_code == 308:
                        server_range = str(uploaded.headers.get('Range') or '')
                        match = re.search(r'bytes=0-(\d+)$', server_range)
                        next_offset = int(match.group(1)) + 1 if match else 0
                        if next_offset <= offset or next_offset > size:
                            raise RuntimeError('Google Drive retornou uma posição inválida para o upload retomável.')
                        offset = next_offset
                        continue
                    if uploaded.status_code in (200, 201):
                        final_metadata = uploaded.json()
                        offset = size
                        break
                    if uploaded.status_code in (404, 410):
                        raise RuntimeError('A sessão de upload do Google Drive expirou; o arquivo local foi preservado.')
                    uploaded.raise_for_status()

            if (not final_metadata
                    or int(final_metadata.get('size') or -1) != size
                    or str(final_metadata.get('md5Checksum') or '').lower() != expected_md5.hexdigest()):
                raise RuntimeError('Google Drive não confirmou o tamanho e checksum; arquivo local preservado.')
            final_stat = p.stat()
            if final_stat.st_size != stamp[0] or final_stat.st_mtime_ns != stamp[1]:
                raise RuntimeError('O arquivo local mudou durante o upload; ele foi preservado para nova tentativa.')
            entry = {
                'kind': kind,
                'filename': filename,
                'drive_file_id': str(final_metadata.get('id') or ''),
                'drive_account_id': session.get('drive_account_id'),
                'sha256': sha,
                'size': size,
            }
            registered = self.session.post(
                self.drive_endpoint(task, '/register'),
                json=entry,
                timeout=(10, 90),
                allow_redirects=False,
            )
            registered.raise_for_status()
            result = registered.json().get('entry') or {}
            if result.get('sha256') != sha or int(result.get('size') or -1) != size:
                raise RuntimeError('O servidor principal não confirmou os metadados do arquivo no Drive.')
            _ACK[key] = stamp
            self._remote_index_cache.setdefault(task_key(task), {})[(kind, filename)] = {
                'sha256': sha,
                'size': size,
            }
            return

        raise RuntimeError('Google Drive não aceitou a substituição após várias tentativas.')

    def hydrate(self, task):
        if not self.enabled:
            return {}
        response = self.session.get(self.endpoint(task), timeout=(10, 60), allow_redirects=False)
        response.raise_for_status()
        from app.services.production_manifest import manifest_dir, record_artifact
        cache = manifest_dir(task) / 'central-cache'
        cache.mkdir(exist_ok=True)
        mapping = {}
        access_token = None
        drive_files = [entry for entry in response.json().get('files') or [] if entry.get('drive_file_id')]
        if drive_files:
            access_response = self.session.get(
                self.drive_endpoint(task, '/access'), timeout=(10, 90), allow_redirects=False
            )
            access_response.raise_for_status()
            access_token = str(access_response.json().get('access_token') or '')
            if not access_token:
                raise RuntimeError('O servidor não autorizou a recuperação direta do Google Drive.')
        for entry in response.json().get('files') or []:
            kind, filename = entry['kind'], entry['filename']
            archive_path(kind, filename)  # validate server metadata before constructing a URL/path
            dest = cache / filename
            if dest.is_file() and digest(dest) == entry['sha256']:
                mapping[filename] = str(dest)
                continue
            tmp = dest.with_suffix(dest.suffix + '.part')
            try:
                if entry.get('drive_file_id'):
                    drive_url = (
                        'https://www.googleapis.com/drive/v3/files/'
                        f"{quote(str(entry['drive_file_id']), safe='')}"
                    )
                    download = requests.get(
                        drive_url, params={'alt': 'media'},
                        headers={'Authorization': f'Bearer {access_token}'},
                        stream=True, timeout=(15, 300), allow_redirects=True,
                    )
                    if download.status_code == 401:
                        download.close()
                        access_response = self.session.get(
                            self.drive_endpoint(task, '/access'), timeout=(10, 90), allow_redirects=False
                        )
                        access_response.raise_for_status()
                        access_token = str(access_response.json().get('access_token') or '')
                        download = requests.get(
                            drive_url, params={'alt': 'media'},
                            headers={'Authorization': f'Bearer {access_token}'},
                            stream=True, timeout=(15, 300), allow_redirects=True,
                        )
                else:
                    download = self.session.get(
                        self.endpoint(task, f'/{kind}/{filename}'), stream=True,
                        timeout=(10, 300), allow_redirects=False,
                    )
                with download:
                    download.raise_for_status()
                    with tmp.open('wb') as stream:
                        for chunk in download.iter_content(1024 * 1024):
                            stream.write(chunk)
                if digest(tmp) != entry['sha256'] or tmp.stat().st_size != entry['size']:
                    raise RuntimeError('Ativo recebido incompleto; render não iniciado.')
                os.replace(tmp, dest)
                record_artifact(
                    task, str(dest), kind=kind,
                    source='google_drive_archive' if entry.get('drive_file_id') else 'central_archive',
                )
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

    def _worker_manifest_path_owners(self):
        """Read manifest ownership once for a cleanup batch on the worker."""
        from app.services.production_manifest import _read_json, _root_dir

        owners = {}
        resolved_cache = {}
        for manifest in Path(_root_dir()).glob("*/manifest.json"):
            task_id = manifest.parent.name
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
                continue
            for item in _read_json(manifest).get("artifacts") or []:
                if not isinstance(item, dict):
                    continue
                for key in ("original_path", "durable_path"):
                    raw = item.get(key)
                    if not raw:
                        continue
                    raw_path = str(raw)
                    if raw_path in resolved_cache:
                        resolved = resolved_cache[raw_path]
                    else:
                        path = Path(raw_path)
                        try:
                            if path.is_symlink():
                                resolved = None
                            else:
                                resolved = path.resolve(strict=True)
                        except (OSError, RuntimeError):
                            resolved = None
                        resolved_cache[raw_path] = resolved
                    if resolved is not None:
                        owners.setdefault(resolved, set()).add(task_id)
        return owners

    def clean_cache(self, task, *, shared_path_owners=None):
        """Remove only stable local copies matching the authoritative Drive index."""
        if not self.enabled:
            return {"files_deleted": 0, "bytes_freed": 0}

        from app.services.production_manifest import (
            _read_json, _root_dir, load_manifest, manifest_dir,
        )
        from app.services.task_manager import get_task

        task = task_key(task)
        response = self.session.get(
            self.endpoint(task, "/drive-index"),
            timeout=(10, 60),
            allow_redirects=False,
        )
        response.raise_for_status()
        response_payload = response.json()
        if not isinstance(response_payload, dict):
            raise RuntimeError("Índice central do Google Drive inválido; arquivos locais preservados.")
        verified = {}
        ambiguous = set()
        for entry in response_payload.get("files") or []:
            if not isinstance(entry, dict) or not entry.get("drive_file_id"):
                continue
            kind = str(entry.get("kind") or "")
            filename = str(entry.get("filename") or "")
            sha = str(entry.get("sha256") or "").strip().lower()
            try:
                size = int(entry.get("size"))
                archive_path(kind, filename, task=task)
            except (TypeError, ValueError):
                continue
            if size <= 0 or not re.fullmatch(r"[0-9a-f]{64}", sha):
                continue
            key = (kind, filename)
            value = (sha, size)
            if key in verified and verified[key] != value:
                ambiguous.add(key)
                verified.pop(key, None)
            elif key not in ambiguous:
                verified[key] = value
        if not verified:
            return {"files_deleted": 0, "bytes_freed": 0, "errors": 0}

        manifest_root = manifest_dir(task)
        manifest = load_manifest(task)
        try:
            task_info = get_task(task) or {}
        except Exception:
            task_info = {}
        task_status = str(task_info.get('status') or manifest.get('status') or '').strip().lower()
        allowed = [Path(root).resolve() for root in roots().values()]
        from app.config import COVERS_OUTPUT_DIR
        allowed.append(Path(COVERS_OUTPUT_DIR).resolve())
        # Each local path is mapped to the exact Drive name that represents it.
        # Manifest durable copies can have generated filenames, so they use the
        # artifact's original filename just as publish_manifest() does.
        candidates = {}

        def add_candidate(raw_path, refs):
            if not raw_path:
                return
            # Production scripts are retained locally so the user can revise
            # a completed or failed production without restoring from Drive.
            refs = {(kind, filename) for kind, filename in refs if kind != 'script'}
            if not refs:
                return
            path = Path(raw_path)
            try:
                if path.is_symlink() or not path.is_file():
                    return
                resolved = path.resolve(strict=True)
                if not any(resolved.is_relative_to(root) for root in allowed):
                    return
            except (OSError, RuntimeError):
                return
            candidates.setdefault(resolved, set()).update(refs)

        for path in (manifest_root / "central-cache").glob("*"):
            kind = next((name for name, extensions in EXTENSIONS.items()
                         if path.suffix.lower() in extensions), None)
            if kind:
                add_candidate(path, {(kind, path.name)})
        for path in manifest_root.glob(f"{task}-*-script.json"):
            add_candidate(path, {("script", path.name)})

        local_script = manifest_root / "script.json"
        add_candidate(local_script, {("script", f"{task}-script.json")})

        for item in manifest.get("artifacts") or []:
            if not isinstance(item, dict) or item.get("source") == "filesystem_checkpoint":
                continue
            kind = str(item.get("kind") or "").strip().lower()
            if kind not in EXTENSIONS:
                continue
            original_path = str(item.get("original_path") or "")
            filename = Path(original_path).name if original_path else ""
            if not filename:
                continue
            ref = {(kind, filename)}
            for key in ("original_path", "durable_path"):
                add_candidate(item.get(key), ref)

        # A batch caller supplies one precomputed ownership map so this task
        # does not rescan every other project's manifest.
        shared_paths = None
        if shared_path_owners is None:
            # Keep the standalone cleanup path safe and backward compatible.
            shared_paths = set()
            for manifest in Path(_root_dir()).glob("*/manifest.json"):
                if manifest.parent.name == task:
                    continue
                for item in _read_json(manifest).get("artifacts") or []:
                    if not isinstance(item, dict):
                        continue
                    for key in ("original_path", "durable_path"):
                        raw = item.get(key)
                        if not raw:
                            continue
                        path = Path(raw)
                        try:
                            if path.is_symlink():
                                continue
                            shared_paths.add(path.resolve(strict=True))
                        except (OSError, RuntimeError):
                            continue

        groups = {}
        for path, refs in candidates.items():
            try:
                info = path.stat()
                inode = (int(info.st_dev), int(info.st_ino))
                groups.setdefault(inode, []).append((path, refs, info))
            except OSError:
                continue

        files_deleted = 0
        bytes_freed = 0
        for inode, group in groups.items():
            if shared_path_owners is None:
                is_shared = any(path in shared_paths for path, _, _ in group)
            else:
                is_shared = any(
                    any(owner != task for owner in shared_path_owners.get(path, ()))
                    for path, _, _ in group
                )
            if is_shared:
                continue
            try:
                link_count = int(group[0][2].st_nlink)
                if link_count != len(group) or any(int(info.st_nlink) != link_count for _, _, info in group):
                    continue
                expected = set()
                for _, refs, _ in group:
                    expected.update(refs)
                if (any(kind == 'video' for kind, _ in expected)
                        and preserves_final_video(task_status)):
                    # Keep finished video outputs locally available.
                    continue
                # Identical inode aliases may be removed together only when
                # every alias is represented in this task's verified archive.
                actual_sha = digest(group[0][0])
                actual_size = int(group[0][2].st_size)
                expected_kinds = {kind for kind, _ in expected}
                if (not expected_kinds
                        or any(not any(key[0] == kind and value == (actual_sha, actual_size)
                                       for key, value in verified.items())
                               for kind in expected_kinds)):
                    continue
                stable = True
                for path, _, before in group:
                    after = path.stat()
                    if (path.is_symlink()
                            or (int(after.st_dev), int(after.st_ino)) != inode
                            or int(after.st_size) != int(before.st_size)
                            or int(after.st_mtime_ns) != int(before.st_mtime_ns)
                            or int(after.st_nlink) != link_count):
                        stable = False
                        break
                if not stable:
                    continue
                for path, _, _ in group:
                    path.unlink(missing_ok=True)
                    files_deleted += 1
                bytes_freed += actual_size
            except OSError:
                continue

        return {"files_deleted": files_deleted, "bytes_freed": bytes_freed, "errors": 0}

    def cleanup_confirmed_worker_cache(
        self,
        task_ids=None,
        *,
        current_task=None,
        max_tasks=50,
    ):
        """Clean Drive-confirmed files for terminal tasks on this worker only."""
        try:
            task_limit = max(1, min(100, int(max_tasks)))
        except (TypeError, ValueError):
            task_limit = 50

        if not self.enabled:
            return {
                "enabled": False,
                "tasks_checked": 0,
                "tasks_cleaned": 0,
                "files_deleted": 0,
                "bytes_freed": 0,
                "free_before_bytes": 0,
                "free_after_bytes": 0,
                "reclaimed_bytes": 0,
                "errors": 0,
            }

        storage_paths = set()
        for root in roots().values():
            try:
                resolved = Path(root).resolve()
                if resolved.exists():
                    storage_paths.add((int(resolved.stat().st_dev), str(resolved)))
            except OSError:
                continue
        paths_by_device = {}
        for device, path in storage_paths:
            paths_by_device.setdefault(device, path)

        def free_bytes():
            total = 0
            for path in paths_by_device.values():
                try:
                    total += int(shutil.disk_usage(path).free)
                except OSError:
                    continue
            return total

        before = free_bytes()
        try:
            # This is the expensive cross-project scan. Do it once for the
            # whole batch instead of once for every task being cleaned.
            shared_path_owners = self._worker_manifest_path_owners()
        except Exception:
            # If the shared ownership index cannot be built, clean_cache falls
            # back to its original per-task scan rather than weakening safety.
            shared_path_owners = None
        checked = 0
        cleaned = 0
        files_deleted = 0
        bytes_freed = 0
        errors = 0
        from app.services.production_manifest import _root_dir
        from app.services.task_manager import get_task

        if task_ids is None:
            manifests = list(Path(_root_dir()).glob("*/manifest.json"))
            manifests.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0)
            candidates = [path.parent.name for path in manifests]
        else:
            supplied_ids = [task_ids] if isinstance(task_ids, str) else (task_ids or [])
            candidates = list(dict.fromkeys(str(value) for value in supplied_ids))
        for task_id in candidates:
            if checked >= task_limit:
                break
            if task_id == str(current_task or "") or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
                continue
            try:
                task = get_task(task_id)
                status = str((task if isinstance(task, dict) else {}).get("status") or "").strip().lower()
                if status not in {
                    "completed", "awaiting_review", "approved",
                    "rendered_upload_failed", "failed", "paused",
                    "cancelled", "published",
                }:
                    continue
                checked += 1
                result = self.clean_cache(
                    task_id, shared_path_owners=shared_path_owners
                )
                errors += int(result.get("errors") or 0)
                if result["files_deleted"]:
                    cleaned += 1
                    files_deleted += int(result["files_deleted"])
                    bytes_freed += int(result["bytes_freed"])
            except Exception:
                errors += 1

        after = free_bytes()
        return {
            "enabled": bool(self.enabled),
            "tasks_checked": checked,
            "tasks_cleaned": cleaned,
            "files_deleted": files_deleted,
            "bytes_freed": bytes_freed,
            "free_before_bytes": before,
            "free_after_bytes": after,
            "reclaimed_bytes": max(0, after - before),
            "errors": errors,
        }


    def cleanup_archived_worker_cache(
        self,
        current_task,
        *,
        target_free_gb=5.0,
        max_tasks=50,
    ):
        """Compatibility wrapper for the safe, Drive-confirmed cache sweep."""
        report = self.cleanup_confirmed_worker_cache(
            current_task=current_task, max_tasks=max_tasks
        )
        before = int(report.get("free_before_bytes") or 0)
        after = int(report.get("free_after_bytes") or 0)
        try:
            target_gb = max(1.0, min(50.0, float(target_free_gb)))
        except (TypeError, ValueError):
            target_gb = 5.0
        report.update({
            "target_free_gb": round(target_gb, 2),
            "free_before_gb": round(before / (1024 ** 3), 2),
            "free_after_gb": round(after / (1024 ** 3), 2),
            "reclaimed_gb": round(max(0, after - before) / (1024 ** 3), 2),
        })
        return report


def cleanup_confirmed_worker_cache_job(task_ids=None, *, max_tasks=25, **_queue_options):
    """RQ entry point for Google Drive-confirmed cleanup on the CX33 worker."""
    return CentralAssetTransfer().cleanup_confirmed_worker_cache(
        task_ids=task_ids, max_tasks=max_tasks
    )


def map_cached_references(value, mapping):
    if isinstance(value, dict):
        return {k: map_cached_references(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [map_cached_references(v, mapping) for v in value]
    if isinstance(value, str):
        filename = Path(value.split('?', 1)[0]).name
        return mapping.get(filename, value)
    return value
