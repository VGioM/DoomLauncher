# DOOM Launcher

Petit lanceur graphique (GTK4 / libadwaita) pour [UZDoom](https://github.com/UZDoom/UZDoom) sous Linux, pensé pour Bazzite.

- **Télécharge / met à jour UZDoom** depuis les releases GitHub officielles
- **Choix de l'épisode** : Ultimate DOOM, DOOM II, TNT, Plutonia, Freedoom, Heretic, Hexen…
  Les extensions (SIGIL, SIGIL II, Master Levels, ou tout autre PWAD avec des cartes) sont lancées automatiquement avec le bon IWAD
- **Mods optionnels** (`.pk3`, `.wad`, `.zip`, `.deh`…) : interrupteur par mod, ordre de chargement réglable, bouton « Aucun mod »
- Difficulté, arguments supplémentaires, mémorisation des choix, journal en cas de plantage

## Installation

Télécharger `DOOM_Launcher-x86_64.AppImage` dans les [Releases](../../releases), puis :

```bash
chmod +x DOOM_Launcher-x86_64.AppImage
./DOOM_Launcher-x86_64.AppImage
```

Ou l'ouvrir avec **Gear Lever** pour l'ajouter au menu.

**Dépendances** (déjà présentes sur Bazzite / Fedora Atomic) : `python3`, `python3-gobject`, `gtk4`, `libadwaita`.

## Dossiers

Réglables dans ☰ → **Paramètres** :

| Quoi | Par défaut |
|---|---|
| WAD (jeux) | `~/.local/share/games/uzdoom` |
| Mods | `~/Games/UZDoom` |
| UZDoom | `~/Applications/UZDoom.AppImage` |

Au premier lancement, si un `UZDoom.sh` existe dans le dossier des mods, les `-file` qu'il contient sont repris comme mods activés.

Configuration : `~/.config/doom-launcher/config.json` · Journal : `~/.cache/doom-launcher/uzdoom.log`

> Les fichiers WAD commerciaux (DOOM, DOOM II…) ne sont pas fournis : utilisez vos propres copies (Steam, GOG…) ou [Freedoom](https://freedoom.github.io/).

## Construire l'AppImage

```bash
./build.sh
```

Le code est dans `AppDir/usr/share/doom-launcher/launcher.py`.

## Licence

MIT
