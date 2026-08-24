"""Full-screen pygame dashboard, one panel per person.

Runs without a desktop: on the Pi, SDL's kmsdrm backend draws straight to
the display. On a dev machine it opens a normal window (--windowed).
"""

import math
import os
import socket
import time
from dataclasses import dataclass

import pygame

from . import network, predict
from .config import SCREEN_PNG, Config, merged_thresholds
from .store import Store, UserSnapshot


@dataclass(frozen=True)
class Palette:
    name: str
    bg: tuple
    band: tuple        # chart background
    line: tuple        # dividers, gridlines
    fg: tuple
    dim: tuple
    stale: tuple
    in_range: tuple
    high: tuple
    low: tuple
    urgent: tuple


DARK = Palette(
    name="dark",
    bg=(13, 17, 23), band=(24, 32, 28), line=(45, 51, 59),
    fg=(235, 238, 241), dim=(110, 118, 129), stale=(90, 96, 104),
    in_range=(63, 185, 80), high=(210, 153, 34), low=(248, 81, 73),
    urgent=(255, 40, 40),
)
LIGHT = Palette(
    name="light",
    bg=(244, 246, 248), band=(232, 237, 240), line=(198, 204, 211),
    fg=(26, 32, 39), dim=(92, 102, 112), stale=(158, 166, 175),
    in_range=(22, 138, 58), high=(178, 108, 6), low=(206, 38, 38),
    urgent=(226, 0, 0),
)
THEMES = {p.name: p for p in (DARK, LIGHT)}
THEME_STATE_USER = "__display"     # params-table key for persisted UI state

DIRECTION_ANGLES = {
    "DoubleUp": (-90, 2),
    "SingleUp": (-90, 1),
    "FortyFiveUp": (-45, 1),
    "Flat": (0, 1),
    "FortyFiveDown": (45, 1),
    "SingleDown": (90, 1),
    "DoubleDown": (90, 2),
}


class FramebufferPresenter:
    """Presents pygame surfaces straight to /dev/fb0.

    Bypasses SDL's EGL/GLES scanout path entirely — the same route the
    text console uses, so if boot text is visible, this works. Selected
    with TRIO_DISPLAY=fbdev (SDL renders into a dummy surface and we
    copy the pixels out once a second).
    """

    def __init__(self, device: str = "/dev/fb0"):
        base = "/sys/class/graphics/" + os.path.basename(device)
        w, h = open(base + "/virtual_size").read().strip().split(",")
        self.width, self.height = int(w), int(h)
        self.bpp = int(open(base + "/bits_per_pixel").read())
        self.stride = int(open(base + "/stride").read())
        if self.bpp not in (16, 32):
            raise RuntimeError(f"unsupported framebuffer depth: {self.bpp}")
        self.dev = open(device, "r+b", buffering=0)
        self._conv = (
            pygame.Surface((self.width, self.height), 0, 16,
                           masks=(0xF800, 0x07E0, 0x001F, 0))
            if self.bpp == 16 else None
        )

    def present(self, surface: pygame.Surface) -> None:
        if self.bpp == 32:
            data = pygame.image.tobytes(surface, "BGRA")
            row = self.width * 4
        else:
            self._conv.blit(surface, (0, 0))
            raw = self._conv.get_buffer().raw
            pitch = self._conv.get_pitch()
            row = self.width * 2
            data = (raw if pitch == row else b"".join(
                raw[y * pitch:y * pitch + row] for y in range(self.height)
            ))
        if self.stride == row:
            self.dev.seek(0)
            self.dev.write(data)
        else:
            for y in range(self.height):
                self.dev.seek(y * self.stride)
                self.dev.write(data[y * row:(y + 1) * row])


