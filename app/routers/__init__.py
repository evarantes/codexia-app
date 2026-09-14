"""Router package bootstrap.

Codexia V2 keeps the existing YouTube router as the canonical production entry
and mounts the new cinematic control plane below /youtube/cinematic without
changing the large legacy router.
"""
from . import pydantic_compat as _pydantic_compat  # noqa: F401
from . import youtube as youtube
from . import cinematic_campaign as _cinematic_campaign
from .cinematic_budget_optimizer import wrap_budget_guard
from .cinematic_compose import router as _cinematic_compose_router

# The first guard guarantees we never exceed the user's ceiling. The quality
# wrapper then uses safe unused headroom to restore economical movement when
# Claude under-plans it, keeping the Cinematográfico Inteligente actually cinematic.
_cinematic_campaign._rebalance_plan_to_budget = wrap_budget_guard(
    _cinematic_campaign._rebalance_plan_to_budget,
    fx_getter=_cinematic_campaign._usd_brl,
)
_cinematic_router = _cinematic_campaign.router

# Safe to execute once per process: package __init__ is cached by Python.
youtube.router.include_router(_cinematic_router)
youtube.router.include_router(_cinematic_compose_router)
