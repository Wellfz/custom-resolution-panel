"""GTK4/libadwaita user interface of Custom Resolution Panel."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import display, edid, system  # noqa: E402

APP_ID = "io.github.wellfz.CustomResolutionPanel"
VERSION = "0.3"
CONFIRM_SECONDS = 15
STYLE = Path(__file__).with_name("style.css")
ICON = Path(__file__).parent.parent / "data" / f"{APP_ID}.svg"
TILE_SCALE = 0.11  # screen pixels per monitor pixel in the layout map


def mhz(khz: int) -> str:
    return f"{khz / 1000:.2f} MHz"


def hz(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


def label(text: str, *css: str, **props) -> Gtk.Label:
    return Gtk.Label(label=text, css_classes=list(css), **props)


def timing_summary(t: edid.Timing) -> str:
    return f"{mhz(t.clock_khz)} · {t.h_freq_khz:.2f} kHz · total {t.h_total}×{t.v_total}"


class ModeDialog(Adw.Dialog):
    """Create or edit a resolution: the user picks size and refresh rate, the timing is calculated."""

    def __init__(self, mon: system.Monitor, on_save, existing: system.CustomMode | None = None) -> None:
        super().__init__(title="Edit resolution" if existing else "New resolution", content_width=460)
        self.mon = mon
        self.on_save = on_save
        self.timing: edid.Timing | None = None
        native = mon.original.native

        self.add_btn = Gtk.Button(label="Save" if existing else "Add",
                                  css_classes=["cta"])
        self.add_btn.connect("clicked", self._add)
        header = Adw.HeaderBar()
        header.pack_end(self.add_btn)

        page = Adw.PreferencesPage()
        size = Adw.PreferencesGroup(title="Resolution")
        self.width = self._spin("Width", 320, 7680, 8, native.h_active if native else 1920)
        self.height = self._spin("Height", 200, 4320, 1, native.v_active if native else 1080)
        self.refresh = self._spin("Refresh rate (Hz)", 24, 500, 1,
                                  round(max((t.refresh_hz for t in mon.original.timings), default=60)),
                                  digits=3)
        for row in (self.width, self.height, self.refresh):
            size.add(row)

        self.standard_keys = list(edid.STANDARDS)
        self.standard = Adw.ComboRow(
            title="Timing", subtitle="How blanking and pixel clock are calculated",
            model=Gtk.StringList.new(list(edid.STANDARDS.values())))
        self.standard.connect("notify::selected", lambda *_: self._recalc())
        size.add(self.standard)
        page.add(size)

        result = Adw.PreferencesGroup(title="Result")
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["result-card"])
        numbers = Gtk.Box(spacing=12)
        self.result_res = label("", "result-number", xalign=0)
        self.result_hz = label("", "result-hz", xalign=0)
        numbers.append(self.result_res)
        numbers.append(self.result_hz)
        self.result_tech = label("", "card-tech", xalign=0, selectable=True, wrap=True)
        self.warn_label = label("", "warning-text", xalign=0, wrap=True, visible=False)
        for w in (numbers, self.result_tech, self.warn_label):
            card.append(w)
        result.add(card)
        page.add(result)

        advanced = Adw.PreferencesGroup(
            title="Advanced", description="Fine-tune the calculated timing, like in CRU")
        self.manual = Adw.SwitchRow(title="Edit timing manually")
        self.manual.connect("notify::active", self._toggle_manual)
        advanced.add(self.manual)
        self.fields = {
            "clock_khz": self._spin("Pixel clock (kHz)", 1000, 655350, 10, 0),
            "h_front": self._spin("Horizontal front porch", 1, 1023, 1, 0),
            "h_sync": self._spin("Horizontal sync", 1, 1023, 1, 0),
            "h_back": self._spin("Horizontal back porch", 1, 4095, 1, 0),
            "v_front": self._spin("Vertical front porch", 1, 63, 1, 0),
            "v_sync": self._spin("Vertical sync", 1, 63, 1, 0),
            "v_back": self._spin("Vertical back porch", 1, 4095, 1, 0),
        }
        for row in self.fields.values():
            row.set_sensitive(False)
            advanced.add(row)
        page.add(advanced)

        for row in (self.width, self.height, self.refresh, *self.fields.values()):
            row.connect("notify::value", lambda *_: self._recalc())

        view = Adw.ToolbarView(content=page)
        view.add_top_bar(header)
        self.set_child(view)
        if existing:
            self._load(existing)
        else:
            self._recalc()

    def _load(self, existing: system.CustomMode) -> None:
        """Fill in the saved timing without recalculating, so hand-tuned values stay as they are."""
        t = existing.timing
        self._busy = True
        try:
            self.width.set_value(t.h_active)
            self.height.set_value(t.v_active)
            self.refresh.set_value(round(t.refresh_hz, 3))
            if existing.standard in self.standard_keys:
                self.standard.set_selected(self.standard_keys.index(existing.standard))
            for key, row in self.fields.items():
                row.set_value(getattr(t, key))
            self.manual.set_active(existing.standard == "manual")
        finally:
            self._busy = False
        self._show(t)

    @staticmethod
    def _spin(title, low, high, step, value, digits=0) -> Adw.SpinRow:
        row = Adw.SpinRow.new_with_range(low, high, step)
        row.set_title(title)
        row.set_digits(digits)
        row.set_value(value)
        return row

    def _toggle_manual(self, *_):
        manual = self.manual.get_active()
        for row in self.fields.values():
            row.set_sensitive(manual)
        self.standard.set_sensitive(not manual)
        self.refresh.set_sensitive(not manual)
        self._recalc()

    def _recalc(self) -> None:
        if getattr(self, "_busy", False):
            return
        self._busy = True
        try:
            self._do_recalc()
        finally:
            self._busy = False

    def _do_recalc(self) -> None:
        h, v = int(self.width.get_value()), int(self.height.get_value())
        refresh = self.refresh.get_value()
        info = self.mon.original
        try:
            if self.manual.get_active() and self.timing:
                values = {k: int(r.get_value()) for k, r in self.fields.items()}
                t = replace(self.timing, h_active=h, v_active=v, **values)
                self.refresh.set_value(round(t.refresh_hz, 3))
            else:
                std = self.standard_keys[self.standard.get_selected()]
                t = edid.calculate(std, h, v, refresh, edid.best_native(info, refresh))
                for key, row in self.fields.items():
                    row.set_value(getattr(t, key))
            edid.encode_dtd(t)
        except edid.EdidError as e:
            self.timing = None
            self.result_tech.set_label(str(e))
            self.add_btn.set_sensitive(False)
            return
        self._show(t)

    def _show(self, t: edid.Timing) -> None:
        self.timing = t
        self.result_res.set_label(f"{t.h_active} × {t.v_active}")
        self.result_hz.set_label(f"{hz(t.refresh_hz, 3)} Hz")
        self.result_tech.set_label(timing_summary(t))
        warnings = edid.check_limits(t, self.mon.original)
        self.warn_label.set_visible(bool(warnings))
        self.warn_label.set_label("\n".join(f"⚠ {w}" for w in warnings))
        self.add_btn.set_sensitive(True)

    def _add(self, _btn) -> None:
        if self.timing:
            std = "manual" if self.manual.get_active() else self.standard_keys[self.standard.get_selected()]
            self.on_save(system.CustomMode(self.timing, std))
        self.close()


def status_of(mon: system.Monitor) -> tuple[str, str]:
    """(pill CSS class, text) describing the monitor's state."""
    live, persisted = mon.live_state, mon.persist_state
    if not mon.modes:
        if live != "original" or persisted != "none":
            return "pending", "Custom EDID without modes; restore it"
        return "idle", "No custom resolutions"
    if live == "applied":
        return ("ok", "Active and saved to boot") if persisted == "current" else ("live", "Active until reboot")
    if persisted == "current":
        return "pending", "Saved; active from next boot"
    return "pending", "Changes not applied"


