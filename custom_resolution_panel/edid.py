"""EDID parsing and generation of EDIDs with custom modes.

Custom modes go into an extra CTA-861 extension block appended to the end of
the original EDID. Everything else (identity, FreeSync, range limits) stays
untouched, so GNOME keeps recognising the monitor, and removing the modes is
just a matter of dropping that block.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

BLOCK = 128
EDID_HEADER = bytes([0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00])
CTA_TAG = 0x02
# A CTA block with no data blocks fits 6 DTDs of 18 bytes (4 header bytes + 1 checksum).
MAX_CUSTOM_MODES = 6
# The DTD pixel clock is a 16-bit field in 10 kHz units.
MAX_DTD_CLOCK_KHZ = 0xFFFF * 10


class EdidError(ValueError):
    pass


@dataclass(frozen=True)
class Timing:
    """Timing of a progressive mode; clock in kHz, everything else in pixels/lines."""

    clock_khz: int
    h_active: int
    h_front: int
    h_sync: int
    h_back: int
    v_active: int
    v_front: int
    v_sync: int
    v_back: int
    h_sync_positive: bool = True
    v_sync_positive: bool = False
    width_mm: int = 0
    height_mm: int = 0

    @property
    def h_total(self) -> int:
        return self.h_active + self.h_front + self.h_sync + self.h_back

    @property
    def v_total(self) -> int:
        return self.v_active + self.v_front + self.v_sync + self.v_back

    @property
    def h_freq_khz(self) -> float:
        return self.clock_khz / self.h_total

    @property
    def refresh_hz(self) -> float:
        return self.clock_khz * 1000 / (self.h_total * self.v_total)

    def label(self) -> str:
        return f"{self.h_active}x{self.v_active} @ {self.refresh_hz:.3f} Hz"

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, data: dict) -> "Timing":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass
class RangeLimits:
    v_min: int
    v_max: int
    h_min_khz: int
    h_max_khz: int
    max_clock_khz: int | None


@dataclass
class EdidInfo:
    raw: bytes
    vendor: str
    product_code: int
    serial: int
    name: str = ""
    serial_text: str = ""
    version: str = ""
    timings: list[Timing] = field(default_factory=list)
    limits: RangeLimits | None = None

    @property
    def identity(self) -> str:
        """Stable monitor key, the same one GNOME uses (vendor/product/serial)."""
        serial = self.serial_text or str(self.serial)
        return f"{self.vendor}-{self.product_code:04x}-{serial}"

    @property
    def native(self) -> Timing | None:
        return self.timings[0] if self.timings else None


def _checksum(block: bytes | bytearray) -> int:
    return (-sum(block[:127])) & 0xFF


def _blocks(raw: bytes) -> list[bytes]:
    return [raw[i:i + BLOCK] for i in range(0, len(raw), BLOCK)]


def validate(raw: bytes) -> None:
    if len(raw) < BLOCK or len(raw) % BLOCK:
        raise EdidError(f"invalid EDID size: {len(raw)} bytes")
    if raw[:8] != EDID_HEADER:
        raise EdidError("invalid EDID header")
    for i, block in enumerate(_blocks(raw)):
        if sum(block) & 0xFF:
            raise EdidError(f"invalid checksum in block {i}")
    if raw[126] != len(raw) // BLOCK - 1:
        raise EdidError("extension count does not match the EDID size")


def decode_dtd(d: bytes) -> Timing | None:
    clock = (d[0] | d[1] << 8) * 10
    if clock == 0:
        return None
    h_active = d[2] | (d[4] & 0xF0) << 4
    h_blank = d[3] | (d[4] & 0x0F) << 8
    v_active = d[5] | (d[7] & 0xF0) << 4
    v_blank = d[6] | (d[7] & 0x0F) << 8
    h_front = d[8] | (d[11] & 0xC0) << 2
    h_sync = d[9] | (d[11] & 0x30) << 4
    v_front = (d[10] >> 4) | (d[11] & 0x0C) << 2
    v_sync = (d[10] & 0x0F) | (d[11] & 0x03) << 4
    width_mm = d[12] | (d[14] & 0xF0) << 4
    height_mm = d[13] | (d[14] & 0x0F) << 8
    flags = d[17]
    if flags & 0x80:
        return None  # interlaced: not supported
    digital_separate = (flags & 0x18) == 0x18
    return Timing(
        clock_khz=clock,
        h_active=h_active,
        h_front=h_front,
        h_sync=h_sync,
        h_back=h_blank - h_front - h_sync,
        v_active=v_active,
        v_front=v_front,
        v_sync=v_sync,
        v_back=v_blank - v_front - v_sync,
        h_sync_positive=bool(flags & 0x02) if digital_separate else True,
        v_sync_positive=bool(flags & 0x04) if digital_separate else False,
        width_mm=width_mm,
        height_mm=height_mm,
    )


def encode_dtd(t: Timing) -> bytes:
    h_blank = t.h_front + t.h_sync + t.h_back
    v_blank = t.v_front + t.v_sync + t.v_back
    clock = round(t.clock_khz / 10)
    limits = [
        (clock, 0xFFFF, "pixel clock"),
        (t.h_active, 0xFFF, "width"),
        (h_blank, 0xFFF, "horizontal blanking"),
        (t.v_active, 0xFFF, "height"),
        (v_blank, 0xFFF, "vertical blanking"),
        (t.h_front, 0x3FF, "horizontal front porch"),
        (t.h_sync, 0x3FF, "horizontal sync"),
        (t.v_front, 0x3F, "vertical front porch"),
        (t.v_sync, 0x3F, "vertical sync"),
        (t.width_mm, 0xFFF, "width in mm"),
        (t.height_mm, 0xFFF, "height in mm"),
    ]
    for value, top, what in limits:
        if not 0 <= value <= top:
            raise EdidError(f"{what} does not fit in an EDID ({value})")
    if min(t.h_back, t.v_back) < 0 or min(t.h_sync, t.v_sync) < 1 or clock < 1:
        raise EdidError("invalid timing: negative back porch or zero sync")
    flags = 0x18  # digital separate sync
    flags |= 0x04 if t.v_sync_positive else 0
    flags |= 0x02 if t.h_sync_positive else 0
    return bytes([
        clock & 0xFF, clock >> 8,
        t.h_active & 0xFF, h_blank & 0xFF, (t.h_active >> 8) << 4 | h_blank >> 8,
        t.v_active & 0xFF, v_blank & 0xFF, (t.v_active >> 8) << 4 | v_blank >> 8,
        t.h_front & 0xFF, t.h_sync & 0xFF,
        (t.v_front & 0x0F) << 4 | (t.v_sync & 0x0F),
        (t.h_front >> 8) << 6 | (t.h_sync >> 8) << 4 | (t.v_front >> 4) << 2 | t.v_sync >> 4,
        t.width_mm & 0xFF, t.height_mm & 0xFF, (t.width_mm >> 8) << 4 | t.height_mm >> 8,
        0, 0, flags,
    ])


def _cta_dtd_offsets(block: bytes) -> list[int]:
    start = block[2]
    if start < 4:
        return []
    return list(range(start, 127 - 17, 18))


def _parse_descriptor_text(d: bytes) -> str:
    return d[5:18].split(b"\x0a")[0].decode("ascii", "replace").strip()


def parse(raw: bytes) -> EdidInfo:
    validate(raw)
    m = raw[8] << 8 | raw[9]
    vendor = "".join(chr(((m >> s) & 0x1F) + 64) for s in (10, 5, 0))
    info = EdidInfo(
        raw=raw,
        vendor=vendor,
        product_code=raw[10] | raw[11] << 8,
        serial=int.from_bytes(raw[12:16], "little"),
        version=f"{raw[18]}.{raw[19]}",
    )
    for off in range(54, 126, 18):
        d = raw[off:off + 18]
        if d[0] or d[1]:
            t = decode_dtd(d)
            if t:
                info.timings.append(t)
            continue
        tag = d[3]
        if tag == 0xFC:
            info.name = _parse_descriptor_text(d)
        elif tag == 0xFF:
            info.serial_text = _parse_descriptor_text(d)
        elif tag == 0xFD:
            flags = d[4]
            v_min = d[5] + (255 if flags & 0x03 == 0x03 else 0)
            v_max = d[6] + (255 if flags & 0x02 else 0)
            h_min = d[7] + (255 if flags & 0x0C == 0x0C else 0)
            h_max = d[8] + (255 if flags & 0x08 else 0)
            info.limits = RangeLimits(v_min, v_max, h_min, h_max, d[9] * 10_000 if d[9] else None)
    for block in _blocks(raw)[1:]:
        if block[0] != CTA_TAG:
            continue
        for off in _cta_dtd_offsets(block):
            t = decode_dtd(block[off:off + 18])
            if t is None:
                break
            info.timings.append(t)
    return info


def _has_eeodb(block: bytes) -> bool:
    """HF-EEODB overrides the extension count; better to refuse than to edit it wrong."""
    i, end = 4, block[2]
    while i < end:
        tag, length = block[i] >> 5, block[i] & 0x1F
        if tag == 7 and length >= 2 and block[i + 1] == 0x78:
            return True
        i += 1 + length
    return False


def build(original: bytes, modes: list[Timing]) -> bytes:
    """Original EDID + one CTA block holding the custom modes."""
    validate(original)
    if not modes:
        return original
    if len(modes) > MAX_CUSTOM_MODES:
        raise EdidError(f"at most {MAX_CUSTOM_MODES} custom modes per monitor")
    blocks = _blocks(original)
    if len(blocks) >= 255:
        raise EdidError("the EDID already has the maximum number of extensions")
    first_cta = next((b for b in blocks[1:] if b[0] == CTA_TAG), None)
    if first_cta is not None and _has_eeodb(first_cta):
        raise EdidError("this EDID uses HF-EEODB, which is not supported yet")

    ext = bytearray(BLOCK)
    ext[0] = CTA_TAG
    ext[1] = 3
    ext[2] = 4  # no data blocks: DTDs start right after the header
    # CTA-861 requires byte 3 (underscan/audio/YCbCr/native count) to match in every block.
    ext[3] = first_cta[3] if first_cta is not None else 0
    off = 4
    for mode in modes:
        ext[off:off + 18] = encode_dtd(mode)
        off += 18
    ext[127] = _checksum(ext)

    base = bytearray(blocks[0])
    base[126] = len(blocks)
    base[127] = _checksum(base)
    result = bytes(base) + b"".join(blocks[1:]) + bytes(ext)
    validate(result)
    return result


# ---------------------------------------------------------------- timings ---

def _cvt_vsync_width(h: int, v: int) -> int:
    for (aw, ah), width in (((4, 3), 4), ((16, 9), 5), ((16, 10), 6), ((5, 4), 7), ((15, 9), 7)):
        if h * ah == v * aw:
            return width
    return 10


def cvt_rb(h: int, v: int, refresh: float) -> Timing:
    """VESA CVT 1.2, reduced blanking v1 (fixed 160-pixel horizontal blanking)."""
    h = h // 8 * 8
    v_sync = _cvt_vsync_width(h, v)
    h_period = (1_000_000 / refresh - 460) / v
    vbi = max(math.floor(460 / h_period) + 1, 3 + v_sync + 6)
    v_total = v + vbi
    h_total = h + 160
    clock_mhz = 0.25 * math.floor(refresh * v_total * h_total / 1_000_000 / 0.25)
    return Timing(
        clock_khz=round(clock_mhz * 1000),
        h_active=h, h_front=48, h_sync=32, h_back=80,
        v_active=v, v_front=3, v_sync=v_sync, v_back=vbi - 3 - v_sync,
        h_sync_positive=True, v_sync_positive=False,
    )


def cvt_rb2(h: int, v: int, refresh: float) -> Timing:
    """VESA CVT 1.2, reduced blanking v2 (80-pixel horizontal blanking)."""
    h_period = (1_000_000 / refresh - 460) / v
    vbi = max(math.floor(460 / h_period) + 1, 1 + 8 + 6)
    v_total = v + vbi
    h_total = h + 80
    clock_khz = math.floor(refresh * v_total * h_total / 1000)
    return Timing(
        clock_khz=clock_khz,
        h_active=h, h_front=8, h_sync=32, h_back=40,
        v_active=v, v_front=vbi - 8 - 6, v_sync=8, v_back=6,
        h_sync_positive=True, v_sync_positive=False,
    )


def from_native(native: Timing, h: int, v: int, refresh: float) -> Timing:
    """Reuse the blanking of the monitor's native mode and recompute the pixel clock.

    This is what CRU's "LCD native" does: monitor scalers tend to accept timings
    that look like the ones they advertise themselves.
    """
    t = replace(native, h_active=h, v_active=v)
    clock_khz = round(refresh * t.h_total * t.v_total / 1000 / 10) * 10
    return replace(t, clock_khz=clock_khz)


STANDARDS = {
    "native": "Monitor native",
    "cvt-rb2": "CVT-RB2",
    "cvt-rb": "CVT-RB",
}


def calculate(standard: str, h: int, v: int, refresh: float, native: Timing | None) -> Timing:
    if standard == "native":
        if native is None:
            raise EdidError("the monitor does not report a native mode")
        t = from_native(native, h, v, refresh)
    elif standard == "cvt-rb2":
        t = cvt_rb2(h, v, refresh)
    elif standard == "cvt-rb":
        t = cvt_rb(h, v, refresh)
    else:
        raise EdidError(f"unknown timing standard: {standard}")
    if native is not None:
        t = replace(t, width_mm=native.width_mm, height_mm=native.height_mm)
    return t


def best_native(info: EdidInfo, refresh: float) -> Timing | None:
    """The DTD at the native resolution whose refresh rate is closest to the requested one."""
    if not info.native:
        return None
    same_res = [t for t in info.timings
                if (t.h_active, t.v_active) == (info.native.h_active, info.native.v_active)]
    return min(same_res, key=lambda t: abs(t.refresh_hz - refresh))


def check_limits(t: Timing, info: EdidInfo) -> list[str]:
    """Warnings (not errors) when the mode goes beyond what the monitor declares."""
    warnings = []
    if t.clock_khz > MAX_DTD_CLOCK_KHZ:
        warnings.append(f"a {t.clock_khz / 1000:.2f} MHz pixel clock does not fit in a DTD (max 655.35 MHz)")
    lim = info.limits
    if lim:
        if not lim.v_min <= t.refresh_hz <= lim.v_max + 0.5:
            warnings.append(f"refresh rate outside the monitor's range ({lim.v_min}–{lim.v_max} Hz)")
        if lim.max_clock_khz and t.clock_khz > lim.max_clock_khz:
            warnings.append(f"pixel clock above the monitor's maximum ({lim.max_clock_khz // 1000} MHz)")
    native = info.native
    if native and (t.h_active > native.h_active or t.v_active > native.v_active):
        warnings.append("larger than the native resolution: the monitor will probably reject it")
    if t.h_active % 16:
        warnings.append("width is not a multiple of 16: some monitor scalers show a broken image")
    return warnings
