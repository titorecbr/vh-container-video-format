"""
VH Generate — Base backend interface for AI video generation.

All generation backends (SVD, Runway, CogVideo, etc.) implement this interface.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List

try:
    from PIL import Image
except ImportError:
    Image = None


@dataclass
class GenerateRequest:
    """Input for video generation."""
    prompt: Optional[str] = None
    image: Optional[object] = None  # PIL.Image.Image
    num_frames: int = 25
    width: int = 1024
    height: int = 576
    fps: int = 7
    seed: Optional[int] = None
    extra: dict = field(default_factory=dict)


@dataclass
class GenerateResult:
    """Output from video generation."""
    frames: List[object] = field(default_factory=list)  # List[PIL.Image.Image]
    fps: int = 7
    seed: int = 0
    backend_info: dict = field(default_factory=dict)


class GenerateBackend(ABC):
    """Abstract base class for video generation backends.

    To add a new backend:
      1. Create a new file in vh_video_container/generate/ (e.g., runway.py)
      2. Subclass GenerateBackend and implement generate() and name()
      3. Register it in generate/__init__.py get_backend()
    """

    @abstractmethod
    def generate(self, request: GenerateRequest) -> GenerateResult:
        """Generate video frames from a request.

        Args:
            request: GenerateRequest with prompt, image, and parameters.

        Returns:
            GenerateResult with list of PIL Images and metadata.
        """
        ...

    @abstractmethod
    def name(self) -> str:
        """Backend identifier string, e.g. 'svd', 'runway', 'cogvideo'."""
        ...

    def supports_text_to_video(self) -> bool:
        """Whether this backend supports text-only prompts (no conditioning image)."""
        return False

    def supports_image_to_video(self) -> bool:
        """Whether this backend supports image-conditioned generation."""
        return True

    def max_frames(self) -> int:
        """Maximum frames per single generation call."""
        return 25

    def cleanup(self):
        """Release GPU memory, close connections, etc."""
        pass
