"""Connected monitors, saved configuration and calls to the root helper."""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field

from gi.repository import Gio, GLib

from . import edid

HELPER = "/usr/local/libexec/custom-resolution-panel-helper"
FIRMWARE_DIR = "/usr/lib/firmware/edid"
FIRMWARE_PREFIX = "custom-resolution-panel-"
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "custom-resolution-panel")
CONFIG_FILE = os.path.join(CONFIG_DIR, "monitors.json")


@dataclass
class CustomMode:
    timing: edid.Timing
    standard: str

    def to_dict(self) -> dict:
        return {"standard": self.standard, "timing": self.timing.to_dict()}

    @classmethod
    def from_dict(cls, data: dict) -> "CustomMode":
        return cls(edid.Timing.from_dict(data["timing"]), data["standard"])


@dataclass
class Monitor:
    connector: str
    original: edid.EdidInfo
    modes: list[CustomMode] = field(default_factory=list)
    live_raw: bytes = b""

    @property
    def title(self) -> str:
        return f"{self.original.name or self.original.vendor} ({self.connector})"

    def generated(self) -> bytes:
        return edid.build(self.original.raw, [m.timing for m in self.modes])

    @property
    def live_state(self) -> str:
        """"original", "applied" (current EDID == the generated one) or "other"."""
        if self.live_raw == self.original.raw:
            return "original"
        if self.modes and self.live_raw == self.generated():
            return "applied"
        return "other"

    @property
    def persisted_raw(self) -> bytes | None:
        path = f"{FIRMWARE_DIR}/{FIRMWARE_PREFIX}{self.connector}.bin"
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None

    @property
    def persist_state(self) -> str:
        """"none", "current" (boot file == the generated one) or "outdated"."""
        raw = self.persisted_raw
        if raw is None:
            return "none"
        return "current" if self.modes and raw == self.generated() else "outdated"


def _load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(monitors: list[Monitor]) -> None:
    data = _load_config()
    for mon in monitors:
        data[mon.original.identity] = {
            "connector": mon.connector,
            "original": mon.original.raw.hex(),
            "modes": [m.to_dict() for m in mon.modes],
        }
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


def _strip_custom_block(raw: bytes, persisted: bytes | None) -> bytes:
    """If the current EDID is one we generated, recover the original by dropping the last block."""
    if persisted is None or raw != persisted or len(raw) < 3 * edid.BLOCK:
        return raw
    base = bytearray(raw[:edid.BLOCK])
    base[126] -= 1
    base[127] = (-sum(base[:127])) & 0xFF
    return bytes(base) + raw[edid.BLOCK:-edid.BLOCK]


def detect() -> list[Monitor]:
    config = _load_config()
    monitors = []
    for path in sorted(glob.glob("/sys/class/drm/card[0-9]*-*")):
        try:
            with open(f"{path}/status") as f:
                if f.read().strip() != "connected":
                    continue
            with open(f"{path}/edid", "rb") as f:
                live = f.read()
            live_info = edid.parse(live)
        except (OSError, edid.EdidError):
            continue
        connector = os.path.basename(path).split("-", 1)[1]
        saved = config.get(live_info.identity)
        if saved:
            original = edid.parse(bytes.fromhex(saved["original"]))
            modes = [CustomMode.from_dict(m) for m in saved["modes"]]
        else:
            persisted = Monitor(connector, live_info).persisted_raw
            original = edid.parse(_strip_custom_block(live, persisted))
            modes = []
        monitors.append(Monitor(connector, original, modes, live))
    return monitors


def run_helper(args: list[str], callback) -> None:
    """Run the helper through pkexec without blocking the UI; callback(ok, message)."""
    if not os.access(HELPER, os.X_OK):
        callback(False, f"The helper is not installed at {HELPER}. Run install.sh with sudo.")
        return
    proc = Gio.Subprocess.new(["pkexec", HELPER, *args],
                              Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)

    def done(p, result):
        try:
            _ok, out, _err = p.communicate_utf8_finish(result)
        except GLib.Error as e:
            callback(False, e.message)
            return
        status = p.get_exit_status()
        if status == 126 or status == 127:
            callback(False, "Authorization cancelled.")
        else:
            callback(status == 0, (out or "").strip())

    proc.communicate_utf8_async(None, None, done)


def apply_live(mon: Monitor, callback) -> None:
    run_helper(["apply-live", mon.connector, mon.generated().hex()], callback)


def reset_live(mon: Monitor, callback) -> None:
    run_helper(["reset-live", mon.connector], callback)


def persist(monitors: list[Monitor], callback) -> None:
    pairs = [f"{m.connector}={m.generated().hex()}" for m in monitors if m.modes]
    run_helper(["persist", *pairs], callback)
