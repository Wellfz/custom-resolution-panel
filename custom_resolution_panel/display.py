"""Talking to the compositor: list modes, show the layout and switch modes.

Creating custom modes (the EDID part) works on any desktop because it happens in
the kernel. Switching to a mode from inside the app needs to talk to the
compositor, which is what the backends below do:

  GNOME       Mutter D-Bus API (org.gnome.Mutter.DisplayConfig)
  KDE Plasma  kscreen-doctor
  Hyprland    hyprctl
  Sway        swaymsg

All methods take kernel (DRM) connector names such as "DP-3" or "HDMI-A-1".
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from gi.repository import Gio, GLib


@dataclass
class Mode:
    id: str
    width: int
    height: int
    refresh: float
    current: bool = False
    vrr: bool = False


class DisplayError(RuntimeError):
    pass


class Backend:
    name = ""
    # True when the compositor tells us about changes; otherwise the app reloads by itself.
    notifies_changes = False
    on_changed = None

    def modes(self, connector: str) -> list[Mode]:
        raise NotImplementedError

    def layout(self, connectors: list[str]) -> dict[str, tuple[int, int, bool]]:
        """(x, y, is_primary) per connector."""
        raise NotImplementedError

    def apply_mode(self, connector: str, mode: Mode, persistent: bool) -> str | None:
        """Switch modes. Returns a config line to show when the change can't be saved for the user."""
        raise NotImplementedError

    def current_mode(self, connector: str) -> Mode | None:
        return next((m for m in self.modes(connector) if m.current), None)

    def find_mode(self, connector: str, width: int, height: int, refresh: float) -> Mode | None:
        candidates = [m for m in self.modes(connector) if (m.width, m.height) == (width, height) and not m.vrr]
        if not candidates:
            return None
        best = min(candidates, key=lambda m: abs(m.refresh - refresh))
        return best if abs(best.refresh - refresh) < 0.5 else None


def _run(*cmd: str) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DisplayError(f"{cmd[0]} failed: {e}") from e
    if out.returncode != 0:
        raise DisplayError((out.stderr or out.stdout).strip() or f"{cmd[0]} exited with {out.returncode}")
    return out.stdout


def _json(*cmd: str):
    try:
        return json.loads(_run(*cmd))
    except ValueError as e:
        raise DisplayError(f"{cmd[0]} returned invalid JSON") from e


# ------------------------------------------------------------------- GNOME ---

class MutterBackend(Backend):
    name = "GNOME"
    notifies_changes = True
    BUS_NAME = "org.gnome.Mutter.DisplayConfig"
    OBJECT_PATH = "/org/gnome/Mutter/DisplayConfig"
    METHOD_TEMPORARY = 1
    METHOD_PERSISTENT = 2
    LAYOUT_LOGICAL = 1

    def __init__(self) -> None:
        self.proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_AUTO_START, None,
            self.BUS_NAME, self.OBJECT_PATH, self.BUS_NAME, None)
        if self.proxy.get_name_owner() is None:
            raise DisplayError("Mutter is not running")
        self.proxy.connect("g-signal", self._on_signal)

    @staticmethod
    def connector_name(drm_name: str) -> str:
        """Mutter calls the kernel's HDMI-A-1 "HDMI-1"."""
        return re.sub(r"^HDMI-A-", "HDMI-", drm_name)

    def _on_signal(self, _proxy, _sender, signal, _params) -> None:
        if signal == "MonitorsChanged" and self.on_changed:
            self.on_changed()

    def _state(self):
        try:
            return self.proxy.call_sync("GetCurrentState", None, Gio.DBusCallFlags.NONE, -1, None).unpack()
        except GLib.Error as e:
            raise DisplayError(e.message) from e

    def modes(self, connector: str) -> list[Mode]:
        _serial, monitors, _logical, _props = self._state()
        name = self.connector_name(connector)
        for (conn, *_spec), modes, _mprops in monitors:
            if conn == name:
                return [Mode(m[0], m[1], m[2], m[3], bool(m[6].get("is-current")),
                             m[6].get("refresh-rate-mode") == "variable") for m in modes]
        return []

    def layout(self, connectors: list[str]) -> dict[str, tuple[int, int, bool]]:
        _serial, _monitors, logical, _props = self._state()
        by_mutter_name = {spec[0]: (x, y, primary)
                          for x, y, _scale, _transform, primary, specs, _p in logical for spec in specs}
        return {c: by_mutter_name[self.connector_name(c)]
                for c in connectors if self.connector_name(c) in by_mutter_name}

    def apply_mode(self, connector: str, mode: Mode, persistent: bool) -> str | None:
        """Switch one monitor's mode, keep the others, and close up the layout.

        Mutter rejects overlapping monitors and gaps between them, so monitors to
        the right of / below the changed one shift along with its size change.
        """
        serial, monitors, logical, props = self._state()
        name = self.connector_name(connector)
        current = {}
        sizes = {}
        for (conn, *_spec), modes, _mprops in monitors:
            for m in modes:
                sizes[(conn, m[0])] = (m[1], m[2])
                if m[6].get("is-current"):
                    current[conn] = m[0]
        if name not in current:
            raise DisplayError(f"GNOME is not using monitor {name}")
        if (name, mode.id) not in sizes:
            raise DisplayError(f"GNOME does not know mode {mode.id} on {name}")

        logical_scaling = props.get("layout-mode", self.LAYOUT_LOGICAL) == self.LAYOUT_LOGICAL
        target = next(lm for lm in logical if any(spec[0] == name for spec in lm[5]))
        x0, y0, scale = target[0], target[1], target[2]
        divisor = scale if logical_scaling else 1
        old_w, old_h = (v / divisor for v in sizes[(name, current[name])])
        new_w, new_h = (v / divisor for v in sizes[(name, mode.id)])
        dx, dy = round(new_w - old_w), round(new_h - old_h)

        new_logical = []
        for x, y, lm_scale, transform, primary, specs, _lprops in logical:
            if (x, y) != (x0, y0):
                if x >= x0 + old_w:
                    x += dx
                if y >= y0 + old_h:
                    y += dy
            monitors_cfg = [(conn, mode.id if conn == name else current[conn], {}) for conn, *_rest in specs]
            new_logical.append((x, y, lm_scale, transform, primary, monitors_cfg))

        method = self.METHOD_PERSISTENT if persistent else self.METHOD_TEMPORARY
        params = GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})", (serial, method, new_logical, {}))
        try:
            self.proxy.call_sync("ApplyMonitorsConfig", params, Gio.DBusCallFlags.NONE, -1, None)
        except GLib.Error as e:
            raise DisplayError(e.message) from e
        return None


