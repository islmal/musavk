# STATUS.md – musavk (VK Video Fast Client & Downloader)

Stand: 2026-10-28. Dateibaum/Zeilen/Dependencies/Git: siehe `SYSTEM_STATE.txt` (generiert).

## 1. Mission & Out-of-Scope

Windows-Desktop-App (Python + PyQt6) zum **Suchen, Ansehen und Herunterladen von Videos von
vkvideo.ru** (Video-Client, kein Musik-Client). Anonyme VK-Web-API, Kanal-Modus, eingebetteter
libmpv-Player ("Cinema"-Overlay), Downloads per yt-dlp (FIFO-Queue, Batch, optional aria2c/ffmpeg),
PyInstaller-One-Folder-Build (`dist\musavk\musavk.exe`).

Out-of-Scope: VK-Login/Accounts (private Videos, VK-Error 15), Playlists, Untertitel, Kommentare,
Historie/Favoriten, Nicht-Windows, UI in anderen Sprachen als Englisch.

## 2. System Architecture & Data Flow

- `main.py` (GUI-Thread) → `vk_search.SearchWorker` / `ThumbnailLoader` / `StreamResolver` /
  `DownloadWorker` (alles QThreads, Signale für Cross-Thread) ; `player_widget` = libmpv-Overlay;
  `config.Config` = `config.json` neben Script/Exe (atomar via .tmp + os.replace).
- Suche: `VKClient` holt anonymen Token (`login.vk.com get_anonym_token`, Web-Client-ID/Secret als
  Konstanten – öffentliche Werte, nicht ersetzen), Retries bei Error 6/9/10, Refresh bei 5.
- Generation-Counter (`MainWindow.generation`, Player `_token`) verwerfen veraltete Ergebnisse.
- Kanal-Keyword-Suche = clientseitiger Scan des Feeds (VK bietet anonym keine Kanalsuche).
- Streams werden in Python via yt-dlp aufgelöst (mpv `ytdl=False`), mpv bekommt direkte URL+Header.
- Qualität: vorgemuxte `best[height<=N]` zuerst (schneller als split video+audio); Chunking für
  fragmentierte/Nicht-https-Formate aus; ohne ffmpeg nur Single-File. aria2c nur für DASH.
- libmpv: TLS braucht `tls_ca_file` = certifi; DLL-Suche App-Dir → `.\lib` → `_MEIPASS` → PATH.
  `locale LC_NUMERIC="C"` Pflicht; Windowed-Build setzt stdout/stderr auf devnull.
- **yt-dlp-VK-Patch:** `vk_search._patch_yt_dlp_vk()` (idempotent) umhüllt `VKIE._parse_json` und
  entfernt `title: null` aus `subs`-Listen; aufgerufen nach `import yt_dlp` in `StreamResolver.run`
  und `DownloadWorker.run`. Fallen: Patch muss vor `extract_info` laufen; Lazy-Import bleibt.
- Quirks: `selftest-report.json` ist UTF-8 (unter Windows `encoding="utf-8"` beim Lesen);
  Python-Aufrufe mit `PYTHONIOENCODING=utf-8`.

## 3. Roadmap & Known Issues

1. yt-dlp-Issue vorbereiten (Fix upstream: `(sub.get('title') or '.srt')`); danach Patch entfernen.
2. Offline-Unit-Tests für reine Logik (`parse_channel_ref`, `playback_format`, `channel_scan` mit Mock).
3. README + `.gitignore` (`build/`, `dist/`, `__pycache__/`, `config.json`); Git-Repo fehlt.
4. Self-Test gegen die gebaute `dist\musavk\musavk.exe` laufen lassen; Exe ist NICHT mit dem Patch neu gebaut → `python -m PyInstaller musavk.spec --noconfirm --clean`.

Bekannt:
- Abhängig von undokumentierter VK-Web-API (`API_VERSION = "5.245"`) und aktuellem yt-dlp.
- Persönliche Seiten brauchen Login; „Date: oldest" kehrt nur die geladene Seite um.
- Nur ein Download gleichzeitig; `Config.save()` und Thumbnail-Fehler werden still geschluckt.

## 4. Milestone Changelog

- **2026-10-28** – Fix `'NoneType' object has no attribute 'split'` bei Wiedergabe/Download
  (VK liefert Untertitel mit `title: null`, yt-dlp `vk.py:560` crasht): Monkeypatch
  `_patch_yt_dlp_vk()` in `vk_search.py`. Verifiziert: `tests/test_vk_subs_patch.py` ok;
  py_compile aller Module; Live `StreamResolver` → `resolved` (1080p url1080); `extract_info` mit
  `DownloadWorker.build_options()` ok; `main.py --self-test` → alle 14 Schritte ok.
- **2026-10-15** – STATUS.md per Read-only-Survey erstellt (kein Code geändert).

## 5. Dead Ends & Anti-Patterns

- „Could not resolve stream: 'NoneType'…split": ✗ yt-dlp-Update würde helfen → ✓ Bug steckt auch in
  master (`sub.get('title', '.srt')` greift bei Key mit `null` nicht); Workaround per Patch.
