# Plan: Repo öffentlich machen (2026-10-28)

## Ziel & Ansatz
Repo rechtlich und inhaltlich veröffentlichungsfähig machen. Keine Logik-Änderung am Code,
nur ein Kommentar. Freigegebene Defaults: **GPL-3.0**, Git-History **nicht** umschreiben,
`SYSTEM_STATE.txt` untracken.

## Ausgangslage (geprüft)
- `README.md` existiert bereits auf `origin/main` (PR #1); lokaler `main` ist veraltet.
- Keine echten Secrets / persönlichen Pfade in getrackten Dateien. `config.json`,
  `selftest-report.json`, `build/`, `dist/`, `libmpv-2.dll` sind bereits ignoriert.
- Commit-Author-Mail (private Adresse) steht in der History → muss vor dem Public-Schalten per History-Rewrite auf GitHub-noreply umgeschrieben werden.

## Dateien
- `LICENSE` (neu) – vollständiger GPL-3.0-Text (https://www.gnu.org/licenses/gpl-3.0.txt).
- `README.md` – ergänzen.
- `.gitignore` – erweitern.
- `vk_search.py` – nur Kommentar an `CLIENT_SECRET` (Zeile ~28).
- `SYSTEM_STATE.txt` – `git rm --cached`, bleibt lokal.

## Schritte (je Schritt ein Micro-Commit)
1. `git checkout main && git pull` → `git checkout -b public-release`.
2. `LICENSE` mit GPL-3.0-Volltext anlegen (Copyright-Zeile: `Copyright (C) 2026 islmal`). Commit.
3. `README.md`:
   - Abschnitt **Requirements**: Windows 10/11, Python-Mindestversion (aus Code ermitteln:
     z. B. `X | None`/`match` → 3.10+; sonst 3.9+), libmpv, optional ffmpeg/aria2c.
   - Abschnitt **Disclaimer**: nicht mit VK verbunden; nur für privaten Gebrauch;
     Nutzer verantwortlich für Einhaltung der VK-Nutzungsbedingungen und des Urheberrechts.
   - Abschnitt **License**: GPL-3.0, Link auf `LICENSE`; Hinweis, dass PyQt6/libmpv GPL sind.
   - Commit.
4. `.gitignore`: `lib/`, `*.dll`, `.venv/`, `SYSTEM_STATE.txt` ergänzen;
   `git rm --cached SYSTEM_STATE.txt`. Commit.
5. `vk_search.py:28`: Kommentar `# public value shipped in vkvideo.ru's web client, not a private secret`. Commit.
6. Push + PR gegen `main` öffnen.
7. Künftige Commits mit GitHub-noreply-Mail: `git config user.email "<id>+islmal@users.noreply.github.com"` (User liefert ID; nur Hinweis, nicht automatisch setzen).

## Verifizierung (automatisiert, alles muss grün/leer sein)
```bash
git ls-files | grep -E '\.dll$|config\.json|selftest|^build/|^dist/|SYSTEM_STATE' # leer
git grep -nIE 'C:\\\\Users|malik' # leer
python -m py_compile main.py vk_search.py player_widget.py config.py
python tests/test_vk_subs_patch.py
test -f LICENSE && head -1 LICENSE | grep -q 'GNU GENERAL PUBLIC LICENSE'
grep -oE '\]\(([^)#:]+)\)' README.md | sed -E 's/\]\((.*)\)/\1/' | xargs -I{} test -e {} # alle lokalen Links existieren
```

## Manuell durch den User (nach Merge)
GitHub: Description + Topics setzen, Secret Scanning aktivieren,
Settings → Danger Zone → Change visibility → Public.
