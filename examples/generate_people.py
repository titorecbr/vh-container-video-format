"""
Generate a VH video with animated people walking through an urban scene.
People are drawn as stylized figures with walking animation, shadows, and city backdrop.
~20 seconds at 24fps = 480 frames.
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
DURATION_S = 20
TOTAL_FRAMES = FPS * DURATION_S
OUTPUT = 'night_city.vh'
QUALITY = 90

# --- Sky gradient ---
SKY_TOP = (15, 20, 40)
SKY_HORIZON = (45, 55, 85)
GROUND_COLOR = (35, 38, 42)
SIDEWALK = (55, 58, 62)

# --- Building palette ---
BUILDING_COLORS = [
    (30, 33, 40), (35, 38, 48), (25, 30, 38), (40, 43, 52),
    (28, 32, 42), (38, 40, 50), (32, 36, 44), (22, 26, 35),
]
WINDOW_LIT = [(255, 220, 120), (255, 200, 100), (200, 180, 100), (180, 160, 80)]
WINDOW_OFF = [(20, 22, 28), (25, 28, 35)]

# --- Person colors ---
SKIN_TONES = [(210, 170, 130), (180, 140, 100), (140, 100, 70), (100, 70, 50), (230, 190, 150), (160, 120, 85)]
SHIRT_COLORS = [
    (200, 60, 60), (60, 130, 200), (220, 180, 50), (80, 180, 100),
    (180, 80, 180), (220, 120, 50), (100, 100, 200), (200, 200, 200),
    (50, 50, 50), (150, 80, 60), (60, 60, 60), (0, 120, 120),
]
PANTS_COLORS = [
    (40, 40, 60), (50, 50, 80), (30, 30, 45), (60, 50, 40),
    (80, 80, 100), (35, 35, 50), (70, 60, 50), (20, 20, 30),
]
HAIR_COLORS = [(30, 20, 15), (60, 40, 25), (20, 15, 10), (80, 50, 30), (150, 100, 50), (40, 30, 20)]


class Person:
    def __init__(self, seed):
        rng = random.Random(seed)
        self.height = rng.uniform(55, 80)  # pixel height
        self.width = self.height * 0.35
        self.y_base = rng.uniform(H * 0.58, H * 0.88)  # ground level (further = higher)

        # Depth sorting: higher y_base = closer to camera = larger
        depth_factor = (self.y_base - H * 0.55) / (H * 0.35)
        self.height *= 0.6 + depth_factor * 0.8
        self.width = self.height * 0.35

        self.speed = rng.uniform(0.8, 2.5) * (0.5 + depth_factor * 0.7)
        self.direction = rng.choice([-1, 1])
        self.x = rng.uniform(-100, W + 100)

        self.skin = rng.choice(SKIN_TONES)
        self.shirt = rng.choice(SHIRT_COLORS)
        self.pants = rng.choice(PANTS_COLORS)
        self.hair = rng.choice(HAIR_COLORS)
        self.has_hat = rng.random() < 0.2
        self.hat_color = rng.choice(SHIRT_COLORS) if self.has_hat else None
        self.walk_phase = rng.uniform(0, math.pi * 2)
        self.walk_speed = rng.uniform(3.5, 5.0)
        self.arm_swing = rng.uniform(0.3, 0.6)
        self.gender = rng.choice(['m', 'f'])
        self.bag = rng.random() < 0.15
        self.bag_color = rng.choice(SHIRT_COLORS) if self.bag else None

    def update(self):
        self.x += self.speed * self.direction
        # Wrap around
        if self.direction > 0 and self.x > W + 150:
            self.x = -150
        elif self.direction < 0 and self.x < -150:
            self.x = W + 150

    def draw(self, draw_ctx, frame_i):
        t = frame_i / FPS
        walk_cycle = math.sin(t * self.walk_speed + self.walk_phase)
        arm_cycle = math.cos(t * self.walk_speed + self.walk_phase)

        cx = self.x
        by = self.y_base  # bottom (feet)
        h = self.height
        w = self.width

        head_r = h * 0.12
        torso_h = h * 0.35
        leg_h = h * 0.38
        neck_y = by - leg_h - torso_h
        head_cy = neck_y - head_r

        # --- Shadow ---
        shadow_w = w * 1.2
        shadow_h = h * 0.06
        shadow_alpha_ellipse = [
            (cx - shadow_w, by - shadow_h),
            (cx + shadow_w, by + shadow_h)
        ]
        draw_ctx.ellipse(shadow_alpha_ellipse, fill=(15, 16, 20))

        # --- Legs ---
        leg_spread = walk_cycle * h * 0.12
        hip_y = by - leg_h

        # Left leg
        foot_lx = cx - w * 0.15 + leg_spread
        draw_ctx.line([(cx - w * 0.15, hip_y), (foot_lx, by)],
                      fill=self.pants, width=max(2, int(w * 0.22)))
        # Right leg
        foot_rx = cx + w * 0.15 - leg_spread
        draw_ctx.line([(cx + w * 0.15, hip_y), (foot_rx, by)],
                      fill=self.pants, width=max(2, int(w * 0.22)))

        # Shoes
        shoe_color = (25, 25, 30)
        shoe_w = w * 0.18
        shoe_h = h * 0.04
        draw_ctx.ellipse([foot_lx - shoe_w, by - shoe_h, foot_lx + shoe_w, by + shoe_h * 0.5],
                         fill=shoe_color)
        draw_ctx.ellipse([foot_rx - shoe_w, by - shoe_h, foot_rx + shoe_w, by + shoe_h * 0.5],
                         fill=shoe_color)

        # --- Torso ---
        torso_w = w * 0.45
        if self.gender == 'f':
            # Slightly tapered
            draw_ctx.polygon([
                (cx - torso_w * 0.8, neck_y),
                (cx + torso_w * 0.8, neck_y),
                (cx + torso_w, hip_y),
                (cx - torso_w, hip_y),
            ], fill=self.shirt)
        else:
            draw_ctx.rectangle([cx - torso_w, neck_y, cx + torso_w, hip_y], fill=self.shirt)

        # --- Arms ---
        arm_len = h * 0.28
        arm_w = max(2, int(w * 0.15))
        l_arm_angle = arm_cycle * self.arm_swing
        r_arm_angle = -arm_cycle * self.arm_swing

        shoulder_y = neck_y + h * 0.05
        # Left arm
        l_hand_x = cx - torso_w - math.sin(l_arm_angle) * arm_len * 0.5
        l_hand_y = shoulder_y + math.cos(l_arm_angle) * arm_len
        draw_ctx.line([(cx - torso_w, shoulder_y), (l_hand_x, l_hand_y)],
                      fill=self.shirt, width=arm_w)
        # Hand
        hand_r = w * 0.08
        draw_ctx.ellipse([l_hand_x - hand_r, l_hand_y - hand_r,
                          l_hand_x + hand_r, l_hand_y + hand_r], fill=self.skin)

        # Right arm
        r_hand_x = cx + torso_w + math.sin(r_arm_angle) * arm_len * 0.5
        r_hand_y = shoulder_y + math.cos(r_arm_angle) * arm_len
        draw_ctx.line([(cx + torso_w, shoulder_y), (r_hand_x, r_hand_y)],
                      fill=self.shirt, width=arm_w)
        draw_ctx.ellipse([r_hand_x - hand_r, r_hand_y - hand_r,
                          r_hand_x + hand_r, r_hand_y + hand_r], fill=self.skin)

        # --- Bag ---
        if self.bag:
            bag_side = 1 if self.direction > 0 else -1
            bag_x = cx + bag_side * torso_w * 0.6
            bag_y = shoulder_y + torso_h * 0.2
            bag_w = w * 0.2
            bag_h = torso_h * 0.5
            draw_ctx.rectangle([bag_x - bag_w, bag_y, bag_x + bag_w, bag_y + bag_h],
                               fill=self.bag_color)
            draw_ctx.line([(bag_x, bag_y), (bag_x, shoulder_y - h * 0.02)],
                          fill=self.bag_color, width=max(1, int(w * 0.06)))

        # --- Neck ---
        neck_w = w * 0.1
        draw_ctx.rectangle([cx - neck_w, head_cy + head_r * 0.8, cx + neck_w, neck_y + h * 0.02],
                           fill=self.skin)

        # --- Head ---
        draw_ctx.ellipse([cx - head_r, head_cy - head_r, cx + head_r, head_cy + head_r],
                         fill=self.skin)

        # Hair
        hair_top = head_cy - head_r
        if self.gender == 'f':
            # Longer hair
            draw_ctx.ellipse([cx - head_r * 1.1, hair_top - head_r * 0.1,
                              cx + head_r * 1.1, head_cy + head_r * 0.3], fill=self.hair)
            # Side hair
            draw_ctx.ellipse([cx - head_r * 1.2, head_cy - head_r * 0.3,
                              cx - head_r * 0.5, head_cy + head_r * 1.3], fill=self.hair)
            draw_ctx.ellipse([cx + head_r * 0.5, head_cy - head_r * 0.3,
                              cx + head_r * 1.2, head_cy + head_r * 1.3], fill=self.hair)
        else:
            draw_ctx.ellipse([cx - head_r * 1.05, hair_top - head_r * 0.05,
                              cx + head_r * 1.05, head_cy], fill=self.hair)

        # Hat
        if self.has_hat:
            draw_ctx.rectangle([cx - head_r * 1.3, hair_top - head_r * 0.3,
                                cx + head_r * 1.3, hair_top + head_r * 0.15],
                               fill=self.hat_color)
            draw_ctx.rectangle([cx - head_r * 0.9, hair_top - head_r * 0.7,
                                cx + head_r * 0.9, hair_top - head_r * 0.1],
                               fill=self.hat_color)

        # Eyes (face direction)
        eye_y = head_cy - head_r * 0.1
        eye_offset = head_r * 0.3 * self.direction
        eye_r = head_r * 0.08
        draw_ctx.ellipse([cx - head_r * 0.25 + eye_offset - eye_r, eye_y - eye_r,
                          cx - head_r * 0.25 + eye_offset + eye_r, eye_y + eye_r],
                         fill=(20, 20, 25))
        draw_ctx.ellipse([cx + head_r * 0.25 + eye_offset - eye_r, eye_y - eye_r,
                          cx + head_r * 0.25 + eye_offset + eye_r, eye_y + eye_r],
                         fill=(20, 20, 25))


class Building:
    def __init__(self, x, w, seed):
        rng = random.Random(seed)
        self.x = x
        self.w = w
        self.h = rng.uniform(H * 0.25, H * 0.55)
        self.color = rng.choice(BUILDING_COLORS)
        self.y_top = H * 0.52 - self.h
        self.windows = []
        self.window_pattern = rng.random()

        # Generate window grid
        cols = max(2, int(self.w / 22))
        rows = max(2, int(self.h / 28))
        margin_x = self.w * 0.1
        margin_y = self.h * 0.06
        win_w = (self.w - margin_x * 2) / cols * 0.65
        win_h = (self.h - margin_y * 2) / rows * 0.55
        spacing_x = (self.w - margin_x * 2) / cols
        spacing_y = (self.h - margin_y * 2) / rows

        for r in range(rows):
            for c in range(cols):
                wx = self.x + margin_x + c * spacing_x + spacing_x * 0.15
                wy = self.y_top + margin_y + r * spacing_y + spacing_y * 0.2
                lit_prob = rng.random()
                self.windows.append({
                    'x': wx, 'y': wy, 'w': win_w, 'h': win_h,
                    'lit_base': lit_prob,
                    'lit_color': rng.choice(WINDOW_LIT),
                    'off_color': rng.choice(WINDOW_OFF),
                    'flicker_speed': rng.uniform(0.5, 3.0),
                    'flicker_phase': rng.uniform(0, math.pi * 2),
                })

        # Rooftop details
        self.has_antenna = rng.random() < 0.3
        self.antenna_x = self.x + rng.uniform(self.w * 0.3, self.w * 0.7)
        self.antenna_h = rng.uniform(15, 35)
        self.has_ac = rng.random() < 0.4
        self.ac_x = self.x + rng.uniform(self.w * 0.2, self.w * 0.8)

    def draw(self, draw_ctx, frame_i):
        t = frame_i / FPS

        # Main structure
        draw_ctx.rectangle([self.x, self.y_top, self.x + self.w, H * 0.52], fill=self.color)

        # Edge highlight
        edge_color = tuple(min(255, c + 8) for c in self.color)
        draw_ctx.line([(self.x, self.y_top), (self.x, H * 0.52)], fill=edge_color, width=1)
        draw_ctx.line([(self.x, self.y_top), (self.x + self.w, self.y_top)], fill=edge_color, width=1)

        # Windows
        for win in self.windows:
            flicker = math.sin(t * win['flicker_speed'] + win['flicker_phase'])
            is_lit = (win['lit_base'] + flicker * 0.1) > 0.45
            color = win['lit_color'] if is_lit else win['off_color']
            draw_ctx.rectangle([win['x'], win['y'], win['x'] + win['w'], win['y'] + win['h']],
                               fill=color)

        # Rooftop antenna
        if self.has_antenna:
            draw_ctx.line([(self.antenna_x, self.y_top), (self.antenna_x, self.y_top - self.antenna_h)],
                          fill=(60, 65, 75), width=2)
            blink = math.sin(t * 2) > 0.7
            if blink:
                draw_ctx.ellipse([self.antenna_x - 2, self.y_top - self.antenna_h - 2,
                                  self.antenna_x + 2, self.y_top - self.antenna_h + 2],
                                 fill=(255, 50, 50))

        # AC unit
        if self.has_ac:
            draw_ctx.rectangle([self.ac_x - 8, self.y_top - 6, self.ac_x + 8, self.y_top],
                               fill=(50, 55, 60))


class StreetLight:
    def __init__(self, x, seed):
        rng = random.Random(seed)
        self.x = x
        self.pole_h = rng.uniform(100, 130)
        self.y_base = H * 0.6
        self.y_top = self.y_base - self.pole_h
        self.light_color = (255, 230, 150)
        self.glow_r = rng.uniform(50, 70)
        self.flicker_phase = rng.uniform(0, math.pi * 2)

    def draw(self, draw_ctx, img, frame_i):
        t = frame_i / FPS
        # Pole
        draw_ctx.line([(self.x, self.y_base), (self.x, self.y_top)],
                      fill=(60, 65, 70), width=3)
        # Arm
        draw_ctx.line([(self.x, self.y_top), (self.x + 15, self.y_top)],
                      fill=(60, 65, 70), width=2)
        # Lamp
        draw_ctx.rectangle([self.x + 10, self.y_top - 3, self.x + 20, self.y_top + 5],
                           fill=self.light_color)

        # Glow on ground
        flicker = 0.85 + 0.15 * math.sin(t * 6 + self.flicker_phase)
        glow_overlay = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow_overlay)
        for layer in range(8, 0, -1):
            r = self.glow_r * (layer / 8) * 1.8
            alpha = int(20 * (1.0 - layer / 8) * flicker)
            gd.ellipse([self.x + 15 - r, self.y_base - r * 0.3,
                        self.x + 15 + r, self.y_base + r * 0.3],
                       fill=(*self.light_color, alpha))
        img.paste(Image.alpha_composite(img.convert('RGBA'), glow_overlay).convert('RGB'))


def draw_sky(img):
    """Draw gradient sky."""
    draw = ImageDraw.Draw(img)
    horizon_y = int(H * 0.52)
    for y in range(horizon_y):
        t = y / horizon_y
        c = tuple(int(SKY_TOP[i] + (SKY_HORIZON[i] - SKY_TOP[i]) * t) for i in range(3))
        draw.line([(0, y), (W, y)], fill=c)

    # Stars
    rng = random.Random(999)
    for _ in range(80):
        sx = rng.randint(0, W)
        sy = rng.randint(0, int(horizon_y * 0.7))
        brightness = rng.randint(100, 220)
        draw.point((sx, sy), fill=(brightness, brightness, brightness))


def draw_ground(draw):
    """Draw ground plane: road + sidewalks."""
    horizon_y = int(H * 0.52)
    # Road
    draw.rectangle([0, horizon_y, W, H], fill=GROUND_COLOR)
    # Sidewalk top
    draw.rectangle([0, horizon_y, W, int(H * 0.56)], fill=SIDEWALK)
    # Sidewalk bottom
    draw.rectangle([0, int(H * 0.9), W, H], fill=SIDEWALK)

    # Road lines (dashed center)
    center_y = int(H * 0.73)
    dash_w = 40
    gap = 30
    for x in range(0, W, dash_w + gap):
        draw.rectangle([x, center_y - 1, x + dash_w, center_y + 1], fill=(120, 120, 80))

    # Curb lines
    draw.line([(0, int(H * 0.56)), (W, int(H * 0.56))], fill=(70, 73, 78), width=2)
    draw.line([(0, int(H * 0.9)), (W, int(H * 0.9))], fill=(70, 73, 78), width=2)


def draw_moon(draw, frame_i):
    """Draw moon."""
    mx = W * 0.82
    my = H * 0.12
    mr = 22
    draw.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=(220, 220, 200))
    # Crater shadow
    draw.ellipse([mx - mr + 8, my - mr + 5, mx - mr + 18, my - mr + 15], fill=(200, 200, 180))
    draw.ellipse([mx + 3, my + 2, mx + 12, my + 10], fill=(195, 195, 175))


def draw_clouds(draw, frame_i):
    """Slowly drifting clouds."""
    t = frame_i / FPS
    rng = random.Random(77)
    for _ in range(5):
        base_x = rng.uniform(-100, W + 100)
        base_y = rng.uniform(H * 0.05, H * 0.25)
        speed = rng.uniform(3, 8)
        cx = (base_x + t * speed) % (W + 200) - 100
        cloud_color = (25, 30, 50)
        for blob in range(rng.randint(3, 6)):
            bx = cx + rng.uniform(-30, 30)
            by = base_y + rng.uniform(-8, 8)
            br = rng.uniform(15, 35)
            draw.ellipse([bx - br, by - br * 0.5, bx + br, by + br * 0.5], fill=cloud_color)


def draw_hud(draw, frame_i, total, num_people):
    """HUD overlay."""
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 13)
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
    except OSError:
        font = ImageFont.load_default()
        font_title = font

    t_sec = frame_i / FPS
    draw.text((20, H - 30), f"Frame {frame_i:>4d}/{total}  |  {t_sec:.2f}s  |  {num_people} people",
              fill=(70, 80, 95), font=font)
    draw.text((W - 80, 16), ".vh", fill=(46, 213, 115), font=font_title)


def main():
    print(f"Generating {TOTAL_FRAMES} frames ({DURATION_S}s @ {FPS}fps) at {W}x{H}...")
    print(f"Output: {OUTPUT}")

    random.seed(42)

    # --- Generate city ---
    buildings = []
    x = -10
    bseed = 1000
    while x < W + 10:
        bw = random.uniform(50, 120)
        buildings.append(Building(x, bw, bseed))
        x += bw + random.uniform(-5, 3)
        bseed += 1

    # --- Street lights ---
    lights = [StreetLight(x, 2000 + i) for i, x in enumerate(range(80, W, 200))]

    # --- People on sidewalks ---
    NUM_PEOPLE = 18
    people = [Person(3000 + i) for i in range(NUM_PEOPLE)]

    # --- Pre-render static sky ---
    sky_img = Image.new('RGB', (W, H), BG_COLOR := (10, 14, 20))
    draw_sky(sky_img)
    draw_moon(ImageDraw.Draw(sky_img), 0)

    # --- Generate frames ---
    with VHFile(OUTPUT, mode='w') as vh:
        vh.set_meta('width', W)
        vh.set_meta('height', H)
        vh.set_meta('fps', FPS)
        vh.set_meta('duration_s', DURATION_S)
        vh.set_meta('title', 'Night City - People Walking')
        vh.set_meta('generator', 'Pillow + vh-video-container')
        vh.set_meta('scene', 'Urban night scene with pedestrians')

        prev_hash = None

        for i in range(TOTAL_FRAMES):
            # Start from cached sky
            img = sky_img.copy()
            draw = ImageDraw.Draw(img)

            # Clouds (animated)
            draw_clouds(draw, i)

            # Buildings (windows flicker)
            for b in buildings:
                b.draw(draw, i)

            # Ground
            draw_ground(draw)

            # Street lights (with glow - modifies img)
            for light in lights:
                light.draw(draw, img, i)
                draw = ImageDraw.Draw(img)  # refresh after composite

            # People sorted by depth (further = drawn first)
            people_sorted = sorted(people, key=lambda p: p.y_base)
            for person in people_sorted:
                person.update()
                person.draw(draw, i)

            # HUD
            draw_hud(draw, i, TOTAL_FRAMES, NUM_PEOPLE)

            # Encode
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=QUALITY)
            frame_data = buf.getvalue()

            # Dedup
            frame_hash = hashlib.md5(frame_data).hexdigest()
            if prev_hash and frame_hash == prev_hash:
                vh.add_frame_ref(i, (i / FPS) * 1000, ref_frame_id=i - 1)
            else:
                vh.add_frame(i, (i / FPS) * 1000, frame_data, 'jpeg', W, H)
            prev_hash = frame_hash

            if (i + 1) % FPS == 0:
                vh.commit()
                sec = (i + 1) // FPS
                pct = (i + 1) / TOTAL_FRAMES * 100
                print(f"  [{pct:5.1f}%] {sec}s / {DURATION_S}s ({i + 1} frames)")

        # Annotations
        vh.annotate(0, 'scene', 'night_city_intro')
        vh.annotate(0, 'description', f'Urban night scene with {NUM_PEOPLE} pedestrians walking on sidewalks')
        vh.annotate(0, 'people_count', NUM_PEOPLE)
        vh.annotate(FPS * 5, 'timestamp', '5 seconds')
        vh.annotate(FPS * 10, 'scene', 'midpoint')
        vh.annotate(FPS * 10, 'timestamp', '10 seconds')
        vh.annotate(FPS * 15, 'timestamp', '15 seconds')
        vh.annotate(TOTAL_FRAMES - 1, 'scene', 'end')
        vh.commit()

    import os
    size_mb = os.path.getsize(OUTPUT) / (1024 * 1024)
    print(f"\nDone!")
    print(f"  Frames:     {TOTAL_FRAMES}")
    print(f"  Duration:   {DURATION_S}s @ {FPS}fps")
    print(f"  People:     {NUM_PEOPLE}")
    print(f"  Buildings:  {len(buildings)}")
    print(f"  File size:  {size_mb:.1f} MB")
    print(f"  Output:     {OUTPUT}")


if __name__ == '__main__':
    main()
