# Contributing

Thanks for helping. The most useful contribution is a **TV compatibility report**: open an
issue with the "TV compatibility report" form and say what works on your TV.

## Project layout

| Path | What it is |
| --- | --- |
| `extension/` | Chrome extension (Manifest V3, plain JavaScript, no build step) |
| `helper/tvcast.py` | Mac helper (Python 3.8+, standard library only) |
| `tests/` | Tests for both |
| `install.sh`, `uninstall.sh` | Set up / remove the on-demand launchd agent |

## Running it from a checkout

```bash
./install.sh            # copies the checkout to ~/Applications/Send to TV
```

Re-run it after changing the helper. After changing the extension, press the reload arrow
on **Send to TV** in `chrome://extensions`. To watch the helper work:

```bash
tail -f ~/Library/Logs/SendToTV.log
```

You can also run the helper directly, without launchd: `python3 helper/tvcast.py`.

## Checks

```bash
python3 -m unittest discover -s tests -v   # helper
node --test tests/                         # extension
ruff check .                               # lint (pip install ruff)
shellcheck install.sh uninstall.sh
```

The same checks run on every pull request.

## Guidelines

- Keep the helper dependency-free (standard library only) and the extension build-free.
- Keep changes general: fixes should target a kind of stream or TV behaviour, not one
  particular website.
- This project does not bypass DRM or other access controls, and changes that try to
  won't be accepted.
- Add a line to `CHANGELOG.md` for anything a user would notice.
- Don't include private addresses, cookies or tokens in issues, logs or tests. Tests use
  `example.org` and documentation addresses (`192.0.2.x`).

## Releasing

1. Update the version in `extension/manifest.json` and `helper/tvcast.py` (`__version__`).
2. Move the "Unreleased" notes in `CHANGELOG.md` under the new version.
3. Tag it (`git tag v1.2.3 && git push --tags`) and create a GitHub release from the tag.
