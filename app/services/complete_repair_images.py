"""Complete physical image files before any renderer can consume the plan."""
from pathlib import Path
from app.services.recovery_image_budget import RecoveryImageCallBudget


def complete_repair_images(script, renderer, aspect_ratio, checkpoint, cancel):
    budget = RecoveryImageCallBudget(script)
    target = budget.target_image_count
    if not budget.enabled or not target:
        raise RuntimeError('Correção de imagens sem meta e orçamento; render não iniciado.')
    paths = []
    for reference in script.get('selected_images') or []:
        path = renderer._resolve_input_image_path(reference)
        if path and Path(path).is_file() and Path(path).stat().st_size > 0 and path not in paths:
            paths.append(path)
    if len(paths) < int(budget.snapshot()['existing_image_count']):
        raise RuntimeError('Imagens preservadas não estão acessíveis no worker. Render não iniciado.')
    scenes = script.get('scenes') or []
    if not scenes:
        raise RuntimeError('Roteiro indisponível para orientar as imagens faltantes.')
    script['selected_images'] = paths
    checkpoint(paths, target, f'Imagens: {len(paths)}/{target} disponíveis')
    while len(paths) < target:
        cancel()
        scene = scenes[len(paths) % len(scenes)]
        scene = {'text': scene} if isinstance(scene, str) else scene
        prompt = str(scene.get('image_prompt') or scene.get('text') or '')
        prompt += f'. Composição cinematográfica distinta para a imagem {len(paths)+1} de {target}, coerente com esta cena.'
        def status(message):
            cancel()
            checkpoint(paths, target, f'Imagens: {len(paths)}/{target} — gerando {len(paths)+1}/{target}: {message}')
        path = renderer._ensure_image_for_scene(prompt, text_fallback=scene.get('text') or '',
                    aspect_ratio=aspect_ratio, status_callback=status, max_rounds=1,
                    allow_non_ai_fallback=False, paid_call_guard=budget.consume)
        path = renderer._resolve_input_image_path(path)
        if not path or not Path(path).is_file() or Path(path).stat().st_size == 0 or path in paths:
            raise RuntimeError('Geração não retornou uma nova imagem válida; render não iniciado.')
        paths.append(path)
        script['selected_images'] = list(paths)
        checkpoint(paths, target, f'Imagens: {len(paths)}/{target} concluídas')
    # Composition must reuse this complete set without additional image calls.
    script.pop('_partial_image_recovery', None)
    script['repair_complete_visuals'] = False
    script['force_reuse_assets'] = True
    script['expected_image_count'] = target
    return paths
