# WarpTools Tilt Series Visualiser

An interactive quality control tool for tilt series data processed with
[WarpTools](https://github.com/warpem/warp). Inspect tilt images, power
spectra, and motion correction results before proceeding to alignment and
reconstruction.

> **This tool was developed with assistance from [Claude](https://claude.ai)
> (Anthropic) as part of a cryoET subtomogram averaging pipeline.**

---

## Features

- **Side-by-side display** of the tilt image and power spectrum
- **Motion track overlay** drawn spatially on the tilt image — each patch
  placed at its correct grid position and colour-coded by motion magnitude
  (green = low, red = high). Toggle on/off with a checkbox or `Ctrl+M`
- **CTF-colour-coded overview bar** — click any bar to jump directly to
  that tilt
- **Exclusion** of bad tilts writes to both the `.tomostar` and
  `<UseTilt>` in the tilt-series XML; previous exclusions are restored
  automatically on next load
- **Scrollable tilt series list** — switch between datasets with a click
- **Per-tilt metadata** — CTF fit (Å), defocus (µm), and motion (Å) from
  WarpTools per-frame XML

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/jjenkins01/warptools_visualiser.git
cd warptools_visualiser
```

### 2. Create the conda environment

Using the provided `environment.yml`:

```bash
conda env create -f environment.yml
conda activate warp_tools_visualiser
```

Or manually:

```bash
conda create -n warp_tools_visualiser \
    python=3.11 pyqt numpy mrcfile matplotlib \
    -c conda-forge -y
conda activate warp_tools_visualiser
```

### 3. Install the `warptools_visualiser` command (recommended)

Installing the package with `pip` registers a `warptools_visualiser` command
so you can launch it from anywhere without typing `python` or the full path:

```bash
pip install -e .
```

The `-e` (editable) flag means `git pull` updates take effect immediately
without reinstalling. After this you can run:

```bash
warptools_visualiser --tomostar_dir $WARP --stack_dir $warp_ts ...
```

> If you prefer not to install, you can always run the script directly with
> `python warptools_visualiser.py --tomostar_dir $WARP ...`

### 4. Verify the installation

```bash
python -c "from PyQt5.QtWidgets import QApplication; print('PyQt5 OK')"
warptools_visualiser --help
```

---

## Updating

If you already have a previous version installed and want the latest release
from GitHub:

### If you installed with `pip install -e .` (editable mode)

This is the simplest case — the editable install points directly at your
local clone, so a `git pull` is all that is needed. The `warptools_visualiser`
command picks up the new code automatically.

```bash
cd warptools_visualiser     # your local clone
git pull origin main
```

> `git` does not require the conda environment to be active. You only need the
> environment active when you actually run `warptools_visualiser`.

### If you installed with a regular `pip install .` (non-editable)

A plain install copies the code into the environment, so after pulling you
must reinstall for the changes to take effect:

```bash
cd warptools_visualiser
git pull origin main
conda activate warp_tools_visualiser
pip install . --upgrade
```

### If you only downloaded the script (no pip install)

Replace your local `warptools_visualiser.py` with the latest version from the
repository, or re-clone:

```bash
cd warptools_visualiser
git pull origin main
```

then run it directly with `python warptools_visualiser.py ...`.

### If the dependencies changed

The conda environment only needs to be recreated if `environment.yml` has
changed (rare). If a new release notes a dependency change, update with:

```bash
conda env update -f environment.yml --prune
```

### Checking your version

```bash
cd warptools_visualiser
git log --oneline -1        # shows the latest commit you have
git tag --points-at HEAD    # shows the release tag, if any
```

Compare against the [releases page](https://github.com/jjenkins01/warptools_visualiser/releases)
to see whether a newer version is available.

---

## Requirements

- Linux with X11 display (local or SSH with `-X` / `-Y` forwarding)
- Conda / Mamba (Miniforge recommended)
- WarpTools preprocessing already run — the visualiser reads its output
  files directly

---

## Directory layout

The visualiser expects standard WarpTools output structure, here's an example with a single tomogram called tomogram01:

```bash
warp_frameseries                                 Frame-series processing dir
├── tomogram01.tomostar                          Tilt series metadata (this can also be in a separate directory if you like)
├── tomogram01_001_*_Fractions.xml               Per-frame CTF / motion XML
├── powerspectrum/
│   └── tomogram01_001_*_Fractions.mrc           Power spectrum per tilt
└── average/
    └── tomogram01_001_*_Fractions_motion.json   Motion tracks per tilt

warp_tiltseries                                  Tilt-series processing dir
├── warp_tiltseries.settings                     WarpTools settings file
├── tomogram01.xml                               Tilt-series XML (<UseTilt>)
└── tiltstack/
    └── tomogram01/
        └── tomogram01.st                        Tilt series stack
```

Setting shell variables beforehand can help to speed up commands but not essential:

```bash
warp_fs=/path/to/warp_frameseries
warp_tomostar=/path/to/tomostar_dir
warp_ts=/path/to/warp_tiltseries
```

---

## Usage

### Batch mode — all tilt series in a directory

```bash
conda activate warp_tools_visualiser

warptools_visualiser \
    --tomostar_dir $WARP    \
    --stack_dir    $warp_ts \
    --frame_dir    $WARP    \
    --xml_dir      $warp_ts
```

### Single tilt series

```bash
warptools_visualiser \
    --stack     $warp_ts/tiltstack/Position_28/Position_28.st \
    --tomostar  $WARP/Position_28.tomostar \
    --frame_dir $WARP \
    --xml       $warp_ts/Position_28.xml
```

### All arguments

| Argument | Description |
|---|---|
| `--tomostar_dir DIR` | Directory containing `.tomostar` files — typically `$WARP` |
| `--stack_dir DIR` | Directory containing `tiltstack/` subdirs — typically `$warp_ts` |
| `--frame_dir DIR` | Frame-series dir (`$WARP`) — per-frame XMLs, `powerspectrum/`, `average/` |
| `--xml_dir DIR` | Directory containing tilt-series XML files — typically `$warp_ts` |
| `--stack ST` | Single tilt series stack (`.st` or `.mrc`) — single-file mode |
| `--tomostar STAR` | Tomostar file — required with `--stack` |
| `--xml XML` | Tilt-series XML — optional with `--stack`, auto-detected if omitted |
| `--sigma FLOAT` | Sigma for auto-flagging intensity outliers (default: 3.0) |
| `--contrast_lo INT` | Lower percentile for image contrast (default: 2) |
| `--contrast_hi INT` | Upper percentile for image contrast (default: 98) |

---

## Interface

```
┌──────────────────────────┬─────────────────────────┬──────────────────┐
│                          │                         │  Tilt Series     │
│   Tilt Image             │   Power Spectrum        │  ─────────────   │
│   (+ motion overlay)     │   (2:1 aspect ratio)    │  [*] Position_28 │
│                          │                         │  [ ] Position_29 │
│                          │                         │  ...             │
├──────────────────────────┴─────────────────────────┤                  │
│   Overview bar  (click to jump to tilt)             │                  │
├─────────────────────────────────────────────────────┴──────────────────┤
│   CTF: X.X Å  |  Defocus: X.XXX µm  |  Motion: X.XX Å  |  Series: …  │
│                        Tilt N/61   ±XX.XX°                              │
├─────────────────────────────────────────────────────────────────────────┤
│  < Prev  > Next  Exclude [Ctrl+E]  All On  Save  Next Series  Quit+Save │
│  [✓] Motion Overlay [Ctrl+M]                                            │
└─────────────────────────────────────────────────────────────────────────┘
```

### Tilt image panel

Displays the motion-corrected average for the current tilt. When a tilt is
excluded a red overlay appears with a "Bad frame — excluded" text label.

**Motion overlay** — when enabled, draws each motion-correction patch
trajectory at its spatial position on the image. A faint grid shows the
patch boundaries. Tracks are colour-coded by arc-length:

| Colour | Motion |
|---|---|
| Green | Low |
| Yellow | Medium |
| Red / orange | High |

Toggle with the **Motion Overlay** checkbox or `Ctrl+M`.

Two additional controls refine the motion display:

- **Local only** — subtracts the global mean trajectory (averaged across all
  patches) from each patch, leaving only the local, non-global component of
  the motion. This matches the "only local motion" option in the Warp GUI and
  is useful for spotting localised beam-induced movement.
- **Scale** — magnifies the drawn tracks (1×–100×) so small displacements are
  easier to see, without changing the underlying data.

### Power spectrum panel

Displays the CTF power spectrum from `powerspectrum/` with square-root
scaling. The 2:1 aspect ratio is preserved — WarpTools stores only the
non-redundant half of the Fourier transform, so rings always appear as
semicircles.

### Overview bar

One coloured bar per tilt. **Click any bar to jump directly to that tilt.**
Colour coding (priority order):

| Colour | Meaning |
|---|---|
| Red | Excluded |
| Orange | Auto-flagged (intensity outlier, ±3σ from mean) |
| Purple | CTF fit > 10 Å |
| Amber | CTF fit 8–10 Å |
| Green | CTF fit ≤ 8 Å |

### Tilt series list

Lists all tilt series found in the processing directory. Click a name to
switch to it. Scroll with the mouse wheel.

---

## Keyboard shortcuts

| Key | Action |
|---|---|
| `←` / `→` | Previous / next tilt |
| `Ctrl+E` | Toggle exclude on current tilt |
| `Ctrl+M` | Toggle motion overlay |
| `Ctrl+S` | Save exclusions for current series |
| `Ctrl+N` | Move to next series |
| `Ctrl+Q` | Save and quit |
| `Ctrl+R` | Reset — mark all tilts as included |

> **Why Ctrl+E and not just E?** The tilt series list widget consumes
> single-letter keypresses for its built-in search, so bare `E` never
> reaches the window's key handler. `Ctrl+<letter>` combinations bypass
> this.

---

## What gets saved

When you press **Save** or **Quit+Save** two files are updated per series:

**`.tomostar`** — excluded tilt rows are removed. WarpTools reads this for
all downstream processing (`ts_stack`, `ts_ctf`, `ts_reconstruct`), so
excluded tilts are automatically skipped.

**Tilt-series XML `<UseTilt>`** — set to `False` for excluded tilts,
keeping the WarpTools processing state consistent.

Both files receive a timestamped backup before writing:

```
Position_28.tomostar.backup_20260527_103042
Position_28.xml.backup_20260527_103042
```

**Previous exclusions are restored automatically** — the `<UseTilt>` field
is read from the XML every time a series is loaded.

---

## Troubleshooting

**`No module named 'PyQt5'`**
Recreate the environment following the installation steps above.

**Window is blank / no display**
Verify X11 forwarding is active:
```bash
echo $DISPLAY    # should show e.g. localhost:10.0
```
Reconnect with `ssh -X` or `ssh -Y` if empty.

**Power spectrum shows "not found"**
Check that `--frame_dir` points to the directory containing the
`powerspectrum/` subdirectory.

**Motion overlay not appearing**
Motion JSON files are searched in `$WARP/` and `$WARP/average/`. Locate
them with:
```bash
find $WARP -name "*_motion.json" | head -3
```

**Some motion files missing (e.g. 57/61)**
Tilts that failed `fs_motion_and_ctf` have no motion file. This is
expected; those tilts show no overlay and are good candidates for exclusion.

**Qt GLX / session manager warnings**
Harmless X11 messages, suppressed automatically by the script.

---

## Acknowledgements

This tool was written by **Joshua Jenkins** with
assistance from **[Claude](https://claude.ai)** (Anthropic) as part of a
cryoET subtomogram averaging pipeline integrating WarpTools, MissAlignment,
RELION 5, and MTools.

---

## Licence

MIT Licence — see `LICENSE` for details.
