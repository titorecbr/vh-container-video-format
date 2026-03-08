"""
VH Generate — Kling AI backend.

Uses the Kling AI API to generate high-quality realistic videos.
Supports:
  - Text-to-video: cinematic quality with human characters and complex scenes
  - Image-to-video: animate a reference image
  - Up to 1080p (pro mode), 720p (std mode), 30fps
  - 5s or 10s per generation, chainable for longer videos
  - Camera control (presets and fine-grained)
  - Audio generation (v2.6+)

Authentication: JWT (HS256) using access_key + secret_key from Kling developer console.

Set environment variables:
  KLING_ACCESS_KEY=your_access_key
  KLING_SECRET_KEY=your_secret_key

Get keys at: https://klingai.com/global/dev

Requirements:
  pip install PyJWT requests
"""

import os
import io
import time
import base64
import tempfile
import subprocess

from .base import GenerateBackend, GenerateRequest, GenerateResult


class KlingBackend(GenerateBackend):
    """Kling AI backend for high-quality video generation via API."""

    BASE_URL = "https://api.klingai.com"

    def __init__(self, access_key=None, secret_key=None):
        self._ak = access_key or os.environ.get("KLING_ACCESS_KEY")
        self._sk = secret_key or os.environ.get("KLING_SECRET_KEY")
        self._token = None
        self._token_exp = 0

        if not self._ak or not self._sk:
            raise RuntimeError(
                "Kling AI requires API credentials.\n"
                "Set environment variables:\n"
                "  export KLING_ACCESS_KEY=your_access_key\n"
                "  export KLING_SECRET_KEY=your_secret_key\n"
                "Get keys at: https://klingai.com/global/dev"
            )

    def name(self) -> str:
        return 'kling'

    def supports_text_to_video(self) -> bool:
        return True

    def supports_image_to_video(self) -> bool:
        return True

    def max_frames(self) -> int:
        return 300  # 10s @ 30fps

    def _get_token(self):
        """Generate or return cached JWT token."""
        import jwt

        now = int(time.time())
        # Regenerate if within 5 min of expiry
        if self._token and now < self._token_exp - 300:
            return self._token

        headers = {"alg": "HS256", "typ": "JWT"}
        payload = {
            "iss": self._ak,
            "exp": now + 1800,  # 30 min
            "nbf": now - 5,
        }
        self._token = jwt.encode(payload, self._sk, headers=headers)
        self._token_exp = now + 1800
        return self._token

    def _request(self, method, path, json_data=None):
        """Make authenticated API request."""
        import requests

        url = f"{self.BASE_URL}{path}"
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=30)
        else:
            resp = requests.post(url, headers=headers, json=json_data, timeout=30)

        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(f"Kling API error: {data.get('message', 'Unknown')} (code {data.get('code')})")

        return data.get("data", {})

    def _create_task(self, request: GenerateRequest):
        """Create a video generation task on Kling API."""
        # Determine model and mode from extras
        extra = request.extra or {}
        model = extra.get("model", "kling-v2-master")
        mode = extra.get("mode", "std")
        duration = str(extra.get("duration", 5))
        negative_prompt = extra.get("negative_prompt", "")
        aspect_ratio = extra.get("aspect_ratio", "16:9")
        cfg_scale = extra.get("cfg_scale", 0.5)
        camera_control = extra.get("camera_control", None)

        if request.image is not None:
            # Image-to-video
            from PIL import Image

            buf = io.BytesIO()
            img = request.image
            if isinstance(img, Image.Image):
                img.save(buf, format="JPEG", quality=95)
            else:
                buf.write(img)
            img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

            body = {
                "model_name": model,
                "image": img_b64,
                "duration": duration,
                "mode": mode,
                "aspect_ratio": aspect_ratio,
                "cfg_scale": cfg_scale,
            }
            if request.prompt:
                body["prompt"] = request.prompt
            if negative_prompt:
                body["negative_prompt"] = negative_prompt
            if camera_control:
                body["camera_control"] = camera_control

            print(f"  Creating image-to-video task ({model}, {mode}, {duration}s)...")
            return self._request("POST", "/v1/videos/image2video", body)

        else:
            # Text-to-video
            body = {
                "model_name": model,
                "prompt": request.prompt,
                "duration": duration,
                "mode": mode,
                "aspect_ratio": aspect_ratio,
                "cfg_scale": cfg_scale,
            }
            if negative_prompt:
                body["negative_prompt"] = negative_prompt
            if camera_control:
                body["camera_control"] = camera_control

            print(f"  Creating text-to-video task ({model}, {mode}, {duration}s)...")
            return self._request("POST", "/v1/videos/text2video", body)

    def _poll_task(self, task_id, timeout=300, interval=5):
        """Poll task until complete or failed."""
        start = time.time()
        while True:
            elapsed = time.time() - start
            if elapsed > timeout:
                raise TimeoutError(f"Kling task {task_id} timed out after {timeout}s")

            data = self._request("GET", f"/v1/videos/{task_id}")
            status = data.get("task_status", "unknown")

            if status == "succeed":
                videos = data.get("task_result", {}).get("videos", [])
                if not videos:
                    raise RuntimeError("Task succeeded but no video URL returned")
                print(f"  Task complete ({elapsed:.0f}s)")
                return videos[0]["url"]

            elif status == "failed":
                msg = data.get("task_status_msg", "Unknown error")
                raise RuntimeError(f"Kling task failed: {msg}")

            print(f"  Status: {status} ({elapsed:.0f}s elapsed)...", end="\r")
            time.sleep(interval)

    def _download_video(self, url):
        """Download MP4 from Kling CDN."""
        import requests

        print(f"  Downloading generated video...")
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        return resp.content

    def _extract_frames(self, mp4_data, fps=None):
        """Extract frames from MP4 data as PIL Images."""
        from PIL import Image

        # Write MP4 to temp file
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(mp4_data)
            mp4_path = f.name

        try:
            # Get video info
            probe = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_streams", mp4_path],
                capture_output=True, text=True
            )
            import json
            info = json.loads(probe.stdout)
            video_stream = next(s for s in info["streams"] if s["codec_type"] == "video")
            width = int(video_stream["width"])
            height = int(video_stream["height"])
            src_fps = eval(video_stream.get("r_frame_rate", "30/1"))

            # Use source fps if not specified
            target_fps = fps or src_fps

            # Extract frames with ffmpeg
            with tempfile.TemporaryDirectory() as tmpdir:
                subprocess.run(
                    ["ffmpeg", "-i", mp4_path, "-vf", f"fps={target_fps}",
                     f"{tmpdir}/frame_%05d.png", "-y", "-loglevel", "quiet"],
                    check=True
                )

                # Load frames
                frames = []
                import glob
                for frame_path in sorted(glob.glob(f"{tmpdir}/frame_*.png")):
                    img = Image.open(frame_path).convert("RGB")
                    frames.append(img)

                print(f"  Extracted {len(frames)} frames ({width}x{height} @ {target_fps}fps)")
                return frames, target_fps

        finally:
            os.unlink(mp4_path)

    def generate(self, request: GenerateRequest) -> GenerateResult:
        """Generate video using Kling AI API."""
        if not request.prompt and request.image is None:
            raise ValueError("Kling requires a prompt, an image, or both.")

        extra = request.extra or {}
        timeout = extra.get("timeout", 300)

        # Create task
        task_data = self._create_task(request)
        task_id = task_data["task_id"]
        print(f"  Task ID: {task_id}")

        # Poll until done
        video_url = self._poll_task(task_id, timeout=timeout)

        # Download MP4
        mp4_data = self._download_video(video_url)
        print(f"  Video size: {len(mp4_data) / 1024 / 1024:.1f} MB")

        # Extract frames
        target_fps = request.fps if request.fps else None
        frames, actual_fps = self._extract_frames(mp4_data, fps=target_fps)

        return GenerateResult(
            frames=frames,
            fps=int(actual_fps),
            seed=0,
            backend_info={
                "model": extra.get("model", "kling-v2-master"),
                "mode": extra.get("mode", "std"),
                "duration": extra.get("duration", 5),
                "task_id": task_id,
                "video_url": video_url,
            },
        )

    def cleanup(self):
        """Nothing to clean up for API backend."""
        pass
