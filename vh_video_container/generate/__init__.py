"""
VH Generate — AI video generation backends.

Usage:
    from vh_video_container.generate import get_backend, list_backends
    backend = get_backend('svd')
    result = backend.generate(request)
"""

from .base import GenerateBackend, GenerateRequest, GenerateResult


def list_backends():
    """Return list of available backend names."""
    return ['svd', 'kling']


def get_backend(name: str, **kwargs) -> GenerateBackend:
    """Get a generation backend by name (lazy import).

    Args:
        name: Backend identifier ('svd', 'kling', etc.)
        **kwargs: Passed to backend constructor (e.g., api_key for API backends)

    Returns:
        GenerateBackend instance ready to use.
    """
    if name == 'svd':
        from .svd import SVDBackend
        return SVDBackend(**kwargs)
    elif name == 'kling':
        from .kling import KlingBackend
        return KlingBackend(**kwargs)
    # Future backends:
    # elif name == 'runway':
    #     from .runway import RunwayBackend
    #     return RunwayBackend(**kwargs)
    else:
        available = list_backends()
        raise ValueError(f"Unknown backend: '{name}'. Available: {available}")


__all__ = [
    'GenerateBackend', 'GenerateRequest', 'GenerateResult',
    'get_backend', 'list_backends',
]