def get_lan_ip() -> str:
    """Best-effort LAN IP (no packets are actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def age_text(now_ms: int, then_ms: int | None) -> str:
    if then_ms is None:
        return "--"
    minutes = int((now_ms - then_ms) / 60000)
    if minutes < 1:
        return "now"
    if minutes < 60:
        return f"{minutes}m ago"
    if minutes < 24 * 60:
        return f"{minutes // 60}h{minutes % 60:02d}m ago"
    return f"{minutes // (24 * 60)}d ago"


class Display:
    def __init__(self, config: Config, store: Store, windowed: bool = False):
        self.config = config
        self.store = store
        pygame.init()
        pygame.mouse.set_visible(False)
        dc = config.display
        self.fb: FramebufferPresenter | None = None
        if os.environ.get("TRIO_DISPLAY") == "fbdev":
            self.fb = FramebufferPresenter()
            dc.width, dc.height = self.fb.width, self.fb.height
        flags = 0 if (windowed or not dc.fullscreen) else pygame.FULLSCREEN
        self.screen = pygame.display.set_mode((dc.width, dc.height), flags)
        pygame.display.set_caption("Trio Monitor")
        self.clock = pygame.time.Clock()
        self._fonts: dict[int, pygame.font.Font] = {}
        saved = store.get_params(THEME_STATE_USER).get("theme", "dark")
        self.pal = THEMES.get(saved, DARK)
        self._toggle_rect = pygame.Rect(0, 0, 0, 0)
        self._last_toggle = 0.0
        self._qr_cache: tuple[str, pygame.Surface | None] | None = None
        self._lan_ip = ("", 0.0)  # (ip, fetched-at monotonic time)
        self._hotspot_pw = store.get_params("__network").get("hotspot_password", "")
        self._hotspot_state = (False, 0.0)  # (active, checked-at monotonic time)

    def toggle_theme(self):
        # Touchscreens can deliver a tap as both finger and mouse events;
        # debounce so one tap doesn't flip the theme twice.
        now = time.monotonic()
        if now - self._last_toggle < 0.5:
            return
        self._last_toggle = now
        self.pal = LIGHT if self.pal.name == "dark" else DARK
        self.store.set_params(THEME_STATE_USER, {"theme": self.pal.name})

    def font(self, px: int) -> pygame.font.Font:
        if px not in self._fonts:
            self._fonts[px] = pygame.font.Font(None, px)
        return self._fonts[px]

    def text(self, surface, s, px, color, center=None, midtop=None, midbottom=None):
        img = self.font(px).render(s, True, color)
        rect = img.get_rect()
        if center:
            rect.center = center
        elif midtop:
            rect.midtop = midtop
        elif midbottom:
            rect.midbottom = midbottom
        surface.blit(img, rect)
        return rect

    # ---- panel pieces ----

    @staticmethod
    def dim(color):
        return tuple(max(0, int(c * 0.72)) for c in color)

    def glucose_color(self, sgv: float | None, stale: bool, th: dict):
        if sgv is None or stale:
            return self.pal.stale
        if sgv <= th["urgent_low"] or sgv >= th["urgent_high"]:
            return self.pal.urgent
        if sgv < th["low"]:
            return self.pal.low
        if sgv > th["high"]:
            return self.pal.high
        return self.pal.in_range

    def draw_arrow(self, surface, center, size, direction, color):
        info = DIRECTION_ANGLES.get(direction or "")
        if info is None:
            return
        angle, count = info
        rad = math.radians(angle)
        cos_a, sin_a = math.cos(rad), math.sin(rad)

        def rot(x, y, cx, cy):
            return (cx + x * cos_a - y * sin_a, cy + x * sin_a + y * cos_a)

        half = size / 2
        # Perpendicular offset separates the arrows of a double arrow.
        perp = (-sin_a, cos_a)
        offsets = [0] if count == 1 else [-size * 0.32, size * 0.32]
        for off in offsets:
            cx = center[0] + perp[0] * off
            cy = center[1] + perp[1] * off
            shaft_w = max(2, int(size * 0.14))
            pygame.draw.line(
                surface, color,
                rot(-half, 0, cx, cy), rot(half * 0.45, 0, cx, cy), shaft_w,
            )
            head = [
                rot(half, 0, cx, cy),
                rot(half * 0.25, -half * 0.55, cx, cy),
                rot(half * 0.25, half * 0.55, cx, cy),
            ]
            pygame.draw.polygon(surface, color, head)

    def draw_sparkline(self, surface, rect, snap: UserSnapshot, stale: bool, th: dict,
                       future=None):
        pygame.draw.rect(surface, self.pal.band, rect, border_radius=6)
        if len(snap.history) < 2:
            return
        now_ms = int(time.time() * 1000)
        t0 = now_ms - 180 * 60 * 1000
        t1 = now_ms + (120 * 60 * 1000 if future else 0)
        values = [v for _, v in snap.history] + [v for _, v in (future or [])]
        lo = min(min(values), th["low"]) - 10
        hi = max(max(values), th["high"]) + 10

        def to_xy(t, v):
            x = rect.left + (t - t0) / (t1 - t0) * rect.width
            y = rect.bottom - (v - lo) / (hi - lo) * rect.height
            return (max(rect.left, min(rect.right, x)), y)

        # Target-range guide lines, labeled on the right edge.
        label_px = max(10, int(rect.height * 0.16))
        for bound in (th["low"], th["high"]):
            y = rect.bottom - (bound - lo) / (hi - lo) * rect.height
            pygame.draw.line(surface, self.pal.line, (rect.left + 4, y), (rect.right - 4, y))
            img = self.font(label_px).render(f"{bound:.0f}", True, self.pal.dim)
            surface.blit(img, img.get_rect(bottomright=(rect.right - 6, y - 1)))

        # "Now" divider between the actual past and the forecast.
        if future:
            x_now = to_xy(now_ms, lo)[0]
            pygame.draw.line(surface, self.pal.line,
                             (x_now, rect.top + 3), (x_now, rect.bottom - 3), 1)

        color = self.pal.stale if stale else self.pal.dim
        points = [to_xy(t, v) for t, v in snap.history]
        for i, (t, v) in enumerate(snap.history):
            dot = self.glucose_color(v, stale, th)
            pygame.draw.circle(surface, dot if not stale else self.pal.stale, points[i], 2)
        if len(points) >= 2:
            pygame.draw.aalines(surface, color, False, points)

        # Forecast: small dots only, no line — clearly not measured data.
        # Tinted by the range they predict, dimmed to read as tentative.
        for t, v in (future or []):
            pygame.draw.circle(surface, self.dim(self.glucose_color(v, False, th)),
                               to_xy(t, v), 1.5)

    def draw_panel(self, rect: pygame.Rect, user_cfg, snap: UserSnapshot):
        surface = self.screen
        dc = self.config.display
        name = user_cfg.name
        th = merged_thresholds(dc, user_cfg)
        now_ms = int(time.time() * 1000)
        h = rect.height
        cx = rect.centerx

        stale = (
            snap.sgv_date is None
            or now_ms - snap.sgv_date > dc.stale_minutes * 60 * 1000
        )
        color = self.glucose_color(snap.sgv, stale, th)

        # Name with a freshness dot: green = live, amber = lagging, red = stale
        name_px = int(h * 0.085)
        name_rect = self.text(
            surface, name, name_px, self.pal.dim, midtop=(cx, rect.top + int(h * 0.025))
        )
        age_min = (now_ms - snap.sgv_date) / 60000 if snap.sgv_date else 1e9
        dot_color = self.pal.in_range if age_min <= 7 else (self.pal.high if age_min <= dc.stale_minutes else self.pal.low)
        pygame.draw.circle(
            surface, dot_color,
            (name_rect.right + int(h * 0.035), name_rect.centery), int(h * 0.014),
        )

        # Big glucose number with trend arrow, centered as a group
        sgv_str = f"{snap.sgv:.0f}" if snap.sgv is not None else "---"
        big_px = int(h * 0.34)
        arrow_size = int(h * 0.095)
        show_arrow = not stale and snap.direction in DIRECTION_ANGLES
        num_img = self.font(big_px).render(sgv_str, True, color)
        arrow_w = int(arrow_size * 1.9) if show_arrow else 0
        num_rect = num_img.get_rect()
        num_rect.left = cx - (num_rect.width + arrow_w) // 2
        num_rect.centery = rect.top + int(h * 0.225)
        surface.blit(num_img, num_rect)
        if show_arrow:
            self.draw_arrow(
                surface,
                (num_rect.right + int(arrow_size * 1.05), num_rect.centery),
                arrow_size, snap.direction, color,
            )

        # Delta and reading age
        parts = []
        if snap.delta is not None and not stale:
            parts.append(f"{snap.delta:+.0f}")
        parts.append(age_text(now_ms, snap.sgv_date))
        line_color = self.pal.low if stale and snap.sgv_date else self.pal.dim
        self.text(
            surface, "   ".join(parts), int(h * 0.062), line_color,
            center=(cx, rect.top + int(h * 0.385)),
        )

        # Chart: 3 hours of history plus the 2-hour forecast
        horizons, future, source = (None, None, None)
        if not stale:
            horizons, future, source = predict.predict(snap, now_ms)
        margin = int(rect.width * 0.06)
        chart = pygame.Rect(
            rect.left + margin, rect.top + int(h * 0.425),
            rect.width - 2 * margin, int(h * 0.215),
        )
        self.draw_sparkline(surface, chart, snap, stale, th, future)

        # Forecast strip: four labeled cells, values tinted by predicted range
        if horizons:
            tilde = "~" if source == "est" else ""
            labels = {30: "+30m", 60: "+1h", 90: "+1.5h", 120: "+2h"}
            cells = [hz for hz in (30, 60, 90, 120) if hz in horizons]
            for idx, hz in enumerate(cells):
                cell_x = rect.left + rect.width * (1 + 2 * idx) // (2 * len(cells))
                self.text(surface, labels[hz], int(h * 0.045), self.pal.dim,
                          midtop=(cell_x, rect.top + int(h * 0.665)))
                value_color = self.dim(self.glucose_color(horizons[hz], False, th))
                self.text(surface, f"{tilde}{horizons[hz]:.0f}", int(h * 0.085),
                          value_color, midtop=(cell_x, rect.top + int(h * 0.705)))

        # Stats row: IOB, COB, last carbs, last bolus
        stats = [
            ("IOB", f"{snap.iob:.1f}U" if snap.iob is not None else "--", None),
            ("COB", f"{snap.cob:.0f}g" if snap.cob is not None else "--", None),
            ("CARBS",
             f"{snap.last_carbs:.0f}g" if snap.last_carbs is not None else "--",
             age_text(now_ms, snap.last_carbs_date) if snap.last_carbs_date else None),
            ("BOLUS",
             f"{snap.last_bolus:.2f}U" if snap.last_bolus is not None else "--",
             age_text(now_ms, snap.last_bolus_date) if snap.last_bolus_date else None),
        ]
        for idx, (label, value, sub) in enumerate(stats):
            cell_x = rect.left + rect.width * (1 + 2 * idx) // 8
            self.text(surface, label, int(h * 0.042), self.pal.dim,
                      midtop=(cell_x, rect.top + int(h * 0.815)))
            self.text(surface, value, int(h * 0.075), self.pal.fg,
                      midtop=(cell_x, rect.top + int(h * 0.855)))
            if sub:
                self.text(surface, sub, int(h * 0.04), self.pal.dim,
                          midtop=(cell_x, rect.top + int(h * 0.928)))

        # Urgent readings get a colored border to catch the eye across a room.
        if color == self.pal.urgent:
            pygame.draw.rect(surface, self.pal.urgent, rect.inflate(-6, -6), 4, border_radius=10)

    # ---- first-boot setup screen ----

    def _cached_lan_ip(self) -> str:
        ip, fetched = self._lan_ip
        if not ip or time.monotonic() - fetched > 60:
            ip = get_lan_ip()
            self._lan_ip = (ip, time.monotonic())
        return ip

    def _qr_surface(self, url: str, target_px: int) -> pygame.Surface | None:
        if self._qr_cache and self._qr_cache[0] == url:
            return self._qr_cache[1]
        surface = None
        try:
            import qrcode
            qr = qrcode.QRCode(
                error_correction=qrcode.constants.ERROR_CORRECT_M, border=2
            )
            qr.add_data(url)
            qr.make(fit=True)
            matrix = qr.get_matrix()
            n = len(matrix)
            scale = max(2, target_px // n)
            # Always dark-on-white regardless of theme — scanners need contrast.
            surface = pygame.Surface((n * scale, n * scale))
            surface.fill((255, 255, 255))
            for y, row in enumerate(matrix):
                for x, dark in enumerate(row):
                    if dark:
                        pygame.draw.rect(
                            surface, (0, 0, 0),
                            (x * scale, y * scale, scale, scale),
                        )
        except ImportError:
            pass  # no qrcode library: the URL text below still shows the way
        self._qr_cache = (url, surface)
        return surface

    def _hotspot_is_active(self) -> bool:
        active, checked = self._hotspot_state
        if time.monotonic() - checked > 5:
            active = network.available() and network.hotspot_active()
            self._hotspot_state = (active, time.monotonic())
        return active

    def draw_hotspot_screen(self):
        screen = self.screen
        w, h = screen.get_width(), screen.get_height()
        cx = w // 2
        s = min(w, h)  # scale text by the smaller dimension (portrait-safe)
        ssid, pw = network.HOTSPOT_SSID, self._hotspot_pw

        self.text(screen, "Connect Trio Monitor to Wi-Fi", int(s * 0.09),
                  self.pal.fg, midtop=(cx, int(h * 0.035)))
        self.text(screen, "1.  Scan to join the setup hotspot", int(s * 0.055),
                  self.pal.dim, midtop=(cx, int(h * 0.14)))

        qr = self._qr_surface(f"WIFI:T:WPA;S:{ssid};P:{pw};;", int(s * 0.42))
        if qr:
            rect = qr.get_rect(center=(cx, int(h * 0.45)))
            screen.blit(qr, rect)
            info_y = rect.bottom + int(s * 0.025)
        else:
            info_y = int(h * 0.40)

        self.text(screen, f"{ssid}   password: {pw}", int(s * 0.055),
                  self.pal.fg, midtop=(cx, info_y))
        self.text(
            screen,
            f"2.  Then open  http://{network.HOTSPOT_ADDR}:{self.config.admin_port}"
            "/settings  to pick your Wi-Fi",
            int(s * 0.05), self.pal.dim, midtop=(cx, info_y + int(s * 0.08)),
        )

    def is_unconfigured(self, snaps) -> bool:
        """True until any data has arrived or any pull source is configured."""
        if any(s.sgv_date for s in snaps):
            return False
        if any(u.source for u in self.config.users):
            return False
        return True

    def draw_setup_screen(self):
        screen = self.screen
        w, h = screen.get_width(), screen.get_height()
        cx = w // 2
        s = min(w, h)  # scale text by the smaller dimension (portrait-safe)
        url = f"http://{self._cached_lan_ip()}:{self.config.admin_port}/settings"

        self.text(screen, "Trio Monitor", int(s * 0.11), self.pal.fg,
                  midtop=(cx, int(h * 0.045)))
        self.text(screen, "Scan from a phone on this network to set up",
                  int(s * 0.055), self.pal.dim, midtop=(cx, int(h * 0.16)))

        qr = self._qr_surface(url, int(s * 0.48)) if self.config.admin_port else None
        if qr:
            rect = qr.get_rect(center=(cx, int(h * 0.51)))
            screen.blit(qr, rect)
            info_y = rect.bottom + int(s * 0.03)
        else:
            info_y = int(h * 0.45)

        self.text(screen, url, int(s * 0.06), self.pal.fg, midtop=(cx, info_y))
        if self.config.admin_password:
            self.text(
                screen,
                f"login:  admin  /  {self.config.admin_password}",
                int(s * 0.05), self.pal.dim,
                midtop=(cx, info_y + int(s * 0.075)),
            )

    def draw(self):
        self.screen.fill(self.pal.bg)
        users = self.config.users
        width = self.screen.get_width() // len(users)
        height = self.screen.get_height()
        if self._hotspot_is_active():
            self.draw_hotspot_screen()
            pygame.display.flip()
            if self.fb:
                self.fb.present(self.screen)
            return
        snaps = [self.store.snapshot(user.name) for user in users]
        if self.is_unconfigured(snaps):
            self.draw_setup_screen()
            pygame.display.flip()
            if self.fb:
                self.fb.present(self.screen)
            return
        full_w = self.screen.get_width()
        portrait = height > full_w
        for i, (user, snap) in enumerate(zip(users, snaps)):
            if portrait:
                row_h = height // len(users)
                rect = pygame.Rect(0, i * row_h, full_w, row_h)
                if i > 0:
                    pygame.draw.line(self.screen, self.pal.line,
                                     (12, rect.top), (full_w - 12, rect.top), 2)
            else:
                rect = pygame.Rect(i * width, 0, width, height)
                if i > 0:
                    pygame.draw.line(self.screen, self.pal.line,
                                     (rect.left, 12), (rect.left, height - 12), 2)
            self.draw_panel(rect, user, snap)
        # Small clock, top-center between the two names.
        self.text(
            self.screen, time.strftime("%H:%M"), int(height * 0.055), self.pal.dim,
            midtop=(self.screen.get_width() // 2, 8),
        )
        self.draw_theme_button()
        pygame.display.flip()
        if self.fb:
            self.fb.present(self.screen)

    def draw_theme_button(self):
        """Sun/moon touch button, bottom-center: tap to switch theme."""
        w, h = self.screen.get_width(), self.screen.get_height()
        center = (w // 2, h - int(h * 0.045))
        r = max(7, int(h * 0.016))
        # Generous touch target (finger-sized), icon drawn smaller.
        self._toggle_rect = pygame.Rect(0, 0, int(h * 0.11), int(h * 0.11))
        self._toggle_rect.center = center
        color = self.pal.dim
        if self.pal.name == "dark":
            # Sun: tapping goes to light mode.
            pygame.draw.circle(self.screen, color, center, r, 2)
            for i in range(8):
                angle = i * math.pi / 4
                inner = (center[0] + math.cos(angle) * (r + 3),
                         center[1] + math.sin(angle) * (r + 3))
                outer = (center[0] + math.cos(angle) * (r + 6),
                         center[1] + math.sin(angle) * (r + 6))
                pygame.draw.line(self.screen, color, inner, outer, 2)
        else:
            # Moon: tapping goes back to dark mode.
            pygame.draw.circle(self.screen, color, center, r + 2)
            pygame.draw.circle(self.screen, self.pal.bg,
                               (center[0] + r // 2 + 2, center[1] - r // 2 + 1), r + 1)

    def save_snapshot(self):
        """Atomically write the current frame for the /screen.png endpoint."""
        tmp = SCREEN_PNG + ".tmp.png"
        try:
            pygame.image.save(self.screen, tmp)
            os.replace(tmp, SCREEN_PNG)
        except (pygame.error, OSError):
            pass  # a missed snapshot is harmless

    def run(self):
        running = True
        last_snapshot = 0.0
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN and event.key in (
                    pygame.K_ESCAPE, pygame.K_q,
                ):
                    running = False
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_t:
                    self.toggle_theme()
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    if self._toggle_rect.collidepoint(event.pos):
                        self.toggle_theme()
                elif event.type == pygame.FINGERDOWN:
                    x = event.x * self.screen.get_width()
                    y = event.y * self.screen.get_height()
                    if self._toggle_rect.collidepoint((x, y)):
                        self.toggle_theme()
            self.draw()
            if time.time() - last_snapshot >= 5:
                self.save_snapshot()
                last_snapshot = time.time()
            self.clock.tick(1)  # 1 fps is plenty for a glucose dashboard
        pygame.quit()

    def screenshot(self, path: str):
        self.draw()
        pygame.image.save(self.screen, path)
        pygame.quit()
