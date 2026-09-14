"""Small compatibility shim for Codexia deployments that still resolve Pydantic v1.

The repository does not pin Pydantic directly, so production and local installs can
resolve different major versions through FastAPI.  V2 uses ``model_dump`` while
V1 uses ``dict``.  Keep the new cinematic router source version-agnostic.
"""
from pydantic import BaseModel

if not hasattr(BaseModel, "model_dump"):
    def _model_dump(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return self.dict(*args, **kwargs)

    setattr(BaseModel, "model_dump", _model_dump)
