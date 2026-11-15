"""StackSense: deterministic tech stack recommendation engine."""

ENGINE_VERSION = "1.0.0"
KB_AS_OF = "2026-11-10"


def run(raw, **kwargs):
    """Run the engine on a parsed scenario; see engine.run."""
    from .engine import run as _run

    return _run(raw, **kwargs)
