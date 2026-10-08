"""Measured progress is distinct from an executor heartbeat."""
import re
import time
from datetime import datetime, timezone
from proglog import ProgressBarLogger


class MeasuredRenderLogger(ProgressBarLogger):
    def __init__(self, callback, message):
        super().__init__()
        self._progress_callback = callback
        self.last_sent = 0.0

    def bars_callback(self, bar, attr, value, old_value=None):
        if attr != 'index' or bar not in {'t', 'frame_index', 'chunk'}:
            return
        total = self.bars.get(bar, {}).get('total')
        if not total or value is None:
            return
        now = time.monotonic()
        if now - self.last_sent < 10 and value < total:
            return
        self.last_sent = now
        kind = 'vídeo' if bar in {'t', 'frame_index'} else 'áudio'
        unit = 'quadros' if kind == 'vídeo' else 'blocos'
        self._progress_callback(95 + min(4, int(4 * value / total)),
                      f'Render {kind}: {int(value)}/{int(total)} {unit}')


def record_progress(previous, stage, message, now=None):
    now = now or datetime.now(timezone.utc).isoformat()
    state = dict(previous or {})
    state['heartbeat_at'] = now
    if stage != state.get('stage'):
        state.update(stage=stage, stage_started_at=now, last_advance_at=now)
        state.pop('render', None)
    match = re.search(r'Render (vídeo|áudio): (\d+)/(\d+) (quadros|blocos)', message)
    if match:
        kind, current, total, unit = match.groups()
        current, total = int(current), int(total)
        if total > 0:
            old = state.get('render') or {}
            if kind != old.get('kind') or current > old.get('current', -1):
                state['last_advance_at'] = now
            state['render'] = dict(kind=kind, current=current, total=total,
                                   unit=unit, percent=min(100, round(100 * current / total, 1)))
    return state


def activity_status(state, status, now=None):
    if status != 'processing':
        return dict(label='Aguardando execução' if status == 'pending' else 'Etapa encerrada ou pausada', state='idle')
    now = now or datetime.now(timezone.utc)
    def age(field):
        try:
            value = datetime.fromisoformat(str(state.get(field) or '').replace('Z', '+00:00'))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return max(0, int((now - value).total_seconds()))
        except (ValueError, TypeError):
            return None
    advance, heartbeat = age('last_advance_at'), age('heartbeat_at')
    if heartbeat is None:
        label, result = 'Atividade não confirmada — tarefa anterior ao monitor', 'unknown'
    elif heartbeat > 90:
        label, result = 'Sem sinal recente do executor — possível interrupção', 'stale'
    elif advance is None or advance > 180:
        label, result = 'Executor responde; sem avanço medido recente — possível travamento', 'stalled'
    elif state.get('render'):
        label, result = 'Processando — avanço de render confirmado', 'advancing'
    else:
        label, result = 'Executor responde; etapa iniciada, avanço interno ainda não medido', 'waiting'
    return dict(label=label, state=result, heartbeat_age_sec=heartbeat, advance_age_sec=advance)
