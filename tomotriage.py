#!/usr/bin/env python3
"""
TomoTriage
==========
PyQt5-based interactive viewer for WarpTools tilt series quality control.

Layout
------
  Left   : tilt image (from average/) with optional motion track overlay
  Right  : power spectrum (aspect-correct, 2:1 for half-Fourier images)
  Far right: scrollable tilt series list
  Bottom : overview bar (click to jump; CTF-colour-coded)
  Info   : CTF fit, defocus, motion per tilt from per-frame XML

Images are loaded from the per-tilt motion-corrected averages in
<frame_dir>/average/ (matched to the tomostar by movie name), NOT from a
.st stack. This means every acquired tilt is always shown — including ones
that have been excluded — so reopening a previously-edited dataset displays
the excluded tilts in red.

Motion tracks are drawn spatially — each patch at its correct grid position
on the image — and colour-coded by arc-length (green=low, red=high). Toggle
the overlay with the checkbox in the button bar. "Local only" subtracts the
global mean trajectory to show only local (non-global) motion, and the Scale
dropdown magnifies the tracks for easier inspection.

Exclusions are written to <UseTilt> in the tilt-series XML (mapped by tilt
angle). The .tomostar is never modified. Previous exclusions are restored
from the XML on load.

Requires
--------
  conda install pyqt numpy mrcfile matplotlib -c conda-forge

Usage
-----
  # Batch mode (all .tomostar in a directory). Images come from --frame_dir/average/
  tomotriage \\
      --tomostar_dir $warp_fs \\
      --frame_dir    $warp_fs \\
      --xml_dir      $warp_ts

  # Single series
  tomotriage \\
      --tomostar $warp_fs/Position_1.tomostar \\
      --frame_dir $warp_fs \\
      --xml $warp_ts/Position_1.xml

Keyboard shortcuts
------------------
  Left / Right   navigate tilts
  Ctrl+E         toggle exclude
  Ctrl+S         save
  Ctrl+N         next series
  Ctrl+Q         save + quit
  Ctrl+R         reset (include all)
  Ctrl+M         toggle motion overlay
"""

import os, sys, glob, shutil, argparse, json
import xml.etree.ElementTree as ET
from datetime import datetime

os.environ.setdefault('QT_LOGGING_RULES',
                      'qt.glx.warning=false;qt.qpa.xcb.warning=false')
os.environ.setdefault('SESSION_MANAGER', '')

import numpy as np
import mrcfile

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QListWidget,
    QPushButton, QCheckBox, QComboBox, QHBoxLayout, QVBoxLayout,
    QSizePolicy, QSplitter, QStatusBar,
    QDialog, QGridLayout, QColorDialog, QMessageBox, QSplashScreen
)
from PyQt5.QtGui import (
    QImage, QPixmap, QColor, QPainter, QPen, QPainterPath,
    QFont, QPalette
)
from PyQt5.QtCore import Qt, QSize, QPointF, pyqtSignal, QTimer, QThread

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

C_BG      = '#1a1a2e'
C_PANEL   = '#16213e'
C_ACCENT  = '#0f3460'
C_HOVER   = '#1e4a7a'
C_GREEN   = '#4ade80'
C_RED     = '#f87171'
C_YELLOW  = '#fbbf24'
C_ORANGE  = '#fb923c'
C_PURPLE  = '#a855f7'
C_TEXT    = '#e2e8f0'
C_DIM     = '#64748b'


class CategoryColors:
    """
    Mutable colour theme for the overview-bar tilt categories. Session-only —
    edited live via the in-app colour picker, not persisted between launches.
    Keys map to the five categories used by the overview bar and bulk-exclude.
    """
    LABELS = {
        'excluded': 'Excluded',
        'flagged':  'Auto-flagged (intensity outlier)',
        'ctf_bad':  'CTF fit > 10 \u00c5',
        'ctf_mod':  'CTF fit 8\u201310 \u00c5',
        'good':     'CTF fit \u2264 8 \u00c5 (good)',
    }
    # display order for the picker dialog
    ORDER = ['excluded', 'flagged', 'ctf_bad', 'ctf_mod', 'good']

    def __init__(self):
        self.colors = {
            'excluded': C_RED,
            'flagged':  C_ORANGE,
            'ctf_bad':  C_PURPLE,
            'ctf_mod':  C_YELLOW,
            'good':     C_GREEN,
        }

    def __getitem__(self, key):
        return self.colors[key]

    def set(self, key, hexcolor):
        if key in self.colors:
            self.colors[key] = hexcolor

# ---------------------------------------------------------------------------
# Sound
# ---------------------------------------------------------------------------

def _play_exclude_sound():
    try:
        import wave, struct, math, tempfile, subprocess
        sr = 44100; dur = 0.18
        frames = []
        for i in range(int(sr * dur)):
            t = i / sr
            f = 500 * (1 - t / dur * 0.75)
            v = 0.25 * max(0, 1 - t / dur)
            frames.append(struct.pack('<h', int(v * 32767 *
                           math.sin(2 * math.pi * f * t))))
        tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        tmp.close()
        with wave.open(tmp.name, 'w') as wf:
            wf.setnchannels(1); wf.setsampwidth(2)
            wf.setframerate(sr); wf.writeframes(b''.join(frames))
        subprocess.Popen(['aplay', '-q', tmp.name],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Motion track colour helper  (green → yellow → red by arc-length)
# ---------------------------------------------------------------------------

def _motion_color(norm):
    """Map normalised arc-length [0,1] to QColor: green→yellow→red."""
    green  = (74,  222, 128)
    yellow = (251, 191,  36)
    red    = (248, 113, 113)
    if norm <= 0.5:
        t = norm * 2
        r, g, b = [int(green[i] + t * (yellow[i] - green[i])) for i in range(3)]
    else:
        t = (norm - 0.5) * 2
        r, g, b = [int(yellow[i] + t * (red[i] - yellow[i])) for i in range(3)]
    return QColor(r, g, b, 210)

# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------

def parse_tomostar(path):
    col_names, rows = [], []
    in_loop = in_cols = in_data = False
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                if in_data: break
                continue
            if line.startswith('loop_'):
                in_loop = True; in_cols = True; in_data = False; continue
            if in_loop and in_cols and line.startswith('_'):
                col_names.append(line.split()[0]); continue
            if in_loop and col_names and not line.startswith('_'):
                in_cols = False; in_data = True
            if in_data and line:
                rows.append(line.split())
    return col_names, rows


def _read_xml_angle_list(root, tag):
    """Return list of float values from a newline-separated XML element."""
    node = root.find('.//' + tag)
    if node is None or not node.text:
        return []
    out = []
    for v in node.text.split('\n'):
        v = v.strip()
        if v:
            try:
                out.append(float(v))
            except ValueError:
                pass
    return out


def _angle_key(angle):
    """Round an angle to 0.1 deg for robust matching between files."""
    return round(float(angle), 1)


def _estimate_angle_offset(stack_angles, xml_angles):
    """
    Estimate a constant offset such that xml_angle ~= stack_angle + offset.

    miss-alignment can shift every tilt angle by a constant (a refined
    stage/axis offset), so the XML's angles no longer equal the tomostar's
    nominal angles. This is robust to one list being a subset of the other and
    to large offsets (bigger than the tilt spacing): the true offset is the
    pairwise (xml - stack) difference shared by the most tilt pairs (a simple
    voting/Hough scheme). Returns the offset in degrees, or 0.0 if unknown.
    """
    if not stack_angles or not xml_angles:
        return 0.0
    votes = {}
    for sa in stack_angles:
        for xa in xml_angles:
            d = round(xa - sa, 1)
            votes[d] = votes.get(d, 0) + 1
    if not votes:
        return 0.0
    return max(votes.items(), key=lambda kv: kv[1])[0]


def update_xml_usetilt(xml_path, excluded, tilt_angles=None):
    """
    Write exclusion state to <UseTilt> in the tilt-series XML.

    The XML's <UseTilt>/<Angles> are ordered by tilt angle and may contain
    MORE entries than the (possibly reduced) tilt stack. The `excluded` list
    is indexed by STACK position, so we map between the two by tilt angle
    using `tilt_angles` (the per-stack-tilt angles, same order as `excluded`).

    XML angles with no matching stack tilt keep their existing UseTilt value.
    If `tilt_angles` is None we fall back to positional mapping (legacy).
    """
    if not xml_path or not os.path.exists(xml_path):
        print(f"  [WARN] XML not found: {xml_path}"); return
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        node = root.find('.//UseTilt')
        if node is None:
            print(f"  [WARN] No <UseTilt> in {xml_path}"); return
        existing = [v.strip() for v in (node.text or '').split('\n') if v.strip()]
        xml_angles = _read_xml_angle_list(root, 'Angles')

        if tilt_angles is not None and xml_angles and \
                len(xml_angles) == len(existing) == len(excluded):
            # Rank-based mapping (robust to a constant angle offset applied by
            # miss-alignment). Both the stack tilts and the XML entries are
            # sorted by angle; the k-th angle-sorted stack tilt corresponds to
            # the k-th angle-sorted XML entry. A constant offset is monotonic,
            # so it preserves this ordering.
            stack_order = sorted(range(len(excluded)),
                                 key=lambda i: tilt_angles[i])
            xml_order = sorted(range(len(xml_angles)),
                               key=lambda j: xml_angles[j])
            updated = list(existing)
            for rank, stack_idx in enumerate(stack_order):
                xml_pos = xml_order[rank]
                updated[xml_pos] = 'False' if excluded[stack_idx] else 'True'
        elif tilt_angles is not None and xml_angles:
            # Fallback for a reduced stack (fewer tilts than the XML template):
            # match by angle, compensating for any constant offset.
            offset = _estimate_angle_offset(list(tilt_angles), xml_angles)
            excl_by_angle = {}
            for i, ang in enumerate(tilt_angles):
                if i < len(excluded):
                    excl_by_angle[_angle_key(ang + offset)] = excluded[i]
            updated = []
            for j, ang in enumerate(xml_angles):
                key = _angle_key(ang)
                if key in excl_by_angle:
                    updated.append('False' if excl_by_angle[key] else 'True')
                else:
                    updated.append(existing[j] if j < len(existing) else 'True')
        else:
            # Legacy positional mapping (orderings assumed identical)
            updated = []
            for i in range(max(len(existing), len(excluded))):
                if i < len(excluded) and excluded[i]: updated.append('False')
                elif i < len(existing):              updated.append(existing[i])
                else:                                updated.append('True')

        # WarpTools format: first value immediately after <UseTilt>, values
        # separated by newlines, last value immediately before </UseTilt>.
        # No leading/trailing newline and NO ET.indent() reformatting, both of
        # which break WarpTools' parser (ts_stack fails with "valid path
        # needed for each tilt").
        node.text = '\n'.join(updated)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        # Back up the current on-disk XML into an xml_original_backups/ subdir
        # alongside the XML, rather than cluttering the XML directory itself.
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(xml_path)),
                                  'xml_original_backups')
        os.makedirs(backup_dir, exist_ok=True)
        backup_name = os.path.basename(xml_path) + f'.backup_{ts}'
        shutil.copy2(xml_path, os.path.join(backup_dir, backup_name))
        xml_string = ET.tostring(root, encoding='unicode')
        with open(xml_path, 'w', encoding='utf-8') as f:
            f.write('<?xml version="1.0" encoding="utf-8"?>\n')
            f.write(xml_string)
        print(f"  XML updated: {xml_path}")
        print(f"  Backup saved: {os.path.join(backup_dir, backup_name)}")
    except Exception as e:
        print(f"  [ERROR] XML: {e}")


