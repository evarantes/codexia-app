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

    def clean_cache(self, task):
        """Delete verified task media only; preserve files referenced by another task."""
        if not self.enabled:
            return
        from app.services.production_manifest import manifest_dir, load_manifest, _root_dir, _read_json
        response = self.session.get(self.endpoint(task), timeout=(10, 60), allow_redirects=False)
        response.raise_for_status()
        hashes = {
            e['sha256'] for e in response.json().get('files') or []
            if e.get('drive_file_id') and e.get('sha256')
        }
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
        max_tasks=50,
    ):
        """Mantém o CX33 como worker temporário e o Google Drive como arquivo permanente.

        Tarefas em execução permanecem intocadas. Ativos de tarefas pendentes,
        pausadas, falhas ou concluídas são enviados ao arquivo central e removidos
        do CX33 somente depois que o SHA-256 é confirmado.
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
            task_limit = 50

        def _free_bytes():
            return int(shutil.disk_usage(storage_path).free)

        before = _free_bytes()
        checked = 0
        errors = 0
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
                    "pending",
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
                # Envia ativos antigos diretamente ao Drive; só depois remove
                # cópias locais confirmadas por checksum e metadados centrais.
                self.publish_manifest(task_id)
                self.clean_cache(task_id)
            except Exception:
                errors += 1
            if checked >= task_limit:
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
