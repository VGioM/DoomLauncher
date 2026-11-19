#!/usr/bin/env python3
"""DOOM Launcher — petit lanceur GTK4/libadwaita pour UZDoom.

- Télécharge / met à jour UZDoom depuis GitHub (UZDoom/UZDoom)
- Choix d'un épisode (IWAD ou PWAD type SIGIL / Master Levels)
- Mods optionnels (.pk3, .wad, .zip...) avec ordre de chargement
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

APP_ID = "io.github.doomlauncher.DoomLauncher"
APP_VERSION = "1.1.0"
GITHUB_API = "https://api.github.com/repos/UZDoom/UZDoom/releases/latest"
APP_REPO = "VGioM/DoomLauncher"
APP_API = f"https://api.github.com/repos/{APP_REPO}/releases/latest"
APP_URL = f"https://github.com/{APP_REPO}"
APP_ASSET = "DOOM_Launcher-x86_64.AppImage"

CONFIG_DIR = Path(GLib.get_user_config_dir()) / "doom-launcher"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = Path(GLib.get_user_cache_dir()) / "doom-launcher" / "uzdoom.log"

MOD_EXTS = {".pk3", ".pk7", ".zip", ".wad", ".deh", ".bex", ".pke"}

# Tailles des WAD "classiques" v1.9 (pour les distinguer des éditions 2024/KEX)
CLASSIC_SIZES = {12408292, 14604584, 18195736, 17420824, 14943400, 11159840}

KNOWN_NAMES = {
    "doom": "The Ultimate DOOM",
    "doom1": "DOOM (shareware)",
    "doomu": "The Ultimate DOOM",
    "doom2": "DOOM II : Hell on Earth",
    "tnt": "Final DOOM : TNT Evilution",
    "plutonia": "Final DOOM : The Plutonia Experiment",
    "freedoom1": "Freedoom : Phase 1",
    "freedoom2": "Freedoom : Phase 2",
    "freedm": "FreeDM",
    "heretic": "Heretic",
    "heretic1": "Heretic (shareware)",
    "hexen": "Hexen",
    "hexdd": "Hexen : Deathkings",
    "strife1": "Strife",
    "chex": "Chex Quest",
    "chex3": "Chex Quest 3",
    "sigil": "SIGIL",
    "sigil2": "SIGIL II",
    "sigil_v1_21": "SIGIL",
    "masterlevels": "Master Levels for DOOM II",
    "nerve": "No Rest for the Living",
    "id1": "Legacy of Rust",
}

SKILLS = [
    ("Choisir dans le jeu", None),
    ("I'm too young to die", 1),
    ("Hey, not too rough", 2),
    ("Hurt me plenty", 3),
    ("Ultra-Violence", 4),
    ("Nightmare!", 5),
]


def default_config():
    home = Path.home()
    samsung = Path("/run/media/system/Samsung_860/Games/uZDoom")
    mods_dir = samsung if samsung.is_dir() else home / "Games" / "UZDoom"
    uzdoom = None
    if mods_dir.is_dir():
        for p in sorted(mods_dir.iterdir()):
            if p.name.lower().startswith("uzdoom") and p.suffix.lower() == ".appimage":
                uzdoom = p
                break
    if uzdoom is None:
        uzdoom = home / "Applications" / "UZDoom.AppImage"
    # Reprend les mods d'un éventuel script UZDoom.sh existant (premier lancement)
    preset = []
    try:
        for line in (mods_dir / "UZDoom.sh").read_text().splitlines():
            parts = line.split("#", 1)[0].split()
            if len(parts) >= 2 and parts[0] == "-file":
                p = (mods_dir / parts[1]).resolve()
                if p.is_file() and p.suffix.lower() in MOD_EXTS:
                    preset.append(str(mods_dir / parts[1].removeprefix("./")))
    except OSError:
        pass
    return {
        "wad_dir": str(home / ".local" / "share" / "games" / "uzdoom"),
        "mods_dir": str(mods_dir),
        "uzdoom_path": str(uzdoom),
        "uzdoom_version": None,
        "uzdoom_size": None,
        "episode": None,
        "mod_order": list(preset),
        "mods_enabled": list(preset),
        "skill": 0,
        "extra_args": "",
    }


def load_config():
    cfg = default_config()
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    os.replace(tmp, CONFIG_FILE)


# ---------------------------------------------------------------- WAD scan

def read_wad(path):
    """Retourne (type, set(noms de lumps)) ou None si ce n'est pas un WAD."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if len(head) < 12 or head[:4] not in (b"IWAD", b"PWAD"):
                return None
            count, offset = struct.unpack("<ii", head[4:])
            if count <= 0 or count > 200000:
                return head[:4].decode(), set()
            f.seek(offset)
            data = f.read(count * 16)
        names = set()
        for i in range(0, len(data) - 15, 16):
            names.add(data[i + 8:i + 16].split(b"\0", 1)[0].decode("latin-1").upper())
        return head[:4].decode(), names
    except OSError:
        return None


