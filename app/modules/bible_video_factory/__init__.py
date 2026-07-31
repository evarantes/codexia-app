from . import models

__all__ = ["models", "router", "service"]


def __getattr__(name: str):
    if name == "router":
        import importlib
        mod = importlib.import_module(__name__ + ".router")
        globals()["router"] = mod
        return mod
    if name == "service":
        import importlib
        mod = importlib.import_module(__name__ + ".service")
        globals()["service"] = mod
        return mod
    raise AttributeError(name)
