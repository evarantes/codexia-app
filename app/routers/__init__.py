"""Router package bootstrap.

Codexia V2 keeps the existing YouTube router as the canonical production entry
and mounts the cinematic control plane below /youtube/cinematic without
changing the large legacy router.
"""
from . import pydantic_compat as _pydantic_compat  # noqa: F401
from . import youtube as youtube
from . import cinematic_campaign as _cinematic_campaign
from .cinematic_budget_optimizer import wrap_budget_guard
from .cinematic_compose import router as _cinematic_compose_router
from app.services.cinematic_ui_patch import install_cinematic_async_ui

_cinematic_campaign._rebalance_plan_to_budget = wrap_budget_guard(
    _cinematic_campaign._rebalance_plan_to_budget,
    fx_getter=_cinematic_campaign._usd_brl,
)
_cinematic_router = _cinematic_campaign.router

# Import after the budget wrapper so background jobs share the same guarded
# budget logic as the synchronous compatibility endpoint.
from .cinematic_director_async import router as _cinematic_director_async_router  # noqa: E402
from .cinematic_project import router as _cinematic_project_router  # noqa: E402
from .cinematic_project_pipeline import router as _cinematic_project_pipeline_router  # noqa: E402
from .cinematic_queue import router as _cinematic_queue_router  # noqa: E402

youtube.router.include_router(_cinematic_router)
youtube.router.include_router(_cinematic_compose_router)
youtube.router.include_router(_cinematic_director_async_router)
youtube.router.include_router(_cinematic_project_router)
youtube.router.include_router(_cinematic_project_pipeline_router)
youtube.router.include_router(_cinematic_queue_router)

# Inject resilient frontend controllers idempotently at startup.
install_cinematic_async_ui()