def read_usetilt_from_xml(xml_path, n, tilt_angles=None):
    """
    Read exclusion state from <UseTilt>, returning a list of length `n`
    indexed by STACK position (True = excluded).

    The XML is ordered by tilt angle and may have more entries than the stack.
    If `tilt_angles` (per-stack-tilt angles, same order as the returned list)
    is given, we map by angle. Otherwise we fall back to positional mapping.
    """
    excluded = [False] * n
    if not xml_path or not os.path.exists(xml_path): return excluded
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        node = root.find('.//UseTilt')
        # NB: an ElementTree element with no children is falsy, so we must
        # test 'is not None' rather than a plain truthiness check here.
        if node is None or not node.text:
            return excluded
        vals = [v.strip() for v in node.text.split('\n') if v.strip()]
        xml_angles = _read_xml_angle_list(root, 'Angles')

        if tilt_angles is not None and xml_angles and \
                len(xml_angles) == len(vals) == n:
            # Rank-based mapping (robust to a constant angle offset from
            # miss-alignment): pair the k-th angle-sorted stack tilt with the
            # k-th angle-sorted XML entry.
            stack_order = sorted(range(n), key=lambda i: tilt_angles[i])
            xml_order = sorted(range(len(xml_angles)),
                               key=lambda j: xml_angles[j])
            for rank, stack_idx in enumerate(stack_order):
                xml_pos = xml_order[rank]
                excluded[stack_idx] = (vals[xml_pos].lower() == 'false')
        elif tilt_angles is not None and xml_angles:
            # Fallback for a reduced stack: offset-compensated angle matching.
            offset = _estimate_angle_offset(list(tilt_angles[:n]), xml_angles)
            excl_by_angle = {}
            for ang, v in zip(xml_angles, vals):
                # shift XML angles back into the stack's frame
                excl_by_angle[_angle_key(ang - offset)] = (v.lower() == 'false')
            for i, ang in enumerate(tilt_angles[:n]):
                key = _angle_key(ang)
                if key in excl_by_angle:
                    excluded[i] = excl_by_angle[key]
        else:
            # Legacy positional mapping
            for i, v in enumerate(vals[:n]):
                excluded[i] = v.lower() == 'false'
    except Exception: pass
    return excluded


def _parse_freq_value_list(text):
    """
    Parse WarpTools' 'freq|value;freq|value;...' encoding into two numpy
    arrays (frequencies, values). Returns (None, None) on empty/missing.
    """
    if not text:
        return None, None
    freqs, vals = [], []
    for pair in text.strip().split(';'):
        if '|' not in pair:
            continue
        f, v = pair.split('|', 1)
        try:
            freqs.append(float(f)); vals.append(float(v))
        except ValueError:
            continue
    if not freqs:
        return None, None
    return np.array(freqs), np.array(vals)


def read_frame_xml(xml_path):
    """
    Read per-tilt metadata and CTF curve data from a WarpTools frame XML.

    Returns a dict with:
      ctf_res, defocus, motion          -- scalar summaries (as before)
      ps1d_freq, ps1d_val               -- experimental 1D power spectrum
      bg_freq, bg_val                   -- simulated background
      scale_freq, scale_val             -- simulated scale envelope
      ctf_params                        -- dict of CTF fit parameters
    """
    meta = {'ctf_res': None, 'defocus': None, 'motion': None,
            'ps1d_freq': None, 'ps1d_val': None,
            'bg_freq': None, 'bg_val': None,
            'scale_freq': None, 'scale_val': None,
            'ctf_params': None}
    if not xml_path or not os.path.exists(xml_path): return meta
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        ctf = root.attrib.get('CTFResolutionEstimate')
        mot = root.attrib.get('MeanFrameMovement')
        if ctf:
            v = float(ctf)
            if v > 0: meta['ctf_res'] = v
        if mot:
            v = float(mot)
            if v > 0: meta['motion'] = v
        nodes = root.findall('.//GridCTF/Node')
        if nodes:
            vals = [float(nd.attrib['Value']) for nd in nodes if 'Value' in nd.attrib]
            if vals: meta['defocus'] = float(np.mean(vals))

        # 1D CTF curve data
        ps = root.find('.//PS1D')
        if ps is not None:
            meta['ps1d_freq'], meta['ps1d_val'] = _parse_freq_value_list(ps.text)
        bg = root.find('.//SimulatedBackground')
        if bg is not None:
            meta['bg_freq'], meta['bg_val'] = _parse_freq_value_list(bg.text)
        sc = root.find('.//SimulatedScale')
        if sc is not None:
            meta['scale_freq'], meta['scale_val'] = _parse_freq_value_list(sc.text)

        # CTF fit parameters (from the <CTF> block)
        ctf_node = root.find('.//CTF')
        if ctf_node is not None:
            params = {}
            for p in ctf_node.findall('Param'):
                name = p.attrib.get('Name'); val = p.attrib.get('Value')
                if name is None or val is None:
                    continue
                try:
                    params[name] = float(val)
                except ValueError:
                    params[name] = val
            meta['ctf_params'] = params
    except Exception: pass
    return meta


# ---------------------------------------------------------------------------
# Post-alignment metrics: alignment loss + tilt-series CTF/motion, and ranking
# ---------------------------------------------------------------------------

