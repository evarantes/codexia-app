"""Router package bootstrap.

Codexia V2 keeps the existing YouTube router as the canonical production entry
and mounts the new cinematic control plane below /youtube/cinematic without
changing the large legacy router.
"""
from . import youtube as youtube
from .cinematic_campaign import router as _cinematic_router

# Safe to execute once per process: package __init__ is cached by Python.
youtube.router.include_router(_cinematic_router)
