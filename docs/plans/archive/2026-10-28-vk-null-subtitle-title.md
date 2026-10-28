# Fix: "Could not resolve stream: 'NoneType' object has no attribute 'split'"

Datum: 2026-10-28 · Status: planned

## Ursache (reproduziert)
Repro-Video: `https://vkvideo.ru/video-226802088_456261236` (Suche „прицесса и лягушка“, Relevance, 1. Treffer).
yt-dlp `yt_dlp/extractor/vk.py:560` (2026.08.19, identisch in 2026.10.26 und GitHub master):

```python
'ext': sub.get('title', '.srt').split('.')[-1],
```

VK liefert Untertitel-Einträge mit `"title": null` → Key existiert, Default greift nicht → `None.split` → AttributeError.
Untertitel werden immer geparst → betrifft **Wiedergabe (StreamResolver) und Download (DownloadWorker)**.
Ein yt-dlp-Update hilft nicht.

## Ansatz
Monkeypatch in musavk, nur angewendet wenn yt-dlp ohnehin importiert wird (Lazy-Import bleibt, kein Startup-Nachteil):
`VKIE._parse_json` umhüllen; im Ergebnis rekursiv in jeder `subs`-Liste `title` entfernen, wenn er `None` ist.
Dann greift yt-dlps eigener `'.srt'`-Default. No-op, sobald upstream gefixt ist.
Prototyp verifiziert: Repro-Video löst danach als `url1080` (1080p) auf.

Prototyp-Code (übernehmen, anpassen an Stil von vk_search.py):

```python
def fix(o):
    if isinstance(o, dict):
        for s in o.get("subs") or []:
            if isinstance(s, dict) and s.get("title") is None:
                s.pop("title", None)
        for v in o.values(): fix(v)
    elif isinstance(o, list):
        for v in o: fix(v)
```

Daten kommen über `_download_json` (→ `_parse_json`, via `__create_download_methods`) bzw. direkt `_parse_json` — beide Pfade sind damit abgedeckt.

## Dateien
- `vk_search.py`
  - neue Funktion `_patch_yt_dlp_vk()` (idempotent, z. B. Flag-Attribut auf `VKIE`), Kommentar:
    `# shortcut: works around yt-dlp VK extractor crashing on subtitles with "title": null (vk.py sub.get('title', '.srt')); drop once yt-dlp fixes it.`
  - Aufruf direkt nach `import yt_dlp` in `StreamResolver.run` (~Z. 571) und `DownloadWorker.run` (~Z. 702).
- `tests/test_vk_subs_patch.py` (neu, offline)

## Schritte
1. `_patch_yt_dlp_vk()` in `vk_search.py` implementieren.
2. Beide Aufrufe einfügen.
3. Offline-Test schreiben + ausführen.
4. Live-Check + Regression.

## Verifizierung (Pflicht)
1. **Offline-Test** `python tests/test_vk_subs_patch.py` (assert-basiert, pytest optional):
   - Patch anwenden, `VKIE()._parse_json('{"params":[{"subs":[{"title":null,"lang":"ru","url":"x"},{"title":"a.vtt","url":"y"}]}]}', "id")`
     → erster Eintrag ohne `title`, `lang`/`url` unverändert; zweiter Eintrag unverändert.
   - Patch zweimal anwenden → `_parse_json` nur einmal umhüllt (Ergebnis identisch, kein Doppel-Wrap).
2. **Live-Check** (Netz nötig, `PYTHONIOENCODING=utf-8`):
   - `StreamResolver(1, "https://vkvideo.ru/video-226802088_456261236", playback_format(None)).run()` direkt aufrufen
     → `resolved` emittiert mit nicht-leerer URL, kein `failed`.
   - `yt_dlp.YoutubeDL(DownloadWorker(...).build_options()).extract_info(url, download=False)` → kein Fehler.
3. **Regression**: `python -m py_compile main.py player_widget.py vk_search.py config.py`, dann
   `python main.py --self-test selftest-report.json` (Suche, Wiedergabe, Download, Kanal, Batch).

## Optional danach
Issue-Text für yt-dlp vorbereiten (Fix: `(sub.get('title') or '.srt')`), damit der Patch später entfernt werden kann.

## Nicht im Scope
- Falsche `ext`-Werte aus Spurtiteln (z. B. `'full Blu-ray CEE'`) — irrelevant, musavk lädt keine Untertitel.
