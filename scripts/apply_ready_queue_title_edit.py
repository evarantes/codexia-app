from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "app/static/index.html"
MARKER = "CODEXIA_READY_QUEUE_TITLE_EDIT_V1"


class PatchError(RuntimeError):
    pass


TITLE_CELL_OLD = '''                                        <td class="p-3 font-medium">{{ video.title }}</td>'''
TITLE_CELL_NEW = '''                                        <td class="p-3 font-medium min-w-[280px]">
                                            <!-- CODEXIA_READY_QUEUE_TITLE_EDIT_V1 -->
                                            <div class="flex items-center gap-2">
                                                <span class="break-words">{{ video.title }}</span>
                                                <button
                                                    v-if="(video._source || 'scheduled') === 'scheduled'"
                                                    @click="editReadyScheduledTitle(video)"
                                                    class="text-indigo-600 hover:text-indigo-900 bg-indigo-50 hover:bg-indigo-100 px-2 py-1 rounded text-xs whitespace-nowrap"
                                                    title="Editar título antes de publicar"
                                                ><i class="fas fa-pen"></i> Editar</button>
                                            </div>
                                        </td>'''

METHOD_ANCHOR = '''                async saveScheduledVideo(video) {'''
METHOD_BLOCK = '''                // CODEXIA_READY_QUEUE_TITLE_EDIT_V1
                async editReadyScheduledTitle(video) {
                    if (!video || !video.id) return;
                    const currentTitle = String(video.title || '').trim();
                    const proposed = window.prompt('Editar título do vídeo (máximo 100 caracteres):', currentTitle);
                    if (proposed === null) return;
                    const title = String(proposed || '').trim();
                    if (!title) {
                        alert('O título não pode ficar vazio.');
                        return;
                    }
                    if (title.length > 100) {
                        alert('O título pode ter no máximo 100 caracteres, limite do YouTube.');
                        return;
                    }
                    if (title === currentTitle) return;
                    try {
                        const res = await this.authFetch(`/youtube/schedule/${video.id}`, {
                            method: 'PUT',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({ title })
                        });
                        const data = await res.json().catch(() => ({}));
                        if (!res.ok) {
                            throw new Error(data.detail || data.message || `HTTP ${res.status}`);
                        }
                        video.title = title;
                        await this.fetchScheduledVideos();
                        alert('Título salvo. Este será o título usado em Publicar agora.');
                    } catch (e) {
                        alert('Erro ao salvar título: ' + (e.message || e));
                    }
                },

'''


def patch_index(text: str) -> str:
    if MARKER not in text:
        count = text.count(TITLE_CELL_OLD)
        if count != 1:
            raise PatchError(f"célula de título de Aguardando Publicação esperada 1 vez; encontrada {count}")
        text = text.replace(TITLE_CELL_OLD, TITLE_CELL_NEW, 1)

        count = text.count(METHOD_ANCHOR)
        if count != 1:
            raise PatchError(f"método saveScheduledVideo esperado 1 vez; encontrado {count}")
        text = text.replace(METHOD_ANCHOR, METHOD_BLOCK + METHOD_ANCHOR, 1)
    return text


def apply() -> None:
    original = INDEX.read_text(encoding="utf-8")
    transformed = patch_index(original)
    if patch_index(transformed) != transformed:
        raise PatchError("patch de edição de título não é idempotente")
    if transformed != original:
        INDEX.write_text(transformed, encoding="utf-8")


def check() -> None:
    text = INDEX.read_text(encoding="utf-8")
    required = (
        MARKER,
        'editReadyScheduledTitle(video)',
        "máximo 100 caracteres",
        "JSON.stringify({ title })",
        "Título salvo. Este será o título usado em Publicar agora.",
        '/youtube/schedule/${video.id}',
    )
    missing = [token for token in required if token not in text]
    if missing:
        raise PatchError("edição de título em Aguardando Publicação incompleta: " + ", ".join(missing))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply ou --check")
    try:
        if args.apply:
            apply()
        if args.check:
            check()
    except PatchError as exc:
        print(f"ERRO READY QUEUE TITLE EDIT: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
