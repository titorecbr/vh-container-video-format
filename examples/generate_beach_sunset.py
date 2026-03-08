"""
Generate a 60-second beach sunset video transitioning from golden hour to night.

Prompt used:
  "Generate a 60-second video of a beach with the sunset transitioning into night,
   using the VH format features to create a demo of the vh cli."

Features demonstrated:
  - Programmatic video creation with VHFile API
  - Per-frame annotations (time of day, light level, scene description)
  - Metadata (fps, dimensions, title, generator)
  - Frame deduplication check
  - 60s @ 24fps = 1440 frames, 1280x720
"""

import io
import math
import random
import hashlib

from vh_video_container import VHFile
from PIL import Image, ImageDraw, ImageFont

# --- Config ---
W, H = 1280, 720
FPS = 24
DURATION_S = 60
TOTAL_FRAMES = FPS * DURATION_S
OUTPUT = "beach_sunset.vh"
QUALITY = 90

# --- Timeline (0.0 = start, 1.0 = end) ---
# 0.00 - 0.25  Golden hour (warm orange sky, sun above horizon)
# 0.25 - 0.50  Sunset (sun sinking, sky turns pink/purple)
# 0.50 - 0.70  Twilight (sun below horizon, deep blue/purple)
# 0.70 - 1.00  Night (dark sky, stars, moon rising)


def lerp(a, b, t):
    return a + (b - a) * max(0, min(1, t))


def lerp_color(c1, c2, t):
    t = max(0, min(1, t))
    return tuple(int(lerp(a, b, t)) for a, b in zip(c1, c2))


def multi_lerp_color(colors_and_stops, t):
    """Interpolate through multiple color stops. colors_and_stops = [(t, (r,g,b)), ...]"""
    if t <= colors_and_stops[0][0]:
        return colors_and_stops[0][1]
    if t >= colors_and_stops[-1][0]:
        return colors_and_stops[-1][1]
    for i in range(len(colors_and_stops) - 1):
        t0, c0 = colors_and_stops[i]
        t1, c1 = colors_and_stops[i + 1]
        if t0 <= t <= t1:
            local_t = (t - t0) / (t1 - t0)
            return lerp_color(c0, c1, local_t)
    return colors_and_stops[-1][1]


# Sky color stops
SKY_TOP_COLORS = [
    (0.00, (40, 100, 200)),    # blue sky
    (0.20, (80, 120, 200)),    # warm blue
    (0.35, (120, 80, 140)),    # pink-purple
    (0.50, (60, 40, 100)),     # deep purple
    (0.65, (25, 20, 60)),      # twilight
    (0.80, (10, 12, 30)),      # near-night
    (1.00, (8, 10, 22)),       # night
]

SKY_HORIZON_COLORS = [
    (0.00, (255, 200, 100)),   # golden
    (0.15, (255, 160, 80)),    # warm orange
    (0.30, (255, 100, 80)),    # red-orange
    (0.45, (180, 60, 100)),    # magenta
    (0.55, (80, 40, 80)),      # dark purple
    (0.70, (20, 18, 40)),      # dark
    (1.00, (10, 12, 25)),      # night
]

# Water color stops
WATER_COLORS = [
    (0.00, (30, 80, 140)),     # daylight ocean
    (0.20, (40, 70, 120)),     # warm ocean
    (0.40, (50, 40, 90)),      # purple tint
    (0.60, (20, 25, 60)),      # twilight water
    (0.80, (10, 14, 35)),      # dark water
    (1.00, (8, 10, 25)),       # night water
]

# Sand color stops
SAND_COLORS = [
    (0.00, (220, 190, 140)),   # warm sand
    (0.30, (180, 150, 110)),   # cooling sand
    (0.50, (120, 100, 80)),    # twilight sand
    (0.70, (60, 55, 45)),      # dark sand
    (1.00, (30, 28, 22)),      # night sand
]

# Sun color
SUN_COLORS = [
    (0.00, (255, 240, 180)),   # bright yellow
    (0.15, (255, 200, 100)),   # golden
    (0.30, (255, 130, 60)),    # orange
    (0.45, (255, 80, 50)),     # red
    (0.55, (200, 50, 50)),     # deep red (setting)
]


class Wave:
    def __init__(self, y_base, amplitude, speed, phase, width_factor):
        self.y_base = y_base
        self.amplitude = amplitude
        self.speed = speed
        self.phase = phase
        self.width_factor = width_factor

    def y_at(self, x, t):
        return self.y_base + self.amplitude * math.sin(
            x * self.width_factor + t * self.speed + self.phase
        )