# -------------------------------------------------------------- KDE Plasma ---

class KScreenBackend(Backend):
    """kscreen-doctor; KWin uses the kernel connector names and saves changes itself."""

    name = "KDE Plasma"

    def _outputs(self) -> list[dict]:
        return [o for o in _json("kscreen-doctor", "-j").get("outputs", [])
                if o.get("connected") and o.get("enabled", True)]

    def modes(self, connector: str) -> list[Mode]:
        for o in self._outputs():
            if o.get("name") == connector:
                current = str(o.get("currentModeId"))
                return [Mode(str(m["id"]), m["size"]["width"], m["size"]["height"], float(m["refreshRate"]),
                             str(m["id"]) == current) for m in o.get("modes", [])]
        return []

    def layout(self, connectors: list[str]) -> dict[str, tuple[int, int, bool]]:
        return {o["name"]: (o["pos"]["x"], o["pos"]["y"], o.get("priority") == 1)
                for o in self._outputs() if o.get("name") in connectors}

    def apply_mode(self, connector: str, mode: Mode, persistent: bool) -> str | None:
        _run("kscreen-doctor", f"output.{connector}.mode.{mode.id}")
        return None


# ---------------------------------------------------------------- Hyprland ---

class HyprlandBackend(Backend):
    name = "Hyprland"
    MODE_RE = re.compile(r"^(\d+)x(\d+)@([\d.]+)Hz$")

    def _monitors(self) -> list[dict]:
        return [m for m in _json("hyprctl", "-j", "monitors", "all") if not m.get("disabled")]

    def _monitor(self, connector: str) -> dict:
        for m in self._monitors():
            if m.get("name") == connector:
                return m
        raise DisplayError(f"Hyprland is not using monitor {connector}")

    def modes(self, connector: str) -> list[Mode]:
        try:
            mon = self._monitor(connector)
        except DisplayError:
            return []
        modes = []
        for text in mon.get("availableModes", []):
            m = self.MODE_RE.match(text)
            if not m:
                continue
            w, h, r = int(m[1]), int(m[2]), float(m[3])
            current = (w, h) == (mon["width"], mon["height"]) and abs(r - mon["refreshRate"]) < 0.01
            modes.append(Mode(text, w, h, r, current))
        return modes

    def layout(self, connectors: list[str]) -> dict[str, tuple[int, int, bool]]:
        return {m["name"]: (m["x"], m["y"], False) for m in self._monitors() if m["name"] in connectors}

    def _rule(self, connector: str, mode: Mode) -> str:
        mon = self._monitor(connector)
        return f"{connector}, {mode.width}x{mode.height}@{mode.refresh:.2f}, {mon['x']}x{mon['y']}, {mon['scale']:g}"

    def apply_mode(self, connector: str, mode: Mode, persistent: bool) -> str | None:
        rule = self._rule(connector, mode)
        _run("hyprctl", "keyword", "monitor", rule.replace(" ", ""))
        # hyprctl changes are runtime only; the user's config decides what happens next login.
        return f"monitor = {rule}" if persistent else None


# -------------------------------------------------------------------- Sway ---

class SwayBackend(Backend):
    name = "Sway"

    def _outputs(self) -> list[dict]:
        return [o for o in _json("swaymsg", "-r", "-t", "get_outputs") if o.get("active")]

    def modes(self, connector: str) -> list[Mode]:
        for o in self._outputs():
            if o.get("name") != connector:
                continue
            cur = o.get("current_mode") or {}
            modes = []
            for m in o.get("modes", []):
                w, h, r = m["width"], m["height"], m["refresh"] / 1000
                current = (w, h, m["refresh"]) == (cur.get("width"), cur.get("height"), cur.get("refresh"))
                modes.append(Mode(f"{w}x{h}@{r:.3f}Hz", w, h, r, current))
            return modes
        return []

    def layout(self, connectors: list[str]) -> dict[str, tuple[int, int, bool]]:
        return {o["name"]: (o["rect"]["x"], o["rect"]["y"], False)
                for o in self._outputs() if o["name"] in connectors}

    def apply_mode(self, connector: str, mode: Mode, persistent: bool) -> str | None:
        _run("swaymsg", "output", connector, "mode", mode.id)
        # swaymsg changes are runtime only; the user's config decides what happens next login.
        return f"output {connector} mode {mode.id}" if persistent else None


def detect() -> Backend | None:
    """The backend for the running desktop, or None when mode switching isn't supported."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):
        return HyprlandBackend()
    if os.environ.get("SWAYSOCK") and shutil.which("swaymsg"):
        return SwayBackend()
    if "KDE" in desktop and shutil.which("kscreen-doctor"):
        return KScreenBackend()
    try:
        return MutterBackend()
    except (GLib.Error, DisplayError):
        return None