def same_mode(a, t: edid.Timing) -> bool:
    return a is not None and (a.width, a.height) == (t.h_active, t.v_active) and abs(a.refresh - t.refresh_hz) < 0.5


class MonitorTile(Gtk.Button):
    """Monitor thumbnail in the layout map, sized after the resolution in use."""

    def __init__(self, mon: system.Monitor, current, primary: bool, selected: bool) -> None:
        # vexpand=False explicitly: the name label expands inside the tile, and without this
        # GTK would propagate that upwards and stretch the whole layout map.
        super().__init__(css_classes=["monitor-tile"] + (["selected"] if selected else []),
                         valign=Gtk.Align.END, vexpand=False)
        w, h = (current.width, current.height) if current else (1920, 1080)
        self.set_size_request(max(120, int(w * TILE_SCALE)), max(70, int(h * TILE_SCALE)))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        top = Gtk.Box(spacing=6)
        top.append(label(mon.connector, "badge"))
        if primary:
            top.append(label("primary", "badge", "primary"))
        box.append(top)
        box.append(label(mon.original.name or mon.original.vendor, "tile-name", xalign=0, vexpand=True,
                         valign=Gtk.Align.END, ellipsize=3))
        box.append(label(f"{w}×{h} · {hz(current.refresh, 0)} Hz" if current else "—", "tile-mode", xalign=0))
        self.set_child(box)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="Custom Resolution Panel",
                         default_width=980, default_height=900, css_classes=["pr-window"])
        self.display = display.detect()
        if self.display and self.display.notifies_changes:
            self.display.on_changed = self._schedule_reload
        self._reload_source = 0
        self.monitors: list[system.Monitor] = []
        self.selected: str | None = None

        header = Adw.HeaderBar(title_widget=self._brand())
        reload_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload monitors",
                                css_classes=["icon-btn"])
        reload_btn.connect("clicked", lambda *_: self.reload())
        header.pack_end(reload_btn)

        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=26,
                            margin_top=8, margin_bottom=28, margin_start=24, margin_end=24)
        clamp = Adw.Clamp(maximum_size=1040, tightening_threshold=900, child=self.body)
        scroller = Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.toasts = Adw.ToastOverlay(child=scroller)

        view = Adw.ToolbarView(content=self.toasts, bottom_bar_style=Adw.ToolbarStyle.FLAT)
        view.add_top_bar(header)
        view.add_bottom_bar(self._action_bar())
        self.set_content(view)
        self.reload()

    # ---------------------------------------------------------------- parts ---

    def _brand(self) -> Gtk.Widget:
        box = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        if ICON.exists():
            box.append(Gtk.Image(file=str(ICON), pixel_size=28))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        title = Gtk.Box(spacing=8)
        title.append(label("Custom Resolution Panel", "brand-title"))
        title.append(label(f"v{VERSION}", "version-badge", valign=Gtk.Align.CENTER))
        text.append(title)
        box.append(text)
        return box

    def _action_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(spacing=10, css_classes=["action-bar"])
        self.action_status = label("", "action-status", xalign=0, hexpand=True, ellipsize=3)
        bar.append(self.action_status)

        def button(icon, text, css, handler, tooltip):
            btn = Gtk.Button(css_classes=css, tooltip_text=tooltip,
                             child=Adw.ButtonContent(icon_name=icon, label=text))
            btn.connect("clicked", handler)
            bar.append(btn)
            return btn

        self.restore_btn = button("edit-undo-symbolic", "Restore", ["ghost"], self._restore,
                                  "Remove this monitor's custom resolutions, now and at boot")
        self.persist_btn = button("document-save-symbolic", "Save to boot", ["ghost"], self._persist,
                                  "Write the EDID and drm.edid_firmware to GRUB")
        self.test_btn = button("media-playback-start-symbolic", "Test now", ["cta"], self._test,
                               "Apply the EDID until reboot, without touching the boot setup")
        return bar

    def _section(self, title: str, count: str = "", suffix: Gtk.Widget | None = None) -> Gtk.Box:
        box = Gtk.Box(spacing=10)
        box.append(label(title, "section-title"))
        if count:
            box.append(label(count, "section-count"))
        box.append(Gtk.Box(hexpand=True))
        if suffix:
            box.append(suffix)
        return box

    def _layout_strip(self) -> Gtk.Widget:
        strip = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, css_classes=["layout-strip"])
        tiles = Gtk.Box(spacing=16, halign=Gtk.Align.CENTER)
        try:
            layout = self.display.layout([m.connector for m in self.monitors]) if self.display else {}
        except display.DisplayError:
            layout = {}

        def position(mon):
            return layout.get(mon.connector, (10**6, 0, False))

        for mon in sorted(self.monitors, key=lambda m: position(m)[:2]):
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.END)
            tile = MonitorTile(mon, self.display_current(mon.connector), position(mon)[2],
                               mon.connector == self.selected)
            tile.connect("clicked", lambda _b, c=mon.connector: self.select(c))
            column.append(tile)
            tiles.append(column)
        strip.append(tiles)
        return strip

    def _hero(self, mon: system.Monitor) -> Gtk.Widget:
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, css_classes=["hero"])
        top = Gtk.Box(spacing=10)
        top.append(label(mon.original.name or mon.original.vendor, "hero-name", xalign=0))
        top.append(label(mon.connector, "badge", valign=Gtk.Align.CENTER))
        top.append(Gtk.Box(hexpand=True))
        kind, text = status_of(mon)
        pill = Gtk.Box(spacing=8, css_classes=["pill", kind], valign=Gtk.Align.CENTER)
        pill.append(Gtk.Box(css_classes=["dot"], valign=Gtk.Align.CENTER))
        pill.append(label(text))
        top.append(pill)
        hero.append(top)

        current = self.display_current(mon.connector)
        numbers = Gtk.Box(spacing=14)
        if current:
            numbers.append(label(str(current.width), "hero-number"))
            numbers.append(label("×", "hero-times", valign=Gtk.Align.CENTER))
            numbers.append(label(str(current.height), "hero-number"))
            numbers.append(Gtk.Box(width_request=18))
            numbers.append(label(hz(current.refresh), "hero-hz"))
            numbers.append(label("Hz", "hero-unit", valign=Gtk.Align.END, margin_bottom=10))
        else:
            numbers.append(label("—", "hero-number"))
        hero.append(numbers)

        native = mon.original.native
        meta = []
        if native:
            meta.append(f"Panel {native.h_active}×{native.v_active}")
        lim = mon.original.limits
        if lim:
            meta.append(f"accepts {lim.v_min}–{lim.v_max} Hz")
            if lim.max_clock_khz:
                meta.append(f"up to {lim.max_clock_khz // 1000} MHz")
        meta.append(f"EDID {mon.original.version}")
        meta.append(self.display.name if self.display else "mode switching not supported on this desktop")
        hero.append(label("  ·  ".join(meta), "hero-meta", xalign=0))
        return hero

    def _mode_card(self, mon: system.Monitor, index: int, cm: system.CustomMode, current) -> Gtk.Widget:
        t = cm.timing
        in_use = same_mode(current, t)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       css_classes=["mode-card"] + (["in-use"] if in_use else []))
        top = Gtk.Box(spacing=8)
        top.append(label(f"{t.h_active} × {t.v_active}", "card-res", xalign=0, hexpand=True))
        if in_use:
            top.append(label("In use", "inuse-badge", valign=Gtk.Align.CENTER))
        card.append(top)

        tags = Gtk.Box(spacing=6)
        tags.append(label(f"{hz(t.refresh_hz)} Hz", "hz"))
        tags.append(label(edid.STANDARDS.get(cm.standard, "Manual"), "tag", valign=Gtk.Align.CENTER))
        card.append(tags)
        card.append(label(f"{mhz(t.clock_khz)} · {hz(t.h_freq_khz)} kHz\ntotal {t.h_total}×{t.v_total}",
                          "card-tech", xalign=0))

        actions = Gtk.Box(spacing=4, margin_top=4)
        available = self.display_find(mon.connector, t)
        if self.display is None:
            hint = "Switching modes from here isn't supported on this desktop; pick it in your display settings"
        else:
            hint = None if available else "Click “Test now” first"
        use = Gtk.Button(label="Use", css_classes=["use-btn"], hexpand=True, halign=Gtk.Align.START,
                         sensitive=available is not None and not in_use, tooltip_text=hint)
        use.connect("clicked", lambda _b: self.switch_mode(mon, t))
        edit = Gtk.Button(icon_name="document-edit-symbolic", css_classes=["icon-btn", "flat"],
                          tooltip_text="Edit")
        edit.connect("clicked", lambda _b: ModeDialog(
            mon, lambda new: self.edit_modes(mon, lambda modes: modes.__setitem__(index, new)), cm).present(self))
        delete = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["icon-btn", "flat"],
                            tooltip_text="Remove")
        delete.connect("clicked", lambda _b: self.edit_modes(mon, lambda modes: modes.pop(index)))
        for w in (use, edit, delete):
            actions.append(w)
        card.append(actions)
        return card

    def _cards(self, mon: system.Monitor) -> Gtk.Widget:
        flow = Gtk.FlowBox(css_classes=["cards"], selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                           min_children_per_line=1, max_children_per_line=3,
                           column_spacing=14, row_spacing=14)
        current = self.display_current(mon.connector)
        for i, cm in enumerate(mon.modes):
            flow.append(self._mode_card(mon, i, cm, current))
        if len(mon.modes) < edid.MAX_CUSTOM_MODES:
            add = Gtk.Button(css_classes=["add-card"])
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER)
            inner.append(Gtk.Image(icon_name="list-add-symbolic", pixel_size=26))
            inner.append(label("New resolution", "tile-name"))
            add.set_child(inner)
            add.connect("clicked", lambda _b: ModeDialog(
                mon, lambda new: self.edit_modes(mon, lambda modes: modes.append(new))).present(self))
            flow.append(add)
        return flow

    def _factory(self, mon: system.Monitor) -> Gtk.Widget:
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=8, row_spacing=8,
                           max_children_per_line=8)
        seen = set()
        for t in mon.original.timings:
            key = (t.h_active, t.v_active, round(t.refresh_hz, 1))
            if key in seen:
                continue
            seen.add(key)
            chip = label(f"{t.h_active}×{t.v_active} · {hz(t.refresh_hz, 0)} Hz", "chip",
                         tooltip_text=timing_summary(t))
            flow.append(chip)
        return flow

    # ----------------------------------------------------------------- data ---

    def current_monitor(self) -> system.Monitor | None:
        return next((m for m in self.monitors if m.connector == self.selected), None)

    def select(self, connector: str) -> None:
        self.selected = connector
        self.rebuild()

    def reload(self) -> bool:
        self._reload_source = 0
        self.monitors = system.detect()
        self.save()
        if self.selected not in [m.connector for m in self.monitors]:
            self.selected = self.monitors[0].connector if self.monitors else None
        self.rebuild()
        return False

    def rebuild(self) -> None:
        while (child := self.body.get_first_child()) is not None:
            self.body.remove(child)
        mon = self.current_monitor()
        if mon is None:
            self.body.append(Adw.StatusPage(title="No monitor with an EDID found",
                                            icon_name="video-display-symbolic"))
            for btn in (self.test_btn, self.persist_btn, self.restore_btn):
                btn.set_sensitive(False)
            return

        self.body.append(self._layout_strip())
        self.body.append(self._hero(mon))

        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        section.append(self._section("Custom resolutions", f"{len(mon.modes)}/{edid.MAX_CUSTOM_MODES}"))
        section.append(self._cards(mon))
        self.body.append(section)

        factory = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        factory.append(self._section("Factory modes", "from the monitor's EDID"))
        factory.append(self._factory(mon))
        self.body.append(factory)

        live, persisted = mon.live_state, mon.persist_state
        self.test_btn.set_sensitive(bool(mon.modes) and live != "applied")
        self.persist_btn.set_sensitive(bool(mon.modes) and persisted != "current")
        self.restore_btn.set_sensitive(live != "original" or persisted != "none")
        self.action_status.set_label(f"{mon.original.name or mon.connector} — {status_of(mon)[1]}")

    def _schedule_reload(self, delay_ms: int = 400) -> None:
        # Mutter sends several MonitorsChanged in a row; reload only once.
        if not self._reload_source:
            self._reload_source = GLib.timeout_add(delay_ms, self.reload)

    def save(self) -> None:
        system.save(self.monitors)

    def edit_modes(self, mon: system.Monitor, change) -> None:
        current = next((m for m in self.monitors if m.original.identity == mon.original.identity), mon)
        change(current.modes)
        self.save()
        self.reload()

    def display_current(self, connector):
        try:
            return self.display.current_mode(connector) if self.display else None
        except display.DisplayError:
            return None

    def display_find(self, connector, t: edid.Timing):
        try:
            return self.display.find_mode(connector, t.h_active, t.v_active, t.refresh_hz) if self.display else None
        except display.DisplayError:
            return None

    def _reload_soon(self) -> None:
        """For compositors that don't announce changes, look again once they have settled."""
        if self.display is None or not self.display.notifies_changes:
            self._schedule_reload(1500)

    # -------------------------------------------------------------- actions ---

    def _test(self, _btn) -> None:
        mon = self.current_monitor()
        system.apply_live(mon, self.helper_done("EDID applied; GNOME will re-read the monitor"))

    def _persist(self, _btn) -> None:
        system.persist(self.monitors, self.helper_done("Saved; takes effect from the next boot"))

    def _restore(self, _btn) -> None:
        mon = self.current_monitor()
        was_persisted = mon.persisted_raw is not None
        live_changed = mon.live_state != "original"
        self.edit_modes(mon, lambda modes: modes.clear())
        done = self.helper_done("Monitor restored")

        def after_reset(ok, msg):
            if ok and was_persisted:
                system.persist(self.monitors, done)
            else:
                done(ok, msg)

        if live_changed:
            system.reset_live(mon, after_reset)
        else:
            after_reset(True, "")

    # --------------------------------------------------------------- notices ---

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def helper_done(self, success_text: str):
        def callback(ok: bool, msg: str) -> None:
            if ok:
                self.toast(success_text)
            else:
                alert = Adw.AlertDialog(heading="Something went wrong", body=msg or "The helper failed without a message.")
                alert.add_response("ok", "OK")
                alert.present(self)
            self.reload()
            self._reload_soon()
        return callback

    # ---------------------------------------------------------- mode switch ---

    def switch_mode(self, mon: system.Monitor, t: edid.Timing) -> None:
        """Like on Windows: apply, ask, and revert on its own if there is no answer."""
        target = self.display_find(mon.connector, t)
        previous = self.display_current(mon.connector)
        if not target or not previous:
            self.toast("The desktop does not offer this mode yet")
            return
        try:
            self.display.apply_mode(mon.connector, target, persistent=False)
        except display.DisplayError as e:
            self.toast(f"The desktop rejected the mode: {e}")
            return
        self._reload_soon()

        alert = Adw.AlertDialog(heading="Keep this resolution?")
        alert.add_response("revert", "Revert")
        alert.add_response("keep", "Keep")
        alert.set_response_appearance("keep", Adw.ResponseAppearance.SUGGESTED)
        alert.set_default_response("revert")
        alert.set_close_response("revert")
        countdown = {"left": CONFIRM_SECONDS, "source": 0}

        def tick():
            countdown["left"] -= 1
            if countdown["left"] <= 0:
                countdown["source"] = 0
                alert.close()
                return False
            alert.set_body(f"Reverting to the previous resolution in {countdown['left']} s.")
            return True

        alert.set_body(f"Reverting to the previous resolution in {CONFIRM_SECONDS} s.")
        countdown["source"] = GLib.timeout_add_seconds(1, tick)

        def answered(_alert, response):
            if countdown["source"]:
                GLib.source_remove(countdown["source"])
                countdown["source"] = 0
            try:
                keep = response == "keep"
                config_line = self.display.apply_mode(mon.connector, target if keep else previous, persistent=True)
            except display.DisplayError as e:
                self.toast(f"Failed to apply: {e}")
                return
            self._reload_soon()
            if keep and config_line:
                self._show_config_line(config_line)

        alert.connect("response", answered)
        alert.present(self)


    def _show_config_line(self, line: str) -> None:
        """Hyprland and Sway only change modes until logout; the user's config has the final say."""
        alert = Adw.AlertDialog(
            heading="Make it stick after logout",
            body=f"{self.display.name} applied the mode for this session only. "
                 "To keep it, add this line to your config file:")
        alert.set_extra_child(label(line, "card-tech", selectable=True, wrap=True, xalign=0))
        alert.add_response("ok", "OK")
        alert.present(self)


class App(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        # The app has its own dark look, independent of the system theme.
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        css = Gtk.CssProvider()
        css.load_from_path(str(STYLE))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self) -> None:
        win = self.get_active_window() or MainWindow(self)
        win.present()


def main() -> int:
    return App().run(sys.argv)