class Bird:
    def __init__(self, seed):
        rng = random.Random(seed)
        self.x = rng.uniform(-200, W + 200)
        self.y = rng.uniform(H * 0.08, H * 0.30)
        self.speed = rng.uniform(1.5, 3.5)
        self.wing_speed = rng.uniform(4, 7)
        self.wing_phase = rng.uniform(0, math.pi * 2)
        self.size = rng.uniform(4, 8)
        self.direction = rng.choice([-1, 1])

    def update(self):
        self.x += self.speed * self.direction
        if self.direction > 0 and self.x > W + 200:
            self.x = -200
        elif self.direction < 0 and self.x < -200:
            self.x = W + 200

    def draw(self, draw_ctx, t, progress):
        if progress > 0.75:
            return  # birds gone at night
        wing = math.sin(t * self.wing_speed + self.wing_phase)
        alpha = max(0, 1.0 - progress / 0.75)
        s = self.size
        color_val = int(30 * alpha)
        c = (color_val, color_val, color_val + 5)
        # V-shape bird
        draw_ctx.line([(self.x - s, self.y - abs(wing) * s * 0.6),
                       (self.x, self.y),
                       (self.x + s, self.y - abs(wing) * s * 0.6)],
                      fill=c, width=max(1, int(s * 0.2)))


class Palm:
    def __init__(self, x, seed):
        rng = random.Random(seed)
        self.x = x
        self.trunk_h = rng.uniform(120, 200)
        self.y_base = H * 0.62 + rng.uniform(-10, 20)
        self.lean = rng.uniform(-0.15, 0.15)
        self.frond_count = rng.randint(5, 8)
        self.frond_lengths = [rng.uniform(50, 90) for _ in range(self.frond_count)]
        self.frond_angles = [rng.uniform(-math.pi * 0.4, math.pi * 0.4) + i * (math.pi * 2 / self.frond_count)
                             for i in range(self.frond_count)]
        self.sway_phase = rng.uniform(0, math.pi * 2)
        self.sway_speed = rng.uniform(0.8, 1.5)

    def draw(self, draw_ctx, t, progress):
        sway = math.sin(t * self.sway_speed + self.sway_phase) * 0.04
        total_lean = self.lean + sway

        # Trunk color darkens with time
        trunk_light = lerp(90, 20, progress)
        trunk_color = (int(trunk_light * 0.7), int(trunk_light * 0.5), int(trunk_light * 0.3))

        # Trunk (curved line segments)
        segments = 10
        points = []
        for s in range(segments + 1):
            frac = s / segments
            sx = self.x + total_lean * self.trunk_h * frac * frac
            sy = self.y_base - self.trunk_h * frac
            points.append((sx, sy))

        for i in range(len(points) - 1):
            w = max(2, int(lerp(8, 3, i / len(points))))
            draw_ctx.line([points[i], points[i + 1]], fill=trunk_color, width=w)

        # Crown position
        crown_x, crown_y = points[-1]

        # Fronds
        frond_light = lerp(60, 12, progress)
        frond_color = (int(frond_light * 0.3), int(frond_light * 0.8), int(frond_light * 0.3))

        for fi in range(self.frond_count):
            angle = self.frond_angles[fi] + sway * 2
            length = self.frond_lengths[fi]
            # Drooping curve
            segs = 8
            prev = (crown_x, crown_y)
            for s in range(1, segs + 1):
                frac = s / segs
                droop = frac * frac * length * 0.4  # gravity droop
                fx = crown_x + math.cos(angle) * length * frac
                fy = crown_y + math.sin(angle) * length * frac * 0.3 + droop - length * 0.3
                cur = (fx, fy)
                draw_ctx.line([prev, cur], fill=frond_color, width=max(1, int(3 * (1 - frac))))
                prev = cur

            # Coconuts near crown
            if fi < 2:
                cx = crown_x + math.cos(angle) * 8
                cy = crown_y + 5
                draw_ctx.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=trunk_color)


