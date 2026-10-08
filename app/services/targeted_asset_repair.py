"""Build a scoped recovery request without discarding completed assets."""
from copy import deepcopy


def repair_payload(payload, result, plan, asset):
    patched = deepcopy(payload)
    script = deepcopy(result.get('script') or payload.get('seeded_script') or {})
    images = list(dict.fromkeys(plan.get('existing_image_paths') or script.get('selected_images') or []))
    audio = plan.get('audio_path') or plan.get('audio_candidate_path') or ((result.get('render_report') or {}).get('audio_generation') or {}).get('output_path')
    if asset != 'script' and not script.get('scenes'):
        raise ValueError('Roteiro indisponível. Corrija o roteiro antes deste ativo.')
    if asset not in {'script', 'narration'} and not audio:
        raise ValueError('Narração indisponível. Corrija a narração antes deste ativo.')
    if asset not in {'script', 'images'} and not images:
        raise ValueError('Imagens indisponíveis. Corrija as imagens antes deste ativo.')
    for key in ('repair_image_budget', 'reuse_audio_from', 'recovery_image_budget', 'strict_visual_target_count', 'custom_image_paths'):
        patched.pop(key, None)
    script.pop('_partial_image_recovery', None)
    script.pop('repair_regenerate_audio', None)
    script.pop('force_render_only', None)
    script.pop('strict_visual_target_count', None)
    script['selected_images'] = images
    script['targeted_repair_asset'] = asset
    script['force_reuse_assets'] = True
    script['repair_complete_visuals'] = asset == 'images'
    if audio and asset not in {'script', 'narration'}:
        script['seed_audio_path'] = audio
        audio_report = (result.get('render_report') or {}).get('audio_generation') or {}
        script['seed_narration_text'] = audio_report.get('final_text_sent_to_tts') or script.get('seed_narration_text') or ''
    else:
        script.pop('seed_audio_path', None)
    patched.update(targeted_repair_asset=asset, seeded_script=script if asset != 'script' else None,
                   selected_images=images, force_reuse_assets=False, force_regenerate=False,
                   force_render_only=False, repair_mode=True, repair_complete_visuals=asset == 'images',
                   repair_regenerate_audio=asset in {'script', 'narration'}, auto_publish=False)
    if asset == 'images':
        target = max(len(images), int(plan.get('expected_image_count') or 0))
        budget = dict(enabled=True, existing_image_count=len(images), expected_image_count=target,
                      missing_image_count=target-len(images), max_new_image_calls=target-len(images))
        patched.update(repair_image_budget=budget, expected_image_count=target)
    elif images:
        patched['expected_image_count'] = len(images)
    return patched