def map_family(lumps):
    if any(n.startswith("MAP") and n[3:].isdigit() for n in lumps):
        return "doom2"
    if any(len(n) == 4 and n[0] == "E" and n[2] == "M" for n in lumps):
        return "doom1"
    return None


def nice_name(path):
    stem = path.stem.lower()
    return KNOWN_NAMES.get(stem, path.stem.replace("_", " ").title())


def human_size(n):
    for unit in ("o", "Ko", "Mo", "Go"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} To"


def scan(wad_dir, mods_dir):
    """Renvoie (episodes, mods). Un épisode = dict(id, title, subtitle, iwad, pwad)."""
    iwads, pwads, mods = [], [], []
    wd = Path(wad_dir)
    if wd.is_dir():
        for p in sorted(wd.iterdir(), key=lambda x: x.name.lower()):
            if not p.is_file() or p.suffix.lower() not in (".wad", ".iwad", ".ipk3"):
                continue
            info = read_wad(p) if p.suffix.lower() != ".ipk3" else ("IWAD", set())
            if info is None:
                continue
            kind, lumps = info
            fam = map_family(lumps)
            if kind == "IWAD":
                iwads.append((p, fam))
            elif fam:
                pwads.append((p, fam))
            else:
                mods.append(p)  # PWAD sans cartes (ex: extras.wad) -> mod

    episodes = []
    for p, fam in iwads:
        size = p.stat().st_size
        tag = " · v1.9 classique" if size in CLASSIC_SIZES else ""
        episodes.append({
            "id": str(p), "title": nice_name(p),
            "subtitle": f"{p.name} · {human_size(size)}{tag}",
            "iwad": str(p), "pwad": None,
        })

    def base_for(fam):
        want = "doom" if fam == "doom1" else "doom2"
        cands = [p for p, f in iwads if f == fam]
        exact = [p for p in cands if p.stem.lower() == want]
        pool = exact or cands
        return max(pool, key=lambda x: x.stat().st_size) if pool else None

    for p, fam in pwads:
        base = base_for(fam)
        episodes.append({
            "id": str(p), "title": nice_name(p),
            "subtitle": (f"{p.name} + {base.name}" if base
                         else f"{p.name} · nécessite {'doom.wad' if fam == 'doom1' else 'doom2.wad'}"),
            "iwad": str(base) if base else None, "pwad": str(p),
        })

    md = Path(mods_dir)
    if md.is_dir():
        for p in sorted(md.iterdir(), key=lambda x: x.name.lower()):
            if p.is_file() and p.suffix.lower() in MOD_EXTS:
                mods.append(p)
    return episodes, [str(m) for m in mods]


# ------------------------------------------------------------ UZDoom update