def read_alignment_loss(loss_dir, series_name):
    """
    Read a miss-alignment '<series>_alignment_loss.json' and return its
    final_loss (lower = better), or None if unavailable. miss-alignment writes
    one JSON per tilt series; final_loss is a precision-weighted model score
    from the alignment optimisation.
    """
    if not loss_dir or not os.path.isdir(loss_dir):
        return None
    # miss-alignment names the file '<series>_alignment_loss.json'
    candidates = [
        os.path.join(loss_dir, f"{series_name}_alignment_loss.json"),
        os.path.join(loss_dir, f"{series_name}.json"),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                fl = data.get('final_loss')
                return float(fl) if fl is not None else None
            except Exception:
                return None
    return None


def read_tiltseries_ctf_motion(xml_path):
    """
    Read series-level CTF resolution and a motion summary from a WarpTools
    *tilt-series* XML (the post-alignment / post-ts_ctf file). Returns
    (ctf_res, motion) with either possibly None.

    CTF: the root 'CTFResolutionEstimate' attribute (updated by ts_ctf).
    Motion: mean absolute local motion from GridMovementX/Y if present, else
    None. This is a summary for ranking, not the per-tilt overlay data.
    """
    ctf_res = None
    motion = None
    if not xml_path or not os.path.exists(xml_path):
        return ctf_res, motion
    try:
        root = ET.parse(xml_path).getroot()
        c = root.attrib.get('CTFResolutionEstimate')
        if c:
            v = float(c)
            if v > 0:
                ctf_res = v
        gx = root.find('.//GridMovementX')
        gy = root.find('.//GridMovementY')
        if gx is not None and gy is not None:
            xs = [float(n.attrib['Value']) for n in gx.findall('Node')
                  if 'Value' in n.attrib]
            ys = [float(n.attrib['Value']) for n in gy.findall('Node')
                  if 'Value' in n.attrib]
            if xs and ys:
                mags = [np.hypot(x, y) for x, y in zip(xs, ys)]
                motion = float(np.mean(mags))
    except Exception:
        pass
    return ctf_res, motion


def _ranks_from_values(values):
    """
    Given a list of per-series values (lower = better, None = missing), return
    a list of 1-based ranks (1 = best). Series with a missing value get None.
    Ties share the average of the ranks they span.
    """
    idx_vals = [(i, v) for i, v in enumerate(values) if v is not None]
    ranks = [None] * len(values)
    if not idx_vals:
        return ranks
    # sort by value ascending (lower is better)
    idx_vals.sort(key=lambda t: t[1])
    # assign ranks, averaging ties
    i = 0
    n = len(idx_vals)
    while i < n:
        j = i
        while j + 1 < n and idx_vals[j + 1][1] == idx_vals[i][1]:
            j += 1
        # positions i..j are tied; ranks are (i+1)..(j+1) -> average
        avg_rank = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[idx_vals[k][0]] = avg_rank
        i = j + 1
    return ranks


def compute_series_ranking(metrics):
    """
    Combine per-series metrics into an overall ranking (1 = best).

    metrics: list of dicts, one per series, each with keys:
      'ctf'    : CTF resolution (A, lower better) or None
      'motion' : motion summary (A, lower better) or None
      'loss'   : alignment final_loss (lower better) or None

    Auto-detects stage: if ANY series has a loss value, loss is included as a
    third ranking component (post-alignment); otherwise ranking uses CTF +
    motion only (pre-alignment).

    Returns (overall_rank, used_loss) where overall_rank is a list of 1-based
    integer ranks (1 = best) aligned with the input, and used_loss is True if
    the loss component was included. Series missing all components rank last.
    """
    n = len(metrics)
    ctf_vals = [m.get('ctf') for m in metrics]
    mot_vals = [m.get('motion') for m in metrics]
    loss_vals = [m.get('loss') for m in metrics]

    used_loss = any(v is not None for v in loss_vals)

    ctf_ranks = _ranks_from_values(ctf_vals)
    mot_ranks = _ranks_from_values(mot_vals)
    loss_ranks = _ranks_from_values(loss_vals) if used_loss else [None] * n

    # Average the available component ranks per series
    composite = []
    for i in range(n):
        comps = [ctf_ranks[i], mot_ranks[i]]
        if used_loss:
            comps.append(loss_ranks[i])
        present = [c for c in comps if c is not None]
        composite.append(sum(present) / len(present) if present else None)

    # Final 1..N ranking from the composite (lower composite = better)
    overall = _ranks_from_values(composite)
    # Convert to clean integer ranks by ordering; missing -> last
    order = sorted(range(n),
                   key=lambda i: (overall[i] is None, overall[i] if overall[i] is not None else 0))
    final = [None] * n
    for pos, i in enumerate(order, start=1):
        final[i] = pos
    return final, used_loss


def compute_ctf_fit_curves(meta):
    """
    Reconstruct the Warp CTF fit display, following Warp's own fitting
    convention (Tegunov & Cramer 2019, and the Warp source pseudocode):

      Background = spline through the CTF zeros of the experimental spectrum
      Envelope   = spline through the CTF peaks of (experimental - background)
      experimental displayed = PS1D - Background        (envelopes naturally)
      fitted displayed       = CTF^2 * Envelope         (shares the envelope)

    Both curves therefore decay together with matching amplitude, exactly as
    shown in the Warp GUI. The full frequency range from the XML is returned
    (no cropping); callers scale the y-axis robustly so the low-frequency
    rolloff spike does not dominate. Returns (s_inv_angstrom, experimental,
    fitted) or (None, None, None).
    """
    freq = meta.get('ps1d_freq'); ps = meta.get('ps1d_val')
    if freq is None or ps is None:
        return None, None, None
    params = meta.get('ctf_params') or {}
    try:
        defocus = float(params.get('Defocus', 0.0)) * 1e4      # µm -> Å
        cs      = float(params.get('Cs', 2.7)) * 1e7           # mm -> Å
        volt    = float(params.get('Voltage', 300.0)) * 1e3    # kV -> V
        amp     = float(params.get('Amplitude', 0.1))
        phase   = float(params.get('PhaseShift', 0.0))
        pixel   = float(params.get('PixelSize', 1.0))          # Å/px
    except (TypeError, ValueError):
        return None, None, None

    # Electron wavelength (Å), relativistic
    lam = 12.2639 / np.sqrt(volt + 0.97845e-6 * volt * volt)

    # Spatial frequency in 1/Å (PS1D freq is cycles/pixel)
    s = freq / max(pixel, 1e-6)

    # Analytical CTF^2
    gamma = (np.pi * lam * s**2 * defocus
             - 0.5 * np.pi * lam**3 * s**4 * cs)
    ctf2 = np.sin(gamma + phase + np.arcsin(np.clip(amp, 0, 1)))**2

    # Experimental = PS1D - background (NOT divided by the envelope, so the
    # natural amplitude decay is preserved and the curve envelopes)
    exp = ps.copy()
    bgf, bgv = meta.get('bg_freq'), meta.get('bg_val')
    if bgf is not None and bgv is not None and not np.allclose(bgv, 0):
        exp = exp - np.interp(freq, bgf, bgv)

    # Fitted = CTF^2 * envelope (the same envelope Warp fits to the
    # experimental peaks), so both curves share the same decay
    fitted = ctf2
    scf, scv = meta.get('scale_freq'), meta.get('scale_val')
    if scf is not None and scv is not None:
        fitted = ctf2 * np.interp(freq, scf, scv)

    return s, exp, fitted


def load_motion_json(json_path):
    if not json_path or not os.path.exists(json_path): return None
    try:
        with open(json_path) as f: return json.load(f)
    except Exception: return None


def get_movie_names(col_names, rows):
    try:
        idx = next(i for i, c in enumerate(col_names)
                   if 'MovieName' in c or 'Name' in c)
        return [os.path.basename(r[idx]) for r in rows]
    except StopIteration:
        return [str(i) for i in range(len(rows))]


def get_tilt_angles(col_names, rows):
    try:
        idx = next(i for i, c in enumerate(col_names) if 'AngleTilt' in c)
        return [float(r[idx]) for r in rows]
    except StopIteration:
        return list(range(len(rows)))


def load_mrc_image(path):
    if not path or not os.path.exists(path): return None
    try:
        with mrcfile.open(path, mode='r', permissive=True) as m:
            return m.data.astype(np.float32).squeeze()
    except Exception: return None


def auto_flag_candidates_from_paths(paths, sigma=3.0):
    """
    Flag intensity-outlier tilts given a list of per-tilt image paths.
    Loads each image once to compute its mean; missing files are skipped
    (never flagged). Returns list[bool] aligned with `paths`.
    """
    n = len(paths)
    means = np.full(n, np.nan, dtype=np.float64)
    for i, p in enumerate(paths):
        img = load_mrc_image(p)
        if img is not None:
            means[i] = float(img.mean())
    valid = ~np.isnan(means)
    if valid.sum() < 2:
        return [False] * n
    mu = means[valid].mean()
    sd = means[valid].std()
    flagged = [False] * n
    if sd == 0:
        return flagged
    for i in range(n):
        if valid[i] and abs(means[i] - mu) / sd > sigma:
            flagged[i] = True
    return flagged


def resolve_average_paths(frame_dir, movies):
    """
    Given the frame-series dir and the list of movie names from a tomostar,
    return a list of paths to the per-tilt averaged .mrc images in
    <frame_dir>/average/. Each tomostar _wrpMovieName matches an average
    filename exactly. Missing files (e.g. tilts that failed motion correction)
    are returned as None so they can be shown as placeholders.

    Robust to frame_dir pointing either at the frame-series dir (containing
    average/) OR directly at the average/ dir itself.
    """
    avg_dir = os.path.join(frame_dir, 'average')
    if not os.path.isdir(avg_dir):
        # frame_dir may already BE the average directory
        avg_dir = frame_dir
    paths = []
    for mv in movies:
        cand = os.path.join(avg_dir, mv)
        paths.append(cand if os.path.exists(cand) else None)
    return paths


def find_tilt_series(tomostar_dir, frame_dir, xml_dir=None):
    """
    Discover tilt series for batch mode.

    Images are taken from <frame_dir>/average/ (the per-tilt motion-corrected
    averages), NOT from a .st stack — this means every acquired tilt is always
    available for display, including ones that have been excluded, so reopening
    a previously-edited dataset shows excluded tilts in red.

    Returns a list of (tomostar_path, xml_path) tuples.
    """
    pairs = []
    for ts_path in sorted(glob.glob(os.path.join(tomostar_dir, '*.tomostar'))):
        name = os.path.basename(os.path.splitext(ts_path)[0])
        xml_path = None
        for xd in ([xml_dir] if xml_dir else []) + [os.path.dirname(ts_path)]:
            c = os.path.join(xd, name + '.xml')
            if os.path.exists(c): xml_path = c; break
        pairs.append((ts_path, xml_path))
    return pairs

# ---------------------------------------------------------------------------
# Image display widget with motion overlay
# ---------------------------------------------------------------------------

class ImageLabel(QLabel):
    """
    QLabel that displays a numpy array at correct aspect ratio.
    Supports:
      - Red exclusion overlay with text
      - Spatial motion track overlay drawn with QPainter
      - Mouse-wheel scrolling to navigate tilts (emits wheel_scrolled)
    """

    # +1 for scroll up (towards next), -1 for scroll down (towards previous)
    wheel_scrolled = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw_pixmap   = None
        self._excluded     = False
        self._candidate    = False
        self._motion_data  = None
        self._show_motion  = True
        self._local_motion = False   # subtract mean shift (show only local)
        self._motion_scale = 1.0     # display magnification of tracks
        self.setMinimumSize(200, 150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(
            f"background-color: {C_PANEL}; border: 1px solid #334155;")

    def wheelEvent(self, event):
        # Scroll over the tilt image to step through tilts. angleDelta is in
        # eighths of a degree; sign gives direction. Scroll up -> next tilt.
        dy = event.angleDelta().y()
        if dy != 0:
            self.wheel_scrolled.emit(1 if dy > 0 else -1)
            event.accept()
        else:
            super().wheelEvent(event)

    # ── Public API ─────────────────────────────────────────────────────────

    def set_array(self, array, contrast_lo=2, contrast_hi=98,
                  excluded=False, candidate=False, motion_data=None):
        self._excluded    = excluded
        self._candidate   = candidate
        self._motion_data = motion_data
        if array is None:
            self._raw_pixmap = None; self.clear(); return
        lo  = float(np.percentile(array, contrast_lo))
        hi  = float(np.percentile(array, contrast_hi))
        eps = max(hi - lo, 1e-6)
        gray8 = (np.clip((array - lo) / eps, 0, 1) * 255).astype(np.uint8)
        h, w = gray8.shape
        qimg = QImage(gray8.data.tobytes(), w, h, w, QImage.Format_Grayscale8)
        self._raw_pixmap = QPixmap.fromImage(qimg)
        self._update_display()

    def set_show_motion(self, show):
        self._show_motion = show
        self._update_display()

    def set_local_motion(self, local):
        self._local_motion = local
        self._update_display()

    def set_motion_scale(self, scale):
        self._motion_scale = scale
        self._update_display()

    # ── Internal rendering ─────────────────────────────────────────────────

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_display()

    def _update_display(self):
        if self._raw_pixmap is None: return
        scaled = self._raw_pixmap.scaled(
            self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        W, H = scaled.width(), scaled.height()

        result = QPixmap(scaled.size())
        p = QPainter(result)
        p.drawPixmap(0, 0, scaled)

        # Motion overlay (drawn before exclusion so exclusion is always on top)
        if (self._show_motion and self._motion_data
                and not self._excluded):
            self._draw_motion_overlay(p, W, H)

        # Exclusion overlay
        if self._excluded:
            p.setOpacity(0.28)
            p.fillRect(result.rect(), QColor(220, 50, 50))
            p.setOpacity(1.0)
            font = QFont()
            font.setPointSize(max(10, H // 18))
            font.setBold(True)
            p.setFont(font)
            rect = result.rect()
            # Shadow
            p.setPen(QColor(0, 0, 0))
            for dx, dy in [(-1,0),(1,0),(0,-1),(0,1)]:
                p.drawText(rect.translated(dx, dy),
                           Qt.AlignCenter, "Bad frame\n— excluded —")
            p.setPen(QColor(255, 255, 255))
            p.drawText(rect, Qt.AlignCenter, "Bad frame\n— excluded —")

        p.end()
        self.setPixmap(result)

    def _draw_motion_overlay(self, painter, W, H):
        """
        Draw motion patch trajectories spatially on the image using QPainter.
        Each patch is positioned at its grid location; track is colour-coded
        by arc-length (green = low motion, red = high motion).

        If local_motion is enabled, the mean trajectory across all patches is
        subtracted from each patch so only the local (non-global) component is
        shown — matching the "only local motion" option in the Warp GUI.
        """
        mdata = self._motion_data
        patches = {}
        for key, track in mdata.items():
            try:
                row, col = map(int, key.split('_'))
                patches[(row, col)] = track
            except ValueError:
                continue
        if not patches: return

        n_rows = max(r for r, c in patches) + 1
        n_cols = max(c for r, c in patches) + 1
        cell_w = W / n_cols
        cell_h = H / n_rows

        # Build per-patch x/y arrays, optionally removing the global mean
        # trajectory (local motion mode)
        xy = {}
        n_frames = min(len(t['x']) for t in patches.values())
        for k, t in patches.items():
            xy[k] = (np.array(t['x'][:n_frames]),
                     np.array(t['y'][:n_frames]))

        if self._local_motion:
            mean_x = np.mean([v[0] for v in xy.values()], axis=0)
            mean_y = np.mean([v[1] for v in xy.values()], axis=0)
            xy = {k: (x - mean_x, y - mean_y) for k, (x, y) in xy.items()}

        # Arc-length per patch for colour normalisation
        arc_lengths = {}
        for k, (x, y) in xy.items():
            arc_lengths[k] = float(
                np.sum(np.sqrt(np.diff(x)**2 + np.diff(y)**2)))
        min_arc = min(arc_lengths.values())
        max_arc = max(arc_lengths.values())
        arc_range = max(max_arc - min_arc, 1e-6)

        # Scale tracks to fit within 40% of cell, times the user scale factor
        all_disp = []
        for (x, y) in xy.values():
            all_disp.extend(list(x) + list(y))
        max_disp = max(abs(v) for v in all_disp) if all_disp else 1.0
        scale = (0.40 * min(cell_w, cell_h) / max(max_disp, 1e-6)
                 * self._motion_scale)

        # Faint grid lines
        painter.setOpacity(0.18)
        painter.setPen(QPen(QColor(200, 200, 255), 0.5))
        for i in range(1, n_cols):
            x = int(i * cell_w)
            painter.drawLine(x, 0, x, H)
        for i in range(1, n_rows):
            y = int(i * cell_h)
            painter.drawLine(0, y, W, y)

        painter.setOpacity(0.88)

        for (row, col) in sorted(xy.keys()):
            cx = (col + 0.5) * cell_w
            cy = (row + 0.5) * cell_h   # row 0 at top

            x = xy[(row, col)][0] * scale
            y = xy[(row, col)][1] * scale

            norm  = (arc_lengths[(row, col)] - min_arc) / arc_range
            color = _motion_color(norm)

            # Build polyline path
            path = QPainterPath()
            path.moveTo(QPointF(cx + x[0], cy + y[0]))
            for xi, yi in zip(x[1:], y[1:]):
                path.lineTo(QPointF(cx + xi, cy + yi))

            pen = QPen(color, 1.5)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)   # prevent path from being filled
            painter.drawPath(path)

            # Start dot (circle)
            painter.setBrush(color)
            painter.setPen(Qt.NoPen)
            r = max(2, int(min(cell_w, cell_h) * 0.05))
            painter.drawEllipse(
                QPointF(cx + x[0], cy + y[0]), r, r)
            # End square
            painter.drawRect(
                int(cx + x[-1]) - r, int(cy + y[-1]) - r, r*2, r*2)

        painter.setOpacity(1.0)

# ---------------------------------------------------------------------------
# Overview bar (matplotlib in Qt, with CTF colour coding + click signal)
# ---------------------------------------------------------------------------

class OverviewCanvas(FigureCanvasQTAgg):
    tilt_clicked = pyqtSignal(int)

    def __init__(self, parent=None, colors=None):
        fig = plt.Figure(figsize=(10, 0.7), facecolor=C_PANEL)
        fig.subplots_adjust(left=0.02, right=0.99, top=0.75, bottom=0.25)
        self.ax = fig.add_subplot(111)
        self.ax.set_facecolor(C_PANEL)
        for sp in self.ax.spines.values(): sp.set_edgecolor('#334155')
        super().__init__(fig)
        self.setParent(parent)
        self.setFixedHeight(80)
        self._n = 0
        self._order = []      # display position -> real tilt index
        self.colors = colors or CategoryColors()
        self.mpl_connect('button_press_event', self._bar_click)

    def _bar_click(self, event):
        if event.xdata is not None and event.button == 1 and self._n > 0:
            pos = max(0, min(int(round(event.xdata)), self._n - 1))
            # Map the clicked display position back to the real tilt index
            real = self._order[pos] if pos < len(self._order) else pos
            self.tilt_clicked.emit(real)

    def update_overview(self, excluded, flagged, current_idx,
                        ctf_values=None, order=None, angles=None):
        """
        order : list mapping display position -> real tilt index. When given
                (angle-sorted), the bar is drawn in that order so the centre
                is 0 deg and the extremes sit at the edges. Defaults to natural
                acquisition order.
        angles: real per-tilt angles, used for sparse x-axis tick labels.
        """
        self._n = len(excluded)
        n = len(excluded)
        if order is None:
            order = list(range(n))
        self._order = order

        self.ax.cla()
        self.ax.set_facecolor(C_PANEL)
        for sp in self.ax.spines.values(): sp.set_edgecolor('#334155')

        C = self.colors
        colours = []
        for real in order:
            if excluded[real]:
                colours.append(C['excluded'])
            elif flagged[real]:
                colours.append(C['flagged'])
            elif ctf_values and real < len(ctf_values) and ctf_values[real]:
                ctf = ctf_values[real]
                if   ctf > 10: colours.append(C['ctf_bad'])
                elif ctf > 8:  colours.append(C['ctf_mod'])
                else:          colours.append(C['good'])
            else:
                colours.append(C['good'])

        self.ax.bar(range(n), [1]*n, color=colours, width=0.85, edgecolor='none')

        # Highlight current tilt at its DISPLAY position
        try:
            disp_pos = order.index(current_idx)
        except ValueError:
            disp_pos = current_idx
        self.ax.axvline(disp_pos, color=C_TEXT, lw=2, zorder=10)

        self.ax.set_xlim(-0.5, n-0.5); self.ax.set_ylim(0, 1.2)
        self.ax.set_yticks([])

        # Sparse angle tick labels (every ~8th display position) if we have
        # angles, so the user can read the -60..0..+60 layout
        if angles is not None and n > 0:
            step = max(1, n // 12)
            ticks = list(range(0, n, step))
            self.ax.set_xticks(ticks)
            self.ax.set_xticklabels(
                [f'{angles[order[t]]:+.0f}' for t in ticks])
        self.ax.tick_params(axis='x', colors=C_TEXT, labelsize=6)

        n_excl = sum(excluded)
        self.ax.set_title(
            f'{n_excl} excluded / {n}   '
            'red=excl  orange=flagged  purple=CTF>10\u00c5  '
            'amber=8\u201310\u00c5  green=good   '
            '(ordered by tilt angle)',
            color=C_TEXT, fontsize=7, pad=2)
        self.draw_idle()


# ---------------------------------------------------------------------------
# Right-hand plots: CTF fit + CTF resolution / defocus / motion vs tilt angle
# ---------------------------------------------------------------------------

class PlotsCanvas(FigureCanvasQTAgg):
    """
    Four stacked, equal-height plots shown on the right beneath the power
    spectrum:
      1) CTF fit       — background-subtracted experimental vs fitted CTF^2,
                         coloured by the current tilt's category
      2) CTF resolution (A) vs tilt angle   (scatter, per-tilt colours)
      3) Defocus (um)        vs tilt angle
      4) Mean motion (A)     vs tilt angle
    The current tilt is drawn enlarged in plots 2-4.
    """

    def __init__(self, parent=None):
        fig = plt.Figure(figsize=(5, 8), facecolor=C_PANEL)
        # Symmetric left/right margins so the plot data area is centred within
        # the panel (lining up with the centred power-spectrum image above).
        # A fixed y-label x-coordinate (set in update) keeps the four labels
        # aligned without needing asymmetric margins.
        fig.subplots_adjust(left=0.15, right=0.985, top=0.96, bottom=0.07,
                            hspace=0.55)
        self.ax_ctf = fig.add_subplot(411)
        self.ax_res = fig.add_subplot(412)
        self.ax_def = fig.add_subplot(413)
        self.ax_mot = fig.add_subplot(414)
        super().__init__(fig)
        self.setParent(parent)
        for ax in (self.ax_ctf, self.ax_res, self.ax_def, self.ax_mot):
            self._style_axes(ax)

    def _style_axes(self, ax):
        ax.set_facecolor(C_PANEL)
        for sp in ax.spines.values():
            sp.set_edgecolor('#334155')
        ax.tick_params(colors=C_TEXT, labelsize=9)
        ax.xaxis.label.set_color(C_TEXT)
        ax.yaxis.label.set_color(C_TEXT)

    def update_plots(self, angles, ctf_res, defocus, motion, colours,
                     current_idx, current_meta, current_color):
        """
        angles       : list of per-tilt angles (real/acquisition order)
        ctf_res/...  : per-tilt scalar lists (may contain None)
        colours      : per-tilt hex colour list (category colours)
        current_idx  : index of the active tilt
        current_meta : parsed frame meta dict for the active tilt (CTF curves)
        current_color: hex colour for the active tilt (fit line colour)
        """
        LBL = 10   # axis label font size
        # ---- 1) CTF fit for the current tilt ----
        ax = self.ax_ctf
        ax.cla(); self._style_axes(ax)
        s, exp_curve, fitted = compute_ctf_fit_curves(current_meta or {})
        if s is not None:
            # Both curves share the same envelope by construction (experimental
            # = PS1D - background; fitted = CTF^2 * envelope). Put them on a
            # common scale so they overlay with matching amplitude, using a
            # robust high-percentile reference so the low-frequency rolloff
            # spike doesn't flatten the rest. The full frequency range is kept.
            exp_f = np.asarray(exp_curve, dtype=float)
            fit_f = np.asarray(fitted, dtype=float)
            finite = exp_f[np.isfinite(exp_f)]
            ref = np.percentile(finite, 99) if finite.size else 1.0
            if ref <= 0:
                ref = np.nanmax(finite) if finite.size else 1.0
            exp_disp = exp_f / ref
            # fitted shares the envelope; scale by its own 99th pct so its
            # peak amplitude matches the experimental's
            fit_finite = fit_f[np.isfinite(fit_f)]
            fref = np.percentile(fit_finite, 99) if fit_finite.size else 1.0
            fit_disp = fit_f / fref if fref > 0 else fit_f

            ax.plot(s, exp_disp, color=C_TEXT, lw=0.9, label='experimental')
            ax.plot(s, fit_disp, color=current_color, lw=1.1, label='fitted CTF')
            ax.set_xlim(s.min(), s.max())
            # headroom; clip the spike at the top rather than rescaling
            ax.set_ylim(-0.05, 1.15)
            ax.legend(fontsize=8, loc='upper right', framealpha=0.85,
                      facecolor=C_PANEL, edgecolor='#334155',
                      labelcolor=C_TEXT)
        else:
            ax.text(0.5, 0.5, 'no CTF curve', color=C_DIM, fontsize=9,
                    ha='center', va='center', transform=ax.transAxes)
        ax.set_ylabel('Intensity', fontsize=LBL)
        ax.set_xlabel('spatial frequency (1/\u00c5)', fontsize=LBL)
        ax.set_yticks([0, 0.5, 1.0])

        # ---- 2-4) scatter plots vs tilt angle ----
        specs = [
            (self.ax_res, ctf_res, 'CTF res (\u00c5)'),
            (self.ax_def, defocus, 'Defocus (\u00b5m)'),
            (self.ax_mot, motion,  'Mean motion (\u00c5)'),
        ]
        for ax, values, ylabel in specs:
            ax.cla(); self._style_axes(ax)
            xs, ys, cs = [], [], []
            for i, v in enumerate(values):
                if v is None:
                    continue
                xs.append(angles[i]); ys.append(v); cs.append(colours[i])
            if xs:
                ax.scatter(xs, ys, c=cs, s=22, edgecolors='none', zorder=3)
                # enlarge the current tilt's point
                if current_idx < len(values) and values[current_idx] is not None:
                    ax.scatter([angles[current_idx]], [values[current_idx]],
                               c=[current_color], s=95, edgecolors=C_TEXT,
                               linewidths=1.0, zorder=5)
            ax.set_ylabel(ylabel, fontsize=LBL)
            ax.set_xlabel('tilt angle (\u00b0)', fontsize=LBL)
            ax.margins(x=0.03)

        # Pin all four y-axis labels to the same x-coordinate (in axes
        # fraction, negative = left of the axes) so they line up vertically
        # regardless of tick-label width.
        YLBL_X = -0.13
        for ax in (self.ax_ctf, self.ax_res, self.ax_def, self.ax_mot):
            ax.yaxis.set_label_coords(YLBL_X, 0.5)
        self.draw_idle()


# ---------------------------------------------------------------------------
# Button helper
# ---------------------------------------------------------------------------

def _btn(text, callback):
    b = QPushButton(text)
    b.clicked.connect(callback)
    b.setStyleSheet(f"""
        QPushButton {{
            background: {C_ACCENT}; color: {C_TEXT};
            border: 1px solid #334155; border-radius: 4px;
            padding: 6px 10px; font-size: 12px;
        }}
        QPushButton:hover   {{ background: {C_HOVER}; }}
        QPushButton:pressed {{ background: #0a2040; }}
    """)
    return b


class ColorPickerDialog(QDialog):
    """
    Small dialog to recolour the overview-bar categories. Changes apply live
    via the on_change callback. Session-only — not persisted between launches.
    """

    def __init__(self, colors, on_change, parent=None):
        super().__init__(parent)
        self.colors = colors
        self.on_change = on_change
        self._swatches = {}
        self.setWindowTitle("Category Colours")
        self.setStyleSheet(f"background: {C_BG}; color: {C_TEXT};")

        layout = QGridLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("Click a colour to change it")
        title.setStyleSheet(f"color: {C_TEXT}; font-weight: bold; "
                            f"font-size: 12px;")
        layout.addWidget(title, 0, 0, 1, 2)

        for row, key in enumerate(CategoryColors.ORDER, start=1):
            lbl = QLabel(CategoryColors.LABELS[key])
            lbl.setStyleSheet(f"color: {C_TEXT}; font-size: 12px;")
            layout.addWidget(lbl, row, 0)

            sw = QPushButton()
            sw.setFixedSize(60, 24)
            sw.setCursor(Qt.PointingHandCursor)
            self._style_swatch(sw, self.colors[key])
            sw.clicked.connect(lambda _checked, k=key: self._pick(k))
            self._swatches[key] = sw
            layout.addWidget(sw, row, 1)

        # Reset + Close
        btn_reset = _btn("Reset to defaults", self._reset)
        btn_close = _btn("Close", self.accept)
        layout.addWidget(btn_reset, len(CategoryColors.ORDER) + 1, 0)
        layout.addWidget(btn_close, len(CategoryColors.ORDER) + 1, 1)

    def _style_swatch(self, btn, hexcolor):
        btn.setStyleSheet(
            f"background: {hexcolor}; border: 1px solid {C_TEXT}; "
            f"border-radius: 4px;")

    def _pick(self, key):
        current = QColor(self.colors[key])
        chosen = QColorDialog.getColor(current, self,
                                       f"Choose colour: "
                                       f"{CategoryColors.LABELS[key]}")
        if chosen.isValid():
            self.colors.set(key, chosen.name())
            self._style_swatch(self._swatches[key], chosen.name())
            self.on_change()

    def _reset(self):
        defaults = CategoryColors()
        for key in CategoryColors.ORDER:
            self.colors.set(key, defaults[key])
            self._style_swatch(self._swatches[key], self.colors[key])
        self.on_change()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class _RankingThread(QThread):
    """
    Background thread that gathers the per-series ranking metrics (the slow,
    I/O-bound part) without blocking the GUI. It emits the collected metrics
    back to the main thread, where the ranking is applied and the list
    re-sorted. Only does filesystem reads; never touches Qt widgets directly.
    """
    finished_metrics = pyqtSignal(list)

    def __init__(self, window):
        super().__init__()
        self._window = window

    def run(self):
        try:
            metrics = self._window._gather_metrics_parallel()
        except Exception as e:
            print(f"  [WARN] background ranking failed: {e}")
            metrics = [{} for _ in self._window.series_list]
        self.finished_metrics.emit(metrics)


class MainWindow(QMainWindow):

    def __init__(self, series_list, frame_dir=None,
                 sigma=3.0, contrast_lo=2, contrast_hi=98,
                 loss_dir=None, xml_dir=None, io_workers=16):
        super().__init__()
        self.series_list = series_list
        self.loss_dir    = loss_dir
        self.xml_dir     = xml_dir
        self.io_workers  = max(1, int(io_workers))
        # Normalise frame_dir: if the user pointed it at the average/ subdir,
        # step back to its parent so average/, powerspectrum/ and the per-frame
        # XMLs are all found consistently.
        if frame_dir:
            fd = os.path.normpath(frame_dir)
            if os.path.basename(fd) == 'average' and os.path.isdir(fd):
                parent = os.path.dirname(fd)
                # only step back if the parent looks like the frame-series dir
                # (i.e. it actually contains the average/ dir we were given)
                if os.path.isdir(os.path.join(parent, 'average')):
                    print(f"  Note: --frame_dir pointed at average/; using "
                          f"parent '{parent}' as frame dir")
                    fd = parent
            frame_dir = fd
        self.frame_dir   = frame_dir
        self.sigma       = sigma
        self.clo         = contrast_lo
        self.chi         = contrast_hi
        self.series_idx  = 0
        self.tilt_idx    = 0
        self._cache      = {}
        self.colors      = CategoryColors()   # session-only category colours

        # Create the xml_original_backups/ directory up front for every XML
        # location in the series list, so it exists as soon as the tool runs.
        for _, xp in self.series_list:
            if xp:
                bdir = os.path.join(os.path.dirname(os.path.abspath(xp)),
                                    'xml_original_backups')
                try:
                    os.makedirs(bdir, exist_ok=True)
                except OSError as e:
                    print(f"  [WARN] Could not create backup dir {bdir}: {e}")

        # Start in table order with no ranking yet; the ranking is computed in
        # the background after the window is shown (see _start_background_ranking).
        # This lets the window open in ~1-2s instead of waiting for all per-frame
        # XMLs to be read up front.
        self.series_rank = [None] * len(self.series_list)
        self.series_metrics = [{} for _ in self.series_list]
        self._used_loss_in_rank = False
        self._ranking_done = False

        self._load_series(0)
        self._build_ui()
        self._refresh()
        self.setWindowTitle("TomoTriage")

        # Kick off ranking in the background once the event loop is running.
        QTimer.singleShot(0, self._start_background_ranking)

    # ── Ranking ─────────────────────────────────────────────────────────────

    def _series_name(self, tomostar_path):
        return os.path.splitext(os.path.basename(tomostar_path))[0]

    def _format_series_item(self, i):
        """Format a series-list row: rank, name, and key metrics."""
        tp, _ = self.series_list[i]
        name = self._series_name(tp)
        rank = self.series_rank[i] if hasattr(self, 'series_rank') else None
        m = self.series_metrics[i] if hasattr(self, 'series_metrics') else {}
        rank_str = f"#{rank:>2}" if rank is not None else "  -"
        # compact metric suffix
        bits = []
        if m.get('ctf') is not None:
            bits.append(f"{m['ctf']:.1f}\u00c5")
        if m.get('loss') is not None:
            bits.append(f"L{m['loss']:.2f}")
        suffix = ("  " + " ".join(bits)) if bits else ""
        return f"{rank_str}  {name}{suffix}"

    def _gather_metrics_parallel(self):
        """
        Read the per-series ranking metrics (CTF, motion, loss) for every
        series, in parallel across a thread pool. This is the slow part on a
        network mount (thousands of per-frame XML reads), and it is I/O-bound,
        so threads overlap the filesystem latency effectively.

        Returns a list of metric dicts aligned with self.series_list. Pure
        reads only — does NOT touch any Qt state, so it is safe to run off the
        main thread.
        """
        n = len(self.series_list)
        results = [None] * n

        def work(i):
            tp, xp = self.series_list[i]
            name = self._series_name(tp)
            loss = read_alignment_loss(self.loss_dir, name)
            ts_ctf, ts_motion = read_tiltseries_ctf_motion(xp)
            if loss is not None and ts_ctf is not None:
                ctf, motion = ts_ctf, ts_motion
            else:
                ctf, motion = self._preali_series_metrics(tp, xp)
                if ctf is None:
                    ctf = ts_ctf
                if motion is None:
                    motion = ts_motion
            return i, {'ctf': ctf, 'motion': motion, 'loss': loss}

        workers = min(self.io_workers, n) if n else 1
        if workers <= 1:
            for i in range(n):
                _, results[i] = work(i)
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(work, i) for i in range(n)]
                for fut in as_completed(futs):
                    i, m = fut.result()
                    results[i] = m
        return results

    def _start_background_ranking(self):
        """
        Launch the ranking metric-gathering on a background thread, so the
        window (already shown) stays responsive. When it finishes, the results
        are applied on the main thread via _apply_ranking.
        """
        self.statusBar().showMessage(
            "Ranking datasets in the background\u2026", 0)
        self._rank_thread = _RankingThread(self)
        self._rank_thread.finished_metrics.connect(self._apply_ranking)
        self._rank_thread.start()

    def _apply_ranking(self, metrics):
        """
        Apply ranking results (runs on the MAIN/GUI thread via signal). Computes
        the ranks, reorders the series list so rank 1 is first, and rebuilds the
        list widget — preserving whichever series is currently selected.
        """
        ranks, used_loss = compute_series_ranking(metrics)
        self._used_loss_in_rank = used_loss

        # Remember the currently-selected series so we can keep it selected
        # after the reorder.
        cur_tp = self.series_list[self.series_idx][0] \
            if 0 <= self.series_idx < len(self.series_list) else None

        indexed = list(range(len(self.series_list)))
        rank_of = {i: (ranks[i] if ranks[i] is not None else 10**9)
                   for i in indexed}
        order = sorted(indexed, key=lambda i: rank_of[i])

        self.series_list = [self.series_list[i] for i in order]
        self.series_rank = [ranks[i] for i in order]
        self.series_metrics = [metrics[i] for i in order]
        # Remap the cache (keyed by old index) to the new positions.
        old_cache = self._cache
        self._cache = {}
        for new_i, old_i in enumerate(order):
            if old_i in old_cache:
                self._cache[new_i] = old_cache[old_i]

        # Find where the previously-selected series went.
        new_sel = 0
        if cur_tp is not None:
            for j, (tp, _) in enumerate(self.series_list):
                if tp == cur_tp:
                    new_sel = j
                    break
        self.series_idx = new_sel

        self._ranking_done = True
        self._rebuild_series_list_widget()

        n_ranked = sum(1 for r in ranks if r is not None)
        mode = "post-alignment (CTF + motion + loss)" if used_loss \
            else "pre-alignment (CTF + motion)"
        self.statusBar().showMessage(
            f"Ranked {n_ranked}/{len(self.series_list)} series \u2014 {mode}",
            5000)

    def _rebuild_series_list_widget(self):
        """Repopulate the series list widget after a reorder (main thread)."""
        if not hasattr(self, 'series_list_widget'):
            return
        # update the header to reflect the ranking mode now known
        if hasattr(self, 'series_list_header'):
            rank_mode = "CTF+motion+loss" if self._used_loss_in_rank \
                else "CTF+motion"
            self.series_list_header.setText(
                f"Tilt Series \u2014 ranked ({rank_mode})")
        w = self.series_list_widget
        w.blockSignals(True)
        w.clear()
        for i in range(len(self.series_list)):
            w.addItem(self._format_series_item(i))
        w.setCurrentRow(self.series_idx)
        w.blockSignals(False)

    def _preali_series_metrics(self, tomostar_path, xml_path):
        """
        Aggregate per-frame CTF resolution and motion into a single per-series
        value (median across tilts) for pre-alignment ranking. Reads the
        per-frame XMLs from frame_dir by movie name. Returns (ctf, motion),
        either possibly None. Kept lightweight — no image loading.
        """
        if not self.frame_dir:
            return None, None
        try:
            col_names, rows = parse_tomostar(tomostar_path)
        except Exception:
            return None, None
        movies = get_movie_names(col_names, rows)
        if not movies:
            return None, None
        ctfs, motions = [], []
        for mv in movies:
            stem = os.path.splitext(mv)[0]
            # per-frame XML lives in frame_dir (or its average/ subdir)
            fxml = None
            for md in [self.frame_dir, os.path.join(self.frame_dir, 'average')]:
                cand = os.path.join(md, stem + '.xml')
                if os.path.exists(cand):
                    fxml = cand
                    break
            if not fxml:
                continue
            m = read_frame_xml(fxml)
            if m.get('ctf_res'):
                ctfs.append(m['ctf_res'])
            if m.get('motion') is not None:
                motions.append(m['motion'])
        ctf = float(np.median(ctfs)) if ctfs else None
        motion = float(np.median(motions)) if motions else None
        return ctf, motion

    # ── Data loading ───────────────────────────────────────────────────────

    def _load_series(self, idx):
        if idx in self._cache: return
        tp, xp = self.series_list[idx]
        name = os.path.splitext(os.path.basename(tp))[0]
        print(f"  Loading [{idx+1}/{len(self.series_list)}] {name} ...")
        col_names, rows = parse_tomostar(tp)
        movies  = get_movie_names(col_names, rows)
        angles  = get_tilt_angles(col_names, rows)
        n = len(movies)

        # Per-tilt images come from <frame_dir>/average/ — one .mrc per tilt,
        # matched to the tomostar by movie name. This shows every acquired
        # tilt (including excluded ones), unlike a reduced .st stack.
        image_paths = resolve_average_paths(self.frame_dir, movies) \
            if self.frame_dir else [None] * n

        # Map exclusions by tilt angle (the XML <UseTilt> is angle-ordered).
        excluded = read_usetilt_from_xml(xp, n, tilt_angles=angles)

        # Per-frame XML (small) read up front for CTF colouring; motion JSON
        # paths resolved now and parsed lazily on first view.
        frame_meta  = []
        motion_paths = []
        for mv in movies:
            xml_f = mot_f = None
            if self.frame_dir:
                stem = os.path.splitext(mv)[0]
                cx = os.path.join(self.frame_dir, stem + '.xml')
                if os.path.exists(cx): xml_f = cx
                for md in [self.frame_dir,
                            os.path.join(self.frame_dir, 'average')]:
                    cm = os.path.join(md, stem + '_motion.json')
                    if os.path.exists(cm): mot_f = cm; break
            frame_meta.append(read_frame_xml(xml_f))
            motion_paths.append(mot_f)

        n_img = sum(1 for p in image_paths if p is not None)
        n_mot = sum(1 for m in motion_paths if m is not None)
        print(f"  Average images: {n_img}/{n}   Motion files: {n_mot}/{n}")

        # Display order for the overview bar: sort tilt indices by angle so the
        # bar reads -60 ... 0 ... +60 (centre = 0 deg). Underlying data and
        # <UseTilt> mapping stay in acquisition order; this is display-only.
        angle_order = sorted(range(n), key=lambda i: angles[i])

        self._cache[idx] = dict(
            name=name, tomostar_path=tp, ts_xml=xp,
            col_names=col_names, rows=rows, n=n,
            excluded=excluded,
            flagged=auto_flag_candidates_from_paths(image_paths, self.sigma),
            angles=angles, movies=movies,
            angle_order=angle_order,          # display pos -> real tilt index
            image_paths=image_paths,          # per-tilt average .mrc paths
            image_cache={},                   # idx -> loaded image (lazy)
            frame_meta=frame_meta,
            motion_paths=motion_paths,
            motion_cache={},                  # idx -> parsed JSON (lazy)
        )

    def _get_image(self, ti):
        """Lazily load and cache the average image for tilt ti."""
        s = self._s()
        if ti in s['image_cache']:
            return s['image_cache'][ti]
        path = s['image_paths'][ti] if ti < len(s['image_paths']) else None
        img = load_mrc_image(path)
        s['image_cache'][ti] = img
        return img

    def _get_motion(self, ti):
        """Lazily load and cache the motion JSON for tilt ti of current series."""
        s = self._s()
        if ti in s['motion_cache']:
            return s['motion_cache'][ti]
        path = s['motion_paths'][ti] if ti < len(s['motion_paths']) else None
        data = load_motion_json(path)
        s['motion_cache'][ti] = data
        return data

    def _s(self): return self._cache[self.series_idx]

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self):
        self.setStyleSheet(f"background-color: {C_BG}; color: {C_TEXT};")
        self.resize(1600, 950)
        # Accept keyboard focus so arrow keys / shortcuts always work
        self.setFocusPolicy(Qt.StrongFocus)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # ── Top content row ───────────────────────────────────────────
        splitter = QSplitter(Qt.Horizontal)
        splitter.setStyleSheet("QSplitter::handle { background: #334155; }")

        # Left: tilt image (motion overlay drawn directly onto it)
        self.img_tilt = ImageLabel()
        self.img_tilt.setMinimumWidth(400)
        # Scroll wheel over the tilt image steps through tilts
        self.img_tilt.wheel_scrolled.connect(self._on_wheel)
        splitter.addWidget(self.img_tilt)

        # Middle: power spectrum on top (cropped to signal), then the
        # CTF-fit / resolution / defocus / motion plots filling the rest.
        middle = QWidget()
        middle.setStyleSheet(f"background: {C_BG};")
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.setSpacing(4)

        self.img_ps = ImageLabel()
        self.img_ps.setMinimumWidth(300)
        # Power spectrum cropped to 512x128 -> keep its panel short so it shows
        # just the signal band rather than reserving the full square.
        self.img_ps.setMaximumHeight(150)
        middle_layout.addWidget(self.img_ps, stretch=0)

        self.plots = PlotsCanvas()
        middle_layout.addWidget(self.plots, stretch=1)

        middle.setMinimumWidth(320)
        splitter.addWidget(middle)

        # Right: series list
        right = QWidget()
        right.setStyleSheet(f"background: {C_BG};")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 4, 4, 4)
        right_layout.setSpacing(4)
        # Ranking runs in the background after the window shows; start with a
        # neutral header and update it when ranking completes.
        if getattr(self, '_ranking_done', False):
            rank_mode = "CTF+motion+loss" if getattr(
                self, '_used_loss_in_rank', False) else "CTF+motion"
            header_text = f"Tilt Series \u2014 ranked ({rank_mode})"
        else:
            header_text = "Tilt Series \u2014 ranking\u2026"
        lbl = QLabel(header_text)
        lbl.setStyleSheet(
            f"color: {C_TEXT}; font-weight: bold; font-size: 12px;")
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setWordWrap(True)
        right_layout.addWidget(lbl)
        self.series_list_header = lbl
        self.series_list_widget = QListWidget()
        self.series_list_widget.setStyleSheet(f"""
            QListWidget {{
                background: #0d1117; color: {C_TEXT};
                border: 1px solid #334155; font-size: 11px;
                font-family: monospace;
            }}
            QListWidget::item:selected {{ background: {C_HOVER}; }}
            QListWidget::item:hover    {{ background: #1a2a3a; }}
        """)
        for i, (tp, _) in enumerate(self.series_list):
            self.series_list_widget.addItem(self._format_series_item(i))
        self.series_list_widget.setCurrentRow(0)
        self.series_list_widget.currentRowChanged.connect(
            self._on_series_changed)
        # Click-to-select but never hold keyboard focus, so arrow keys always
        # reach the main window's keyPressEvent
        self.series_list_widget.setFocusPolicy(Qt.ClickFocus)
        right_layout.addWidget(self.series_list_widget)
        right.setMinimumWidth(180); right.setMaximumWidth(280)
        splitter.addWidget(right)

        splitter.setSizes([720, 650, 220])
        root.addWidget(splitter, stretch=10)

        # ── Overview bar ──────────────────────────────────────────────
        self.overview = OverviewCanvas(colors=self.colors)
        self.overview.tilt_clicked.connect(self._on_overview_click)
        root.addWidget(self.overview, stretch=0)

        # ── Info bar ──────────────────────────────────────────────────
        self.info_label = QLabel()
        self.info_label.setStyleSheet(
            f"background: {C_ACCENT}; color: {C_TEXT}; "
            f"font-size: 11px; padding: 4px 10px;")
        self.info_label.setFixedHeight(28)
        root.addWidget(self.info_label, stretch=0)

        # ── Tilt title ────────────────────────────────────────────────
        self.tilt_title = QLabel()
        self.tilt_title.setStyleSheet(
            f"color: {C_TEXT}; font-size: 13px; font-weight: bold; "
            f"padding: 2px 8px;")
        self.tilt_title.setAlignment(Qt.AlignCenter)
        root.addWidget(self.tilt_title, stretch=0)

        # ── Button bar ────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        for text, cb in [
            ('< Prev',              self._on_prev),
            ('> Next',              self._on_next),
            ('Exclude [Ctrl+E]',    self._on_toggle),
            ('All On  [Ctrl+R]',    self._on_include_all),
            ('Save  [Ctrl+S]',      self._on_save),
            ('Next Series [Ctrl+N]',self._on_next_series),
            ('Quit+Save [Ctrl+Q]',  self._on_quit_save),
        ]:
            btn_row.addWidget(_btn(text, cb))

        # Motion toggle checkbox
        self._motion_check = QCheckBox("Motion Overlay  [Ctrl+M]")
        self._motion_check.setChecked(True)
        self._motion_check.setStyleSheet(f"""
            QCheckBox {{
                color: {C_TEXT}; font-size: 12px; spacing: 6px;
                padding: 6px 8px;
            }}
            QCheckBox::indicator {{
                width: 14px; height: 14px;
                border: 1px solid #334155; border-radius: 3px;
                background: {C_PANEL};
            }}
            QCheckBox::indicator:checked {{
                background: {C_HOVER}; border-color: {C_HOVER};
            }}
        """)
        self._motion_check.stateChanged.connect(
            lambda s: self.img_tilt.set_show_motion(s == Qt.Checked))
        btn_row.addWidget(self._motion_check)

        # Local-motion-only checkbox
        self._local_check = QCheckBox("Local only")
        self._local_check.setChecked(False)
        self._local_check.setStyleSheet(self._motion_check.styleSheet())
        self._local_check.stateChanged.connect(
            lambda s: self.img_tilt.set_local_motion(s == Qt.Checked))
        btn_row.addWidget(self._local_check)

        root.addLayout(btn_row, stretch=0)

        # ── Bulk exclude-by-colour row ────────────────────────────────
        # Quickly exclude every tilt of a given overview-bar category with a
        # single click, instead of stepping through them one at a time.
        bulk_row = QHBoxLayout()
        bulk_row.setSpacing(6)

        bulk_lbl = QLabel("Exclude all:")
        bulk_lbl.setStyleSheet(
            f"color: {C_TEXT}; font-size: 12px; font-weight: bold; "
            f"padding: 6px 4px;")
        bulk_row.addWidget(bulk_lbl)

        # (label, category) — colour comes from the live theme
        bulk_specs = [
            ('Purple (CTF > 10 \u00c5)', 'ctf_bad'),
            ('Amber (CTF 8\u201310 \u00c5)', 'ctf_mod'),
            ('Orange (flagged)',          'flagged'),
        ]
        self._bulk_buttons = []   # (button, category) for live recolour
        for label, category in bulk_specs:
            b = QPushButton(label)
            b.clicked.connect(
                lambda _checked, c=category: self._exclude_category(c))
            self._bulk_buttons.append((b, category))
            bulk_row.addWidget(b)
        self._restyle_bulk_buttons()

        # Exclude every tilt in the current dataset (reject the whole dataset).
        btn_all_frames = QPushButton("Exclude ALL frames")
        btn_all_frames.setStyleSheet(
            f"QPushButton {{ background: {C_RED}; color: white; "
            f"font-weight: bold; border: none; border-radius: 4px; "
            f"padding: 6px 10px; }}"
            f"QPushButton:hover {{ background: #b91c1c; }}")
        btn_all_frames.clicked.connect(self._exclude_all_frames)
        bulk_row.addWidget(btn_all_frames)

        # Colour-picker button
        btn_colours = _btn("Colours\u2026", self._open_color_picker)
        bulk_row.addWidget(btn_colours)

        bulk_row.addStretch(1)
        root.addLayout(bulk_row, stretch=0)

        # Second row: exclude a category across ALL loaded datasets at once.
        # These are destructive/sweeping, so each asks for confirmation.
        allbulk_row = QHBoxLayout()
        allbulk_row.setSpacing(6)
        allbulk_lbl = QLabel("Exclude in ALL datasets:")
        allbulk_lbl.setStyleSheet(
            f"color: {C_YELLOW}; font-size: 12px; font-weight: bold; "
            f"padding: 6px 4px;")
        allbulk_row.addWidget(allbulk_lbl)
        self._allbulk_buttons = []
        for label, category in bulk_specs:
            b = QPushButton(label)
            b.clicked.connect(
                lambda _checked, c=category: self._exclude_category_all(c))
            self._allbulk_buttons.append((b, category))
            allbulk_row.addWidget(b)
        self._restyle_bulk_buttons()
        allbulk_row.addStretch(1)
        root.addLayout(allbulk_row, stretch=0)

    # ── Keyboard shortcuts (keyPressEvent avoids QListWidget focus issue) ──

    def keyPressEvent(self, event):
        key  = event.key()
        mods = event.modifiers()
        if   key == Qt.Key_Left:  self._on_prev()
        elif key == Qt.Key_Right: self._on_next()
        elif mods & Qt.ControlModifier:
            if   key == Qt.Key_E: self._on_toggle()
            elif key == Qt.Key_S: self._on_save()
            elif key == Qt.Key_N: self._on_next_series()
            elif key == Qt.Key_Q: self._on_quit_save()
            elif key == Qt.Key_R: self._on_include_all()
            elif key == Qt.Key_M:
                self._motion_check.setChecked(
                    not self._motion_check.isChecked())
        else:
            super().keyPressEvent(event)

    # ── Display refresh ────────────────────────────────────────────────────

    def _refresh(self):
        s  = self._s()
        ti = self.tilt_idx
        img   = self._get_image(ti)
        angle = s['angles'][ti] if ti < len(s['angles']) else ti
        excl  = s['excluded'][ti]
        cand  = s['flagged'][ti]
        meta  = s['frame_meta'][ti]  if ti < len(s['frame_meta'])  else {}
        mdata = self._get_motion(ti)

        # Tilt image with motion overlay (img may be None if the average is
        # missing — set_array handles None by clearing the panel)
        self.img_tilt.set_array(img, self.clo, self.chi,
                                 excluded=excl, candidate=cand,
                                 motion_data=mdata)

        # Power spectrum — crop to the signal band. The .mrc is 512x256
        # (half-Fourier); keep the central 128-row band so just the signal is
        # shown, as in the Warp GUI.
        ps_img = None
        if self.frame_dir and ti < len(s['movies']):
            ps_path = os.path.join(self.frame_dir, 'powerspectrum',
                                   s['movies'][ti])
            ps_img = load_mrc_image(ps_path)
        if ps_img is not None:
            ps_d = np.sqrt(np.abs(ps_img))
            h = ps_d.shape[0]
            if h >= 256:
                # keep the lower 128 rows (the signal half nearest the origin)
                ps_d = ps_d[h - 128:h, :]
            self.img_ps.set_array(ps_d, 2, 98)
        else:
            self.img_ps.set_array(None)

        # Overview — ordered by tilt angle (centre = 0 deg)
        ctf_vals = [m.get('ctf_res') for m in s['frame_meta']]
        self.overview.update_overview(s['excluded'], s['flagged'],
                                      ti, ctf_vals,
                                      order=s['angle_order'],
                                      angles=s['angles'])

        # Right-hand plots: CTF fit (current tilt) + res/defocus/motion scatter
        per_tilt_colours = [self.colors[self._categorise(i)]
                            for i in range(s['n'])]
        res_vals = [m.get('ctf_res') for m in s['frame_meta']]
        def_vals = [m.get('defocus') for m in s['frame_meta']]
        mot_vals = [m.get('motion')  for m in s['frame_meta']]
        self.plots.update_plots(
            s['angles'], res_vals, def_vals, mot_vals, per_tilt_colours,
            ti, meta, per_tilt_colours[ti])

        # Tilt title
        status = '  [EXCLUDED]' if excl else ('  [candidate]' if cand else '')
        col = (self.colors['excluded'] if excl else
               (self.colors['flagged'] if cand else C_TEXT))
        self.tilt_title.setText(
            f'Tilt {ti+1}/{s["n"]}   {angle:+.2f}\u00b0{status}')
        self.tilt_title.setStyleSheet(
            f"color: {col}; font-size: 13px; font-weight: bold; "
            f"padding: 2px 8px;")

        # Info bar
        parts = []
        if meta.get('ctf_res'):  parts.append(f"CTF: {meta['ctf_res']:.1f} \u00c5")
        if meta.get('defocus'):  parts.append(f"Defocus: {meta['defocus']:.3f} \u00b5m")
        if meta.get('motion') is not None:
                                 parts.append(f"Motion: {meta['motion']:.2f} \u00c5")
        parts.append(f"Series: {s['name']}")
        self.info_label.setText('    |    '.join(parts))

        # Series list highlight
        self.series_list_widget.blockSignals(True)
        self.series_list_widget.setCurrentRow(self.series_idx)
        self.series_list_widget.blockSignals(False)

    # ── Event handlers ─────────────────────────────────────────────────────

    def _on_series_changed(self, idx):
        if idx < 0 or idx == self.series_idx: return
        self.series_idx = idx; self.tilt_idx = 0
        self._load_series(idx); self._refresh()
        # Return focus to the main window so arrow keys / shortcuts work
        # immediately without needing to click elsewhere first
        self.setFocus()

    def _on_overview_click(self, idx):
        if idx != self.tilt_idx:
            self.tilt_idx = idx; self._refresh()

    def _on_prev(self):
        if self.tilt_idx > 0:
            self.tilt_idx -= 1; self._refresh()

    def _on_next(self):
        if self.tilt_idx < self._s()['n'] - 1:
            self.tilt_idx += 1; self._refresh()

    def _on_wheel(self, direction):
        """
        Scroll-wheel navigation over the tilt image. Steps through tilts in the
        angle-sorted display order (matching the overview bar), so scrolling up
        moves towards the right-hand (more positive) tilts and down towards the
        left (more negative).
        """
        s = self._s()
        order = s.get('angle_order') or list(range(s['n']))
        try:
            pos = order.index(self.tilt_idx)
        except ValueError:
            pos = 0
        pos = max(0, min(pos + direction, len(order) - 1))
        new_idx = order[pos]
        if new_idx != self.tilt_idx:
            self.tilt_idx = new_idx
            self._refresh()

    def _on_toggle(self):
        s = self._s()
        was = s['excluded'][self.tilt_idx]
        s['excluded'][self.tilt_idx] = not was
        if not was: _play_exclude_sound()
        self._refresh()

    def _on_include_all(self):
        self._s()['excluded'] = [False] * self._s()['n']
        self._refresh()

    def _categorise(self, i):
        """
        Return the overview-bar category for tilt i of the current series.
        One of: 'excluded', 'flagged', 'ctf_bad' (>10A), 'ctf_mod' (8-10A),
        'good'. Matches the colour logic in OverviewCanvas.update_overview.
        """
        s = self._s()
        if s['excluded'][i]:
            return 'excluded'
        if s['flagged'][i]:
            return 'flagged'
        ctf = s['frame_meta'][i].get('ctf_res') if i < len(s['frame_meta']) else None
        if ctf:
            if ctf > 10: return 'ctf_bad'
            if ctf > 8:  return 'ctf_mod'
        return 'good'

    def _exclude_category(self, category):
        """Exclude every tilt currently in the given category."""
        s = self._s()
        count = 0
        for i in range(s['n']):
            if not s['excluded'][i] and self._categorise(i) == category:
                s['excluded'][i] = True
                count += 1
        if count:
            _play_exclude_sound()
        self._refresh()
        self.statusBar().showMessage(
            f"Excluded {count} {category.replace('_', ' ')} tilt(s)", 3000)
        self.setFocus()

    def _exclude_all_frames(self):
        """
        Exclude every tilt in the current dataset, effectively rejecting the
        whole dataset. Asks for confirmation first, since it is a sweeping
        action. Nothing is written to disk until Save.
        """
        s = self._s()
        name = self._series_name(self.series_list[self.series_idx][0])
        already = all(s['excluded'][i] for i in range(s['n'])) if s['n'] else False
        if already:
            self.statusBar().showMessage(
                "All tilts in this dataset are already excluded", 3000)
            self.setFocus()
            return
        reply = QMessageBox.question(
            self, "Exclude entire dataset",
            f"Exclude ALL {s['n']} tilts in '{name}'?\n\n"
            f"This rejects the whole dataset by marking every tilt as excluded. "
            f"You can still re-include tilts afterwards, and nothing is written "
            f"to disk until you Save.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            self.setFocus()
            return
        count = 0
        for i in range(s['n']):
            if not s['excluded'][i]:
                s['excluded'][i] = True
                count += 1
        if count:
            _play_exclude_sound()
        self._refresh()
        self.statusBar().showMessage(
            f"Excluded all {s['n']} tilts in '{name}' \u2014 dataset rejected "
            f"(not saved until you Save)", 5000)
        self.setFocus()

    def _categorise_in(self, s, i):
        """Category of tilt i within an arbitrary loaded series dict s."""
        if s['excluded'][i]:
            return 'excluded'
        if s['flagged'][i]:
            return 'flagged'
        ctf = s['frame_meta'][i].get('ctf_res') if i < len(s['frame_meta']) \
            else None
        if ctf:
            if ctf > 10: return 'ctf_bad'
            if ctf > 8:  return 'ctf_mod'
        return 'good'

    def _exclude_category_all(self, category):
        """
        Exclude every tilt of the given category across ALL loaded datasets,
        after confirmation. This is a sweeping action, so it asks first and
        reports how many tilts across how many series were affected.
        """
        label = {'ctf_bad': 'CTF > 10 \u00c5', 'ctf_mod': 'CTF 8\u201310 \u00c5',
                 'flagged': 'flagged (intensity outlier)'}.get(
                     category, category)
        n_series = len(self.series_list)
        reply = QMessageBox.question(
            self, "Exclude across all datasets",
            f"Exclude every '{label}' tilt in ALL {n_series} datasets?\n\n"
            f"This marks matching tilts as excluded in every series. You can "
            f"still re-include them per series afterwards, and nothing is "
            f"written to disk until you Save.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            self.setFocus()
            return

        # Ensure every series is loaded so we can categorise its tilts
        total = 0
        affected_series = 0
        for idx in range(len(self.series_list)):
            self._load_series(idx)
            s = self._cache[idx]
            count = 0
            for i in range(s['n']):
                if not s['excluded'][i] and \
                        self._categorise_in(s, i) == category:
                    s['excluded'][i] = True
                    count += 1
            if count:
                affected_series += 1
                total += count
        if total:
            _play_exclude_sound()
        self._refresh()
        self.statusBar().showMessage(
            f"Excluded {total} '{label}' tilt(s) across {affected_series} "
            f"dataset(s)", 5000)

        # Offer to save all now, since the change spans many series and Save
        # only writes the current one.
        if total:
            save_reply = QMessageBox.question(
                self, "Save all datasets?",
                f"Excluded {total} tilt(s) across {affected_series} dataset(s).\n\n"
                f"Save these exclusions to all affected tilt-series XMLs now?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if save_reply == QMessageBox.Yes:
                self._save_all()
        self.setFocus()

    def _save_all(self):
        """Write exclusions to the XML for every loaded series."""
        saved = 0
        for idx in range(len(self.series_list)):
            if idx not in self._cache:
                continue
            s = self._cache[idx]
            if s.get('ts_xml') and any(s['excluded']):
                update_xml_usetilt(s['ts_xml'], s['excluded'],
                                   tilt_angles=s['angles'])
                saved += 1
        self.statusBar().showMessage(f"Saved {saved} dataset(s)", 4000)

    # ── Colour customisation ─────────────────────────────────────────────────

    def _restyle_bulk_buttons(self):
        """Recolour the bulk-exclude buttons from the live theme."""
        buttons = list(getattr(self, '_bulk_buttons', [])) + \
            list(getattr(self, '_allbulk_buttons', []))
        for b, category in buttons:
            colour = self.colors[category]
            b.setStyleSheet(f"""
                QPushButton {{
                    background: {colour}; color: #1a1a2e;
                    border: 1px solid #334155; border-radius: 4px;
                    padding: 6px 10px; font-size: 12px; font-weight: bold;
                }}
                QPushButton:hover   {{ border: 2px solid {C_TEXT}; }}
                QPushButton:pressed {{ background: {C_ACCENT}; color: {C_TEXT}; }}
            """)

    def _on_colors_changed(self):
        """Called live from the colour picker — repaint everything."""
        self._restyle_bulk_buttons()
        self._refresh()

    def _open_color_picker(self):
        dlg = ColorPickerDialog(self.colors, self._on_colors_changed, self)
        dlg.exec_()
        self.setFocus()

    # ── Save ───────────────────────────────────────────────────────────────

    def _save_current(self):
        s = self._s()
        n_excl = sum(s['excluded'])
        if n_excl == 0:
            print(f"  No exclusions for {s['name']}"); return
        # Exclusions are recorded ONLY in the tilt-series XML <UseTilt> field,
        # which is WarpTools' native mechanism. We deliberately do NOT remove
        # rows from the .tomostar: doing so shortens the file relative to the
        # 61-entry <UseTilt> list, which (a) breaks alignment when the state is
        # read back on reopen, and (b) means exclusions are applied twice once
        # ts_stack regenerates the stack. Keeping the tomostar full-length and
        # letting <UseTilt> drive exclusion keeps everything consistent and
        # round-trips correctly.
        if s['ts_xml']:
            update_xml_usetilt(s['ts_xml'], s['excluded'],
                               tilt_angles=s['angles'])
        else:
            print(f"  [WARN] No tilt-series XML for {s['name']} — "
                  "cannot save exclusions")

    def _on_save(self):
        self._save_current()
        self.statusBar().showMessage(f"Saved {self._s()['name']}", 3000)

    def _on_next_series(self):
        nxt = self.series_idx + 1
        if nxt < len(self.series_list):
            self.series_idx = nxt; self.tilt_idx = 0
            self._load_series(nxt); self._refresh()
            self.setFocus()
        else:
            self.statusBar().showMessage("Last series.", 2000)

    def _on_quit_save(self):
        self._save_current(); QApplication.quit()

    def closeEvent(self, event):
        self._save_current(); event.accept()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _show_splash(app, logo_path=None, seconds=5):
    """
    Compose and show the TomoTriage splash screen for a fixed number of
    seconds (hard hold) before the main window opens.

    The splash is built in code around the bare wordmark image: the wordmark is
    centred on a black card with a neon-blue border, and a white caption is
    drawn underneath. Returns the splash (still shown; caller finishes it) or
    None if no wordmark image is found (splash skipped entirely).

    Logo resolution order:
      1. an explicit --logo path
      2. 'logo.png' next to this script
    """
    from PyQt5.QtCore import QEventLoop, QTimer, QRectF

    candidates = []
    if logo_path:
        candidates.append(logo_path)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates += [
        os.path.join(here, 'logo.png'),
        os.path.join(here, 'tomotriage_logo.png'),
    ]
    logo = next((c for c in candidates if c and os.path.exists(c)), None)
    if not logo:
        return None   # skip splash entirely if the wordmark is missing

    wordmark = QPixmap(logo)
    if wordmark.isNull():
        return None

    # ---- Layout constants ----
    NEON   = QColor('#00e5ff')   # neon-blue border
    BLACK  = QColor('#000000')
    WHITE  = QColor('#ffffff')
    CARD_W       = 900           # overall splash width (px)
    BORDER       = 10            # neon border thickness
    PAD          = 26            # inner black padding around the wordmark
    CAPTION_H    = 54            # height of the caption band
    CAPTION_TEXT = ("TomoTriage - an interactive quality control tool "
                    "for tilt series data")

    # Scale the wordmark to fit the inner width, preserving aspect ratio
    inner_w = CARD_W - 2 * (BORDER + PAD)
    scaled = wordmark.scaledToWidth(inner_w, Qt.SmoothTransformation)
    card_h = BORDER + PAD + scaled.height() + PAD + CAPTION_H + BORDER

    # ---- Compose the card ----
    canvas = QPixmap(CARD_W, card_h)
    canvas.fill(NEON)   # border colour shows as the outer frame

    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

    # black interior (inside the neon border)
    painter.fillRect(BORDER, BORDER,
                     CARD_W - 2 * BORDER, card_h - 2 * BORDER, BLACK)

    # wordmark, centred horizontally in the padded area
    wx = (CARD_W - scaled.width()) // 2
    wy = BORDER + PAD
    painter.drawPixmap(wx, wy, scaled)

    # caption, white, centred in the caption band below the wordmark
    font = QFont()
    font.setPointSize(13)
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(WHITE)
    cap_top = wy + scaled.height() + PAD
    cap_rect = QRectF(BORDER, cap_top,
                      CARD_W - 2 * BORDER, CAPTION_H)
    painter.drawText(cap_rect, Qt.AlignCenter, CAPTION_TEXT)
    painter.end()

    splash = QSplashScreen(canvas)
    splash.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    splash.show()
    app.processEvents()

    # Hard hold for `seconds`, keeping the UI responsive meanwhile
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec_()

    return splash


def run_batch(tomostar_dir, frame_dir, xml_dir=None,
              sigma=3.0, contrast_lo=2, contrast_hi=98, loss_dir=None,
              logo=None, splash_seconds=5, io_workers=16):
    pairs = find_tilt_series(tomostar_dir, frame_dir, xml_dir)
    if not pairs:
        print(f"[ERROR] No .tomostar files in {tomostar_dir}"); sys.exit(1)
    print(f"Found {len(pairs)} tilt series")
    app = QApplication.instance() or QApplication(sys.argv)
    splash = _show_splash(app, logo, splash_seconds) if splash_seconds else None
    win = MainWindow(pairs, frame_dir, sigma, contrast_lo, contrast_hi,
                     loss_dir=loss_dir, xml_dir=xml_dir, io_workers=io_workers)
    win.show()
    if splash is not None:
        splash.finish(win)
    sys.exit(app.exec_())


def parse_args():
    p = argparse.ArgumentParser(
        description="TomoTriage \u2014 interactive WarpTools tilt series QC (PyQt5)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--tomostar_dir', metavar='DIR',
                      help="Directory of .tomostar files (batch mode)")
    mode.add_argument('--tomostar',     metavar='STAR',
                      help="A single .tomostar file (single-series mode)")
    p.add_argument('--frame_dir',   metavar='DIR', required=True,
                   help="Frame-series dir ($warp_fs) containing average/ "
                        "(per-tilt images + *_motion.json), powerspectrum/, "
                        "and per-frame XMLs. REQUIRED — images are loaded "
                        "from average/.")
    p.add_argument('--xml',         metavar='XML',
                   help="Tilt-series XML for single-series mode "
                        "(auto-detected next to the tomostar if omitted)")
    p.add_argument('--xml_dir',     metavar='DIR',
                   help="Directory of tilt-series XML files (batch mode; "
                        "defaults to the tomostar's own directory)")
    p.add_argument('--loss_dir',    metavar='DIR',
                   help="Directory of miss-alignment '*_alignment_loss.json' "
                        "files. When present, the alignment loss is included "
                        "in the dataset ranking (post-alignment mode).")
    p.add_argument('--sigma',       type=float, default=3.0)
    p.add_argument('--contrast_lo', type=int,   default=2)
    p.add_argument('--contrast_hi', type=int,   default=98)
    p.add_argument('--logo',        metavar='IMG',
                   help="Path to a splash-screen logo image. Defaults to "
                        "logo.png next to the script if present.")
    p.add_argument('--no_splash',   action='store_true',
                   help="Skip the startup splash screen.")
    p.add_argument('--io_workers',  type=int, default=16, metavar='N',
                   help="Number of parallel workers for reading ranking "
                        "metadata at startup (default 16). Higher can be faster "
                        "on high-latency network storage; 1 = serial. Tune if "
                        "startup is slow or the storage prefers fewer requests.")
    return p.parse_args()


def main():
    args = parse_args()
    splash_seconds = 0 if args.no_splash else 5
    if args.tomostar_dir:
        run_batch(args.tomostar_dir, args.frame_dir, args.xml_dir,
                  args.sigma, args.contrast_lo, args.contrast_hi,
                  loss_dir=args.loss_dir,
                  logo=args.logo, splash_seconds=splash_seconds,
                  io_workers=args.io_workers)
    else:
        ts_xml = args.xml
        if not ts_xml:
            auto = os.path.splitext(args.tomostar)[0] + '.xml'
            if os.path.exists(auto): ts_xml = auto
        app = QApplication.instance() or QApplication(sys.argv)
        splash = _show_splash(app, args.logo, splash_seconds) \
            if splash_seconds else None
        win = MainWindow([(args.tomostar, ts_xml)],
                         args.frame_dir, args.sigma,
                         args.contrast_lo, args.contrast_hi,
                         loss_dir=args.loss_dir, xml_dir=args.xml_dir,
                         io_workers=args.io_workers)
        win.show()
        if splash is not None:
            splash.finish(win)
        sys.exit(app.exec_())


if __name__ == '__main__':
    main()