class Cloud:
    def __init__(self, seed):
        rng = random.Random(seed)
        self.base_x = rng.uniform(-100, W + 100)
        self.y = rng.uniform(H * 0.05, H * 0.28)
        self.speed = rng.uniform(2, 6)
        self.blobs = []
        for _ in range(rng.randint(3, 7)):
            self.blobs.append({
                'dx': rng.uniform(-40, 40),
                'dy': rng.uniform(-10, 10),
                'rx': rng.uniform(20, 50),
                'ry': rng.uniform(10, 25),
            })

    def draw(self, draw_ctx, t, progress):
        x = (self.base_x + t * self.speed) % (W + 300) - 150
        # Cloud color: lit by sunset, then darkens
        cloud_color = multi_lerp_color([
            (0.00, (255, 250, 240)),
            (0.20, (255, 200, 150)),
            (0.35, (255, 140, 100)),
            (0.50, (150, 80, 100)),
            (0.65, (50, 40, 60)),
            (0.80, (25, 22, 35)),
            (1.00, (15, 14, 22)),
        ], progress)
        for blob in self.blobs:
            bx = x + blob['dx']
            by = self.y + blob['dy']
            draw_ctx.ellipse([bx - blob['rx'], by - blob['ry'],
                              bx + blob['rx'], by + blob['ry']], fill=cloud_color)


def draw_sky(draw, progress):
    """Draw gradient sky with smooth color transition."""
    horizon_y = int(H * 0.45)
    sky_top = multi_lerp_color(SKY_TOP_COLORS, progress)
    sky_horizon = multi_lerp_color(SKY_HORIZON_COLORS, progress)
    for y in range(horizon_y + 30):
        t = y / (horizon_y + 30)
        c = lerp_color(sky_top, sky_horizon, t)
        draw.line([(0, y), (W, y)], fill=c)


def draw_sun(draw, img, progress):
    """Draw sun moving down and setting below horizon."""
    if progress > 0.58:
        return  # sun fully set

    # Sun position: high at start, sinks to horizon
    sun_y_start = H * 0.15
    sun_y_end = H * 0.45
    sun_x = W * 0.5 + math.sin(progress * 0.5) * 40
    sun_y = lerp(sun_y_start, sun_y_end, progress / 0.55)
    sun_r = lerp(35, 45, progress / 0.55)  # gets bigger near horizon

    sun_color = multi_lerp_color(SUN_COLORS, progress)

    # Glow layers
    overlay = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for layer in range(15, 0, -1):
        r = sun_r * (layer / 15) * 3.0
        alpha = int(40 * (1.0 - layer / 15) * max(0, 1.0 - progress / 0.58))
        od.ellipse([sun_x - r, sun_y - r, sun_x + r, sun_y + r],
                   fill=(*sun_color, alpha))

    # Core
    core_alpha = int(255 * max(0, 1.0 - progress / 0.58))
    od.ellipse([sun_x - sun_r, sun_y - sun_r, sun_x + sun_r, sun_y + sun_r],
               fill=(*sun_color, core_alpha))

    img.paste(Image.alpha_composite(img.convert('RGBA'), overlay).convert('RGB'))

    # Sun reflection on water
    if progress < 0.55:
        refl_alpha = max(0, 1.0 - progress / 0.55) * 0.4
        refl_color = tuple(int(v * refl_alpha) for v in sun_color)
        water_top = int(H * 0.45)
        for ry in range(water_top, int(H * 0.62)):
            spread = (ry - water_top) * 0.8
            wobble = math.sin(ry * 0.3 + progress * 20) * spread * 0.3
            rx = sun_x + wobble
            rw = 3 + spread * 0.15
            fade = 1.0 - (ry - water_top) / (H * 0.17)
            rc = tuple(int(v * fade) for v in refl_color)
            rc = tuple(max(0, min(255, v)) for v in rc)
            draw_ctx = ImageDraw.Draw(img)
            draw_ctx.line([(rx - rw, ry), (rx + rw, ry)], fill=rc, width=1)


def draw_water(draw, t, progress):
    """Draw ocean with waves."""
    water_top = int(H * 0.45)
    water_bottom = int(H * 0.62)
    water_color = multi_lerp_color(WATER_COLORS, progress)

    # Base water
    draw.rectangle([0, water_top, W, water_bottom], fill=water_color)

    # Wave lines
    for wi in range(6):
        wave_y = water_top + wi * ((water_bottom - water_top) / 6) + 5
        wave_color = lerp_color(water_color, (255, 255, 255), 0.08 - wi * 0.01)
        points = []
        for x in range(0, W, 3):
            wy = wave_y + math.sin(x * 0.015 + t * (1.5 + wi * 0.3) + wi * 2) * (3 - wi * 0.3)
            points.append((x, wy))
        if len(points) > 1:
            draw.line(points, fill=wave_color, width=1)

    # Foam line at shore
    foam_y = water_bottom - 3
    foam_progress = (t * 0.4) % 1.0
    foam_x_start = foam_progress * W * 0.1
    brightness = lerp(220, 40, progress)
    foam_color = (int(brightness), int(brightness), int(brightness * 0.95))
    for x in range(0, W, 2):
        fy = foam_y + math.sin(x * 0.05 + t * 2) * 2
        if random.random() < 0.6:
            draw.line([(x, fy), (x + 2, fy)], fill=foam_color, width=1)


