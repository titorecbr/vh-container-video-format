"""
VH Generate — Stable Video Diffusion backend.

Uses HuggingFace diffusers to generate video frames locally on GPU.
Supports:
  - Image-to-video: provide a conditioning image
  - Text-to-video: internally generates conditioning image via SDXL-Turbo

Optimized for RTX 3070 (8GB VRAM):
  - float16 precision
  - CPU model offloading
  - Chunked VAE decoding (decode_chunk_size=4)

Requirements:
  pip install torch diffusers transformers accelerate safetensors
"""

import gc
import random

from .base import GenerateBackend, GenerateRequest, GenerateResult


class SVDBackend(GenerateBackend):
    """Stable Video Diffusion backend for local GPU generation."""

    def __init__(self):
        self._svd_pipeline = None
        self._text_pipeline = None

    def name(self) -> str:
        return 'svd'

    def supports_text_to_video(self) -> bool:
        return True  # via internal text-to-image stage

    def supports_image_to_video(self) -> bool:
        return True

    def max_frames(self) -> int:
        return 25

    def _check_deps(self):
        """Check that required packages are installed."""
        try:
            import torch
            import diffusers
        except ImportError:
            raise RuntimeError(
                "SVD backend requires: pip install torch diffusers transformers accelerate safetensors\n"
                "Or: pip install vh-video-container[generate-svd]"
            )
        if not torch.cuda.is_available():
            raise RuntimeError(
                "SVD backend requires CUDA GPU. No CUDA device found.\n"
                f"PyTorch version: {torch.__version__}"
            )

    def _get_conditioning_image(self, prompt: str, width: int, height: int, seed: int):
        """Generate a conditioning image from text prompt using SDXL-Turbo."""
        import torch
        from diffusers import AutoPipelineForText2Image

        print(f"  Generating conditioning image from prompt...")
        if self._text_pipeline is None:
            self._text_pipeline = AutoPipelineForText2Image.from_pretrained(
                "stabilityai/sdxl-turbo",
                torch_dtype=torch.float16,
                variant="fp16",
            )
            # CPU offloading to stay within 8GB VRAM
            self._text_pipeline.enable_model_cpu_offload()

        generator = torch.Generator(device="cpu").manual_seed(seed)
        result = self._text_pipeline(
            prompt=prompt,
            num_inference_steps=4,
            guidance_scale=0.0,
            width=width,
            height=height,
            generator=generator,
        )
        image = result.images[0]

        # Fully unload text pipeline to free VRAM for SVD
        del self._text_pipeline
        self._text_pipeline = None
        torch.cuda.empty_cache()
        gc.collect()

        print(f"  Conditioning image ready ({width}x{height})")
        return image

    def _load_svd_pipeline(self, num_frames: int):
        """Load SVD pipeline, choosing model variant based on frame count."""
        import torch
        from diffusers import StableVideoDiffusionPipeline

        if self._svd_pipeline is not None:
            return

        # XT variant supports 25 frames, base supports 14
        if num_frames > 14:
            model_id = "stabilityai/stable-video-diffusion-img2vid-xt"
            print(f"  Loading SVD-XT model (up to 25 frames)...")
        else:
            model_id = "stabilityai/stable-video-diffusion-img2vid"
            print(f"  Loading SVD model (up to 14 frames)...")

        self._svd_pipeline = StableVideoDiffusionPipeline.from_pretrained(
            model_id,
            torch_dtype=torch.float16,
            variant="fp16",
        )
        # Sequential CPU offloading: moves each layer to GPU individually (slower but fits 8GB)
        self._svd_pipeline.enable_sequential_cpu_offload()

    def generate(self, request: GenerateRequest) -> GenerateResult:
        """Generate video frames using Stable Video Diffusion."""
        import torch

        self._check_deps()

        seed = request.seed if request.seed is not None else random.randint(0, 2**32 - 1)
        num_frames = min(request.num_frames, self.max_frames())

        # SVD native resolution
        width = request.width or 1024
        height = request.height or 576

        # Step 1: Get conditioning image
        if request.image is not None:
            from PIL import Image
            cond_image = request.image
            if cond_image.size != (width, height):
                cond_image = cond_image.resize((width, height), Image.LANCZOS)
            print(f"  Using provided conditioning image ({width}x{height})")
        elif request.prompt:
            cond_image = self._get_conditioning_image(request.prompt, width, height, seed)
        else:
            raise ValueError("SVD requires either a prompt or an image.")

        # Step 2: Generate video frames
        self._load_svd_pipeline(num_frames)

        generator = torch.Generator(device="cpu").manual_seed(seed)
        decode_chunk = request.extra.get('decode_chunk_size', 4)
        motion_bucket = request.extra.get('motion_bucket_id', 127)
        noise_aug = request.extra.get('noise_aug_strength', 0.02)

        print(f"  Generating {num_frames} frames @ {width}x{height}...")
        print(f"  decode_chunk_size={decode_chunk}, motion_bucket={motion_bucket}")

        frames = self._svd_pipeline(
            image=cond_image,
            num_frames=num_frames,
            decode_chunk_size=decode_chunk,
            motion_bucket_id=motion_bucket,
            noise_aug_strength=noise_aug,
            generator=generator,
        ).frames[0]

        print(f"  Generated {len(frames)} frames")

        return GenerateResult(
            frames=list(frames),
            fps=request.fps,
            seed=seed,
            backend_info={
                'model': 'svd-xt' if num_frames > 14 else 'svd',
                'decode_chunk_size': decode_chunk,
                'motion_bucket_id': motion_bucket,
                'noise_aug_strength': noise_aug,
            },
        )

    def cleanup(self):
        """Release GPU memory."""
        import torch

        if self._svd_pipeline is not None:
            del self._svd_pipeline
            self._svd_pipeline = None
        if self._text_pipeline is not None:
            del self._text_pipeline
            self._text_pipeline = None

        torch.cuda.empty_cache()
        gc.collect()
        print("  GPU memory released")
