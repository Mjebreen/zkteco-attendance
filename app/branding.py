"""Branding an admin can change from the UI: company name, tagline, logo, accent colour, default theme.

Stored in the database (app_settings / app_assets) so it survives redeploys and works in a split
deploy. COMPANY_NAME from the environment is only the default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AppAsset, AppSetting

DEFAULT_ACCENT = "#0f766e"
LOGO_NAME = "logo"
MAX_LOGO_BYTES = 1024 * 1024
THEMES = ("auto", "light", "dark")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


class BrandingError(ValueError):
    """User-facing validation problem (message is an i18n key)."""


# --------------------------------------------------------------------------- #
# Colour helpers
# --------------------------------------------------------------------------- #


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def mix(color: str, other: str, amount: float) -> str:
    """Blend `color` towards `other` (0 = color, 1 = other)."""
    a, b = _rgb(color), _rgb(other)
    return _hex(tuple(a[i] + (b[i] - a[i]) * amount for i in range(3)))


def luminance(color: str) -> float:
    def ch(v: int) -> float:
        x = v / 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4

    r, g, b = _rgb(color)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def on_color(color: str) -> str:
    """Readable text colour on top of `color`."""
    return "#0b1220" if luminance(color) > 0.45 else "#ffffff"


def normalize_hex(value: str | None) -> str | None:
    v = (value or "").strip()
    if not v:
        return None
    if not v.startswith("#"):
        v = "#" + v
    if len(v) == 4 and re.fullmatch(r"#[0-9a-fA-F]{3}", v):
        v = "#" + "".join(c * 2 for c in v[1:])
    if not _HEX.match(v):
        raise BrandingError("bad_color")
    return v.lower()


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Branding:
    company_name: str
    tagline: str
    accent: str
    accent_dark: str
    default_theme: str  # auto | light | dark
    logo_version: str  # "" when there is no logo; otherwise a cache-busting stamp
    custom_accent: bool

    @property
    def has_logo(self) -> bool:
        return bool(self.logo_version)

    @property
    def logo_url(self) -> str:
        return f"/branding/logo?v={self.logo_version}" if self.logo_version else ""

    @property
    def css(self) -> str:
        """CSS variable overrides injected after the base stylesheet."""
        if not self.custom_accent:
            return ""
        light, dark = self.accent, self.accent_dark
        return (
            f":root{{--accent:{light};--accent-hover:{mix(light, '#000000', 0.18)};"
            f"--accent-soft:{mix(light, '#ffffff', 0.86)};--link:{mix(light, '#000000', 0.08)};--on-accent:{on_color(light)}}}"
            f"html[data-theme=dark]{{--accent:{dark};--accent-hover:{mix(dark, '#ffffff', 0.2)};"
            f"--accent-soft:{mix(dark, '#0e1319', 0.8)};--link:{mix(dark, '#ffffff', 0.15)};--on-accent:{on_color(dark)}}}"
            ".btn,.brand .logo,html[data-theme=dark] .btn{color:var(--on-accent)}"
        )


def _settings_map(session: Session) -> dict[str, str]:
    return {k: (v or "") for k, v in session.execute(select(AppSetting.key, AppSetting.value)).all()}


def load(session: Session, settings: Settings) -> Branding:
    values = _settings_map(session)
    accent = values.get("accent") or ""
    accent_dark = values.get("accent_dark") or ""
    base = accent or DEFAULT_ACCENT
    stamp = session.scalar(select(AppAsset.updated_at).where(AppAsset.name == LOGO_NAME))
    theme = values.get("default_theme") or "auto"
    return Branding(
        company_name=values.get("company_name") or settings.company_name,
        tagline=values.get("tagline") or "",
        accent=base,
        accent_dark=accent_dark or mix(base, "#ffffff", 0.45),
        default_theme=theme if theme in THEMES else "auto",
        logo_version=stamp.strftime("%Y%m%d%H%M%S") if stamp else "",
        custom_accent=bool(accent or accent_dark),
    )


def _set(session: Session, key: str, value: str | None) -> None:
    row = session.get(AppSetting, key)
    if value:
        if row is None:
            session.add(AppSetting(key=key, value=value))
        else:
            row.value = value
    elif row is not None:
        session.delete(row)


def save(session: Session, *, company_name: str, tagline: str, accent: str, accent_dark: str, default_theme: str) -> None:
    if default_theme not in THEMES:
        raise BrandingError("bad_theme")
    _set(session, "company_name", (company_name or "").strip()[:120])
    _set(session, "tagline", (tagline or "").strip()[:160])
    _set(session, "accent", normalize_hex(accent))
    _set(session, "accent_dark", normalize_hex(accent_dark))
    _set(session, "default_theme", default_theme if default_theme != "auto" else None)
    session.commit()


# --------------------------------------------------------------------------- #
# Logo
# --------------------------------------------------------------------------- #


def sniff_image(data: bytes) -> str | None:
    """Content type from magic bytes. SVG is deliberately not accepted (it can carry scripts)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _decodes(data: bytes) -> bool:
    """True when the bytes are a complete, readable image (rejects truncated / corrupt uploads)."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            img.load()
            return img.width > 0 and img.height > 0 and img.width * img.height <= 40_000_000
    except Exception:
        return False


def set_logo(session: Session, data: bytes) -> str:
    if not data:
        raise BrandingError("logo_empty")
    if len(data) > MAX_LOGO_BYTES:
        raise BrandingError("logo_too_big")
    content_type = sniff_image(data)
    if content_type is None or not _decodes(data):
        raise BrandingError("logo_bad_type")
    row = session.get(AppAsset, LOGO_NAME)
    if row is None:
        session.add(AppAsset(name=LOGO_NAME, content_type=content_type, data=data, updated_at=datetime.now()))
    else:
        row.content_type, row.data, row.updated_at = content_type, data, datetime.now()
    session.commit()
    return content_type


def remove_logo(session: Session) -> bool:
    row = session.get(AppAsset, LOGO_NAME)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


def get_logo(session: Session) -> tuple[bytes, str] | None:
    row = session.get(AppAsset, LOGO_NAME)
    return (row.data, row.content_type) if row else None