def draw_sand(draw, progress):
    """Draw beach sand."""
    sand_top = int(H * 0.62)
    sand_color = multi_lerp_color(SAND_COLORS, progress)
    draw.rectangle([0, sand_top, W, H], fill=sand_color)

    # Wet sand near water
    wet_color = lerp_color(sand_color, multi_lerp_color(WATER_COLORS, progress), 0.3)
    draw.rectangle([0, sand_top, W, sand_top + 15], fill=wet_color)


def draw_stars(draw, progress):
    """Stars fade in during twilight and night."""
    if progress < 0.50:
        return
    star_alpha = min(1.0, (progress - 0.50) / 0.25)
    rng = random.Random(12345)
    for _ in range(150):
        sx = rng.randint(0, W)
        sy = rng.randint(0, int(H * 0.42))
        brightness = rng.randint(120, 255)
        twinkle = rng.uniform(1, 4)
        b = int(brightness * star_alpha * (0.7 + 0.3 * math.sin(progress * 60 * twinkle + sx)))
        b = max(0, min(255, b))
        size = rng.choice([0, 0, 0, 1])  # mostly single pixels
        if size == 0:
            draw.point((sx, sy), fill=(b, b, int(b * 0.9)))
        else:
            draw.ellipse([sx - 1, sy - 1, sx + 1, sy + 1], fill=(b, b, int(b * 0.9)))


def draw_moon(draw, progress):
    """Moon rises during night phase."""
    if progress < 0.65:
        return
    moon_t = (progress - 0.65) / 0.35
    mx = W * 0.25
    my = lerp(H * 0.45, H * 0.12, moon_t)
    mr = 20
    alpha = min(1.0, moon_t / 0.3)
    moon_color = tuple(int(v * alpha) for v in (220, 220, 200))
    draw.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=moon_color)
    # Craters
    crater_color = tuple(int(v * alpha) for v in (195, 195, 175))
    draw.ellipse([mx - 8, my - 6, mx - 2, my], fill=crater_color)
    draw.ellipse([mx + 4, my + 2, mx + 10, my + 8], fill=crater_color)

    # Moon reflection on water
    if moon_t > 0.2:
        refl_alpha = min(1.0, (moon_t - 0.2) / 0.3) * 0.2
        water_y = int(H * 0.45)
        for ry in range(water_y, water_y + 40):
            wobble = math.sin(ry * 0.4 + progress * 30) * 3
            rc = int(180 * refl_alpha * (1.0 - (ry - water_y) / 40))
            draw.line([(mx + wobble - 2, ry), (mx + wobble + 2, ry)],
                      fill=(rc, rc, int(rc * 0.9)), width=1)


def draw_hud(draw, frame_i, progress):
    """Minimal HUD overlay."""
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 12)
        font_vh = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
    except OSError:
        font = ImageFont.load_default()
        font_vh = font

    t_sec = frame_i / FPS
    phases = ["Golden Hour", "Sunset", "Twilight", "Night"]
    phase_idx = min(3, int(progress * 4))
    phase = phases[phase_idx]

    draw.text((18, H - 26),
              f"Frame {frame_i:>5d}/{TOTAL_FRAMES}  |  {t_sec:05.2f}s  |  {phase}",
              fill=(80, 85, 95), font=font)
    draw.text((W - 60, 14), ".vh", fill=(46, 213, 115), font=font_vh)


def get_phase_name(progress):
    if progress < 0.25:
        return "golden_hour"
    elif progress < 0.50:
        return "sunset"
    elif progress < 0.70:
        return "twilight"
    else:
        return "night"


