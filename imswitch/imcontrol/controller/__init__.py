"""Controller package exports.

Keep imports lazy so importing one controller module for unit tests does not
initialize the full imcontrol GUI/controller stack.
"""


def __getattr__(name):
    if name == "ImConMainController":
        from .ImConMainController import ImConMainController

        return ImConMainController
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["ImConMainController"]
