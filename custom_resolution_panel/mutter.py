"""Video mode switching through Mutter's D-Bus API (org.gnome.Mutter.DisplayConfig)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from gi.repository import Gio, GLib

BUS_NAME = "org.gnome.Mutter.DisplayConfig"
OBJECT_PATH = "/org/gnome/Mutter/DisplayConfig"
METHOD_TEMPORARY = 1
METHOD_PERSISTENT = 2
LAYOUT_LOGICAL = 1


@dataclass
class Mode:
    id: str
    width: int
    height: int
    refresh: float
    current: bool
    vrr: bool


def mutter_connector(drm_name: str) -> str:
    """Mutter calls the kernel's HDMI-A-1 "HDMI-1"."""
    return re.sub(r"^HDMI-A-", "HDMI-", drm_name)


class DisplayConfig:
    def __init__(self) -> None:
        self.proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_AUTO_START, None,
            BUS_NAME, OBJECT_PATH, BUS_NAME, None)
        self.on_changed = None
        self.proxy.connect("g-signal", self._on_signal)

    def _on_signal(self, _proxy, _sender, signal, _params) -> None:
        if signal == "MonitorsChanged" and self.on_changed:
            self.on_changed()

    def state(self):
        return self.proxy.call_sync("GetCurrentState", None, Gio.DBusCallFlags.NONE, -1, None).unpack()

    def modes(self, drm_name: str) -> list[Mode]:
        _serial, monitors, _logical, _props = self.state()
        connector = mutter_connector(drm_name)
        for (conn, *_spec), modes, _mprops in monitors:
            if conn == connector:
                return [Mode(m[0], m[1], m[2], m[3], bool(m[6].get("is-current")),
                             m[6].get("refresh-rate-mode") == "variable") for m in modes]
        return []

    def find_mode(self, drm_name: str, width: int, height: int, refresh: float) -> Mode | None:
        candidates = [m for m in self.modes(drm_name)
                      if (m.width, m.height) == (width, height) and not m.vrr]
        if not candidates:
            return None
        best = min(candidates, key=lambda m: abs(m.refresh - refresh))
        return best if abs(best.refresh - refresh) < 0.5 else None

    def layout(self) -> dict[str, tuple[int, int, bool]]:
        """Position (x, y) and whether it is primary, keyed by Mutter connector name."""
        _serial, _monitors, logical, _props = self.state()
        return {spec[0]: (x, y, primary)
                for x, y, _scale, _transform, primary, specs, _p in logical for spec in specs}

    def current_mode(self, drm_name: str) -> Mode | None:
        return next((m for m in self.modes(drm_name) if m.current), None)

    def apply_mode(self, drm_name: str, mode_id: str, persistent: bool) -> None:
        """Switch one monitor's mode, keep the others, and close up the layout.

        Mutter rejects overlapping monitors and gaps between them, so monitors to
        the right of / below the changed one shift along with its size change.
        """
        serial, monitors, logical, props = self.state()
        connector = mutter_connector(drm_name)
        current = {}
        sizes = {}
        for (conn, *_spec), modes, mprops in monitors:
            for m in modes:
                sizes[(conn, m[0])] = (m[1], m[2])
                if m[6].get("is-current"):
                    current[conn] = m[0]
        if connector not in current:
            raise RuntimeError(f"GNOME is not using monitor {connector}")
        if (connector, mode_id) not in sizes:
            raise RuntimeError(f"GNOME does not know mode {mode_id} on {connector}")

        logical_scaling = props.get("layout-mode", LAYOUT_LOGICAL) == LAYOUT_LOGICAL
        target = next(lm for lm in logical if any(spec[0] == connector for spec in lm[5]))
        x0, y0, scale = target[0], target[1], target[2]
        divisor = scale if logical_scaling else 1
        old_w, old_h = (v / divisor for v in sizes[(connector, current[connector])])
        new_w, new_h = (v / divisor for v in sizes[(connector, mode_id)])
        dx, dy = round(new_w - old_w), round(new_h - old_h)

        new_logical = []
        for x, y, lm_scale, transform, primary, specs, _lprops in logical:
            if (x, y) != (x0, y0):
                if x >= x0 + old_w:
                    x += dx
                if y >= y0 + old_h:
                    y += dy
            monitors_cfg = []
            for conn, *_rest in specs:
                mode = mode_id if conn == connector else current[conn]
                monitors_cfg.append((conn, mode, {}))
            new_logical.append((x, y, lm_scale, transform, primary, monitors_cfg))

        method = METHOD_PERSISTENT if persistent else METHOD_TEMPORARY
        params = GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})", (serial, method, new_logical, {}))
        self.proxy.call_sync("ApplyMonitorsConfig", params, Gio.DBusCallFlags.NONE, -1, None)
