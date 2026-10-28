# musavk – VK Video Fast Client & Downloader

Windows desktop app (Python 3 + PyQt6) to search, watch and download videos from
[vkvideo.ru](https://vkvideo.ru). No VK login needed.

## Features

- Global search via VK's anonymous web API, with sort options
- Channel mode: browse a community's uploads or search by keyword inside a channel
- Embedded playback (libmpv) with seek bar and keyboard shortcuts
- Downloads via yt-dlp: quality presets, FIFO queue, batch selection, cancel / skip
- Optional aria2c (faster DASH) and ffmpeg (merging video + audio)

Private or personal-page videos (VK error 15) are not supported.

## Setup

```bash
pip install -r requirements.txt
```

`libmpv-2.dll` is not pip-installable and not in this repo (too large). Put it in the project
root or in `lib\`. Builds: [shinchiro/mpv-winbuild-cmake](https://github.com/shinchiro/mpv-winbuild-cmake).
`ffmpeg` and `aria2c` on `PATH` are optional.

## Run

```bash
python main.py
```

Player shortcuts: `Q` / `Esc` close, `Space` pause, `←` / `→` seek ±5 s.

## Tests

```bash
python tests/test_vk_subs_patch.py
```

End-to-end self-test (needs network, downloads a real clip):

```bash
python main.py --self-test selftest-report.json
```

## Build

```bash
python -m PyInstaller musavk.spec --noconfirm --clean
```

Output: `dist\musavk\musavk.exe` (needs `libmpv-2.dll` in the project root or `lib\`).

## Notes

- Depends on VK's undocumented web API and a current yt-dlp (`pip install -U yt-dlp`).
- `vk_search._patch_yt_dlp_vk()` works around a yt-dlp VK extractor crash on subtitles with
  `"title": null`; remove it once upstream is fixed.
- Architecture and project status: see [STATUS.md](STATUS.md).
