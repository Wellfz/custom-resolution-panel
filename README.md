# Custom Resolution Panel

Create custom resolutions and refresh rates on **GNOME Wayland**, the way the NVIDIA
Control Panel (or CRU) lets you do it on Windows.

![Custom Resolution Panel](docs/screenshot.png)

GNOME only offers the modes your monitor advertises in its EDID and has no way to
add a new one, neither in Settings nor from the command line. From GNOME 50 on there
is no X11 session anymore either, so `xrandr --newmode` and `nvidia-settings` are no
longer an option. This app solves it at the kernel level: it builds an EDID that
contains the modes you want and makes the monitor "advertise" them. To GNOME, they
are just regular modes.

## Features

- Up to 6 custom resolutions per monitor: width, height and refresh rate.
- Timing calculated from the monitor's own blanking (like CRU's "LCD native"),
  CVT-RB2 or CVT-RB, with manual control over porches, sync and pixel clock.
- Warnings when a mode goes beyond the limits the monitor declares.
- **Test now**: applies immediately, no reboot and no changes to your boot setup.
- **Use**: switches to the resolution and reverts by itself after 15 s unless you
  confirm it.
- **Save to boot**: makes it permanent via `drm.edid_firmware` in GRUB, with a backup.
- **Restore**: brings back the monitor's original EDID.

## How it works

1. The app reads the monitor's EDID and appends an extra CTA-861 extension block with
   your modes. The rest of the EDID (vendor, serial number, FreeSync, range limits) is
   left untouched, so GNOME still recognises the monitor and keeps its settings.
2. **Test now** writes the new EDID to
   `/sys/kernel/debug/dri/*/<connector>/edid_override`, re-probes the connector and
   sends a hotplug event so GNOME picks it up.
3. **Save to boot** copies the file to `/usr/lib/firmware/edid/` and adds
   `drm.edid_firmware=` to `/etc/default/grub`.
4. Mode switching uses Mutter's D-Bus API (`org.gnome.Mutter.DisplayConfig`).

Everything that needs root lives in a small separate helper started through `pkexec`.
It receives the EDID as hex, never a file path, and is installed in a root-owned
location so no user process can replace the code that runs as root.

## Requirements

- GNOME on Wayland (Mutter). Tested on GNOME 50.
- Python 3, GTK 4, libadwaita 1.6+ and PyGObject.
- polkit (`pkexec`) and debugfs mounted (the default on most distributions).
- GRUB, for the permanent mode. Live testing works with any bootloader.

Arch / Manjaro: `sudo pacman -S python-gobject gtk4 libadwaita`

### GPU drivers

It works with any driver that honours the DRM EDID override. Tested with the
**NVIDIA 610 driver** (open kernel modules, RTX 2060 SUPER): `nvidia-drm` hands the
override EDID to the driver, and NVIDIA's parser reads modes from more than one CTA
block. `amdgpu`, `i915` and `nouveau` use the same generic kernel mechanism but have not
been tested yet. If you try it, please open an issue with the result.

## Installation

```sh
git clone https://github.com/Wellfz/custom-resolution-panel.git
cd custom-resolution-panel
sudo ./install.sh
```

Then open **Custom Resolution Panel** from your app menu, or run
`custom-resolution-panel`.

To uninstall, click **Restore** on every monitor first, then run:

```sh
sudo ./install.sh --remove
```

## Tips and limitations

- **Resolutions below native look slightly soft.** The monitor's scaler stretches the
  image; GNOME has no "perform scaling on GPU" option like Windows does.
- **Prefer widths that are a multiple of 16** (1440, 1680, 1760…). Some monitor scalers
  can't handle unusual widths and show a broken or black picture.
- If the monitor shows "out of range", just wait: the previous mode comes back in 15 s.
- If the picture doesn't come back, reboot — live tests don't survive a reboot. If you
  had saved to boot, remove `drm.edid_firmware=...` from the kernel command line in the
  GRUB menu (press `e`).
- EDIDs that use HF-EEODB (some HDMI 2.1 TVs) are not supported yet.

## License

[GPL-3.0-or-later](LICENSE)