def main():
    print(f"Generating {TOTAL_FRAMES} frames ({DURATION_S}s @ {FPS}fps) at {W}x{H}...")
    print(f"Output: {OUTPUT}")

    random.seed(42)

    # Scene objects
    clouds = [Cloud(5000 + i) for i in range(8)]
    birds = [Bird(6000 + i) for i in range(12)]
    palms = [Palm(x, 7000 + i) for i, x in enumerate([60, 180, W - 150, W - 60, W - 250])]
    palms.append(Palm(W * 0.5 - 300, 7010))

    with VHFile(OUTPUT, mode='w') as vh:
        vh.set_meta('width', W)
        vh.set_meta('height', H)
        vh.set_meta('fps', FPS)
        vh.set_meta('duration_s', DURATION_S)
        vh.set_meta('title', 'Beach Sunset — Golden Hour to Night')
        vh.set_meta('generator', 'Python + Pillow + vh-video-container')
        vh.set_meta('scene', 'Tropical beach sunset transitioning through golden hour, sunset, twilight, and night')
        vh.set_meta('prompt', 'Generate a 60-second video of a beach with the sunset transitioning into night')

        prev_hash = None
        dupes = 0

        for i in range(TOTAL_FRAMES):
            progress = i / (TOTAL_FRAMES - 1)  # 0.0 to 1.0
            t = i / FPS

            img = Image.new('RGB', (W, H), (0, 0, 0))
            draw = ImageDraw.Draw(img)

            # Sky
            draw_sky(draw, progress)
            draw_stars(draw, progress)
            draw_moon(draw, progress)

            # Clouds
            for cloud in clouds:
                cloud.draw(draw, t, progress)

            # Sun (modifies img via composite)
            draw_sun(draw, img, progress)
            draw = ImageDraw.Draw(img)

            # Water
            draw_water(draw, t, progress)

            # Sand
            draw_sand(draw, progress)

            # Palm trees
            for palm in palms:
                palm.draw(draw, t, progress)

            # Birds
            for bird in birds:
                bird.update()
                bird.draw(draw, t, progress)

            # HUD
            draw_hud(draw, i, progress)

            # Encode
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=QUALITY)
            frame_data = buf.getvalue()

            # Dedup
            frame_hash = hashlib.md5(frame_data).hexdigest()
            if prev_hash and frame_hash == prev_hash:
                vh.add_frame_ref(i, (i / FPS) * 1000, ref_frame_id=i - 1)
                dupes += 1
            else:
                vh.add_frame(i, (i / FPS) * 1000, frame_data, 'jpeg', W, H)
            prev_hash = frame_hash

            # Commit periodically
            if (i + 1) % (FPS * 2) == 0:
                vh.commit()
                sec = (i + 1) // FPS
                pct = (i + 1) / TOTAL_FRAMES * 100
                print(f"  [{pct:5.1f}%] {sec}s / {DURATION_S}s ({i + 1} frames)")

        # --- Annotations ---
        # Phase markers
        vh.annotate(0, 'phase', 'golden_hour')
        vh.annotate(0, 'description', 'Tropical beach at golden hour. Warm sunlight, calm ocean, birds in flight.')
        vh.annotate(0, 'light_level', 'bright')

        vh.annotate(FPS * 15, 'phase', 'sunset')
        vh.annotate(FPS * 15, 'description', 'Sun approaching the horizon. Sky turns orange and pink.')
        vh.annotate(FPS * 15, 'light_level', 'medium')

        vh.annotate(FPS * 30, 'phase', 'deep_sunset')
        vh.annotate(FPS * 30, 'description', 'Sun touching the waterline. Magenta and purple hues dominate.')
        vh.annotate(FPS * 30, 'light_level', 'low')

        vh.annotate(FPS * 42, 'phase', 'twilight')
        vh.annotate(FPS * 42, 'description', 'Sun below horizon. Deep blue twilight. First stars appearing.')
        vh.annotate(FPS * 42, 'light_level', 'very_low')

        vh.annotate(FPS * 52, 'phase', 'night')
        vh.annotate(FPS * 52, 'description', 'Full night sky. Stars visible, moon rising. Ocean reflects moonlight.')
        vh.annotate(FPS * 52, 'light_level', 'dark')

        vh.annotate(TOTAL_FRAMES - 1, 'phase', 'night_end')
        vh.annotate(TOTAL_FRAMES - 1, 'description', 'Peaceful night beach. End of sequence.')

        vh.commit()

    import os
    size_mb = os.path.getsize(OUTPUT) / (1024 * 1024)
    print(f"\nDone!")
    print(f"  Frames:     {TOTAL_FRAMES}")
    print(f"  Duplicates: {dupes}")
    print(f"  Duration:   {DURATION_S}s @ {FPS}fps")
    print(f"  File size:  {size_mb:.1f} MB")
    print(f"  Output:     {OUTPUT}")
    print(f"\nAnnotations added:")
    print(f"  golden_hour (0s), sunset (15s), deep_sunset (30s),")
    print(f"  twilight (42s), night (52s), night_end (60s)")
    print(f"\nPlay it:   vh viewer {OUTPUT}")
    print(f"Info:      vh info {OUTPUT}")
    print(f"Search:    vh search {OUTPUT} -k phase -v night")


if __name__ == '__main__':
    main()
