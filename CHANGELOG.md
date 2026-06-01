# Changelog

All notable changes to the WarpTools Tilt Series Visualiser are documented here.

---

## [1.1.0] - 2026-05-27

### Fixed
- **XML declaration preserved on save** — the `<?xml version="1.0" encoding="utf-8"?>` header
  is now written correctly when saving exclusions. Previously this line was stripped, causing
  downstream WarpTools commands (e.g. `ts_aretomo`) to fail with a path validation error.
- **Motion track fill artefact** — motion patch trajectories were being filled with colour
  from the previous patch due to a QPainter brush state not being cleared between iterations.
  Fixed by explicitly setting `Qt.NoBrush` before each `drawPath()` call.

### Changed
- **Motion tracks overlaid on tilt image** — the motion correction patch trajectories are
  now drawn directly on top of the tilt image using QPainter, replacing the separate
  motion panel. Tracks are positioned spatially at their correct grid locations on the image.
- **Motion overlay toggle** — a `Motion Overlay [Ctrl+M]` checkbox in the button bar
  shows or hides the motion tracks without navigating away from the current tilt.
- **Power spectrum restored to side-by-side layout** — the power spectrum panel is back
  next to the tilt image, preserving the correct 2:1 aspect ratio.
- **Motion track colour coding** — tracks are now coloured by arc-length (total
  frame-to-frame displacement): green = low motion, yellow = moderate, red/orange = high.
  Previously all tracks used the plasma colourmap indexed by patch position.

---

## [1.0.0] - 2026-05-27

### Added
- Initial release of the PyQt5-based tilt series visualiser.
- **Tilt image display** — hardware-accelerated `QLabel`/`QPixmap` rendering with
  percentile-based contrast stretching.
- **Power spectrum display** — loads `powerspectrum/*.mrc` from the WarpTools frame-series
  directory; displayed with square-root scaling at correct 2:1 aspect ratio.
- **Spatial motion map** — patch trajectories from `average/*_motion.json` shown in a
  5×5 grid layout; each patch positioned at its correct image location.
- **CTF-colour-coded overview bar** — one bar per tilt, clickable to jump directly to
  that tilt. Colour scheme: red = excluded, orange = auto-flagged intensity outlier,
  purple = CTF > 10 Å, amber = 8–10 Å, green ≤ 8 Å.
- **Exclusion overlay** — excluded tilts show a red overlay and "Bad frame — excluded"
  text. A short descending audio tone plays as feedback (requires `aplay`).
- **Scrollable tilt series list** — `QListWidget` with mouse-wheel scrolling; click any
  name to switch series.
- **State restoration** — previous exclusions are read from `<UseTilt>` in the tilt-series
  XML on load, so the GUI reopens in the same state.
- **Dual file save** — exclusions are written to both the `.tomostar` (rows removed) and
  the tilt-series XML (`<UseTilt>` set to `False`). Timestamped backups are created for
  both files before any write.
- **Per-tilt metadata bar** — CTF fit (Å), defocus (µm), and mean frame motion (Å) read
  from per-frame WarpTools XML.
- **Keyboard shortcuts** — `←/→` navigate, `Ctrl+E` exclude, `Ctrl+S` save,
  `Ctrl+N` next series, `Ctrl+Q` save+quit, `Ctrl+R` reset all, `Ctrl+M` motion toggle.
- **Separate Save and Next Series buttons** — decoupled from earlier versions where save
  and series navigation were combined.
- **Qt warning suppression** — `QT_LOGGING_RULES` and `SESSION_MANAGER` environment
  variables set at startup to silence harmless X11/GLX messages.
- `environment.yml` for one-command conda environment creation.