def fetch_latest_release():
    req = urllib.request.Request(GITHUB_API, headers={"Accept": "application/vnd.github+json",
                                                      "User-Agent": "doom-launcher"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    for a in data.get("assets", []):
        name = a["name"]
        if name.endswith(".AppImage") and "linux" in name.lower() and "x86_64" in name:
            return {"version": data["tag_name"], "url": a["browser_download_url"],
                    "size": a["size"], "name": name}
    raise RuntimeError("Aucun AppImage Linux trouvé dans la dernière version")


def parse_version(v):
    out = []
    for part in v.lstrip("vV").split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def fetch_launcher_release():
    """Dernière version publiée de DOOM Launcher lui-même."""
    req = urllib.request.Request(APP_API, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "doom-launcher"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    asset = next((a for a in data.get("assets", []) if a["name"] == APP_ASSET), None)
    return {"version": data["tag_name"].lstrip("vV"),
            "url": asset["browser_download_url"] if asset else None,
            "page": data.get("html_url") or f"{APP_URL}/releases/latest"}


def own_appimage():
    """Chemin de l'AppImage en cours d'exécution, s'il est modifiable."""
    p = os.environ.get("APPIMAGE")
    if p and os.path.isfile(p) and os.access(os.path.dirname(p), os.W_OK):
        return Path(p)
    return None


def download(url, dest, progress_cb):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "doom-launcher"})
    with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                progress_cb(done / total)
    os.chmod(part, 0o755)
    os.replace(part, dest)


def clean_env():
    """Environnement sans les variables de notre propre AppImage."""
    env = dict(os.environ)
    for k in ("APPIMAGE", "APPDIR", "ARGV0", "OWD", "APPIMAGE_SILENT_INSTALL"):
        env.pop(k, None)
    return env


# ----------------------------------------------------------------- Interface

class ModRow(Adw.ActionRow):
    def __init__(self, win, path, enabled):
        super().__init__()
        self.win = win
        self.path = path
        p = Path(path)
        self.set_title(GLib.markup_escape_text(p.stem))
        try:
            size = human_size(p.stat().st_size)
        except OSError:
            size = "?"
        self.set_subtitle(GLib.markup_escape_text(f"{p.name} · {size}"))

        self.switch = Gtk.Switch(active=enabled, valign=Gtk.Align.CENTER)
        self.switch.connect("notify::active", lambda *_: win.on_mods_changed())
        self.add_prefix(self.switch)
        self.set_activatable_widget(self.switch)

        up = Gtk.Button(icon_name="go-up-symbolic", valign=Gtk.Align.CENTER,
                        tooltip_text="Charger plus tôt")
        down = Gtk.Button(icon_name="go-down-symbolic", valign=Gtk.Align.CENTER,
                          tooltip_text="Charger plus tard")
        for b in (up, down):
            b.add_css_class("flat")
        up.connect("clicked", lambda *_: win.move_mod(self.path, -1))
        down.connect("clicked", lambda *_: win.move_mod(self.path, +1))
        self.add_suffix(up)
        self.add_suffix(down)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="DOOM Launcher")
        self.set_default_size(620, 820)
        self.cfg = load_config()
        self.latest = None
        self.busy = False
        self.episode_rows = []
        self.mod_rows = []
        self.proc = None

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        header = Adw.HeaderBar()
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Actualiser")
        refresh.connect("clicked", lambda *_: self.refresh())
        header.pack_start(refresh)

        menu = Gio.Menu()
        menu.append("Paramètres", "win.settings")
        menu.append("Vérifier les mises à jour", "win.check")
        menu.append("Ouvrir le dossier des mods", "win.open-mods")
        menu.append("Ouvrir le dossier des WAD", "win.open-wads")
        menu.append("Voir le journal du dernier lancement", "win.open-log")
        menu.append("À propos", "win.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        view.add_top_bar(header)

        for name, cb in (("settings", self.show_settings), ("check", lambda: (self.check_update(True), self.check_self_update(True))),
                         ("open-mods", lambda: self.open_dir(self.cfg["mods_dir"])),
                         ("open-wads", lambda: self.open_dir(self.cfg["wad_dir"])),
                         ("open-log", lambda: self.open_dir(str(LOG_FILE))),
                         ("about", self.show_about)):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda _a, _p, f=cb: f())
            self.add_action(act)

        self.app_banner = Adw.Banner()
        self.app_banner.connect("button-clicked", lambda *_: self.install_self_update())
        view.add_top_bar(self.app_banner)
        self.app_latest = None

        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", lambda *_: self.install_update())
        view.add_top_bar(self.banner)

        self.progress = Gtk.ProgressBar(visible=False)
        self.progress.add_css_class("osd")
        view.add_top_bar(self.progress)

        # Contenu
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=640)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                           margin_top=18, margin_bottom=18, margin_start=12, margin_end=12)
        clamp.set_child(self.box)
        scroller.set_child(clamp)
        view.set_content(scroller)

        self.ep_group = Adw.PreferencesGroup(title="Épisode",
                                             description="Le jeu ou l'extension à lancer")
        self.mod_group = Adw.PreferencesGroup(
            title="Mods", description="Activez ceux à charger (de haut en bas)")
        none_btn = Gtk.Button(label="Aucun mod", valign=Gtk.Align.CENTER)
        none_btn.add_css_class("flat")
        none_btn.connect("clicked", lambda *_: self.disable_all_mods())
        self.mod_group.set_header_suffix(none_btn)

        opt_group = Adw.PreferencesGroup(title="Options")
        self.skill_row = Adw.ComboRow(title="Difficulté",
                                      model=Gtk.StringList.new([s[0] for s in SKILLS]))
        self.skill_row.set_selected(min(int(self.cfg.get("skill") or 0), len(SKILLS) - 1))
        self.skill_row.connect("notify::selected", lambda *_: self.store_options())
        self.args_row = Adw.EntryRow(title="Arguments supplémentaires (ex : +map MAP07)")
        self.args_row.set_text(self.cfg.get("extra_args") or "")
        self.args_row.connect("changed", lambda *_: self.store_options())
        opt_group.add(self.skill_row)
        opt_group.add(self.args_row)

        for w in (self.ep_group, self.mod_group, opt_group):
            self.box.append(w)

        # Barre du bas
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                         margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.summary = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.summary.add_css_class("dim-label")
        self.play_btn = Gtk.Button(label="JOUER", halign=Gtk.Align.CENTER)
        self.play_btn.add_css_class("suggested-action")
        self.play_btn.add_css_class("pill")
        self.play_btn.set_size_request(240, 52)
        self.play_btn.connect("clicked", lambda *_: self.launch())
        bottom.append(self.summary)
        bottom.append(self.play_btn)
        view.add_bottom_bar(bottom)

        self.refresh()
        self.set_focus(self.play_btn)
        self.check_update(False)
        self.check_self_update(False)

    # ---------- helpers
    def toast(self, msg, timeout=3):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(msg), timeout=timeout))

    def open_dir(self, path):
        p = Path(path)
        if not p.exists():
            self.toast(f"Introuvable : {path}")
            return
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(p))).launch(self, None, None, None)

    def save(self):
        try:
            save_config(self.cfg)
        except OSError as e:
            self.toast(f"Impossible d'enregistrer la config : {e}")

    def store_options(self):
        self.cfg["skill"] = self.skill_row.get_selected()
        self.cfg["extra_args"] = self.args_row.get_text()
        self.save()

    # ---------- listes
    def refresh(self):
        for r in self.episode_rows:
            self.ep_group.remove(r)
        for r in self.mod_rows:
            self.mod_group.remove(r)
        self.episode_rows, self.mod_rows = [], []

        episodes, mods = scan(self.cfg["wad_dir"], self.cfg["mods_dir"])
        self.episodes = {e["id"]: e for e in episodes}

        if not episodes:
            row = Adw.ActionRow(title="Aucun WAD trouvé",
                                subtitle=GLib.markup_escape_text(
                                    f"Placez doom.wad, doom2.wad… dans {self.cfg['wad_dir']}"))
            self.ep_group.add(row)
            self.episode_rows.append(row)

        current = self.cfg.get("episode")
        if current not in self.episodes and episodes:
            current = next((e["id"] for e in episodes if e["iwad"]), None)
        group_btn = None
        for e in episodes:
            row = Adw.ActionRow(title=GLib.markup_escape_text(e["title"]),
                                subtitle=GLib.markup_escape_text(e["subtitle"]))
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            if group_btn is None:
                group_btn = check
            else:
                check.set_group(group_btn)
            check.set_active(e["id"] == current)
            check.connect("toggled", self.on_episode_toggled, e["id"])
            row.add_prefix(check)
            row.set_activatable_widget(check)
            row.set_sensitive(e["iwad"] is not None)
            self.ep_group.add(row)
            self.episode_rows.append(row)
        self.cfg["episode"] = current

        # Ordre des mods : ordre mémorisé puis nouveaux fichiers
        order = [m for m in self.cfg.get("mod_order", []) if m in mods]
        order += [m for m in mods if m not in order]
        self.cfg["mod_order"] = order
        enabled = set(self.cfg.get("mods_enabled", []))
        if not order:
            row = Adw.ActionRow(title="Aucun mod trouvé",
                                subtitle=GLib.markup_escape_text(
                                    f"Placez vos .pk3 / .wad dans {self.cfg['mods_dir']}"))
            self.mod_group.add(row)
            self.mod_rows.append(row)
        for m in order:
            row = ModRow(self, m, m in enabled)
            self.mod_group.add(row)
            self.mod_rows.append(row)
        self.update_summary()

    def on_episode_toggled(self, check, ep_id):
        if check.get_active():
            self.cfg["episode"] = ep_id
            self.save()
            self.update_summary()

    def on_mods_changed(self):
        self.cfg["mods_enabled"] = [r.path for r in self.mod_rows
                                    if isinstance(r, ModRow) and r.switch.get_active()]
        self.save()
        self.update_summary()

    def disable_all_mods(self):
        for r in self.mod_rows:
            if isinstance(r, ModRow):
                r.switch.set_active(False)

    def move_mod(self, path, delta):
        order = self.cfg["mod_order"]
        i = order.index(path)
        j = i + delta
        if 0 <= j < len(order):
            order[i], order[j] = order[j], order[i]
            self.save()
            self.refresh()

    def enabled_mods(self):
        enabled = set(self.cfg.get("mods_enabled", []))
        return [m for m in self.cfg["mod_order"] if m in enabled and Path(m).exists()]

    def update_summary(self):
        ep = self.episodes.get(self.cfg.get("episode")) if hasattr(self, "episodes") else None
        mods = self.enabled_mods()
        if not ep:
            self.summary.set_text("Choisissez un épisode")
        elif mods:
            self.summary.set_text(f"{ep['title']} + {len(mods)} mod(s)")
        else:
            self.summary.set_text(f"{ep['title']} — sans mod")

    # ---------- UZDoom : installation / mise à jour
    def uzdoom_path(self):
        return Path(self.cfg["uzdoom_path"]).expanduser()

    def check_update(self, manual):
        path = self.uzdoom_path()
        if not path.exists():
            self.banner.set_title("UZDoom n'est pas installé")
            self.banner.set_button_label("Télécharger")
            self.banner.set_revealed(True)

        def work():
            try:
                latest = fetch_latest_release()
            except Exception as e:  # réseau absent, quota GitHub...
                GLib.idle_add(self.on_check_failed, str(e), manual)
                return
            GLib.idle_add(self.on_check_done, latest, manual)

        threading.Thread(target=work, daemon=True).start()

    def on_check_failed(self, err, manual):
        if manual:
            self.toast(f"Vérification impossible : {err}", 5)

    def on_check_done(self, latest, manual):
        self.latest = latest
        path = self.uzdoom_path()
        if not path.exists():
            self.banner.set_title(f"UZDoom n'est pas installé (dernière version : {latest['version']})")
            self.banner.set_button_label("Télécharger")
            self.banner.set_revealed(True)
            return
        size = path.stat().st_size
        installed = self.cfg.get("uzdoom_version") if self.cfg.get("uzdoom_size") == size else None
        if installed is None and size == latest["size"]:
            installed = latest["version"]
            self.cfg["uzdoom_version"], self.cfg["uzdoom_size"] = installed, size
            self.save()
        if installed == latest["version"]:
            self.banner.set_revealed(False)
            if manual:
                self.toast(f"UZDoom est à jour ({installed})")
        else:
            cur = installed or "inconnue"
            self.banner.set_title(f"Mise à jour d'UZDoom disponible : {cur} → {latest['version']}")
            self.banner.set_button_label("Mettre à jour")
            self.banner.set_revealed(True)

    def install_update(self):
        if self.busy:
            return
        if self.latest is None:
            self.toast("Recherche de la dernière version…")
            self.check_update(True)
            return
        self.busy = True
        latest = self.latest
        dest = self.uzdoom_path()
        self.banner.set_revealed(False)
        self.progress.set_fraction(0)
        self.progress.set_visible(True)
        self.play_btn.set_sensitive(False)
        self.toast(f"Téléchargement d'UZDoom {latest['version']}…")

        def progress(frac):
            GLib.idle_add(self.progress.set_fraction, frac)

        def work():
            try:
                download(latest["url"], dest, progress)
                GLib.idle_add(self.on_install_done, latest, None)
            except Exception as e:
                GLib.idle_add(self.on_install_done, latest, str(e))

        threading.Thread(target=work, daemon=True).start()

    def on_install_done(self, latest, err):
        self.busy = False
        self.progress.set_visible(False)
        self.play_btn.set_sensitive(True)
        if err:
            self.toast(f"Échec du téléchargement : {err}", 6)
            self.check_update(False)
            return
        path = self.uzdoom_path()
        self.cfg["uzdoom_version"] = latest["version"]
        self.cfg["uzdoom_size"] = path.stat().st_size
        self.save()
        self.toast(f"UZDoom {latest['version']} installé ✔")

    # ---------- DOOM Launcher : mise à jour de lui-même
    def check_self_update(self, manual):
        def work():
            try:
                latest = fetch_launcher_release()
            except Exception as e:
                GLib.idle_add(self.on_check_failed, str(e), manual)
                return
            GLib.idle_add(self.on_self_check_done, latest, manual)

        threading.Thread(target=work, daemon=True).start()

    def on_self_check_done(self, latest, manual):
        self.app_latest = latest
        if parse_version(latest["version"]) <= parse_version(APP_VERSION):
            self.app_banner.set_revealed(False)
            if manual:
                self.toast(f"DOOM Launcher est à jour ({APP_VERSION})")
            return
        self.app_banner.set_title(
            f"Nouvelle version de DOOM Launcher : {APP_VERSION} → {latest['version']}")
        auto = own_appimage() is not None and latest["url"]
        self.app_banner.set_button_label("Mettre à jour" if auto else "Télécharger")
        self.app_banner.set_revealed(True)

    def install_self_update(self):
        latest, target = self.app_latest, own_appimage()
        if latest is None:
            return
        if target is None or not latest["url"]:
            # Pas lancé depuis un AppImage modifiable : on ouvre la page de la release
            Gtk.UriLauncher.new(latest["page"]).launch(self, None, None, None)
            return
        if self.busy:
            return
        self.busy = True
        self.app_banner.set_revealed(False)
        self.progress.set_fraction(0)
        self.progress.set_visible(True)
        self.play_btn.set_sensitive(False)
        self.toast(f"Téléchargement de DOOM Launcher {latest['version']}…")

        def progress(frac):
            GLib.idle_add(self.progress.set_fraction, frac)

        def work():
            try:
                # Remplacer le fichier est sans risque : l'instance en cours garde l'ancien
                download(latest["url"], target, progress)
                GLib.idle_add(self.on_self_install_done, latest, target, None)
            except Exception as e:
                GLib.idle_add(self.on_self_install_done, latest, target, str(e))

        threading.Thread(target=work, daemon=True).start()

    def on_self_install_done(self, latest, target, err):
        self.busy = False
        self.progress.set_visible(False)
        self.play_btn.set_sensitive(True)
        if err:
            self.toast(f"Échec de la mise à jour : {err}", 6)
            self.app_banner.set_revealed(True)
            return
        dlg = Adw.AlertDialog(heading=f"DOOM Launcher {latest['version']} installé",
                              body="Redémarrer maintenant pour utiliser la nouvelle version ?")
        dlg.add_response("later", "Plus tard")
        dlg.add_response("restart", "Redémarrer")
        dlg.set_response_appearance("restart", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("restart")

        def done(_d, response):
            if response == "restart":
                subprocess.Popen([str(target)], env=clean_env(), start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.get_application().quit()

        dlg.connect("response", done)
        dlg.present(self)

    # ---------- Lancement
    def build_command(self):
        ep = self.episodes.get(self.cfg.get("episode"))
        if not ep or not ep["iwad"]:
            raise RuntimeError("Choisissez un épisode")
        exe = self.uzdoom_path()
        if not exe.exists():
            raise RuntimeError("UZDoom n'est pas installé — cliquez sur « Télécharger »")
        if not os.access(exe, os.X_OK):
            os.chmod(exe, 0o755)
        cmd = [str(exe), "-iwad", ep["iwad"]]
        files = ([ep["pwad"]] if ep["pwad"] else []) + self.enabled_mods()
        if files:
            cmd += ["-file", *files]
        skill = SKILLS[self.skill_row.get_selected()][1]
        if skill:
            cmd += ["-skill", str(skill)]
        extra = self.args_row.get_text().strip()
        if extra:
            cmd += GLib.shell_parse_argv(extra)[1]
        return cmd

    def launch(self):
        if self.proc and self.proc.poll() is None:
            self.toast("UZDoom est déjà lancé")
            return
        try:
            cmd = self.build_command()
        except (RuntimeError, GLib.Error, OSError) as e:
            self.toast(str(e), 5)
            return
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        log = open(LOG_FILE, "w")
        log.write("$ " + " ".join(GLib.shell_quote(c) for c in cmd) + "\n\n")
        log.flush()
        cwd = self.cfg["mods_dir"] if Path(self.cfg["mods_dir"]).is_dir() else str(Path.home())
        try:
            self.proc = subprocess.Popen(cmd, cwd=cwd, env=clean_env(), stdout=log,
                                         stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            log.close()
            self.toast(f"Lancement impossible : {e}", 6)
            return
        log.close()
        self.started = time.monotonic()
        self.toast("Bon jeu ! 🔥")
        self.play_btn.set_label("EN COURS…")
        self.play_btn.set_sensitive(False)
        GLib.timeout_add(500, self.watch_proc)

    def watch_proc(self):
        code = self.proc.poll()
        if code is None:
            return True
        self.play_btn.set_label("JOUER")
        self.play_btn.set_sensitive(True)
        if code != 0:
            t = Adw.Toast(title=f"UZDoom s'est arrêté avec une erreur ({code})", timeout=8,
                          button_label="Journal")
            t.connect("button-clicked", lambda *_: self.open_dir(str(LOG_FILE)))
            self.toasts.add_toast(t)
        return False

    # ---------- Paramètres
    def show_settings(self):
        dlg = Adw.PreferencesDialog(title="Paramètres")
        page = Adw.PreferencesPage()
        grp = Adw.PreferencesGroup(title="Emplacements")
        page.add(grp)
        dlg.add(page)

        def path_row(title, key, folder):
            row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(self.cfg[key]))
            btn = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text="Choisir…")
            btn.add_css_class("flat")

            def pick(*_):
                fd = Gtk.FileDialog(title=title)
                cur = Path(self.cfg[key]).expanduser()
                start = cur if folder else cur.parent
                if start.is_dir():
                    fd.set_initial_folder(Gio.File.new_for_path(str(start)))

                def done(d, res):
                    try:
                        f = d.select_folder_finish(res) if folder else d.save_finish(res)
                    except GLib.Error:
                        return
                    if f:
                        self.cfg[key] = f.get_path()
                        row.set_subtitle(GLib.markup_escape_text(self.cfg[key]))
                        self.save()
                        self.refresh()
                        self.check_update(False)

                if folder:
                    fd.select_folder(self, None, done)
                else:
                    fd.set_initial_name(cur.name)
                    fd.save(self, None, done)

            btn.connect("clicked", pick)
            row.add_suffix(btn)
            row.set_activatable_widget(btn)
            grp.add(row)

        path_row("Dossier des WAD (jeux)", "wad_dir", True)
        path_row("Dossier des mods", "mods_dir", True)
        path_row("Fichier UZDoom (AppImage)", "uzdoom_path", False)

        info = Adw.PreferencesGroup(title="UZDoom")
        page.add(info)
        ver = self.cfg.get("uzdoom_version") or "inconnue"
        latest = self.latest["version"] if self.latest else "?"
        info.add(Adw.ActionRow(title="Version installée", subtitle=ver))
        info.add(Adw.ActionRow(title="Dernière version", subtitle=latest))
        force = Gtk.Button(label="Retélécharger", valign=Gtk.Align.CENTER)
        force.connect("clicked", lambda *_: (dlg.close(), self.install_update()))
        info.set_header_suffix(force)
        dlg.present(self)

    def show_about(self):
        about = Adw.AboutDialog(application_name="DOOM Launcher", version=APP_VERSION,
                        application_icon="doom-launcher",
                        comments="Lanceur simple pour UZDoom : épisodes, mods et mises à jour.",
                        website=APP_URL, issue_url=f"{APP_URL}/issues",
                        license_type=Gtk.License.MIT_X11)
        about.add_link("UZDoom", "https://github.com/UZDoom/UZDoom")
        about.present(self)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        icons = os.environ.get("APPDIR")
        if icons:
            theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
            theme.add_search_path(str(Path(icons) / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps"))
        Gtk.Window.set_default_icon_name("doom-launcher")

    def do_activate(self):
        win = self.props.active_window or MainWindow(self)
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
