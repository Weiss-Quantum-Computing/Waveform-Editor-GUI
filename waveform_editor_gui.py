#!/usr/bin/env python
# -*- coding: ascii -*-
"""
Waveform Editor GUI - build, cut up and save arbitrary waveforms as CSV.

The BK4063B AWG GUI's waveform building with the instrument taken out: the same
shape library, the same segment assembler, the same preview - but nothing here
talks to hardware, and the only thing it produces is a file. Written for Python
2.7 on Windows XP, so it uses nothing but the standard library: no numpy, no
matplotlib, and the preview is drawn on a plain Tk canvas.

Beyond what the AWG panel does, a waveform can be built out of PIECES of other
waveforms: take a span out of a long record, split one into equal parts or into
fixed-length chunks, and lay the pieces end to end in any order with their own
scale, offset and repeat count.

File format, in and out: two columns, `index,value`, index counting from 1, no
header. The reader is more tolerant than the writer - it also takes a single
column of values, other delimiters, and `#` comment lines, so the AWG GUI's own
`Waveforms/*.csv` cache files load here directly.

The Supertime ramps tab turns an ILC drive (`time_us,voltage_V`) into the
two shape files the sequence plays - cut at the plateau, DC offset taken out of
every sample, `time_us,value` with no header - and works out the start and end
numbers for the edge from an EOM table.

Run with:  pythonw waveform_editor_gui.py     (pythonw = no console window)
           python  waveform_editor_gui.py --selftest    (checks the maths only)

Runs unchanged on Python 3, which is how it gets tested off the XP machine.
"""

from __future__ import division, print_function

import json
import math
import os
import re
import sys
from collections import OrderedDict

# Python 2 spells every one of these differently, and the XP machine has only
# Python 2. Both spellings are tried so the same file can be run on a modern
# interpreter to check a change before it is carried over.
try:                                            # Python 2.7
    import Tkinter as tk
    import ttk
    import tkFileDialog as filedialog
    import tkMessageBox as messagebox
except ImportError:                             # Python 3
    import tkinter as tk
    from tkinter import ttk
    from tkinter import filedialog
    from tkinter import messagebox

try:
    xrange                                      # noqa: F821  (Python 2)
except NameError:
    xrange = range

try:
    string_types = basestring                   # noqa: F821  (Python 2)
except NameError:
    string_types = str


def as_text(value):
    """Whatever came out of a widget, as ASCII this program can match on.

    Python 2 hands back `unicode` from every Tk entry, and str() on one with a
    micro sign in it raises UnicodeEncodeError - so `200us` typed with the real
    mu character failed as "cannot read that" instead of being understood. The
    characters that genuinely turn up in these boxes are folded here, and
    anything else becomes a question mark, which is not a number either way.
    """
    if value is None:
        return ""
    try:
        text = value if isinstance(value, string_types) else "%s" % (value,)
    except Exception:
        return ""
    out = []
    for char in text:
        code = ord(char)
        if code in (0xb5, 0x3bc):            # micro sign, Greek small mu
            out.append("u")
        elif code == 0x2212:                 # a minus sign pasted from a document
            out.append("-")
        elif code < 128:
            out.append(char)
        else:
            out.append("?")
    return "".join(out)


APP_NAME = "Waveform Editor"

# Remembered between sessions: the working folder, the sample rate, the window
# size. Kept out of the program folder so it survives the program being copied
# or replaced.
CONFIG_PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                           "Waveform-Editor-GUI", "config.json")

# Same palette as the AWG panel, so a waveform looks the same in both.
TRACE_COLOUR = "#1f77b4"          # the waveform being shown
PIECE_COLOUR = "#d62728"          # the part of it that is selected
SELECT_FILL = "#dce9f5"           # the shaded span behind the trace
GRID_COLOUR = "#dcdcdc"
ZERO_COLOUR = "#9a9a9a"
AXIS_COLOUR = "#000000"
NOTE_GREY = "#666666"
NOTE_WARN = "#cc6600"
BOUND_COLOUR = "#b0b0b0"          # segment boundaries in an assembled record

# Courier New rather than Consolas: Consolas ships with Vista and Office 2007,
# so on a bare XP install it silently falls back to something proportional and
# the columns of numbers stop lining up.
MONO_FONT = ("Courier New", 9)
# The library column grows to fit the longest name, between these bounds
# (characters); longer names are shortened with ".." rather than clipped.
NAME_COLUMN_MIN = 17
NAME_COLUMN_MAX = 44

BAD_NAME_CHARS = r'<>:"/\|?*'

# How many points will go into the Values box. A Tk text widget holding a line
# per sample is comfortable at ten thousand and unusable at a million, and the
# box is for reading and nudging a record rather than for holding a long one.
VALUES_MAX_LINES = 20000

# Refuse to build something absurd rather than hanging the window for a minute.
# Measured on a modern machine: a million points builds in 0.4 s, writes in
# 0.9 s (21 MB), reads back in 3 s, and the list itself holds about 32 MB. An
# XP-era machine is several times slower and has far less to spare, so the cap
# is two million - already ten times the longest arb any of these generators
# take, and still short of the point where the window stops responding.
MAX_POINTS = 2000000

# Samples per carrier cycle. Below two the carrier is aliased outright; below
# twelve it is technically resolved but visibly stepped, and the peaks read low.
ALIAS_LIMIT = 2.0
COARSE_LIMIT = 12.0


# ---------------------------------------------------------------------------
# Waveform library
#
# Every shape is a pure function of a point count and a parameter lookup, so it
# can be built, previewed and tested without a window open. Envelopes come out
# unipolar (0..1) because that is what an intensity control wants; the
# oscillating shapes come out bipolar (-1..+1). Either way the numbers are a
# shape - what they mean in volts is decided by whatever plays them.
#
# Ported from the BK4063B AWG GUI without numpy, sample for sample: the same
# record built there and here has to be the same record, because the CSVs are
# meant to move between the two.
# ---------------------------------------------------------------------------

ENV_CHOICES = ("None", "Blackman-Harris", "Gaussian", "Hann", "Tukey")


class Params(object):
    """Panel strings read as numbers, with a fallback when a box is empty."""

    def __init__(self, values):
        self.values = values or {}

    def num(self, key, default):
        try:
            text = as_text(self.values.get(key, "")).strip()
            return float(text) if text else default
        except ValueError:
            return default

    def txt(self, key, default=""):
        return as_text(self.values.get(key, "")).strip() or default

    def tones(self, key, default=(10.0,)):
        out = []
        for token in as_text(self.values.get(key, "")).replace(";", ",").split(","):
            token = token.strip()
            if token:
                try:
                    out.append(float(token))
                except ValueError:
                    pass
        return out or list(default)


def _unit(n):
    """0 .. 1 across the record, endpoints included."""
    if n < 2:
        return [0.0] * max(n, 0)
    last = n - 1.0
    return [i / last for i in xrange(n)]


def _centred(n):
    """-1 .. +1 across the record, endpoints included."""
    return [2.0 * u - 1.0 for u in _unit(n)]


def _gaussian(n, trunc):
    """Truncated Gaussian. trunc is the half-width of the record in sigma, so 3
    puts the ends at exp(-4.5) ~ 1% rather than cutting a visible step."""
    a = max(trunc, 1e-6)
    return [math.exp(-0.5 * (x * a) ** 2) for x in _centred(n)]


def _hann(n):
    """The symmetric Hann window, matching numpy.hanning point for point."""
    if n < 2:
        return [0.0] * max(n, 0)
    last = n - 1.0
    return [0.5 - 0.5 * math.cos(2.0 * math.pi * i / last) for i in xrange(n)]


def _blackman_harris(n):
    """The four-term Blackman-Harris window.

    Sidelobes at about -92 dB where the classic three-term Blackman manages
    -58, which is the whole reason to reach for a window here: the sidelobes
    are what drives the line you are trying not to drive.
    """
    out = []
    for u in _unit(n):
        x = 2.0 * math.pi * u
        out.append(0.35875 - 0.48829 * math.cos(x) + 0.14128 * math.cos(2 * x)
                   - 0.01168 * math.cos(3 * x))
    return out


def _tukey(n, flat):
    """Flat top with raised-cosine shoulders. flat=0 is a Hann, flat=1 a square."""
    flat = min(max(flat, 0.0), 1.0)
    taper = (1.0 - flat) / 2.0
    out = []
    for x in _unit(n):
        if taper > 0 and x < taper:
            out.append(0.5 * (1.0 - math.cos(math.pi * x / taper)))
        elif taper > 0 and x > 1.0 - taper:
            out.append(0.5 * (1.0 - math.cos(math.pi * (1.0 - x) / taper)))
        else:
            out.append(1.0)
    return out


def _trapezoid(n, rise, fall):
    rise, fall = max(rise, 0.0), max(fall, 0.0)
    if rise + fall > 1.0:                   # keep a sane shape if over-specified
        total = rise + fall
        rise, fall = rise / total, fall / total
    out = []
    for x in _unit(n):
        if rise > 0 and x < rise:
            out.append(x / rise)
        elif fall > 0 and x > 1.0 - fall:
            out.append((1.0 - x) / fall)
        else:
            out.append(1.0)
    return out


def _tanh_top(n, edge, flat):
    """Flat top with tanh shoulders - a smooth switch-on with no corner, which
    is what an AOM or EOM intensity ramp usually wants."""
    a = min(max(flat, 0.0), 1.0)
    w = max(edge, 1e-4)
    y = [0.5 * (math.tanh((x + a) / w) - math.tanh((x - a) / w))
         for x in _centred(n)]
    peak = max(y) if y else 0.0
    return [v / peak for v in y] if peak > 0 else y


def _sinc(x):
    """sin(pi x) / (pi x), and 1 at the origin - numpy's convention."""
    if x == 0.0:
        return 1.0
    a = math.pi * x
    return math.sin(a) / a


def _envelope(name, n, trunc=3.0, flat=0.5):
    if name in ("Blackman-Harris", "Blackman"):
        return _blackman_harris(n)
    if name == "Gaussian":
        return _gaussian(n, trunc)
    if name == "Hann":
        return _hann(n)
    if name == "Tukey":
        return _tukey(n, flat)
    return [1.0] * n


def _normalise(y):
    peak = max([abs(v) for v in y]) if y else 0.0
    return [v / peak for v in y] if peak > 0 else list(y)


def _build_gaussian(n, p):
    return _gaussian(n, p.num("trunc", 3.0))


def _build_blackman(n, p):
    return _blackman_harris(n)


def _build_hann(n, p):
    return _hann(n)


def _build_tukey(n, p):
    return _tukey(n, p.num("flat", 0.5))


def _build_sech(n, p):
    """Hyperbolic secant - the amplitude profile for adiabatic rapid passage,
    and analytically solvable as the Rosen-Zener model."""
    a = max(p.num("trunc", 4.0), 1e-6)
    return [1.0 / math.cosh(x * a) for x in _centred(n)]


def _build_sinc(n, p):
    """Bipolar. A sinc in time is a rectangle in frequency, so this is the
    starting point for a flat-topped spectral profile."""
    a = max(p.num("lobes", 4.0), 1e-6)
    return [_sinc(x * a) for x in _centred(n)]


def _build_square(n, p):
    width = min(max(p.num("width", 0.5), 0.0), 1.0)
    return [1.0 if abs(x) <= width else 0.0 for x in _centred(n)]


def _build_trapezoid(n, p):
    return _trapezoid(n, p.num("rise", 0.1), p.num("fall", 0.1))


def _build_tanh_top(n, p):
    return _tanh_top(n, p.num("edge", 0.1), p.num("flat", 0.6))


def _build_dc(n, p):
    """A flat level. On its own it is the hold between two ramps; with a
    carrier it is a rectangular-envelope burst."""
    return [p.num("level", 1.0)] * n


def _build_linear(n, p):
    start, end = p.num("start", 0.0), p.num("end", 1.0)
    return [start + (end - start) * u for u in _unit(n)]


def _build_exp(n, p):
    """Exponential approach from start to end. The usual evaporative-cooling
    ramp is start 1, end 0, with tau setting how hard the knee is."""
    start, end = p.num("start", 1.0), p.num("end", 0.0)
    tau = max(p.num("tau", 0.3), 1e-4)
    span = 1.0 - math.exp(-1.0 / tau)
    return [start + (end - start) * (1.0 - math.exp(-u / tau)) / span
            for u in _unit(n)]


def _build_smoothstep(n, p):
    """Minimum-jerk ramp: zero slope and zero curvature at both ends, which is
    what keeps a transport or a trap handover adiabatic."""
    start, end = p.num("start", 0.0), p.num("end", 1.0)
    return [start + (end - start) * (u ** 3) * (10 - 15 * u + 6 * u * u)
            for u in _unit(n)]


def _build_chirp(n, p):
    """Linear frequency sweep across the record, in cycles. Pair a chirp with a
    sech envelope for adiabatic rapid passage."""
    c0, c1 = p.num("c0", 10.0), p.num("c1", 100.0)
    env = _envelope(p.txt("env", "None"), n)
    out = []
    for i, u in enumerate(_unit(n)):
        out.append(math.sin(2.0 * math.pi * (c0 * u + 0.5 * (c1 - c0) * u * u))
                   * env[i])
    return out


def _build_multitone(n, p):
    """Sum of sines, each given as a whole number of cycles across the record so
    every tone closes cleanly when the waveform repeats."""
    unit = _unit(n)
    out = [0.0] * n
    for cycles in p.tones("tones"):
        w = 2.0 * math.pi * cycles
        for i in xrange(n):
            out[i] += math.sin(w * unit[i])
    out = _normalise(out)
    env = _envelope(p.txt("env", "None"), n)
    return [out[i] * env[i] for i in xrange(n)]


def _build_dgauss(n, p):
    """Derivative of a Gaussian: the quadrature half of a DRAG pulse. Put a
    Gaussian on one channel and this, scaled by beta, on the other."""
    trunc = max(p.num("trunc", 3.0), 1e-6)
    y = [-x * trunc ** 2 * math.exp(-0.5 * (x * trunc) ** 2)
         for x in _centred(n)]
    beta = p.num("beta", 1.0)
    return [v * beta for v in _normalise(y)]


# name -> (builder, [(label, key, default, choices or None)])
_CARRIER = [("Carrier cycles", "cycles", "0", None),
            ("Carrier phase (deg)", "cphase", "0", None)]

BUILD_SHAPES = OrderedDict([
    ("Gaussian",         (_build_gaussian,
                          [("Truncate (+/-sigma)", "trunc", "3", None)] + _CARRIER)),
    ("Blackman-Harris",  (_build_blackman, list(_CARRIER))),
    ("Hann",             (_build_hann, list(_CARRIER))),
    ("Tukey flat-top",   (_build_tukey,
                          [("Flat fraction", "flat", "0.5", None)] + _CARRIER)),
    ("Sech (ARP)",       (_build_sech,
                          [("Truncate (+/-units)", "trunc", "4", None)] + _CARRIER)),
    ("Sinc",             (_build_sinc,
                          [("Zero crossings", "lobes", "4", None)] + _CARRIER)),
    ("Square pulse",     (_build_square,
                          [("Width fraction", "width", "0.5", None)] + _CARRIER)),
    ("Trapezoid",        (_build_trapezoid,
                          [("Rise fraction", "rise", "0.1", None),
                           ("Fall fraction", "fall", "0.1", None)] + _CARRIER)),
    ("Tanh flat-top",    (_build_tanh_top,
                          [("Edge fraction", "edge", "0.1", None),
                           ("Flat fraction", "flat", "0.6", None)] + _CARRIER)),
    ("Hold (DC)",        (_build_dc,
                          [("Level", "level", "1", None)] + _CARRIER)),
    ("Linear ramp",      (_build_linear,
                          [("Start", "start", "0", None),
                           ("End", "end", "1", None)] + _CARRIER)),
    ("Exponential ramp", (_build_exp,
                          [("Start", "start", "1", None),
                           ("End", "end", "0", None),
                           ("Time constant", "tau", "0.3", None)] + _CARRIER)),
    ("Smoothstep ramp",  (_build_smoothstep,
                          [("Start", "start", "0", None),
                           ("End", "end", "1", None)] + _CARRIER)),
    ("Chirp",            (_build_chirp,
                          [("Start cycles", "c0", "10", None),
                           ("End cycles", "c1", "100", None),
                           ("Envelope", "env", "Blackman-Harris", ENV_CHOICES)])),
    ("Multitone",        (_build_multitone,
                          [("Cycles (comma list)", "tones", "10, 20, 35", None),
                           ("Envelope", "env", "None", ENV_CHOICES)])),
    ("Gaussian deriv",   (_build_dgauss,
                          [("Truncate (+/-sigma)", "trunc", "3", None),
                           ("Beta", "beta", "1", None)])),
])
BUILD_SLOTS = max([len(spec) for _, spec in BUILD_SHAPES.values()])


def build_waveform(shape, n_points, values):
    """Make the samples for one shape. Pure - no widgets, no files."""
    if shape not in BUILD_SHAPES:
        raise ValueError("unknown shape %r" % (shape,))
    n = int(n_points)
    if n < 2:
        raise ValueError("need at least 2 points")
    if n > MAX_POINTS:
        raise ValueError("%s points is more than this program will build (%s)"
                         % (fmt_count(n), fmt_count(MAX_POINTS)))
    builder, _ = BUILD_SHAPES[shape]
    p = Params(values)
    y = builder(n, p)

    cycles = p.num("cycles", 0.0)
    if cycles > 0:
        # An envelope times a carrier: the shape becomes the burst outline and
        # the carrier fills it, which is how a Raman or Rabi pulse is specified.
        phase = math.radians(p.num("cphase", 0.0))
        w = 2.0 * math.pi * cycles
        unit = _unit(n)
        y = [y[i] * math.sin(w * unit[i] + phase) for i in xrange(n)]
    return y


def shape_extras(shape):
    """A shape's own parameters as `key=value`, the carrier aside.

    The Build tab gives each of them a labelled box. An Assemble row has one
    free text field instead, so the defaults are written into it when the shape
    is picked - otherwise they are reachable but invisible, and you would have
    to already know that a Tukey takes `flat=` to discover that it does.
    """
    if shape not in BUILD_SHAPES:
        return ""
    return " ".join(["%s=%s" % (key, default)
                     for _, key, default, _ in BUILD_SHAPES[shape][1]
                     if key not in ("cycles", "cphase")])


def build_warnings(shape, n_points, values, rate=0.0, carrier_hz=None):
    """Ways a record of `n_points` is too coarse for what is being asked of it.

    A built record is a list of numbers with no time in it until a clock is
    named, so most of this is about the point count alone: how many points each
    carrier cycle gets, and whether a shape's own detail survives. `rate` only
    matters where the answer is in hertz - the carrier against Nyquist.
    """
    lines = []
    n = int(n_points) if n_points else 0
    if n < 2:
        return ["a record needs at least 2 points"]
    p = Params(values)

    cycles = p.num("cycles", 0.0)
    if cycles > 0:
        per_cycle = n / cycles
        if per_cycle < ALIAS_LIMIT:
            lines.append(
                "%g carrier cycles across %s points is %.2g points each - "
                "below two the carrier is aliased, and what comes out is not "
                "the tone you asked for" % (cycles, fmt_count(n), per_cycle))
        elif per_cycle < COARSE_LIMIT:
            lines.append(
                "%g carrier cycles across %s points is %.2g points each - the "
                "carrier is only just resolved, so the peaks will read low and "
                "the shape will be visibly stepped"
                % (cycles, fmt_count(n), per_cycle))
        if rate > 0 and carrier_hz and carrier_hz > rate / 2.0:
            lines.append(
                "a %s carrier is above the Nyquist limit of %s for %s - it "
                "will come out as a lower frequency, not as itself"
                % (fmt_hz(carrier_hz), fmt_hz(rate / 2.0), fmt_hz(rate)))

    # A shape's own detail is a fraction of the record, so it is the point
    # count that decides whether it survives - the clock never enters into it.
    for key, name in (("rise", "rise"), ("fall", "fall"), ("edge", "edge"),
                      ("flat", "flat top"), ("width", "width")):
        fraction = p.num(key, 0.0)
        if 0 < fraction < 1 and fraction * n < 8:
            lines.append("the %s is %g of the record, which is %.2g points - "
                         "too few to shape it" % (name, fraction, fraction * n))
    return lines


# ---------------------------------------------------------------------------
# Units, and reading half-typed numbers out of boxes
#
# A CSV here has no time axis in it - just an index and a value - so the native
# unit everywhere is the sample. A sample rate is optional, and setting one only
# adds a second way to say the same thing: `2ms` wherever a count is wanted,
# `5kHz` wherever a carrier is. One parser does each, so every box in the
# program accepts the same spellings.
# ---------------------------------------------------------------------------

_TIME_UNITS = {"s": 1.0, "ms": 1e-3, "m": 1e-3, "us": 1e-6, "u": 1e-6,
               "ns": 1e-9, "n": 1e-9}
_FREQ_UNITS = {"hz": 1.0, "khz": 1e3, "k": 1e3, "mhz": 1e6, "meg": 1e6,
               "ghz": 1e9, "g": 1e9}

_NUMBER = re.compile(r"([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*"
                     r"([a-zA-Z%]*)\Z")


def fmt_count(n):
    """1234567 -> '1,234,567', without depending on the locale."""
    sign = "-" if n < 0 else ""
    digits = str(abs(int(n)))
    groups = []
    while len(digits) > 3:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    groups.insert(0, digits)
    return sign + ",".join(groups)


def fmt_hz(value):
    if value == 0:
        return "0 Hz"
    for scale, suffix in ((1e6, " MHz"), (1e3, " kHz"), (1.0, " Hz")):
        if abs(value) >= scale:
            return "%.4g%s" % (value / scale, suffix)
    return "%.4g Hz" % (value,)


def fmt_secs(value):
    if value == 0:
        return "0 s"                   # not "0 ps", which is where the ladder
                                       # below lands and reads as a precision
                                       # claim rather than as the origin
    for scale, suffix in ((1.0, " s"), (1e-3, " ms"), (1e-6, " us"),
                          (1e-9, " ns")):
        if abs(value) >= scale:
            return "%.4g%s" % (value / scale, suffix)
    return "%.4g ps" % (value * 1e12,)


def as_float(text, default=0.0):
    try:
        return float(as_text(text).strip())
    except (TypeError, ValueError):
        return default


def as_bool(text, default=False):
    value = as_text(text).strip().lower()
    if value in ("on", "yes", "true", "1"):
        return True
    if value in ("off", "no", "false", "0", ""):
        return False
    return default


def parse_count(text, rate=0.0, total=0):
    """A number of samples, however it was written.

    A bare number is a sample count. `40%` is that fraction of `total`, which is
    how you ask for the middle half of a record without doing the arithmetic.
    `2ms`, `500us`, `3.5e-4s` need a sample rate to mean anything, and say so
    rather than quietly coming out as a count.

    Returns None for an empty box - every caller has its own idea of what a
    blank means (the whole record, the native length, no resampling), and none
    of them is this function's business.
    """
    raw = as_text(text).strip().lower().replace("sec", "s").replace(",", "")
    if not raw:
        return None
    match = _NUMBER.match(raw)
    if not match:
        raise ValueError("cannot read %r as a number of points" % (text,))
    value, suffix = float(match.group(1)), match.group(2)

    if suffix == "%":
        if total <= 0:
            raise ValueError("%r is a percentage, but there is nothing for it "
                             "to be a percentage of" % (text,))
        return int(round(value / 100.0 * total))
    if not suffix:
        return int(round(value))
    if suffix in ("pt", "pts", "point", "points", "sa", "samples"):
        return int(round(value))
    if suffix in _TIME_UNITS:
        if rate <= 0:
            raise ValueError("%r is a time, so it needs a sample rate - set "
                             "one in the top bar" % (text,))
        return int(round(value * _TIME_UNITS[suffix] * rate))
    raise ValueError("unknown unit %r in %r - use points, a percentage, or a "
                     "time like 2ms" % (suffix, text))


def parse_cycles(text, points, rate=0.0):
    """A carrier as cycles across the record.

    A bare number is already cycles, which is the one description that survives
    being replayed at any clock. `5kHz` is the useful thing to type, so it is
    converted here - cycles = frequency x points / rate. Returns
    (cycles, frequency in Hz or None).
    """
    raw = as_text(text).strip().lower().replace(" ", "")
    if not raw:
        return 0.0, None
    match = _NUMBER.match(raw)
    if not match:
        raise ValueError("cannot read %r as a carrier" % (text,))
    value, suffix = float(match.group(1)), match.group(2)
    if not suffix or suffix in ("c", "cyc", "cycles"):
        return value, None
    if suffix in _FREQ_UNITS:
        if rate <= 0:
            raise ValueError("%r is a frequency, so it needs a sample rate - "
                             "set one in the top bar" % (text,))
        if points < 1:
            raise ValueError("%r is a frequency, so the record needs a length "
                             "before it can be turned into cycles" % (text,))
        freq = value * _FREQ_UNITS[suffix]
        return freq * points / rate, freq
    raise ValueError("unknown unit %r in %r - use cycles or a frequency like "
                     "5kHz" % (suffix, text))


def parse_kv(text):
    """'trunc=3 flat=0.6' -> {'trunc': '3', 'flat': '0.6'}.

    Split on whitespace and semicolons but never on commas, so a value that is
    itself a list - Multitone's 'tones=10,20,35' - survives intact.
    """
    out = {}
    for token in re.split(r"[;\s]+", as_text(text).strip()):
        if not token:
            continue
        key, sep, value = token.partition("=")
        if not sep or not key.strip():
            raise ValueError("extra parameter %r should be key=value" % (token,))
        out[key.strip()] = value.strip()
    return out


# ---------------------------------------------------------------------------
# Waveform operations
#
# All pure: a list of floats in, a new list of floats out. Nothing mutates its
# argument, so an operation can be previewed and then thrown away, which is
# what makes the preview honest about what a button would do.
# ---------------------------------------------------------------------------

def resample(samples, n):
    """Linear interpolation onto n points, both ends kept.

    Stretching or squeezing a piece into whatever length a segment gives it is
    what lets one stored record appear at two lengths in the same waveform. It
    is interpolation, not resynthesis: squeezing a record with fine structure
    in it will alias that structure, and nothing here can prevent that.
    """
    src = len(samples)
    n = int(n)
    if n < 1:
        raise ValueError("cannot resample to %d points" % (n,))
    if n == src:
        return list(samples)
    if src == 0:
        raise ValueError("nothing to resample")
    if src == 1:
        return [samples[0]] * n
    if n == 1:
        return [samples[0]]
    step = (src - 1.0) / (n - 1.0)
    out = []
    for i in xrange(n):
        pos = i * step
        lo = int(pos)
        if lo >= src - 1:
            out.append(samples[-1])
        else:
            frac = pos - lo
            out.append(samples[lo] + (samples[lo + 1] - samples[lo]) * frac)
    return out


def take_piece(samples, first=None, last=None):
    """A span of a record, numbered the way the CSV numbers its rows.

    1-based and inclusive on both ends, so `1` to `100` is the first hundred
    samples and the numbers you type are the numbers in the file's first
    column. Off-by-one here would be invisible in the preview and wrong in
    every file the program wrote, so it is the one thing the self-test checks
    hardest.
    """
    n = len(samples)
    if n == 0:
        raise ValueError("that waveform is empty")
    first = 1 if first is None else int(first)
    last = n if last is None else int(last)
    if first < 1 or last < 1:
        raise ValueError("sample numbers start at 1, not 0")
    if first > n or last > n:
        raise ValueError("that waveform only has %s points, so there is no "
                         "sample %s" % (fmt_count(n), fmt_count(max(first, last))))
    if first > last:
        raise ValueError("the piece starts at %s and ends at %s - swap them, "
                         "or tick Reverse to play it backwards"
                         % (fmt_count(first), fmt_count(last)))
    return list(samples[first - 1:last])


def split_equal(samples, parts):
    """A record cut into `parts` pieces of as near equal length as they go.

    The boundaries are rounded from the exact fractions rather than each piece
    being floor(n/parts) long, so the pieces tile the record exactly and the
    last one is not left holding the remainder.
    """
    n = len(samples)
    parts = int(parts)
    if parts < 1:
        raise ValueError("split into at least one piece")
    if parts > n:
        raise ValueError("cannot cut %s points into %d pieces"
                         % (fmt_count(n), parts))
    out, edges = [], [int(round(i * n / parts)) for i in xrange(parts + 1)]
    for i in xrange(parts):
        out.append(list(samples[edges[i]:edges[i + 1]]))
    return out


def split_every(samples, size, keep_short=True):
    """A record cut into fixed-length chunks, left to right."""
    n = len(samples)
    size = int(size)
    if size < 1:
        raise ValueError("a chunk needs at least one point")
    out = []
    for start in xrange(0, n, size):
        chunk = list(samples[start:start + size])
        if len(chunk) < size and not keep_short:
            break
        out.append(chunk)
    if not out:
        raise ValueError("nothing came out of that split")
    return out


def scaled(samples, scale=1.0, offset=0.0):
    return [v * scale + offset for v in samples]


def normalised(samples):
    """Scaled so the largest excursion from zero is 1, sign kept."""
    peak = max([abs(v) for v in samples]) if samples else 0.0
    if peak <= 0:
        raise ValueError("that waveform is flat at zero - there is nothing to "
                         "normalise")
    return [v / peak for v in samples]


def unipolar(samples):
    """Stretched onto 0..1, which is what an intensity control wants."""
    if not samples:
        raise ValueError("that waveform is empty")
    lo, hi = min(samples), max(samples)
    if hi <= lo:
        raise ValueError("that waveform is flat, so it cannot be stretched "
                         "onto 0..1")
    span = hi - lo
    return [(v - lo) / span for v in samples]


def clipped(samples, low, high):
    if low > high:
        low, high = high, low
    return [min(max(v, low), high) for v in samples]


# ---------------------------------------------------------------------------
# Supertime ramps
#
# The sequence plays an EO ramp as a custom edge: a shape file, plus a start and
# an end value in the log's units. Only those two numbers go through the EOM
# interpolation table; the shape's own samples are laid between the two
# voltages as they are. So a shape file wants to be the ILC drive cut at its
# plateau, with the drive's small DC offset taken out of every sample (not just
# the end sample forced to zero, which leaves a step of the offset's size in
# the first 2 us), written as `time_us,value` with no header. These functions
# do that; the Supertime tab is their panel.
# ---------------------------------------------------------------------------

_TIME_COLUMN_SCALE = {"time_us": 1.0, "t_us": 1.0, "time_ms": 1000.0,
                      "t_ms": 1000.0, "time_s": 1e6, "t_s": 1e6, "time": 1.0}


def read_timed(path):
    """(time_us, values) from a two-column file with a time axis.

    The ILC drives are `time_us,voltage_V` under `#` comment lines; a header
    naming the time column in ms or s is rescaled to microseconds, and a file
    with no header is taken to be in microseconds already.
    """
    columns, names = read_table(path)
    if len(columns) < 2:
        raise ValueError("%s has one column: a Supertime ramp needs a time "
                         "axis beside the values" % (os.path.basename(path),))
    index, _ = value_column(columns, names)
    tcol = 1 if index == 0 else 0
    scale = 1.0
    if names and tcol < len(names):
        scale = _TIME_COLUMN_SCALE.get(names[tcol].strip().lower(), 1.0)
    t = [x * scale for x in columns[tcol]]
    v = list(columns[index])
    if len(t) < 3:
        raise ValueError("only %d points in %s" % (len(t), os.path.basename(path)))
    steps = [t[i + 1] - t[i] for i in xrange(len(t) - 1)]
    if min(steps) <= 0 or max(steps) - min(steps) > 1e-6 * max(abs(steps[0]), 1.0):
        raise ValueError("the time axis of %s is not a uniform grid"
                         % (os.path.basename(path),))
    return t, v


def plateau_split(values, tolerance=1e-3):
    """Index of the middle of the flat top: the up ramp is everything before it.

    The flat top is every sample within `tolerance` of the full stroke below the
    maximum. Splitting in the middle of it puts the cut where the drive is
    flattest, so neither half starts or ends on a slope.
    """
    hi, lo = max(values), min(values)
    if hi <= lo:
        raise ValueError("that record is flat - there is no plateau to split at")
    band = (hi - lo) * tolerance
    top = [i for i, x in enumerate(values) if x >= hi - band]
    return (top[0] + top[-1]) // 2


def split_at_time(t, values, split_us):
    """(up, down) halves, each (time_us, values), cut at the first sample >= split_us."""
    # A grid written to ten digits reads back as 5673.999999999999, and that
    # sample is the one at 5674: compare with a slack of a millionth of a step.
    slack = 1e-6 * abs(t[1] - t[0])
    k = len(t)
    for i, x in enumerate(t):
        if x >= split_us - slack:
            k = i
            break
    if k < 2 or k > len(t) - 2:
        raise ValueError("a split at %g us leaves nothing on one side" % (split_us,))
    return (t[:k], values[:k]), (t[k:], values[k:])


def remove_offset(values, anchor):
    """(values with the offset taken out, the offset).

    `anchor` is 'first' for an up ramp (the idle level is the first sample) or
    'last' for a down ramp (it is the last). The offset is subtracted from every
    sample, so the shape keeps its slope everywhere and only the level moves.
    """
    if anchor not in ("first", "last"):
        raise ValueError("anchor is 'first' or 'last', not %r" % (anchor,))
    offset = values[0] if anchor == "first" else values[-1]
    return [x - offset for x in values], offset


def zero_end_sample(values, anchor):
    """The `_fixed` files' recipe: only the anchor sample set to zero. Kept so
    a file made the old way can be reproduced and compared, not recommended."""
    out = list(values)
    if anchor == "first":
        out[0] = 0.0
    else:
        out[-1] = 0.0
    return out


def resample_grid(t, values, step_us):
    """(time_us, values) on a new grid of `step_us` from the first sample to the
    last, linear interpolation. Points past the last source sample are not made:
    the new grid ends at or before the source's last time."""
    if step_us <= 0:
        raise ValueError("the grid step must be positive")
    src_dt = t[1] - t[0]
    n = int(math.floor((t[-1] - t[0]) / step_us + 1e-9)) + 1
    out_t, out_v = [], []
    for i in xrange(n):
        x = t[0] + i * step_us
        pos = (x - t[0]) / src_dt
        lo = int(pos)
        if lo >= len(t) - 1:
            out_v.append(values[-1])
        else:
            frac = pos - lo
            out_v.append(values[lo] + (values[lo + 1] - values[lo]) * frac)
        out_t.append(x)
    return out_t, out_v


def write_supertime_csv(path, t, values):
    """`time_us,value` per line, comma, CRLF, no header, no final newline -
    byte for byte the layout of the ramp files the sequence already reads."""
    if len(t) != len(values):
        raise ValueError("time and values differ in length")
    lines = ["%.10g,%.10g" % (x, y) for x, y in zip(t, values)]
    handle = open(path, "wb")
    try:
        handle.write("\r\n".join(lines).encode("ascii"))
    finally:
        handle.close()


def ramp_timing(t, values, low=0.05, high=0.95, flat=0.01):
    """When a single up-hold-down drive moves, on its own time axis.

    `rise` is the low->high crossing on the way up, `fall` the high->low
    crossing on the way down (fractions of the stroke), `plateau` the first
    and last samples within `flat` of the top. `index` holds the samples
    behind each pair.
    """
    n = len(values)
    if n < 3:
        raise ValueError("too short to be a ramp")
    base, top = min(values), max(values)
    stroke = top - base
    if stroke <= 0:
        raise ValueError("a flat record has no ramp in it")

    def first_above(level):
        for i in range(n):
            if values[i] >= level:
                return i
        return n - 1

    def last_above(level):
        for i in range(n - 1, -1, -1):
            if values[i] >= level:
                return i
        return 0

    i_rs, i_re = first_above(base + low * stroke), first_above(base + high * stroke)
    i_fs, i_fe = last_above(base + high * stroke), last_above(base + low * stroke)
    i_ps, i_pe = first_above(top - flat * stroke), last_above(top - flat * stroke)
    return {"stroke": stroke, "base": base, "top": top,
            "rise": (t[i_rs], t[i_re]), "plateau": (t[i_ps], t[i_pe]),
            "fall": (t[i_fs], t[i_fe]),
            "index": {"rise": (i_rs, i_re), "plateau": (i_ps, i_pe),
                      "fall": (i_fs, i_fe)}}


def interp_to(t_new, t_old, values):
    """A timed record read off at other times, linearly; held flat past
    either end of the old record."""
    out = []
    j = 0
    m = len(t_old)
    for x in t_new:
        if x <= t_old[0]:
            out.append(values[0])
            continue
        if x >= t_old[-1]:
            out.append(values[-1])
            continue
        while j < m - 2 and t_old[j + 1] < x:
            j += 1
        t0, t1 = t_old[j], t_old[j + 1]
        f = (x - t0) / (t1 - t0) if t1 > t0 else 0.0
        out.append(values[j] + f * (values[j + 1] - values[j]))
    return out


def series_pair(t_a, v_a, t_b, v_b, guard_us=0.0):
    """Two drives meant to run in series: the outer one (A) does its whole
    swing first, the inner one (B) swings only while A sits at its top, and
    A comes back only after B has.

    The point of the arrangement: an EOM's extinction dips at its 45 degree
    point, and the motion is most sensitive to polarisation errors at the
    90 degree point of the total rotation. In parallel both EOMs sit at
    45 degrees exactly when the total is 90. In series the total passes 90
    with A at 90 and B at 0, both clean.

    Returns (lines, ok, angle, marks): a report, whether the pair passes,
    the total rotation in degrees on A's time axis (90 per full stroke of
    either drive), and plot marks at the segment boundaries.
    """
    if len(t_b) != len(t_a) or any(abs(x - y) > 1e-6 for x, y in zip(t_a, t_b)):
        v_b = interp_to(t_a, t_b, v_b)
    a, b = ramp_timing(t_a, v_a), ramp_timing(t_a, v_b)
    lines = []
    ok = [True]

    def bad(text):
        ok[0] = False
        lines.append("  !! " + text)

    for name, r in (("outer", a), ("inner", b)):
        lines.append("%-5s rise %6.0f..%6.0f us (%4.0f)  top %6.0f..%6.0f us (%4.0f)  "
                     "fall %6.0f..%6.0f us (%4.0f)  stroke %.3f V"
                     % (name, r["rise"][0], r["rise"][1], r["rise"][1] - r["rise"][0],
                        r["plateau"][0], r["plateau"][1], r["plateau"][1] - r["plateau"][0],
                        r["fall"][0], r["fall"][1], r["fall"][1] - r["fall"][0], r["stroke"]))
    lead = b["rise"][0] - a["plateau"][0]
    trail = a["plateau"][1] - b["fall"][1]
    lines.append("inner starts %.0f us after the outer is at its top; outer leaves its "
                 "top %.0f us after the inner is back (guard wanted %.0f us)"
                 % (lead, trail, guard_us))
    if lead < guard_us:
        bad("inner starts too early: the outer is still settling")
    if trail < guard_us:
        bad("outer comes down too early: the inner is still settling")
    fa = [(x - a["base"]) / a["stroke"] for x in v_a]
    fb = [(x - b["base"]) / b["stroke"] for x in v_b]
    angle = [90.0 * (p + q) for p, q in zip(fa, fb)]

    def crossing(fracs, level, rising):
        if rising:
            for i in range(len(fracs)):
                if fracs[i] >= level:
                    return i
        else:
            for i in range(len(fracs) - 1, -1, -1):
                if fracs[i] >= level:
                    return i
        return None

    for name, own, other in (("outer", fa, fb), ("inner", fb, fa)):
        for rising, word in ((True, "up"), (False, "down")):
            i = crossing(own, 0.5, rising)
            if i is None:
                continue
            lines.append("%s at 45 deg on the way %s, %.0f us: other EOM at %.1f deg, "
                         "total %.0f deg" % (name, word, t_a[i], 90.0 * other[i], angle[i]))
            if abs(angle[i] - 90.0) < 10.0:
                bad("that 45 deg extinction dip lands on the 90 deg point")
    for rising, word in ((True, "up"), (False, "down")):
        i = crossing([x / 90.0 for x in angle], 1.0, rising)
        if i is None:
            continue
        lines.append("total 90 deg on the way %s at %.0f us: outer %.1f deg, inner %.1f deg"
                     % (word, t_a[i], 90.0 * fa[i], 90.0 * fb[i]))
        if min(abs(fa[i] - 0.5), abs(fb[i] - 0.5)) < 0.1:
            bad("an EOM is near 45 deg at the total 90 deg point")
    lines.append("series pair OK" if ok[0] else "series pair NOT OK")
    ia, ib = a["index"], b["index"]
    starts = [(0, "outer up"), (ia["rise"][1], "outer top"), (ib["rise"][0], "inner up"),
              (ib["rise"][1], "inner top"), (ib["fall"][0], "inner down"),
              (ib["fall"][1], "outer top"), (ia["fall"][0], "outer down")]
    marks = []
    for k, (start, label) in enumerate(starts):
        nxt = starts[k + 1][0] if k + 1 < len(starts) else len(t_a)
        marks.append((start, label, max(nxt - start, 0)))
    return lines, ok[0], angle, marks


def nested_targets(step_us, lead_us, outer_rise_us, guard_us, inner_rise_us,
                   inner_hold_us, tail_us, outer_level=1.0, inner_level=1.0):
    """Two minimum-jerk ILC targets for a series pair on one time grid.

    The outer target rises, holds for guard + inner rise + inner hold +
    inner rise + guard, and falls; the inner one rises `guard_us` after the
    outer is up, holds, and is back down `guard_us` before the outer falls.
    Returns (t_us, outer, inner).
    """
    if step_us <= 0:
        raise ValueError("the grid step must be positive")
    for name, value in (("lead", lead_us), ("outer rise", outer_rise_us),
                        ("guard", guard_us), ("inner rise", inner_rise_us),
                        ("inner hold", inner_hold_us), ("tail", tail_us)):
        if value < 0:
            raise ValueError("%s cannot be negative" % (name,))
    outer_hold = 2.0 * guard_us + 2.0 * inner_rise_us + inner_hold_us
    total = lead_us + 2.0 * outer_rise_us + outer_hold + tail_us
    n = int(round(total / float(step_us))) + 1
    t = [i * float(step_us) for i in range(n)]

    def smooth(u):
        return u * u * u * (10.0 - 15.0 * u + 6.0 * u * u)

    def envelope(t0, rise, hold, level):
        out = []
        for x in t:
            if x < t0 or x >= t0 + 2.0 * rise + hold:
                y = 0.0
            elif x < t0 + rise:
                y = smooth((x - t0) / rise) if rise > 0 else 1.0
            elif x < t0 + rise + hold:
                y = 1.0
            else:
                y = 1.0 - smooth((x - t0 - rise - hold) / rise) if rise > 0 else 0.0
            out.append(level * y)
        return out

    outer = envelope(lead_us, outer_rise_us, outer_hold, outer_level)
    inner = envelope(lead_us + outer_rise_us + guard_us, inner_rise_us,
                     inner_hold_us, inner_level)
    return t, outer, inner


def read_eom_table(path):
    """[(card_volts, log_units), ...] from an EOM_X*.txt table, sorted by units.

    Left column is the card voltage, right column the number typed in the log
    and the sequence; the sequence interpolates linearly between the rows.
    """
    columns, names = read_table(path)
    if len(columns) != 2:
        raise ValueError("%s has %d columns; an EOM table has two: card volts "
                         "and log units" % (os.path.basename(path), len(columns)))
    rows = sorted(zip(columns[0], columns[1]), key=lambda r: r[1])
    for i in xrange(len(rows) - 1):
        if rows[i + 1][0] < rows[i][0]:
            raise ValueError("%s is not monotonic: the card voltage falls "
                             "between %g and %g units"
                             % (os.path.basename(path), rows[i][1], rows[i + 1][1]))
    return rows


def _interp_rows(rows, x, from_col, to_col):
    if x <= rows[0][from_col]:
        a, b = rows[0], rows[1]
    elif x >= rows[-1][from_col]:
        a, b = rows[-2], rows[-1]
    else:
        for i in xrange(len(rows) - 1):
            if rows[i][from_col] <= x <= rows[i + 1][from_col]:
                a, b = rows[i], rows[i + 1]
                break
    span = b[from_col] - a[from_col]
    if span == 0:
        return a[to_col]
    return a[to_col] + (b[to_col] - a[to_col]) * (x - a[from_col]) / span


def units_to_volts(rows, units):
    """What the card puts out for a number typed in the sequence (the table's
    own linear interpolation; beyond the last row the last segment is extended)."""
    return _interp_rows(rows, units, 1, 0)


def volts_to_units(rows, volts):
    """The number to type into the sequence to get `volts` out of the card."""
    return _interp_rows(rows, volts, 0, 1)



SHAPE_PREFIX = "shape: "


def source_choices(library_names):
    """What an Assemble row's Source box offers, in the order it offers it.

    The stored waveforms first, under their own names, and the built shapes
    after them behind a `shape:` prefix. The prefix is not decoration: without
    it a waveform saved as `Gaussian` and the Gaussian builder are the same
    string, and a row would silently mean whichever one the lookup happened to
    try first.
    """
    return list(library_names) + [SHAPE_PREFIX + name for name in BUILD_SHAPES]


def resolve_source(text, library):
    """An Assemble row's Source as ('wave', name) or ('shape', name)."""
    raw = as_text(text).strip()
    if raw.lower().startswith(SHAPE_PREFIX):
        name = raw[len(SHAPE_PREFIX):].strip()
        for candidate in BUILD_SHAPES:
            if candidate.lower() == name.lower():
                return "shape", candidate
        raise ValueError("unknown shape %r - one of: %s"
                         % (name, ", ".join(BUILD_SHAPES)))
    if raw in library:
        return "wave", raw
    for candidate in library:
        if candidate.lower() == raw.lower():
            return "wave", candidate
    for candidate in BUILD_SHAPES:
        if candidate.lower() == raw.lower():
            return "shape", candidate
    known = ", ".join(sorted(library)) or "nothing loaded"
    raise ValueError("no waveform called %r - the library holds: %s" % (raw, known))


def assemble(rows, library, gap_level=0.0, rate=0.0):
    """Pieces of other waveforms, and freshly built shapes, laid end to end.

    Each row names a source, optionally a span of it, and what to do with that
    span on the way in: resample it to a length, run it backwards, scale it,
    shift it, repeat it, and leave a gap after it. A row whose Source is blank
    is skipped, so a half-filled row at the bottom of the table is not an
    error.

    Returns (samples, marks), where marks is one
    (start index, label, points) per row - what the preview draws its segment
    boundaries from, and what the log lists afterwards.
    """
    out, marks = [], []
    for index, row in enumerate(rows, 1):
        text = as_text(row.get("source", "")).strip()
        if not text:
            continue
        try:
            kind, name = resolve_source(text, library)
            opts = parse_kv(row.get("options"))
            want = parse_count(row.get("points"), rate)

            if kind == "shape":
                if want is None:
                    raise ValueError(
                        "a built shape has no length of its own - put one in "
                        "the Points column")
                piece = build_waveform(name, want, opts)
                label = name
            else:
                src = library[name]
                total = len(src)
                first = parse_count(row.get("first"), rate, total)
                last = parse_count(row.get("last"), rate, total)
                piece = take_piece(src, first, last)
                label = name
                if first is not None or last is not None:
                    label = "%s[%s..%s]" % (
                        name, fmt_count(first or 1), fmt_count(last or total))
                if want is not None:
                    piece = resample(piece, want)

            if as_bool(opts.get("reverse")):
                piece = piece[::-1]
            scale = as_float(row.get("scale"), 1.0)
            offset = as_float(row.get("offset"), 0.0)
            if scale != 1.0 or offset != 0.0:
                piece = scaled(piece, scale, offset)

            repeat = int(round(as_float(row.get("repeat"), 1.0)))
            if repeat < 1:
                raise ValueError("Repeat is %d - a segment appears at least "
                                 "once, or leave the row's Source blank" % (repeat,))
            gap = parse_count(row.get("gap"), rate) or 0
            if gap < 0:
                raise ValueError("a gap cannot be negative")
        except ValueError as exc:
            raise ValueError("segment %d (%s): %s" % (index, text, exc))

        marks.append((len(out), label, len(piece) * repeat))
        for _ in xrange(repeat):
            out.extend(piece)
        if gap:
            out.extend([float(gap_level)] * gap)
        if len(out) > MAX_POINTS:
            raise ValueError("that comes to more than %s points, which is more "
                             "than this program will hold"
                             % (fmt_count(MAX_POINTS),))

    if not out:
        raise ValueError("no segments to build - fill in a row's Source first")
    return out, marks


def assembled_extent(rows, library, rate=0.0):
    """(rows used, points) without building anything.

    Wanted on every keystroke to keep the running total honest, which is why it
    swallows every error instead of raising at a half-typed number.
    """
    count, points = 0, 0
    for row in rows:
        text = as_text(row.get("source", "")).strip()
        if not text:
            continue
        count += 1
        try:
            kind, name = resolve_source(text, library)
            want = parse_count(row.get("points"), rate)
            if kind == "shape":
                length = want or 0
            else:
                total = len(library[name])
                first = parse_count(row.get("first"), rate, total) or 1
                last = parse_count(row.get("last"), rate, total) or total
                length = max(last - first + 1, 0)
                if want is not None:
                    length = want
            repeat = max(int(round(as_float(row.get("repeat"), 1.0))), 0)
            points += length * repeat + (parse_count(row.get("gap"), rate) or 0)
        except (ValueError, KeyError):
            continue
    return count, points


# ---------------------------------------------------------------------------
# Files
#
# What this program writes is exactly what was asked for: two columns,
# `index,value`, the index counting from 1, and nothing else in the file. What
# it reads is deliberately looser, because the files it will be pointed at were
# written by other things - a single column of samples, tabs or semicolons
# instead of commas, a header row from a spreadsheet, `#` comment lines from
# the AWG GUI's own waveform cache. Being strict on the way out and forgiving
# on the way in is the only combination that does not create work.
# ---------------------------------------------------------------------------

# Column headings that are an x axis rather than samples, so a file written as
# time,volts picks the volts. Without the scaled ones a file headed
# `time_us,voltage_V` picks column 0 and takes the TIME AXIS as the waveform -
# which looks like a clean ramp, so it reads as a plausible record rather than
# as a mistake.
INDEX_NAMES = set(("time", "time_s", "times", "t", "t_s", "sec", "secs",
                   "seconds", "s", "x", "index", "idx", "i", "n", "no", "num",
                   "point", "points", "sample", "samples", "row",
                   "time_us", "time_ms", "time_ns", "t_us", "t_ms", "t_ns",
                   "us", "ms", "ns", "usec", "msec"))


def split_fields(line):
    """One line of a table into its fields, trailing empty ones dropped.

    A spreadsheet asked to save a two-column sheet writes `1,0.5,,,` - the
    empty cells of columns it was never given. Those are not columns, and
    reading them as data is what makes a file that opens fine in Excel
    unloadable here. An empty field *between* two real ones is a hole in the
    data rather than a stray comma, so it is left to fail loudly instead of
    silently shifting every column after it.
    """
    if "," in line or ";" in line or "\t" in line:
        fields = [f.strip().strip('"') for f in re.split(r"[,;\t]", line)]
    else:
        fields = [f.strip().strip('"') for f in line.split()]
    while fields and fields[-1] == "":
        fields.pop()
    return fields


def strip_bom(text):
    """Drop a byte-order mark, whichever way the interpreter handed it over.

    Python 2 reads the three bytes as three characters; Python 3 decodes them
    to one. Testing the ordinal covers both without the source file itself
    having to contain a non-ASCII character.
    """
    if text[:3] == "\xef\xbb\xbf":
        return text[3:]
    if text and ord(text[0]) == 0xfeff:
        return text[1:]
    return text


def table_from_lines(lines, source="the data"):
    """Lines of a delimited table into (columns, column names or None).

    Column-major from the start rather than rows-then-transpose: a million-row
    file is the case this has to survive on a machine with a couple of hundred
    megabytes to spare, and holding it twice is what would stop it.
    """
    columns, names = None, None
    for number, raw in enumerate(lines, 1):
        line = strip_bom(raw.strip())
        if not line or line.startswith("#"):
            continue
        fields = split_fields(line)
        if not fields:
            continue
        try:
            values = [float(f) for f in fields]
        except ValueError:
            if columns is None and names is None:
                names = fields                     # a header row, taken as names
                continue
            raise ValueError("cannot read line %d of %s as numbers: %s"
                             % (number, source, line[:60]))
        if columns is None:
            columns = [[] for _ in values]
        if len(values) != len(columns):
            raise ValueError("line %d of %s has %d columns where the ones "
                             "before it had %d"
                             % (number, source, len(values), len(columns)))
        for i, value in enumerate(values):
            columns[i].append(value)
        if len(columns[0]) > MAX_POINTS:
            # Stopped here rather than after the read: the whole cost of a
            # runaway file is in getting to the end of it, so a cap that only
            # speaks up afterwards has already spent the minute it was meant
            # to save.
            raise ValueError("%s has more than %s rows, which is more than "
                             "this program will hold"
                             % (source, fmt_count(MAX_POINTS)))

    if not columns:
        raise ValueError("no numbers found in %s - this does not look like a "
                         "waveform file" % (source,))
    # Numbers typed across one line are a waveform, not one sample of many
    # channels.
    if len(columns[0]) == 1 and len(columns) > 2:
        columns, names = [[c[0] for c in columns]], None
    return columns, names


def read_table(path):
    """A waveform file into (columns, column names or None)."""
    handle = open(path, "r")
    try:
        return table_from_lines(handle, os.path.basename(path))
    finally:
        handle.close()


def looks_like_index(column):
    """True if this column is 1,2,3,... (or 0,1,2,...) and nothing else.

    Sampled rather than walked end to end: the arithmetic pins both ends and
    the length, and twenty spot checks in between catch anything that is not
    actually a counter. On a million-row file the difference is a redraw you
    notice and one you do not.
    """
    n = len(column)
    if n < 2:
        return False
    start = column[0]
    if start not in (0.0, 1.0):
        return False
    if column[-1] != start + n - 1:
        return False
    step = max(n // 20, 1)
    for i in xrange(0, n, step):
        if column[i] != start + i:
            return False
    return True


def value_column(columns, names):
    """(which column holds the samples, whether that is a guess worth asking about).

    The format this program writes - index, value - is recognised outright, so
    reloading its own output never asks anything.
    """
    count = len(columns)
    if count == 1:
        return 0, False
    if names and len(names) == count:
        for i, name in enumerate(names):
            if name.strip().lstrip("#").strip().lower() not in INDEX_NAMES:
                return i, False
    if count == 2:
        # Either the index this program writes or a time axis from something
        # else; both mean the samples are in the second column.
        return 1, not looks_like_index(columns[0])
    return 1, True


def write_csv(path, samples):
    """The two columns that were asked for, and nothing else in the file.

    Ten significant figures: the point of writing a waveform out is being able
    to read it back and get the same waveform, and %.7g - which is what the AWG
    GUI's cache uses, since a 16-bit DAC cannot tell the difference - loses
    digits that a chain of cuts and rescales here would notice.
    """
    handle = open(path, "w")
    try:
        # Built in blocks rather than a write per sample: on XP over a network
        # share, a million separate writes is the difference between a second
        # and most of a minute.
        block = []
        for index, value in enumerate(samples, 1):
            block.append("%d,%.10g" % (index, value))
            if len(block) >= 8192:
                handle.write("\n".join(block) + "\n")
                block = []
        if block:
            handle.write("\n".join(block) + "\n")
    finally:
        handle.close()


def write_time_csv(path, samples, rate, comment=None):
    """`time_us,voltage_V` with a `#` header: the layout the EOM-ILC targets
    and drives use, so a record edited here goes straight back to that
    program as a target or as a seed drive. The default `index,value` save
    is refused there by name -- it has no time axis, and a reader that
    guessed one once read a 5501-point drive as 5500 points at 1 s each.

    `rate` is the sample rate in Hz (the top-bar box); the time column is
    index / rate from 0, in microseconds, six decimals like the files this
    is meant to match. Ten significant figures on the values, as write_csv.
    """
    if not rate or rate <= 0:
        raise ValueError("set the sample rate in the top bar first - the "
                         "time column needs it (500kHz for the 2 us grid)")
    step_us = 1e6 / rate
    handle = open(path, "w")
    try:
        if comment:
            for line in as_text(comment).splitlines():
                handle.write("# " + line + "\n")
        handle.write("# Waveform Editor: %d points at %.6g us (sample rate "
                     "%.10g Hz), time_us from 0\n" % (len(samples), step_us, rate))
        handle.write("time_us,voltage_V\n")
        block = []
        for index, value in enumerate(samples):
            block.append("%.6f,%.10g" % (index * step_us, value))
            if len(block) >= 8192:
                handle.write("\n".join(block) + "\n")
                block = []
        if block:
            handle.write("\n".join(block) + "\n")
    finally:
        handle.close()


def safe_name(text):
    """Trim a typed name down to something legal in a filename."""
    out = "".join(["_" if c in BAD_NAME_CHARS else c
                   for c in as_text(text).strip()])
    return out.strip() or "waveform"


def unique_name(base, taken):
    """`base`, or `base_2`, `base_3`... - the first one nothing else is using."""
    name = safe_name(base)
    if name not in taken:
        return name
    for suffix in xrange(2, 100000):
        candidate = "%s_%d" % (name, suffix)
        if candidate not in taken:
            return candidate
    raise ValueError("cannot find an unused name based on %r" % (base,))


def describe(samples):
    """The one-line summary that follows a waveform everywhere it appears."""
    if not samples:
        return "empty"
    return "%s pts, %.6g to %.6g" % (fmt_count(len(samples)),
                                     min(samples), max(samples))


# ---------------------------------------------------------------------------
# Preview
#
# A scope trace on a plain Tk canvas. matplotlib for Python 2.7 is a wheel that
# may or may not be on an XP machine, and the whole program is worth nothing if
# it will not start - so the plot is drawn by hand out of the standard library,
# and there is nothing to install.
#
# Long records are drawn the way a scope draws them: one vertical line per pixel
# column spanning that column's min and max. A polyline through a million points
# squeezed into seven hundred pixels is both slow and a lie about what is in
# the record - the envelope is neither.
# ---------------------------------------------------------------------------

PLOT_LEFT, PLOT_RIGHT, PLOT_TOP, PLOT_BOTTOM = 62, 16, 12, 30


def nice_step(span, target):
    """A grid step of 1, 2 or 5 times a power of ten, about `target` of them."""
    if span <= 0:
        return 1.0
    raw = span / max(target, 1)
    mag = 10.0 ** math.floor(math.log10(raw))
    for mult in (1.0, 2.0, 5.0):
        if raw <= mult * mag:
            return mult * mag
    return 10.0 * mag


class Plot(object):
    """One waveform, optionally with a span picked out of it.

    `on_span` is called with (first, last) as a drag happens and with None when
    a click clears it; `on_hover` with (sample number, value) or None. Both are
    how the panels get a mouse that means something without any of them knowing
    anything about pixels.
    """

    def __init__(self, master, height=210, on_span=None, on_hover=None):
        self.canvas = tk.Canvas(master, height=height, background="white",
                                highlightthickness=1,
                                highlightbackground="#b0b0b0")
        self.on_span, self.on_hover = on_span, on_hover
        self.samples = []
        self.marks = []               # (start, label, points) per segment
        self.span = None              # (first, last), 1-based inclusive
        self.view = None              # (first, last) shown, None = the lot
        self.rate = 0.0
        self.pickable = False
        self.version = 0              # bumped whenever the samples change
        self._cache = (None, None)
        self._drag_from = None
        self.canvas.bind("<Configure>", lambda e: self.draw())
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda e: self._hover(None))

    # -- what is shown -----------------------------------------------------

    def show(self, samples, marks=None, span=None, rate=0.0, pickable=False,
             keep_view=False):
        self.samples = samples or []
        self.marks = marks or []
        self.span = span
        self.rate = rate
        self.pickable = pickable
        self.version += 1
        if not keep_view:
            self.view = None
        self._clamp_view()
        self.draw()

    def set_span(self, span):
        self.span = span
        self.draw()

    def set_view(self, view):
        self.view = view
        self._clamp_view()
        self.draw()

    def _clamp_view(self):
        n = len(self.samples)
        if not self.view or n < 2:
            self.view = None
            return
        lo, hi = self.view
        lo, hi = max(1, int(lo)), min(n, int(hi))
        self.view = (lo, hi) if hi - lo >= 1 else None

    def _window(self):
        n = len(self.samples)
        if self.view:
            return self.view
        return (1, n)

    # -- geometry ----------------------------------------------------------

    def _box(self):
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        return (PLOT_LEFT, PLOT_TOP, w - PLOT_RIGHT, h - PLOT_BOTTOM)

    def _yrange(self, lo, hi):
        window = self.samples[lo - 1:hi]
        if not window:
            return -1.0, 1.0
        low, high = min(window), max(window)
        if high - low < 1e-12:                 # a flat record still needs an axis
            pad = max(abs(high) * 0.1, 0.5)
            return low - pad, high + pad
        pad = (high - low) * 0.08
        return low - pad, high + pad

    def sample_at(self, px):
        """Which sample number a pixel column is over, 1-based and clamped."""
        left, _, right, _ = self._box()
        lo, hi = self._window()
        if right <= left or not self.samples:
            return None
        frac = (px - left) / float(right - left)
        return int(min(max(round(lo + frac * (hi - lo)), lo), hi))

    # -- drawing -----------------------------------------------------------

    def draw(self):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 80 or h < 60:
            return
        left, top, right, bottom = self._box()
        if right <= left or bottom <= top:
            return

        if len(self.samples) < 1:
            c.create_text((left + right) // 2, (top + bottom) // 2,
                          text="nothing to show", fill=NOTE_GREY)
            return

        lo, hi = self._window()
        ymin, ymax = self._yrange(lo, hi)

        def sy(v):
            return bottom - (v - ymin) / (ymax - ymin) * (bottom - top)

        def sx(i):
            if hi == lo:
                return left
            return left + (i - lo) / float(hi - lo) * (right - left)

        # The shaded span goes down first so the grid and the trace stay
        # readable through it - a highlight drawn over the trace hides the very
        # thing it is pointing at.
        if self.span:
            first, last = self.span
            if last >= lo and first <= hi:
                c.create_rectangle(sx(max(first, lo)), top, sx(min(last, hi)),
                                   bottom, fill=SELECT_FILL, outline="")

        step = nice_step(ymax - ymin, 5)
        tick = math.ceil(ymin / step) * step
        while tick <= ymax + step * 1e-6:
            y = sy(tick)
            c.create_line(left, y, right, y, fill=GRID_COLOUR)
            c.create_text(left - 5, y, text="%.4g" % (tick + 0.0,), anchor="e",
                          fill="#333333", font=("", 7))
            tick += step
        if ymin < 0 < ymax:
            c.create_line(left, sy(0.0), right, sy(0.0), fill=ZERO_COLOUR)

        # With a clock set the axis is a time axis, so the grid has to step in
        # time as well. Stepping in round sample numbers and labelling those
        # gave 499 us, 999 us, 1.499 ms - the right answer to a question
        # nobody asked.
        ticks = []
        if self.rate > 0:
            t_lo, t_hi = (lo - 1) / self.rate, (hi - 1) / self.rate
            step = nice_step(t_hi - t_lo, 6)
            value = math.ceil(t_lo / step) * step
            while value <= t_hi + step * 1e-9:
                ticks.append((sx(value * self.rate + 1), fmt_secs(value)))
                value += step
        else:
            step = max(nice_step(hi - lo, 6), 1.0)
            value = math.ceil(lo / step) * step
            while value <= hi:
                ticks.append((sx(value), fmt_count(int(round(value)))))
                value += step
        # The axis name sits in the bottom right corner, so a tick label that
        # would reach it is dropped - its gridline still says where it is.
        label_edge = right - 36
        for x, label in ticks:
            c.create_line(x, top, x, bottom, fill=GRID_COLOUR)
            if x <= label_edge:
                c.create_text(x, bottom + 4, text=label, anchor="n",
                              fill="#333333", font=("", 7))

        for start, _label, _points in self.marks[1:]:
            if lo <= start + 1 <= hi:
                x = sx(start + 1)
                c.create_line(x, top, x, bottom, fill=BOUND_COLOUR, dash=(2, 3))

        coords = self._coords(lo, hi, left, right, top, bottom, ymin, ymax)
        if len(coords) >= 4:
            c.create_line(*coords, **{"fill": TRACE_COLOUR, "width": 1})
        if self.span:
            first = max(self.span[0], lo)
            last = min(self.span[1], hi)
            if last > first:
                part = self._coords(first, last, sx(first), sx(last), top,
                                    bottom, ymin, ymax, cache=False)
                if len(part) >= 4:
                    c.create_line(*part, **{"fill": PIECE_COLOUR, "width": 1})

        c.create_rectangle(left, top, right, bottom, outline=AXIS_COLOUR)
        c.create_text(right, bottom + 4, anchor="ne", fill=NOTE_GREY,
                      font=("", 7),
                      text="time" if self.rate > 0 else "sample")

    def _coords(self, lo, hi, left, right, top, bottom, ymin, ymax, cache=True):
        """The polyline for samples lo..hi, decimated to the pixels available.

        Cached on everything it depends on, because a drag redraws on every
        mouse move and the envelope of a long record is the only expensive
        thing on the canvas.
        """
        key = (self.version, lo, hi, int(left), int(right), int(top),
               int(bottom), ymin, ymax)
        if cache and self._cache[0] == key:
            return self._cache[1]

        width = max(int(right - left), 1)
        count = hi - lo + 1
        yspan = (ymax - ymin) or 1.0
        scale_y = (bottom - top) / yspan
        out = []
        if count <= width * 1.5:
            span = float(hi - lo) or 1.0
            for i in xrange(lo, hi + 1):
                out.append(left + (i - lo) / span * (right - left))
                out.append(bottom - (self.samples[i - 1] - ymin) * scale_y)
        else:
            for column in xrange(width):
                first = lo + count * column // width
                last = lo + count * (column + 1) // width
                if last <= first:
                    continue
                chunk = self.samples[first - 1:last - 1]
                if not chunk:
                    continue
                x = left + column
                out.append(x)
                out.append(bottom - (min(chunk) - ymin) * scale_y)
                out.append(x)
                out.append(bottom - (max(chunk) - ymin) * scale_y)
        if cache:
            self._cache = (key, out)
        return out

    # -- mouse -------------------------------------------------------------

    def _press(self, event):
        if not self.pickable or not self.samples:
            return
        self._drag_from = (event.x, self.sample_at(event.x))

    def _drag(self, event):
        if self._drag_from is None:
            return
        start = self._drag_from[1]
        here = self.sample_at(event.x)
        if start is None or here is None:
            return
        self.span = (min(start, here), max(start, here))
        self.draw()
        if self.on_span:
            self.on_span(self.span)

    def _release(self, event):
        if self._drag_from is None:
            return
        moved = abs(event.x - self._drag_from[0])
        self._drag_from = None
        if moved < 3:                     # a click, not a drag: clear the span
            self.span = None
            self.draw()
            if self.on_span:
                self.on_span(None)

    def _motion(self, event):
        if not self.samples or not self.on_hover:
            return
        left, top, right, bottom = self._box()
        if not (left <= event.x <= right and top <= event.y <= bottom):
            self._hover(None)
            return
        index = self.sample_at(event.x)
        if index is None:
            self._hover(None)
            return
        self._hover((index, self.samples[index - 1]))

    def _hover(self, what):
        if self.on_hover:
            self.on_hover(what)


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------

def watch(var, callback):
    """Call `callback` whenever a Tk variable changes, on either Python.

    2.7 has only the old `trace`; 3.x deprecated it in favour of `trace_add`
    and has been threatening to remove it for years. Asking for the new one
    first means this keeps working in both directions.
    """
    try:
        var.trace_add("write", lambda *_: callback())
    except AttributeError:
        var.trace("w", lambda *_: callback())


def load_config():
    try:
        handle = open(CONFIG_PATH, "r")
        try:
            return json.load(handle)
        finally:
            handle.close()
    except Exception:
        return {}


def save_config(cfg):
    try:
        folder = os.path.dirname(CONFIG_PATH)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        handle = open(CONFIG_PATH, "w")
        try:
            json.dump(cfg, handle, indent=2)
        finally:
            handle.close()
    except Exception:
        pass                       # a settings file that will not save is not
                                   # a reason to refuse to close the window


# One Assemble row: (label, key, entry width). The order here is the order of
# the columns, and it is the order the values are read in too, so a column
# cannot be added in one place and forgotten in the other.
SEG_COLUMNS = [("Source", "source", 20),
               ("From", "first", 7),
               ("To", "last", 7),
               ("Points", "points", 8),
               ("Rep", "repeat", 4),
               ("Scale", "scale", 6),
               ("Offset", "offset", 6),
               ("Options", "options", 15),
               ("Gap after", "gap", 8)]
SEG_DEFAULT = dict([(key, "") for _, key, _ in SEG_COLUMNS])
SEG_DEFAULT.update({"repeat": "1", "scale": "1", "offset": "0", "gap": "0"})

# Live previews rebuild a shape on every keystroke. Past this many points that
# stops being instant, so the preview is built shorter and says so - the shape
# is specified in fractions of the record, so a shorter one is the same curve.
PREVIEW_MAX = 40000


class App(object):

    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        self.cfg = load_config()

        # name -> samples, and name -> how it came to exist. The second is only
        # ever shown, never written to a file: the format asked for has no room
        # for a comment, so provenance lives in the session and in the log.
        self.library = OrderedDict()
        self.origin = {}
        self.list_names = []
        # The names that are on disk as this program last left them. Without it
        # the close prompt cried wolf on every single exit, including the one
        # right after Save all - and a warning that is always wrong is a
        # warning nobody reads on the day it is right.
        self.saved = set()
        self.preview_job = None
        self._shown_key = None
        self._cut_last = None
        self.seg_rows = [dict(SEG_DEFAULT)]
        self.seg_vars = []

        self.folder = tk.StringVar(value=self.cfg.get("folder", ""))
        self.rate_text = tk.StringVar(value=self.cfg.get("rate", ""))
        # Ticked, Save CSV / Save all write `time_us,voltage_V` with a `#`
        # header (needs the sample rate) - the layout EOM-ILC reads as a
        # target or a seed drive. Off, the plain index,value file.
        self.ilc_header = tk.BooleanVar(value=bool(self.cfg.get("ilc_header", False)))

        self.build_ui()
        watch(self.rate_text, self.on_rate)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.geometry(self.cfg.get("geometry", "1010x730"))
        root.minsize(880, 620)
        self.log("%s - build waveforms, cut them up, save them as CSV." % APP_NAME)
        if self.ilc_header.get():
            self.log("Files are written as time_us,voltage_V with a # header "
                     "(ILC header ticked; needs the sample rate).")
        else:
            self.log("Files are written as index,value with the index counting "
                     "from 1 and no header. Tick 'ILC header' for the "
                     "time_us,voltage_V layout EOM-ILC reads.")

    # -- layout ------------------------------------------------------------

    def build_ui(self):
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        top = ttk.Frame(root)
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
        ttk.Label(top, text="Folder:").pack(side="left")
        ttk.Entry(top, textvariable=self.folder, width=44).pack(side="left",
                                                                padx=(4, 4))
        ttk.Button(top, text="Browse...", command=self.pick_folder).pack(side="left")
        ttk.Label(top, text="Sample rate:").pack(side="left", padx=(16, 4))
        ttk.Entry(top, textvariable=self.rate_text, width=10).pack(side="left")
        ttk.Label(top, text="Sa/s").pack(side="left", padx=(2, 0))
        self.rate_note = ttk.Label(top, text="", foreground=NOTE_GREY)
        self.rate_note.pack(side="left", padx=(8, 0))

        main = ttk.Frame(root)
        main.grid(row=1, column=0, sticky="nsew", padx=8, pady=2)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        self.build_library(main)

        right = ttk.Frame(main)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        self.build_preview(right)
        self.build_tabs(right)

        self.build_log(root)
        self.refresh_library()

    def build_library(self, parent):
        frame = ttk.LabelFrame(parent, text="Library")
        frame.grid(row=0, column=0, sticky="ns")
        frame.rowconfigure(0, weight=1)

        box = ttk.Frame(frame)
        box.grid(row=0, column=0, sticky="nsew", padx=6, pady=(6, 2))
        box.rowconfigure(0, weight=1)
        # exportselection=0 or the highlight vanishes the moment the focus goes
        # to any entry box in the window, which on this panel is constantly.
        self.listbox = tk.Listbox(box, width=NAME_COLUMN_MIN + 11, height=10, exportselection=0,
                                  font=MONO_FONT, activestyle="none")
        bar = ttk.Scrollbar(box, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=bar.set)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        self.listbox.bind("<<ListboxSelect>>", lambda e: self.on_select())
        self.listbox.bind("<Double-Button-1>", lambda e: self.do_rename())

        self.lib_note = ttk.Label(frame, text="", foreground=NOTE_GREY,
                                  justify="left", wraplength=210)
        self.lib_note.grid(row=1, column=0, sticky="w", padx=6, pady=(0, 4))

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 6))
        rows = [("Load CSV...", self.do_load_csv, "Save CSV...", self.do_save_csv),
                ("Load folder...", self.do_load_folder, "Save all...", self.do_save_all),
                ("Rename", self.do_rename, "Duplicate", self.do_duplicate),
                ("Remove", self.do_remove, "Remove all", self.do_remove_all)]
        for index, (left_text, left_cmd, right_text, right_cmd) in enumerate(rows):
            ttk.Button(buttons, text=left_text, width=14,
                       command=left_cmd).grid(row=index, column=0, pady=1)
            ttk.Button(buttons, text=right_text, width=14,
                       command=right_cmd).grid(row=index, column=1, pady=1, padx=(4, 0))
        # Every save goes through write_one, so this one box switches both
        # Save buttons to the layout EOM-ILC reads (time_us,voltage_V with a
        # # header). It needs the sample rate: the time column is index/rate.
        ttk.Checkbutton(buttons, text="ILC header (time_us,voltage_V)",
                        variable=self.ilc_header).grid(
            row=len(rows), column=0, columnspan=2, sticky="w", pady=(4, 0))

    def build_preview(self, parent):
        frame = ttk.LabelFrame(parent, text="Preview")
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        self.caption = ttk.Label(frame, text="", justify="left")
        self.caption.grid(row=0, column=0, sticky="w", padx=6, pady=(4, 2))

        self.plot = Plot(frame, on_span=self.on_plot_span,
                         on_hover=self.on_plot_hover)
        self.plot.canvas.grid(row=1, column=0, sticky="nsew", padx=6)

        under = ttk.Frame(frame)
        under.grid(row=2, column=0, sticky="ew", padx=6, pady=(2, 6))
        self.hover = ttk.Label(under, text="", foreground=NOTE_GREY,
                               font=MONO_FONT, width=34)
        self.hover.pack(side="left")
        ttk.Button(under, text="Show all",
                   command=self.do_zoom_all).pack(side="right")
        ttk.Button(under, text="Zoom to span",
                   command=self.do_zoom_span).pack(side="right", padx=(0, 4))

    def build_log(self, parent):
        frame = ttk.Frame(parent)
        frame.grid(row=2, column=0, sticky="ew", padx=8, pady=(2, 8))
        frame.columnconfigure(0, weight=1)
        self.logbox = tk.Text(frame, height=5, wrap="word", font=MONO_FONT,
                              background="#fbfbfb")
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=bar.set, state="disabled")
        self.logbox.grid(row=0, column=0, sticky="ew")
        bar.grid(row=0, column=1, sticky="ns")

    def log(self, text):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", text + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    # -- the library -------------------------------------------------------

    def rate(self):
        """The sample rate, or 0 when the box is empty or nonsense."""
        value = as_float(self.rate_text.get(), 0.0)
        return value if value > 0 else 0.0

    def on_rate(self):
        rate = self.rate()
        if not str(self.rate_text.get()).strip():
            self.rate_note.configure(text="optional", foreground=NOTE_GREY)
        elif rate <= 0:
            self.rate_note.configure(text="not a rate", foreground=NOTE_WARN)
        else:
            self.rate_note.configure(
                text="boxes now take 2ms and 5kHz too", foreground=NOTE_GREY)
        self.refresh_preview()

    def add_wave(self, name, samples, origin, select=True, quiet=False,
                 saved=False):
        """Put a waveform in the library under a name nothing else is using.

        `saved` says this one came off disk and is already there - which is
        true of a load and of nothing else, because everything the program
        makes itself exists only in the window until it is written out.
        """
        final = unique_name(name, self.library)
        self.library[final] = list(samples)
        self.origin[final] = origin
        if saved:
            self.saved.add(final)
        else:
            self.saved.discard(final)
        self.refresh_library(select=final if select else None)
        if not quiet:
            self.log("%s: %s (%s)" % (final, describe(samples), origin))
        if final != safe_name(name) and not quiet:
            self.log("  (%r was taken, so it went in as %r)"
                     % (safe_name(name), final))
        return final

    def refresh_library(self, select=None):
        keep = select or self.selected()
        self.listbox.delete(0, "end")
        self.list_names = list(self.library)
        # Fit the name column to the longest name so nothing is clipped.
        width = max([len(name) for name in self.list_names] + [NAME_COLUMN_MIN])
        width = min(width, NAME_COLUMN_MAX)
        self.listbox.configure(width=width + 11)
        self.lib_note.configure(wraplength=max(210, 7 * (width + 11)))
        for name in self.list_names:
            shown = name if len(name) <= width else name[:width - 2] + ".."
            # A star in the left margin is everything not written out yet.
            self.listbox.insert("end", "%s%-*s %9s" % (
                " " if name in self.saved else "*", width, shown,
                fmt_count(len(self.library[name]))))
        if keep in self.library:
            index = self.list_names.index(keep)
            self.listbox.selection_set(index)
            self.listbox.see(index)
        elif self.list_names:
            self.listbox.selection_set(0)
        self.refresh_sources()
        self.on_select()

    def selected(self):
        picked = self.listbox.curselection()
        if not picked:
            return None
        index = int(picked[0])
        return self.list_names[index] if index < len(self.list_names) else None

    def selected_samples(self):
        name = self.selected()
        return self.library.get(name) if name else None

    def on_select(self):
        name = self.selected()
        if not name:
            self.lib_note.configure(text="nothing loaded - build a shape, or "
                                         "load a CSV", foreground=NOTE_GREY)
        else:
            self.lib_note.configure(
                text="%s\n%s" % (describe(self.library[name]),
                                 self.origin.get(name, "")),
                foreground=NOTE_GREY)
            # Every tab that works on one waveform follows the selection, so
            # picking a name in the list is the one gesture that aims the whole
            # panel at it.
            for var in (self.cut_source, self.mod_source):
                if var.get() != name:
                    var.set(name)
        self.refresh_preview()

    def refresh_sources(self):
        """Keep every waveform-name dropdown in step with the library."""
        names = list(self.library)
        for box in (self.cut_box, self.mod_box):
            box.configure(values=names)
        for row in self.seg_vars:
            if "source_box" in row:
                row["source_box"].configure(values=source_choices(names))

    # -- files -------------------------------------------------------------

    def pick_folder(self):
        chosen = filedialog.askdirectory(initialdir=self.folder.get() or ".",
                                         title="Folder for waveform CSVs")
        if chosen:
            self.folder.set(os.path.normpath(chosen))

    def start_dir(self):
        folder = self.folder.get().strip()
        return folder if os.path.isdir(folder) else os.path.expanduser("~")

    def do_load_csv(self):
        paths = filedialog.askopenfilenames(
            title="Load waveform CSV", initialdir=self.start_dir(),
            filetypes=[("CSV and text", "*.csv *.txt *.dat"), ("All files", "*.*")])
        if not paths:
            return
        if isinstance(paths, string_types):        # some Tk builds return one string
            paths = self.root.tk.splitlist(paths)
        for path in paths:
            self.load_one(path, ask=len(paths) == 1)

    def do_load_folder(self):
        folder = filedialog.askdirectory(title="Load every CSV in a folder",
                                         initialdir=self.start_dir())
        if not folder:
            return
        self.folder.set(os.path.normpath(folder))
        names = sorted([f for f in os.listdir(folder)
                        if f.lower().endswith((".csv", ".txt", ".dat"))])
        if not names:
            messagebox.showinfo("Nothing to load",
                                "No .csv, .txt or .dat files in that folder.")
            return
        loaded = 0
        for entry in names:
            if self.load_one(os.path.join(folder, entry), ask=False,
                             quiet=True):
                loaded += 1
        self.log("Loaded %d of %d file(s) from %s" % (loaded, len(names), folder))

    def load_one(self, path, ask=True, quiet=False):
        """One file into the library. Returns the name it went in under."""
        try:
            columns, names = read_table(path)
        except Exception as exc:
            self.log("Could not read %s: %s" % (os.path.basename(path), exc))
            if not quiet:
                messagebox.showerror("Cannot read that file", str(exc))
            return None

        index, unsure = value_column(columns, names)
        if unsure and ask:
            index = self.ask_column(columns, names, os.path.basename(path))
            if index is None:
                return None
        elif unsure:
            self.log("  %s: %d columns, took column %d"
                     % (os.path.basename(path), len(columns), index + 1))

        samples = columns[index]
        if len(samples) < 2:
            self.log("Skipped %s: only %d point(s)"
                     % (os.path.basename(path), len(samples)))
            return None
        stem = os.path.splitext(os.path.basename(path))[0]
        origin = "loaded %s" % (os.path.basename(path),)
        if len(columns) > 1:
            origin += ", column %d" % (index + 1,)
        return self.add_wave(stem, samples, origin, quiet=quiet, saved=True)

    def ask_column(self, columns, names, filename):
        """Which column holds the samples, when the file does not make it clear."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Which column?")
        dialog.transient(self.root)
        dialog.grab_set()
        ttk.Label(dialog, justify="left", text=(
            "%s has %d columns. Which one holds the samples?"
            % (filename, len(columns)))).pack(anchor="w", padx=10, pady=(10, 6))

        labels = []
        for i, column in enumerate(columns):
            title = names[i] if names and i < len(names) else "column %d" % (i + 1,)
            labels.append("%d: %s   (%s)" % (i + 1, title, describe(column)))
        choice = tk.StringVar(value=labels[value_column(columns, names)[0]])
        combo = ttk.Combobox(dialog, textvariable=choice, values=labels,
                             state="readonly", width=52)
        combo.pack(padx=10, pady=(0, 10))

        answer = {"index": None}

        def take():
            answer["index"] = labels.index(choice.get())
            dialog.destroy()

        row = ttk.Frame(dialog)
        row.pack(padx=10, pady=(0, 10))
        ttk.Button(row, text="Use this column", command=take).pack(side="left")
        ttk.Button(row, text="Cancel", command=dialog.destroy).pack(side="left",
                                                                    padx=6)
        dialog.wait_window()
        return answer["index"]

    def do_save_csv(self):
        name = self.selected()
        if not name:
            messagebox.showinfo("Nothing selected",
                                "Pick a waveform in the library first.")
            return
        path = filedialog.asksaveasfilename(
            title="Save waveform as CSV", initialdir=self.start_dir(),
            initialfile=safe_name(name) + ".csv", defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        self.write_one(path, name)

    def write_one(self, path, name):
        """One waveform to one file, in whichever layout the ILC header box
        says: plain index,value, or time_us,voltage_V with a # header
        (needs the sample rate - refused with the reason, not guessed)."""
        ilc = bool(self.ilc_header.get())
        rate = self.rate()
        if ilc and rate <= 0:
            self.log("Not saved: 'ILC header' is ticked but no sample rate is set.")
            messagebox.showinfo(
                "Sample rate needed",
                "'ILC header' is ticked, so the file gets a time_us column, "
                "and that needs the sample rate in the top bar (500kHz is "
                "the 2 us grid the ILC targets and drives use). Set it, or "
                "untick the box for the plain index,value file.")
            return False
        try:
            if ilc:
                write_time_csv(path, self.library[name], rate,
                               comment="%s: %s" % (name, self.origin.get(name, name)))
            else:
                write_csv(path, self.library[name])
        except Exception as exc:
            self.log("Could not write %s: %s" % (path, exc))
            messagebox.showerror("Cannot save", str(exc))
            return False
        self.folder.set(os.path.dirname(os.path.abspath(path)))
        self.saved.add(name)
        self.refresh_library()
        if ilc:
            self.log("Saved %s -> %s (%s points, time_us,voltage_V at %.6g us, "
                     "# header)" % (name, path, fmt_count(len(self.library[name])),
                                    1e6 / rate))
        else:
            self.log("Saved %s -> %s (%s points, index,value from 1)"
                     % (name, path, fmt_count(len(self.library[name]))))
        return True

    def do_save_all(self):
        if not self.library:
            messagebox.showinfo("Nothing to save", "The library is empty.")
            return
        folder = filedialog.askdirectory(
            title="Save every waveform in the library as a CSV",
            initialdir=self.start_dir())
        if not folder:
            return
        clashes = [name for name in self.library
                   if os.path.exists(os.path.join(folder, safe_name(name) + ".csv"))]
        if clashes:
            preview = ", ".join(clashes[:6]) + (", ..." if len(clashes) > 6 else "")
            if not messagebox.askyesno(
                    "Overwrite?",
                    "%d file(s) in that folder would be overwritten:\n\n%s\n\n"
                    "Go ahead?" % (len(clashes), preview)):
                return
        self.folder.set(os.path.normpath(folder))
        ilc = bool(self.ilc_header.get())
        rate = self.rate()
        if ilc and rate <= 0:
            self.log("Not saved: 'ILC header' is ticked but no sample rate is set.")
            messagebox.showinfo(
                "Sample rate needed",
                "'ILC header' is ticked, so every file gets a time_us column, "
                "and that needs the sample rate in the top bar (500kHz is "
                "the 2 us grid). Set it, or untick the box.")
            return
        written = 0
        for name in self.library:
            path = os.path.join(folder, safe_name(name) + ".csv")
            try:
                if ilc:
                    write_time_csv(path, self.library[name], rate,
                                   comment="%s: %s" % (name, self.origin.get(name, name)))
                else:
                    write_csv(path, self.library[name])
                self.saved.add(name)
                written += 1
            except Exception as exc:
                self.log("Could not write %s: %s" % (path, exc))
        self.refresh_library()
        self.log("Saved %d waveform(s) to %s (%s)"
                 % (written, folder,
                    "time_us,voltage_V, # header" if ilc else "index,value"))

    # -- library housekeeping ---------------------------------------------

    def do_rename(self):
        name = self.selected()
        if not name:
            return
        new = self.ask_text("Rename", "New name for %r:" % (name,), name)
        if not new or safe_name(new) == name:
            return
        final = unique_name(new, self.library)
        # Rebuilt rather than reassigned, so the entry keeps its place in the
        # list instead of jumping to the bottom under its new name.
        rebuilt = OrderedDict()
        for key in self.library:
            if key == name:
                rebuilt[final] = self.library[key]
            else:
                rebuilt[key] = self.library[key]
        self.library = rebuilt
        self.origin[final] = self.origin.pop(name, "")
        # The file it was saved as still has the old name, so the renamed one
        # is not on disk under the name it now has.
        self.saved.discard(name)
        self.refresh_library(select=final)
        self.log("Renamed %s -> %s" % (name, final))

    def do_duplicate(self):
        name = self.selected()
        if not name:
            return
        self.add_wave(name, self.library[name], "copy of %s" % (name,))

    def do_remove(self):
        name = self.selected()
        if not name:
            return
        del self.library[name]
        self.origin.pop(name, None)
        self.saved.discard(name)
        self.refresh_library()
        self.log("Removed %s from the library (no file was touched)" % (name,))

    def do_remove_all(self):
        if not self.library:
            return
        if not messagebox.askyesno(
                "Empty the library?",
                "Remove all %d waveform(s) from the library?\n\nFiles already "
                "saved are not touched - anything not saved is gone."
                % (len(self.library),)):
            return
        self.library = OrderedDict()
        self.origin = {}
        self.saved = set()
        self.refresh_library()
        self.log("Library emptied.")

    def ask_text(self, title, prompt, initial=""):
        """A one-line prompt. Written out rather than using tkSimpleDialog so
        the two Pythons do not need two import names for it."""
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.grab_set()
        ttk.Label(dialog, text=prompt).pack(anchor="w", padx=10, pady=(10, 4))
        var = tk.StringVar(value=initial)
        entry = ttk.Entry(dialog, textvariable=var, width=34)
        entry.pack(padx=10)
        entry.selection_range(0, "end")
        entry.focus_set()
        answer = {"text": None}

        def take(*_):
            answer["text"] = var.get().strip()
            dialog.destroy()

        row = ttk.Frame(dialog)
        row.pack(padx=10, pady=10)
        ttk.Button(row, text="OK", command=take).pack(side="left")
        ttk.Button(row, text="Cancel", command=dialog.destroy).pack(side="left",
                                                                    padx=6)
        entry.bind("<Return>", take)
        dialog.bind("<Escape>", lambda e: dialog.destroy())
        dialog.wait_window()
        return answer["text"]

    # -- preview routing ---------------------------------------------------

    def on_plot_hover(self, what):
        if not what:
            self.hover.configure(text="")
            return
        index, value = what
        if self.rate() > 0:
            self.hover.configure(text="sample %s (%s) = %.6g"
                                 % (fmt_count(index),
                                    fmt_secs((index - 1) / self.rate()), value))
        else:
            self.hover.configure(text="sample %s = %.6g"
                                 % (fmt_count(index), value))

    def on_plot_span(self, span):
        """A drag on the preview fills in the Cut tab's From and To."""
        if span is None:
            self.cut_first.set("")
            self.cut_last.set("")
        else:
            self.cut_first.set(str(span[0]))
            self.cut_last.set(str(span[1]))

    def do_zoom_span(self):
        if self.plot.span:
            self.plot.set_view(self.plot.span)

    def do_zoom_all(self):
        self.plot.set_view(None)

    def schedule_preview(self, delay=180):
        """Redraw soon, and only once however many keystrokes arrive first."""
        if self.preview_job is not None:
            try:
                self.root.after_cancel(self.preview_job)
            except Exception:
                pass
        self.preview_job = self.root.after(delay, self.refresh_preview)

    def active_tab(self):
        """Which tab is showing, by FRAME rather than by index - the indices
        move every time a tab is added and nothing complains when they do."""
        try:
            current = self.tabs.select()
        except Exception:
            return None
        for key, frame in self.tab_frames.items():
            if str(frame) == str(current):
                return key
        return None

    def refresh_preview(self):
        self.preview_job = None
        tab = self.active_tab()
        if tab == "build":
            self.preview_build()
        elif tab == "cut":
            self.preview_cut()
        elif tab == "assemble":
            self.preview_assemble()
        elif tab == "modify":
            self.preview_modify()
        else:
            self.preview_selected()

    def select_in_list(self, name):
        """Move the library selection to `name`, from wherever it was picked.

        The list is the one place the panel says what it is pointed at, so a
        waveform chosen in a tab's own dropdown has to move it - otherwise the
        Modify tab reads `sweep` while the preview above it draws whatever
        happened to be highlighted, and the buttons act on the one you cannot
        see.
        """
        if name not in self.list_names:
            return
        index = self.list_names.index(name)
        if self.listbox.curselection() and int(self.listbox.curselection()[0]) == index:
            return
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(index)
        self.listbox.see(index)
        self.on_select()

    def show_trace(self, samples, caption, marks=None, span=None,
                   pickable=False, warn=False, key=None):
        """Put a trace on the canvas, and say underneath it what it is.

        `key` is what makes a zoom stick: it identifies what is being shown, so
        redrawing the same record after a keystroke keeps the window you had
        scrolled to, and showing a different one starts again at the full
        extent. Without it, refining a cut inside a zoomed view threw the view
        away on every character typed.
        """
        same = key is not None and key == self._shown_key
        self.plot.show(samples, marks=marks, span=span, rate=self.rate(),
                       pickable=pickable, keep_view=same)
        self._shown_key = key
        self.caption.configure(text=caption,
                               foreground=NOTE_WARN if warn else "#000000")

    def preview_modify(self):
        name = self.mod_source.get()
        if name not in self.library:
            self.preview_selected()
            return
        self.show_trace(self.library[name],
                        "%s - %s" % (name, describe(self.library[name])),
                        key=("wave", name, len(self.library[name])))

    def preview_selected(self):
        name = self.selected()
        if not name:
            self.show_trace([], "nothing selected")
            return
        self.show_trace(self.library[name],
                        "%s - %s" % (name, describe(self.library[name])),
                        key=("wave", name, len(self.library[name])))

    # -- tabs --------------------------------------------------------------

    def build_tabs(self, parent):
        self.tabs = ttk.Notebook(parent)
        self.tabs.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.tab_frames = OrderedDict()
        for key, title, builder in (
                ("build", "Build a shape", self.tab_build),
                ("cut", "Cut into pieces", self.tab_cut),
                ("assemble", "Assemble", self.tab_assemble),
                ("modify", "Modify", self.tab_modify),
                ("supertime", "Supertime ramps", self.tab_supertime),
                ("series", "Series pair", self.tab_series),
                ("values", "Values", self.tab_values)):
            frame = ttk.Frame(self.tabs)
            self.tabs.add(frame, text=title)
            self.tab_frames[key] = frame
            builder(frame)
        self.tabs.bind("<<NotebookTabChanged>>", lambda e: self.on_tab())

    def on_tab(self):
        if self.active_tab() == "values" and not self.values_box.get("1.0", "end").strip():
            self.do_values_load(quiet=True)
        self.refresh_preview()

    # -- tab: build a shape ------------------------------------------------

    def tab_build(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(top, text="Shape:").pack(side="left")
        self.shape = tk.StringVar(value="Gaussian")
        box = ttk.Combobox(top, textvariable=self.shape, values=list(BUILD_SHAPES),
                           width=17, height=len(BUILD_SHAPES), state="readonly")
        box.pack(side="left", padx=(4, 0))
        box.bind("<<ComboboxSelected>>", lambda e: self.on_shape())
        ttk.Label(top, text="Length:").pack(side="left", padx=(14, 4))
        self.build_len = tk.StringVar(value="10000")
        ttk.Entry(top, textvariable=self.build_len, width=11).pack(side="left")
        ttk.Label(top, text="points, or a time once a rate is set",
                  foreground=NOTE_GREY).pack(side="left", padx=(4, 0))

        grid = ttk.Frame(parent)
        grid.pack(fill="x", padx=8, pady=2)
        self.shape_labels, self.shape_vars, self.shape_boxes = [], [], []
        for slot in xrange(BUILD_SLOTS):
            row, col = divmod(slot, 3)
            label = ttk.Label(grid, text="")
            label.grid(row=row, column=col * 2, sticky="e", padx=(0, 4), pady=1)
            var = tk.StringVar()
            widget = ttk.Combobox(grid, textvariable=var, width=14)
            widget.grid(row=row, column=col * 2 + 1, sticky="w", padx=(0, 14),
                        pady=1)
            watch(var, self.on_build_change)
            self.shape_labels.append(label)
            self.shape_vars.append(var)
            self.shape_boxes.append(widget)

        bottom = ttk.Frame(parent)
        bottom.pack(fill="x", padx=8, pady=(2, 8))
        ttk.Label(bottom, text="Name:").pack(side="left")
        self.build_name = tk.StringVar(value="gaussian")
        ttk.Entry(bottom, textvariable=self.build_name, width=30).pack(
            side="left", padx=(4, 6))
        ttk.Button(bottom, text="Build", command=self.do_build).pack(side="left")
        self.build_note = ttk.Label(bottom, text="", foreground=NOTE_GREY,
                                    justify="left", wraplength=430)
        self.build_note.pack(side="left", padx=(12, 0))

        watch(self.build_len, self.on_build_change)
        self.on_shape()

    def on_shape(self):
        """Relabel the parameter slots for the shape now selected."""
        shape = self.shape.get()
        spec = BUILD_SHAPES[shape][1]
        for slot in xrange(BUILD_SLOTS):
            label, var, widget = (self.shape_labels[slot], self.shape_vars[slot],
                                  self.shape_boxes[slot])
            if slot < len(spec):
                text, key, default, choices = spec[slot]
                if key == "cycles":
                    text = "Carrier (cycles or Hz)"
                label.configure(text=text + ":", foreground="#000000")
                widget.configure(values=list(choices) if choices else (),
                                 state="readonly" if choices else "normal")
                var.set(default)
            else:
                label.configure(text="")
                widget.configure(values=(), state="disabled")
                var.set("")
        self.build_name.set(safe_name(
            shape.split("(")[0].strip().replace(" ", "_").replace("-", "_").lower()))
        self.on_build_change()

    def build_spec(self):
        """(shape, points, parameter values, carrier in Hz or None). Raises."""
        shape = self.shape.get()
        spec = BUILD_SHAPES[shape][1]
        values = {}
        for i, item in enumerate(spec):
            values[item[1]] = self.shape_vars[i].get()
        points = parse_count(self.build_len.get(), self.rate())
        if points is None:
            raise ValueError("give the record a length")
        carrier = None
        if "cycles" in values:
            cycles, carrier = parse_cycles(values["cycles"], points, self.rate())
            values["cycles"] = "%.10g" % (cycles,)
        return shape, points, values, carrier

    def on_build_change(self):
        if not hasattr(self, "build_note"):
            return                                  # still assembling the panel
        try:
            shape, points, values, carrier = self.build_spec()
        except ValueError as exc:
            self.build_note.configure(text=str(exc), foreground=NOTE_WARN)
            self.schedule_preview()
            return
        parts = []
        rate = self.rate()
        if points >= 2 and rate > 0:
            # Played straight through at this clock a record of n points lasts
            # n/rate and repeats at rate/n. Both are worth knowing before it is
            # built rather than after it is in a file.
            parts.append("%s pts = %s, repeats at %s"
                         % (fmt_count(points), fmt_secs(points / rate),
                            fmt_hz(rate / points)))
        elif points >= 2:
            parts.append("%s pts" % (fmt_count(points),))
        if carrier:
            parts.append("carrier %s = %s cycles"
                         % (fmt_hz(carrier), values["cycles"]))
        warnings = build_warnings(shape, points, values, rate, carrier)
        self.build_note.configure(
            text=" | ".join(parts + warnings[:1]),
            foreground=NOTE_WARN if warnings else NOTE_GREY)
        self.schedule_preview()

    def preview_build(self):
        try:
            shape, points, values, carrier = self.build_spec()
            shown = min(points, PREVIEW_MAX)
            data = build_waveform(shape, shown, values)
        except Exception as exc:
            self.show_trace([], str(exc), warn=True)
            return
        caption = "%s - %s pts" % (shape, fmt_count(points))
        if shown != points:
            # A shape is specified in fractions of its record, so drawing a
            # short one shows the same curve - only the sampling of it differs.
            caption += " (drawn at %s to keep the preview live)" % (
                fmt_count(shown),)
        self.show_trace(data, caption, key=("build", shape, points, shown))

    def do_build(self):
        try:
            shape, points, values, carrier = self.build_spec()
            data = build_waveform(shape, points, values)
        except Exception as exc:
            self.log("Could not build %s: %s" % (self.shape.get(), exc))
            messagebox.showerror("Cannot build that", str(exc))
            return
        spec = BUILD_SHAPES[shape][1]
        detail = ", ".join(["%s=%s" % (item[1], values[item[1]])
                            for item in spec if values.get(item[1])])
        self.add_wave(self.build_name.get() or shape, data,
                      "built %s (%s)" % (shape, detail))
        for line in build_warnings(shape, points, values, self.rate(), carrier):
            self.log("  warning: %s" % (line,))

    # -- tab: cut into pieces ---------------------------------------------

    def tab_cut(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(top, text="Source:").pack(side="left")
        self.cut_source = tk.StringVar()
        self.cut_box = ttk.Combobox(top, textvariable=self.cut_source, width=36,
                                    state="readonly")
        self.cut_box.pack(side="left", padx=(4, 8))
        self.cut_box.bind("<<ComboboxSelected>>",
                          lambda e: self.select_in_list(self.cut_source.get()))
        ttk.Label(top, text="Drag across the preview to pick a span; click it "
                            "to clear.", foreground=NOTE_GREY).pack(side="left")

        row = ttk.Frame(parent)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="From:").pack(side="left")
        self.cut_first = tk.StringVar()
        ttk.Entry(row, textvariable=self.cut_first, width=9).pack(side="left",
                                                                  padx=(4, 8))
        ttk.Label(row, text="To:").pack(side="left")
        self.cut_last = tk.StringVar()
        ttk.Entry(row, textvariable=self.cut_last, width=9).pack(side="left",
                                                                 padx=(4, 8))
        ttk.Label(row, text="Name:").pack(side="left", padx=(6, 0))
        self.cut_name = tk.StringVar()
        ttk.Entry(row, textvariable=self.cut_name, width=30).pack(side="left",
                                                                  padx=(4, 6))
        ttk.Button(row, text="Take piece", command=self.do_take).pack(side="left")
        self.cut_note = ttk.Label(parent, text="", foreground=NOTE_GREY,
                                  justify="left", wraplength=560)
        self.cut_note.pack(anchor="w", padx=8, pady=(0, 2))

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=8, pady=4)

        split = ttk.Frame(parent)
        split.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(split, text="Split the whole source into").grid(
            row=0, column=0, sticky="w")
        self.cut_parts = tk.StringVar(value="4")
        ttk.Entry(split, textvariable=self.cut_parts, width=6).grid(
            row=0, column=1, padx=4)
        ttk.Label(split, text="equal pieces").grid(row=0, column=2, sticky="w")
        ttk.Button(split, text="Split", width=8,
                   command=self.do_split_equal).grid(row=0, column=3, padx=(8, 0))

        ttk.Label(split, text="or into chunks of").grid(row=1, column=0,
                                                        sticky="w", pady=(4, 0))
        self.cut_chunk = tk.StringVar(value="1000")
        ttk.Entry(split, textvariable=self.cut_chunk, width=6).grid(
            row=1, column=1, padx=4, pady=(4, 0))
        ttk.Label(split, text="points each").grid(row=1, column=2, sticky="w",
                                                  pady=(4, 0))
        ttk.Button(split, text="Split", width=8,
                   command=self.do_split_chunks).grid(row=1, column=3,
                                                      padx=(8, 0), pady=(4, 0))
        self.cut_keep_short = tk.BooleanVar(value=True)
        ttk.Checkbutton(split, text="keep a short last chunk",
                        variable=self.cut_keep_short).grid(row=1, column=4,
                                                           sticky="w", padx=(10, 0),
                                                           pady=(4, 0))
        ttk.Label(split, text="Pieces are named after the source with _1, _2, "
                             "... on the end.", foreground=NOTE_GREY).grid(
            row=2, column=0, columnspan=5, sticky="w", pady=(6, 0))

        for var in (self.cut_first, self.cut_last, self.cut_source):
            watch(var, self.on_cut_change)

    def cut_src(self):
        name = self.cut_source.get()
        return name if name in self.library else None

    def cut_span(self):
        """(first, last) from the boxes, defaulted to the whole record. Raises."""
        name = self.cut_src()
        if not name:
            raise ValueError("pick a source waveform")
        total = len(self.library[name])
        first = parse_count(self.cut_first.get(), self.rate(), total)
        last = parse_count(self.cut_last.get(), self.rate(), total)
        first = 1 if first is None else first
        last = total if last is None else last
        return first, last

    def on_cut_change(self):
        name = self.cut_src()
        if name != self._cut_last:
            # Prior-leak guard: a span of 4,200 to 11,800 carried onto a
            # 3,000-point record is not a span, it is an error message waiting
            # to happen - and worse, 1 to 500 carried onto a different record
            # is a piece of something you did not mean.
            self._cut_last = name
            if self.cut_first.get() or self.cut_last.get():
                self.cut_first.set("")
                self.cut_last.set("")
                return                  # the two set() calls come back through
            self.cut_name.set("")
        if not name:
            self.cut_note.configure(text="pick a source waveform",
                                    foreground=NOTE_GREY)
            self.schedule_preview()
            return
        total = len(self.library[name])
        try:
            first, last = self.cut_span()
            take_piece(self.library[name], first, last)      # for the error only
        except ValueError as exc:
            self.cut_note.configure(text=str(exc), foreground=NOTE_WARN)
            self.schedule_preview()
            return
        count = last - first + 1
        text = ("%s to %s of %s = %s points (%.3g%% of it)"
                % (fmt_count(first), fmt_count(last), fmt_count(total),
                   fmt_count(count), 100.0 * count / total))
        if self.rate() > 0:
            text += ", %s" % (fmt_secs(count / self.rate()),)
        self.cut_note.configure(text=text, foreground=NOTE_GREY)
        if not self.cut_name.get().strip():
            self.cut_name.set("%s_piece" % (name,))
        self.schedule_preview()

    def preview_cut(self):
        name = self.cut_src()
        if not name:
            self.show_trace([], "pick a source waveform on the Cut tab")
            return
        data = self.library[name]
        span = None
        try:
            first, last = self.cut_span()
            if 1 <= first <= last <= len(data):
                span = (first, last)
        except ValueError:
            pass
        self.show_trace(data, "%s - %s" % (name, describe(data)), span=span,
                        pickable=True, key=("cut", name, len(data)))

    def do_take(self):
        name = self.cut_src()
        if not name:
            messagebox.showinfo("No source", "Pick a source waveform first.")
            return
        try:
            first, last = self.cut_span()
            data = take_piece(self.library[name], first, last)
        except ValueError as exc:
            messagebox.showerror("Cannot take that piece", str(exc))
            return
        target = self.cut_name.get().strip() or ("%s_piece" % (name,))
        self.add_wave(target, data, "%s[%s..%s]"
                      % (name, fmt_count(first), fmt_count(last)))
        self.cut_name.set("")

    def do_split_equal(self):
        name = self.cut_src()
        if not name:
            messagebox.showinfo("No source", "Pick a source waveform first.")
            return
        try:
            parts = int(round(as_float(self.cut_parts.get(), 0)))
            pieces = split_equal(self.library[name], parts)
        except ValueError as exc:
            messagebox.showerror("Cannot split that", str(exc))
            return
        self.keep_pieces(name, pieces)

    def do_split_chunks(self):
        name = self.cut_src()
        if not name:
            messagebox.showinfo("No source", "Pick a source waveform first.")
            return
        try:
            size = parse_count(self.cut_chunk.get(), self.rate(),
                               len(self.library[name]))
            if size is None:
                raise ValueError("say how long a chunk should be")
            pieces = split_every(self.library[name], size,
                                 self.cut_keep_short.get())
        except ValueError as exc:
            messagebox.showerror("Cannot split that", str(exc))
            return
        self.keep_pieces(name, pieces)

    def keep_pieces(self, name, pieces):
        """Every piece of a split into the library, numbered from 1.

        Asked about first when it is a lot of them: forty entries appearing in
        the list at once is a mess to undo one Remove at a time.
        """
        if len(pieces) > 12 and not messagebox.askyesno(
                "Split into %d?" % (len(pieces),),
                "That puts %d new waveforms in the library. Go ahead?"
                % (len(pieces),)):
            return
        start = 1
        for number, piece in enumerate(pieces, 1):
            self.add_wave("%s_%d" % (name, number), piece,
                          "%s[%s..%s]" % (name, fmt_count(start),
                                          fmt_count(start + len(piece) - 1)),
                          select=(number == len(pieces)), quiet=True)
            start += len(piece)
        self.log("Split %s into %d piece(s) of %s"
                 % (name, len(pieces),
                    ", ".join([fmt_count(len(p)) for p in pieces[:8]])
                    + (", ..." if len(pieces) > 8 else "")))

    # -- tab: assemble -----------------------------------------------------

    def tab_assemble(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Button(top, text="Add row", command=self.do_seg_add).pack(side="left")
        ttk.Button(top, text="Add the selected waveform",
                   command=self.do_seg_add_selected).pack(side="left", padx=4)
        ttk.Label(top, text="Gap level:").pack(side="left", padx=(14, 4))
        self.seg_gap_level = tk.StringVar(value="0")
        ttk.Entry(top, textvariable=self.seg_gap_level, width=7).pack(side="left")
        self.seg_total = ttk.Label(top, text="", foreground=NOTE_GREY)
        self.seg_total.pack(side="left", padx=(14, 0))

        holder = ttk.Frame(parent)
        holder.pack(fill="both", expand=True, padx=8)
        holder.columnconfigure(0, weight=1)
        self.seg_canvas = tk.Canvas(holder, height=132, highlightthickness=0)
        vbar = ttk.Scrollbar(holder, orient="vertical",
                             command=self.seg_canvas.yview)
        hbar = ttk.Scrollbar(holder, orient="horizontal",
                             command=self.seg_canvas.xview)
        self.seg_canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        self.seg_canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        self.seg_body = ttk.Frame(self.seg_canvas)
        self.seg_canvas.create_window((0, 0), window=self.seg_body, anchor="nw")
        self.seg_body.bind("<Configure>", lambda e: self.seg_canvas.configure(
            scrollregion=self.seg_canvas.bbox("all")))

        bottom = ttk.Frame(parent)
        bottom.pack(fill="x", padx=8, pady=(4, 8))
        ttk.Label(bottom, text="Name:").pack(side="left")
        self.seg_name = tk.StringVar(value="assembled")
        ttk.Entry(bottom, textvariable=self.seg_name, width=30).pack(
            side="left", padx=(4, 6))
        ttk.Button(bottom, text="Build waveform",
                   command=self.do_assemble).pack(side="left")
        # Its own full-width line rather than trailing the button: beside the
        # Build button it had about two hundred pixels and lost its last
        # sentence off the edge of the window.
        ttk.Label(parent, foreground=NOTE_GREY, justify="left", wraplength=700,
                  text="Blank From/To takes the whole source; blank Points "
                       "keeps its own length; Points on a shape: row is how "
                       "long to build it. Options takes reverse=on and a "
                       "shape's own key=value settings.").pack(
            anchor="w", padx=8, pady=(0, 6))

        watch(self.seg_gap_level, self.on_seg_change)
        self.seg_redraw()

    def seg_redraw(self):
        """Rebuild the rows from the data.

        Cheaper to think about than patching widgets in place: reorder, insert
        and delete all become one list operation followed by a redraw, and the
        row numbers cannot drift out of step with the list.
        """
        for child in self.seg_body.winfo_children():
            child.destroy()
        # The variables have to be held here: a StringVar that is garbage
        # collected takes its Tcl variable with it, and the row goes blank.
        self.seg_vars = []

        heads = ["#"] + [label for label, _, _ in SEG_COLUMNS]
        for col, text in enumerate(heads):
            ttk.Label(self.seg_body, text=text, foreground="#444444").grid(
                row=0, column=col, sticky="w", padx=2, pady=(0, 2))

        names = list(self.library)
        for index, row in enumerate(self.seg_rows):
            ttk.Label(self.seg_body, text=str(index + 1),
                      foreground=NOTE_GREY).grid(row=index + 1, column=0,
                                                 padx=(2, 4))
            row_vars = {}
            self.seg_vars.append(row_vars)
            for col, (_, key, width) in enumerate(SEG_COLUMNS, start=1):
                var = tk.StringVar(value=as_text(row.get(key, "")))
                row_vars[key] = var
                if key == "source":
                    widget = ttk.Combobox(self.seg_body, textvariable=var,
                                          values=source_choices(names),
                                          width=width, height=18)
                    row_vars["source_box"] = widget
                else:
                    widget = ttk.Entry(self.seg_body, textvariable=var,
                                       width=width)
                widget.grid(row=index + 1, column=col, sticky="w", padx=2, pady=1)
                watch(var, self.seg_editor(index, key, var))

            tools = ttk.Frame(self.seg_body)
            tools.grid(row=index + 1, column=len(SEG_COLUMNS) + 1, padx=(8, 2))
            for text, call in (("^", self.do_seg_up), ("v", self.do_seg_down),
                               ("D", self.do_seg_dup), ("X", self.do_seg_del)):
                ttk.Button(tools, text=text, width=2,
                           command=lambda f=call, i=index: f(i)).pack(side="left")
        self.on_seg_change()

    def seg_editor(self, index, key, var):
        def edit():
            if not 0 <= index < len(self.seg_rows):
                return
            self.seg_rows[index][key] = var.get()
            if key == "source":
                # A shape takes different settings from a stored waveform, so
                # the ones left over are not just stale, they are meaningless.
                # Written into the box where it stands rather than by rebuilding
                # the row: rebuilding destroys the combobox whose callback this
                # is, and a combobox destroyed while its dropdown is closing
                # never releases its grab - which leaves every box in the window
                # deaf to the keyboard until it is clicked away from and back.
                text = var.get().strip()
                if text.lower().startswith(SHAPE_PREFIX):
                    defaults = shape_extras(text[len(SHAPE_PREFIX):].strip())
                    holder = self.seg_vars[index] if index < len(self.seg_vars) else {}
                    if "options" in holder:
                        holder["options"].set(defaults)
                    else:
                        self.seg_rows[index]["options"] = defaults
            self.on_seg_change()
        return edit

    def on_seg_change(self):
        count, points = assembled_extent(self.seg_rows, self.library, self.rate())
        text = "%d segment%s, %s pts" % (count, "" if count == 1 else "s",
                                         fmt_count(points))
        if self.rate() > 0 and points:
            text += " = %s" % (fmt_secs(points / self.rate()),)
        self.seg_total.configure(text=text)
        self.schedule_preview()

    def assembled(self):
        return assemble(self.seg_rows, self.library,
                        as_float(self.seg_gap_level.get(), 0.0), self.rate())

    def preview_assemble(self):
        try:
            data, marks = self.assembled()
        except Exception as exc:
            self.show_trace([], str(exc), warn=True)
            return
        self.show_trace(data, "assembled - %s from %d segment(s)"
                        % (describe(data), len(marks)),
                        marks=marks, key=("assemble", len(data)))

    def do_assemble(self):
        try:
            data, marks = self.assembled()
        except Exception as exc:
            self.log("Could not assemble: %s" % (exc,))
            messagebox.showerror("Cannot assemble that", str(exc))
            return
        recipe = " + ".join(["%s%s" % (label,
                                       "" if points == 0 else " (%s pts)"
                                       % fmt_count(points))
                             for _, label, points in marks])
        self.add_wave(self.seg_name.get() or "assembled", data,
                      "assembled: %s" % (recipe,))

    def do_seg_add(self):
        self.seg_rows.append(dict(SEG_DEFAULT))
        self.seg_redraw()

    def do_seg_add_selected(self):
        name = self.selected()
        if not name:
            messagebox.showinfo("Nothing selected",
                                "Pick a waveform in the library first.")
            return
        row = dict(SEG_DEFAULT)
        row["source"] = name
        # A first row that has never been touched is filled in rather than
        # pushed down, so the obvious gesture - select, add, select, add -
        # does not leave an empty row at the top.
        if len(self.seg_rows) == 1 and not self.seg_rows[0].get("source", "").strip():
            self.seg_rows[0] = row
        else:
            self.seg_rows.append(row)
        self.seg_redraw()

    def do_seg_dup(self, index):
        self.seg_rows.insert(index + 1, dict(self.seg_rows[index]))
        self.seg_redraw()

    def do_seg_del(self, index):
        self.seg_rows.pop(index)
        if not self.seg_rows:
            self.seg_rows.append(dict(SEG_DEFAULT))
        self.seg_redraw()

    def do_seg_up(self, index):
        if index > 0:
            self.seg_rows[index - 1:index + 1] = [self.seg_rows[index],
                                                  self.seg_rows[index - 1]]
            self.seg_redraw()

    def do_seg_down(self, index):
        if index < len(self.seg_rows) - 1:
            self.seg_rows[index:index + 2] = [self.seg_rows[index + 1],
                                              self.seg_rows[index]]
            self.seg_redraw()

    # -- tab: modify -------------------------------------------------------

    def tab_modify(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(top, text="Waveform:").pack(side="left")
        self.mod_source = tk.StringVar()
        self.mod_box = ttk.Combobox(top, textvariable=self.mod_source, width=36,
                                    state="readonly")
        self.mod_box.pack(side="left", padx=(4, 12))
        self.mod_replace = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="change it in place",
                        variable=self.mod_replace).pack(side="left")
        ttk.Label(top, text="Result name:").pack(side="left", padx=(12, 4))
        self.mod_name = tk.StringVar()
        ttk.Entry(top, textvariable=self.mod_name, width=30).pack(side="left")
        ttk.Label(top, text="(blank names it after the source)",
                  foreground=NOTE_GREY).pack(side="left", padx=(4, 0))

        one = ttk.Frame(parent)
        one.pack(fill="x", padx=8, pady=4)
        ttk.Label(one, text="Multiply by").pack(side="left")
        self.mod_scale = tk.StringVar(value="1")
        ttk.Entry(one, textvariable=self.mod_scale, width=8).pack(side="left",
                                                                  padx=4)
        ttk.Label(one, text="and add").pack(side="left")
        self.mod_offset = tk.StringVar(value="0")
        ttk.Entry(one, textvariable=self.mod_offset, width=8).pack(side="left",
                                                                   padx=4)
        ttk.Button(one, text="Apply", command=self.do_scale).pack(side="left",
                                                                  padx=(4, 16))
        ttk.Button(one, text="Normalise to +/-1",
                   command=self.do_normalise).pack(side="left", padx=2)
        ttk.Button(one, text="Stretch to 0..1",
                   command=self.do_unipolar).pack(side="left", padx=2)
        ttk.Button(one, text="Invert", command=self.do_invert).pack(side="left",
                                                                    padx=2)
        ttk.Button(one, text="Reverse", command=self.do_reverse).pack(side="left",
                                                                      padx=2)

        two = ttk.Frame(parent)
        two.pack(fill="x", padx=8, pady=(4, 8))
        ttk.Label(two, text="Resample to").pack(side="left")
        self.mod_points = tk.StringVar()
        ttk.Entry(two, textvariable=self.mod_points, width=10).pack(side="left",
                                                                    padx=4)
        ttk.Label(two, text="points").pack(side="left")
        ttk.Button(two, text="Resample", command=self.do_resample).pack(
            side="left", padx=(6, 20))
        ttk.Label(two, text="Clip to").pack(side="left")
        self.mod_low = tk.StringVar(value="-1")
        ttk.Entry(two, textvariable=self.mod_low, width=7).pack(side="left", padx=4)
        ttk.Label(two, text="..").pack(side="left")
        self.mod_high = tk.StringVar(value="1")
        ttk.Entry(two, textvariable=self.mod_high, width=7).pack(side="left", padx=4)
        ttk.Button(two, text="Clip", command=self.do_clip).pack(side="left",
                                                                padx=(6, 0))

        three = ttk.Frame(parent)
        three.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(three, text="Offset:").pack(side="left")
        ttk.Button(three, text="Zero the first point",
                   command=lambda: self.do_zero_point("first")).pack(side="left", padx=(6, 2))
        ttk.Button(three, text="Zero the last point",
                   command=lambda: self.do_zero_point("last")).pack(side="left", padx=2)
        ttk.Label(three, text="(that sample's value is subtracted from every point: "
                              "first for a ramp up, last for a ramp down)",
                  foreground=NOTE_GREY).pack(side="left", padx=(4, 0))
        self.mod_note = ttk.Label(parent, text="", foreground=NOTE_GREY,
                                  wraplength=560, justify="left")
        self.mod_note.pack(anchor="w", padx=8, pady=(0, 6))
        self.mod_box.bind("<<ComboboxSelected>>",
                          lambda e: self.select_in_list(self.mod_source.get()))

    def modify(self, suffix, note, operation):
        """Run one operation and put the answer wherever the tab says to."""
        name = self.mod_source.get()
        if name not in self.library:
            messagebox.showinfo("No waveform",
                                "Pick a waveform on the Modify tab first.")
            return
        try:
            data = operation(self.library[name])
        except Exception as exc:
            self.mod_note.configure(text=str(exc), foreground=NOTE_WARN)
            messagebox.showerror("Cannot do that", str(exc))
            return
        self.mod_note.configure(text="", foreground=NOTE_GREY)
        if self.mod_replace.get():
            self.library[name] = data
            self.origin[name] = "%s, %s" % (self.origin.get(name, name), note)
            self.saved.discard(name)
            self.refresh_library(select=name)
            self.log("%s: %s (%s, in place)" % (name, describe(data), note))
        else:
            target = self.mod_name.get().strip() or ("%s_%s" % (name, suffix))
            self.add_wave(target, data, "%s of %s" % (note, name))
            self.mod_name.set("")

    def do_scale(self):
        scale = as_float(self.mod_scale.get(), 1.0)
        offset = as_float(self.mod_offset.get(), 0.0)
        self.modify("scaled", "x %g %+g" % (scale, offset),
                    lambda y: scaled(y, scale, offset))

    def do_normalise(self):
        self.modify("norm", "normalised to +/-1", normalised)

    def do_unipolar(self):
        self.modify("unipolar", "stretched to 0..1", unipolar)

    def do_zero_point(self, anchor):
        """Take the resting offset out of an ILC drive: the first (ramp up) or
        last (ramp down) sample's level, subtracted from every sample."""
        def run(samples):
            values, offset = remove_offset(samples, anchor)
            self.log("Offset: %s sample was %.6g, subtracted from all %s points"
                     % (anchor, offset, fmt_count(len(samples))))
            return values
        self.modify("zeroed", "%s point zeroed" % (anchor,), run)

    def do_invert(self):
        self.modify("inv", "inverted", lambda y: scaled(y, -1.0, 0.0))

    def do_reverse(self):
        self.modify("rev", "reversed", lambda y: list(y)[::-1])

    def do_resample(self):
        def run(samples):
            want = parse_count(self.mod_points.get(), self.rate(), len(samples))
            if want is None:
                raise ValueError("say how many points it should come out at")
            return resample(samples, want)
        self.modify("resampled", "resampled", run)

    def do_clip(self):
        low = as_float(self.mod_low.get(), -1.0)
        high = as_float(self.mod_high.get(), 1.0)
        self.modify("clipped", "clipped to %g..%g" % (low, high),
                    lambda y: clipped(y, low, high))

    # -- tab: values -------------------------------------------------------

    def tab_values(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Button(top, text="Load the selected waveform",
                   command=self.do_values_load).pack(side="left")
        ttk.Button(top, text="Clear",
                   command=lambda: self.values_box.delete("1.0", "end")).pack(
            side="left", padx=4)
        ttk.Label(top, text="Name:").pack(side="left", padx=(14, 4))
        self.values_name = tk.StringVar(value="typed")
        ttk.Entry(top, textvariable=self.values_name, width=30).pack(side="left")
        ttk.Button(top, text="Use these values",
                   command=self.do_values_use).pack(side="left", padx=6)
        self.values_note = ttk.Label(top, text="", foreground=NOTE_GREY)
        self.values_note.pack(side="left", padx=(10, 0))

        box = ttk.Frame(parent)
        box.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.values_box = tk.Text(box, height=7, wrap="none", font=MONO_FONT)
        bar = ttk.Scrollbar(box, orient="vertical", command=self.values_box.yview)
        self.values_box.configure(yscrollcommand=bar.set)
        self.values_box.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")

    def do_values_load(self, quiet=False):
        name = self.selected()
        if not name:
            if not quiet:
                messagebox.showinfo("Nothing selected",
                                    "Pick a waveform in the library first.")
            return
        data = self.library[name]
        self.values_box.delete("1.0", "end")
        if len(data) > VALUES_MAX_LINES:
            self.values_note.configure(
                text="%s points is too many to put in a text box - cut a piece "
                     "out on the Cut tab first" % (fmt_count(len(data)),),
                foreground=NOTE_WARN)
            return
        self.values_box.insert("1.0", "\n".join(["%.10g" % v for v in data]) + "\n")
        self.values_name.set(name)
        self.values_note.configure(text="%s points" % (fmt_count(len(data)),),
                                   foreground=NOTE_GREY)

    def do_values_use(self):
        text = self.values_box.get("1.0", "end")
        try:
            columns, names = table_from_lines(text.splitlines(), "what was typed")
            index, _ = value_column(columns, names)
            data = columns[index]
            if len(data) < 2:
                raise ValueError("that is only %d point(s)" % (len(data),))
        except Exception as exc:
            messagebox.showerror("Cannot read those values", str(exc))
            return
        self.add_wave(self.values_name.get() or "typed", data, "typed in")

    # -- closing -----------------------------------------------------------

    def tab_supertime(self, parent):
        """An ILC drive into the two shape files the sequence plays, and the
        start/end numbers to type into the edge."""
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(row, text="Drive file:").pack(side="left")
        self.st_file = tk.StringVar(value=self.cfg.get("st_file", ""))
        ttk.Entry(row, textvariable=self.st_file, width=46).pack(side="left", padx=4)
        ttk.Button(row, text="Browse...", command=self.st_browse).pack(side="left")
        ttk.Button(row, text="Read", command=self.st_read).pack(side="left", padx=4)

        row = ttk.Frame(parent)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="Split at").pack(side="left")
        self.st_split = tk.StringVar()
        ttk.Entry(row, textvariable=self.st_split, width=9).pack(side="left", padx=4)
        ttk.Label(row, text="us").pack(side="left")
        ttk.Button(row, text="Middle of the plateau",
                   command=self.st_find_plateau).pack(side="left", padx=(4, 14))
        ttk.Label(row, text="Grid").pack(side="left")
        self.st_step = tk.StringVar(value="")
        ttk.Entry(row, textvariable=self.st_step, width=6).pack(side="left", padx=4)
        ttk.Label(row, text="us (blank keeps the file's)").pack(side="left")
        self.st_down_from_zero = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="down ramp's time from 0",
                        variable=self.st_down_from_zero).pack(side="left", padx=(14, 0))

        row = ttk.Frame(parent)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="Offset:").pack(side="left")
        self.st_offset_mode = tk.StringVar(value="subtract")
        ttk.Radiobutton(row, text="subtract it from every sample",
                        variable=self.st_offset_mode, value="subtract").pack(side="left", padx=4)
        ttk.Radiobutton(row, text="only zero the end sample (the old _fixed files)",
                        variable=self.st_offset_mode, value="endsample").pack(side="left", padx=4)
        ttk.Radiobutton(row, text="leave it",
                        variable=self.st_offset_mode, value="keep").pack(side="left", padx=4)

        row = ttk.Frame(parent)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="Write as").pack(side="left")
        self.st_stem = tk.StringVar()
        ttk.Entry(row, textvariable=self.st_stem, width=28).pack(side="left", padx=4)
        ttk.Label(row, text="_up.csv / _down.csv in the folder above").pack(side="left")
        ttk.Button(row, text="Write both files", command=self.st_write).pack(side="left", padx=(12, 4))
        ttk.Button(row, text="Preview in library", command=self.st_preview).pack(side="left")

        row = ttk.Frame(parent)
        row.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(row, text="EOM table:").pack(side="left")
        self.st_table = tk.StringVar(value=self.cfg.get("st_table", ""))
        ttk.Entry(row, textvariable=self.st_table, width=34).pack(side="left", padx=4)
        ttk.Button(row, text="Browse...", command=self.st_browse_table).pack(side="left")
        ttk.Label(row, text="start").pack(side="left", padx=(12, 2))
        self.st_zero = tk.StringVar(value=self.cfg.get("st_zero", "0.012"))
        ttk.Entry(row, textvariable=self.st_zero, width=7).pack(side="left")
        ttk.Label(row, text="end").pack(side="left", padx=(8, 2))
        self.st_end = tk.StringVar(value=self.cfg.get("st_end", ""))
        ttk.Entry(row, textvariable=self.st_end, width=7).pack(side="left")
        ttk.Label(row, text="(coarse-channel log units, or volts with a V; the coarse zero is 0.012)").pack(side="left", padx=(4, 0))
        ttk.Button(row, text="Endpoints", command=self.st_endpoints).pack(side="left", padx=(10, 0))

        self.st_note = ttk.Label(parent, text=(
            "Only the start and end numbers go through the EOM table; the shape's "
            "samples are laid between the two voltages as they are. So the file "
            "is the drive cut at its plateau with the DC offset taken out of every "
            "sample, and the endpoints are what set the stroke."),
            foreground=NOTE_GREY, wraplength=760, justify="left")
        self.st_note.pack(anchor="w", padx=8, pady=(2, 6))
        self.st_data = None                                   # (t_us, values) last read

    def st_browse(self):
        path = filedialog.askopenfilename(
            initialdir=os.path.dirname(self.st_file.get()) or self.folder.get() or ".",
            filetypes=(("waveform files", "*.csv *.txt"), ("all", "*.*")))
        if path:
            self.st_file.set(os.path.normpath(path))
            self.st_read()

    def st_browse_table(self):
        path = filedialog.askopenfilename(
            initialdir=os.path.dirname(self.st_table.get()) or self.folder.get() or ".",
            filetypes=(("EOM tables", "EOM_*.txt"), ("all", "*.*")))
        if path:
            self.st_table.set(os.path.normpath(path))

    def st_read(self):
        path = self.st_file.get().strip()
        try:
            t, v = read_timed(path)
        except Exception as exc:
            self.st_data = None
            self.st_note.configure(text=str(exc), foreground=NOTE_WARN)
            return None
        self.st_data = (t, v)
        self.cfg["st_file"] = path
        stem = os.path.splitext(os.path.basename(path))[0]
        if not self.st_stem.get().strip():
            self.st_stem.set(stem)
        self.st_find_plateau(quiet=True)
        self.st_note.configure(
            text="%s: %s points, %g us grid, %g us long; first %.5g, last %.5g, "
                 "peak %.5g (%s)" % (os.path.basename(path), fmt_count(len(t)),
                                     t[1] - t[0], t[-1] - t[0], v[0], v[-1], max(v),
                                     describe(v)),
            foreground=NOTE_GREY)
        self.log("Supertime: read %s, split suggested at %s us"
                 % (os.path.basename(path), self.st_split.get()))
        self.show_trace(v, "%s - %s" % (stem, describe(v)), key=("supertime", path, len(v)))
        return self.st_data

    def st_find_plateau(self, quiet=False):
        if self.st_data is None and self.st_read() is None:
            return
        t, v = self.st_data
        k = plateau_split(v)
        self.st_split.set("%g" % (t[k],))
        if not quiet:
            self.log("Supertime: plateau middle at sample %d = %g us" % (k + 1, t[k]))

    def st_halves(self):
        """The two halves the panel describes, after the offset rule and any
        regridding: [(label, anchor, t, values, offset), ...]."""
        if self.st_data is None and self.st_read() is None:
            raise ValueError("read a drive file first")
        t, v = self.st_data
        split = as_float(self.st_split.get(), -1.0)
        if split < 0:
            raise ValueError("the split time must be a number of microseconds")
        return self.st_split_drive(t, v, split)

    def st_split_drive(self, t, v, split):
        """One drive into its two halves under the Supertime tab's offset,
        grid and time-axis settings."""
        up, down = split_at_time(t, v, split)
        step = self.st_step.get().strip()
        out = []
        for label, anchor, (tt, vv) in (("up", "first", up), ("down", "last", down)):
            mode = self.st_offset_mode.get()
            offset = 0.0
            if mode == "subtract":
                vv, offset = remove_offset(vv, anchor)
            elif mode == "endsample":
                offset = vv[0] if anchor == "first" else vv[-1]
                vv = zero_end_sample(vv, anchor)
            if step:
                tt, vv = resample_grid(tt, vv, as_float(step, 0.0))
            if label == "down" and self.st_down_from_zero.get():
                tt = [x - tt[0] for x in tt]
            out.append((label, anchor, tt, vv, offset))
        return out

    def st_write(self):
        try:
            halves = self.st_halves()
        except Exception as exc:
            self.st_note.configure(text=str(exc), foreground=NOTE_WARN)
            messagebox.showerror("Cannot write that", str(exc))
            return
        folder = self.folder.get().strip() or os.path.dirname(self.st_file.get())
        stem = safe_name(self.st_stem.get().strip() or "ramp")
        written = []
        for label, anchor, tt, vv, offset in halves:
            path = os.path.join(folder, "%s_%s.csv" % (stem, label))
            if os.path.exists(path) and not messagebox.askyesno(
                    "Overwrite?", "%s exists. Replace it?" % (path,)):
                continue
            write_supertime_csv(path, tt, vv)
            written.append(path)
            self.log("Supertime: wrote %s - %s points, %g..%g us, %s sample was "
                     "%.5g (offset %s), values %.5g..%.5g"
                     % (os.path.basename(path), fmt_count(len(vv)), tt[0], tt[-1],
                        anchor, offset,
                        {"subtract": "subtracted everywhere",
                         "endsample": "end sample zeroed only",
                         "keep": "left in"}[self.st_offset_mode.get()],
                        min(vv), max(vv)))
        self.st_note.configure(
            text="wrote %s" % (", ".join(os.path.basename(p) for p in written) or "nothing"),
            foreground=NOTE_GREY)
        self.cfg["st_file"] = self.st_file.get()

    def st_preview(self):
        try:
            halves = self.st_halves()
        except Exception as exc:
            self.st_note.configure(text=str(exc), foreground=NOTE_WARN)
            return
        stem = safe_name(self.st_stem.get().strip() or "ramp")
        for label, anchor, tt, vv, offset in halves:
            self.add_wave("%s_%s" % (stem, label), vv,
                          "Supertime %s half, %g us grid, offset %.5g" % (label, tt[1] - tt[0], offset))

    def st_parse_point(self, rows, text, what):
        """One ramp endpoint as (log units, card volts); '9.16V' means volts."""
        text = text.strip().lower()
        if text.endswith("v"):
            volts = as_float(text[:-1], None)
            if volts is None:
                raise ValueError("type the %s point in log units, or as volts with a V" % what)
            return volts_to_units(rows, volts), volts
        units = as_float(text, None)
        if units is None:
            raise ValueError("type the %s point in log units, or as volts with a V" % what)
        return units, units_to_volts(rows, units)

    def st_endpoints(self):
        try:
            rows = read_eom_table(self.st_table.get().strip())
            zero, zero_v = self.st_parse_point(rows, self.st_zero.get() or "0.012", "start")
            end_u, end_v = self.st_parse_point(rows, self.st_end.get(), "end")
        except Exception as exc:
            self.st_note.configure(text=str(exc), foreground=NOTE_WARN)
            return
        text = ("start %.4g units = %.4f V at the card; end %.4g units = %.4f V; "
                "stroke %.4f V. Train the ILC on a target that idles at %.3f V and "
                "peaks at %.3f V, not on 0 -> peak."
                % (zero, zero_v, end_u, end_v, end_v - zero_v, zero_v, end_v))
        if self.st_data is not None:
            peak = max(self.st_data[1]) - min(self.st_data[1])
            text += (" The loaded drive's own stroke is %.4f V; the sequence "
                     "rescales the shape onto the endpoints, so a %.0f%% amplitude "
                     "change from the learned one." % (peak, 100.0 * (end_v - zero_v) / peak - 100.0))
        self.st_note.configure(text=text, foreground=NOTE_GREY)
        self.log("Supertime endpoints: " + text)
        self.cfg["st_table"] = self.st_table.get()
        self.cfg["st_zero"] = self.st_zero.get()
        self.cfg["st_end"] = self.st_end.get()

    def tab_series(self, parent):
        """Two ILC drives that run one after the other on the two EOMs: check
        that they nest, see the total rotation, write all four Supertime
        files, and build nested targets for the ILC to learn."""
        self.se_data = {}                                     # "a"/"b" -> (t, v)
        for key, label in (("a", "Outer drive (moves first):"),
                           ("b", "Inner drive (moves inside the outer's hold):")):
            row = ttk.Frame(parent)
            row.pack(fill="x", padx=8, pady=(8 if key == "a" else 2, 2))
            ttk.Label(row, text=label, width=44).pack(side="left")
            var = tk.StringVar(value=self.cfg.get("se_file_" + key, ""))
            setattr(self, "se_file_" + key, var)
            ttk.Entry(row, textvariable=var, width=46).pack(side="left", padx=4)
            ttk.Button(row, text="Browse...",
                       command=lambda k=key: self.se_browse(k)).pack(side="left")

        row = ttk.Frame(parent)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="Guard").pack(side="left")
        self.se_guard = tk.StringVar(value=self.cfg.get("se_guard", "200"))
        ttk.Entry(row, textvariable=self.se_guard, width=7).pack(side="left", padx=4)
        ttk.Label(row, text="us between one EOM settling and the other moving").pack(side="left")
        ttk.Button(row, text="Check the pair", command=self.se_check).pack(side="left", padx=(12, 4))
        ttk.Button(row, text="Drives to library", command=self.se_preview).pack(side="left")
        ttk.Label(row, text="Write four files as").pack(side="left", padx=(14, 2))
        self.se_stem = tk.StringVar()
        ttk.Entry(row, textvariable=self.se_stem, width=18).pack(side="left", padx=2)
        ttk.Label(row, text="_outer/_inner _up/_down.csv").pack(side="left")
        ttk.Button(row, text="Write", command=self.se_write).pack(side="left", padx=(6, 0))

        self.se_report = tk.Text(parent, height=9, width=110, font=MONO_FONT,
                                 state="disabled", wrap="none")
        self.se_report.pack(fill="x", padx=8, pady=(4, 2))

        row = ttk.Frame(parent)
        row.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(row, text="Nested targets for the ILC, us:").pack(side="left")
        self.se_target = {}
        for key, label, default in (("lead", "lead", "500"), ("orise", "outer rise", "1500"),
                                    ("guard", "guard", "300"), ("irise", "inner rise", "2500"),
                                    ("ihold", "inner hold", "1000"), ("tail", "tail", "500"),
                                    ("step", "grid", "2")):
            ttk.Label(row, text=label).pack(side="left", padx=(8, 2))
            var = tk.StringVar(value=self.cfg.get("se_t_" + key, default))
            self.se_target[key] = var
            ttk.Entry(row, textvariable=var, width=6).pack(side="left")
        ttk.Label(row, text="V outer").pack(side="left", padx=(8, 2))
        self.se_level_a = tk.StringVar(value=self.cfg.get("se_level_a", "9.2"))
        ttk.Entry(row, textvariable=self.se_level_a, width=6).pack(side="left")
        ttk.Label(row, text="inner").pack(side="left", padx=(6, 2))
        self.se_level_b = tk.StringVar(value=self.cfg.get("se_level_b", "8.5"))
        ttk.Entry(row, textvariable=self.se_level_b, width=6).pack(side="left")
        ttk.Button(row, text="Build targets", command=self.se_build).pack(side="left", padx=(10, 0))

        self.se_note = ttk.Label(parent, text=(
            "Series: the outer EOM goes to 90 deg on its own, then the inner one does its "
            "full swing while the outer holds, and the outer only comes back once the inner "
            "is home. The total rotation then passes 90 deg with one EOM at 90 and the other "
            "at 0, so neither is at its 45 deg extinction dip on the sensitive point. Read "
            "two drives and Check the pair to see the timings and the total angle; Build "
            "targets makes the nested pair for the ILC to learn (saved with the ILC header)."),
            foreground=NOTE_GREY, wraplength=760, justify="left")
        self.se_note.pack(anchor="w", padx=8, pady=(2, 6))

    def se_browse(self, key):
        var = getattr(self, "se_file_" + key)
        path = filedialog.askopenfilename(
            initialdir=os.path.dirname(var.get()) or self.folder.get() or ".",
            filetypes=(("waveform files", "*.csv *.txt"), ("all", "*.*")))
        if path:
            var.set(os.path.normpath(path))

    def se_say(self, lines, warn=False):
        self.se_report.configure(state="normal")
        self.se_report.delete("1.0", "end")
        self.se_report.insert("end", "\n".join(lines))
        self.se_report.configure(state="disabled")
        self.se_note.configure(foreground=NOTE_WARN if warn else NOTE_GREY)

    def se_read(self):
        """Both drives off disk; raises with a plain message if either fails."""
        out = {}
        for key in ("a", "b"):
            path = getattr(self, "se_file_" + key).get().strip()
            if not path:
                raise ValueError("pick both drive files first")
            out[key] = read_timed(path)
            self.cfg["se_file_" + key] = path
        self.se_data = out
        if not self.se_stem.get().strip():
            stem = os.path.splitext(os.path.basename(self.cfg["se_file_a"]))[0]
            self.se_stem.set(stem.replace("drive_", ""))
        return out

    def se_check(self):
        try:
            data = self.se_read()
            guard = as_float(self.se_guard.get(), 0.0)
            (t_a, v_a), (t_b, v_b) = data["a"], data["b"]
            lines, ok, angle, marks = series_pair(t_a, v_a, t_b, v_b, guard)
        except Exception as exc:
            self.se_say([str(exc)], warn=True)
            return
        self.cfg["se_guard"] = self.se_guard.get()
        self.se_say(lines, warn=not ok)
        for line in lines:
            self.log("Series: " + line.strip())
        self.show_trace(angle, "total rotation, degrees (outer + inner, 90 per stroke) - "
                        "dashed lines at outer up/top, inner up/top/down, outer down",
                        marks=marks, key=("series", self.cfg["se_file_a"], self.cfg["se_file_b"]))

    def se_preview(self):
        try:
            data = self.se_read()
        except Exception as exc:
            self.se_say([str(exc)], warn=True)
            return
        for key, label in (("a", "outer"), ("b", "inner")):
            t, v = data[key]
            name = os.path.splitext(os.path.basename(self.cfg["se_file_" + key]))[0]
            self.add_wave(name, v, "series %s drive, %g us grid" % (label, t[1] - t[0]))

    def se_write(self):
        """All four Supertime files, each drive split at the middle of its own
        plateau, under the Supertime tab's offset and grid settings."""
        try:
            data = self.se_read()
        except Exception as exc:
            self.se_say([str(exc)], warn=True)
            return
        folder = self.folder.get().strip() or os.path.dirname(self.cfg["se_file_a"])
        stem = safe_name(self.se_stem.get().strip() or "series")
        written = []
        for key, which in (("a", "outer"), ("b", "inner")):
            t, v = data[key]
            split = t[plateau_split(v)]
            for label, anchor, tt, vv, offset in self.st_split_drive(t, v, split):
                path = os.path.join(folder, "%s_%s_%s.csv" % (stem, which, label))
                if os.path.exists(path) and not messagebox.askyesno(
                        "Overwrite?", "%s exists. Replace it?" % (path,)):
                    continue
                write_supertime_csv(path, tt, vv)
                written.append(os.path.basename(path))
                self.log("Series: wrote %s - %s points, %g..%g us, split at %g us, "
                         "%s sample was %.5g" % (os.path.basename(path), fmt_count(len(vv)),
                                                 tt[0], tt[-1], split, anchor, offset))
        self.se_say(["wrote " + (", ".join(written) or "nothing"),
                     "offset rule and grid: as set on the Supertime ramps tab"])

    def se_build(self):
        try:
            num = {}
            for key, var in self.se_target.items():
                value = as_float(var.get(), None)
                if value is None:
                    raise ValueError("every target time needs a number (us)")
                num[key] = value
            level_a = as_float(self.se_level_a.get(), 1.0)
            level_b = as_float(self.se_level_b.get(), 1.0)
            t, outer, inner = nested_targets(num["step"], num["lead"], num["orise"],
                                             num["guard"], num["irise"], num["ihold"],
                                             num["tail"], level_a, level_b)
        except Exception as exc:
            self.se_say([str(exc)], warn=True)
            return
        for key, var in self.se_target.items():
            self.cfg["se_t_" + key] = var.get()
        self.cfg["se_level_a"], self.cfg["se_level_b"] = self.se_level_a.get(), self.se_level_b.get()
        rate = 1e6 / num["step"]
        if abs(self.rate() - rate) > 1e-6:
            self.rate_text.set("%g" % (rate,))
            self.log("Sample rate set to %g Sa/s to match the %g us target grid." % (rate, num["step"]))
        self.ilc_header.set(True)
        self.add_wave("series_outer_target", outer,
                      "nested target: lead %g, rise %g, guard %g, inner %g+%g+%g, tail %g us"
                      % (num["lead"], num["orise"], num["guard"], num["irise"],
                         num["ihold"], num["irise"], num["tail"]), select=False)
        self.add_wave("series_inner_target", inner,
                      "nested target inside series_outer_target", select=False)
        lines, ok, angle, marks = series_pair(t, outer, t, inner, num["guard"] * 0.99)
        self.se_say(["built series_outer_target and series_inner_target: %s points, "
                     "%g us grid, %g us long, ILC header ticked for Save CSV"
                     % (fmt_count(len(t)), num["step"], t[-1])] + lines, warn=not ok)
        self.show_trace(angle, "total rotation of the nested targets, degrees",
                        marks=marks, key=("series-targets", len(t)))

    def on_close(self):
        pending = [name for name in self.library if name not in self.saved]
        if pending and not messagebox.askokcancel(
                "Close?",
                "%d waveform(s) have not been written to a CSV, and exist only "
                "in this window:\n\n%s\n\nClose anyway?"
                % (len(pending), ", ".join(pending[:10])
                   + (", ..." if len(pending) > 10 else ""))):
            return
        self.cfg["folder"] = self.folder.get()
        self.cfg["rate"] = self.rate_text.get()
        self.cfg["ilc_header"] = bool(self.ilc_header.get())
        try:
            self.cfg["geometry"] = self.root.geometry()
        except Exception:
            pass
        save_config(self.cfg)
        self.root.destroy()


# ---------------------------------------------------------------------------
# Self-test
#
# Not a substitute for running the window, but it does check the arithmetic
# that a preview cannot show you is wrong: the 1-based inclusive slicing, the
# resampler's endpoints, that a split tiles its source exactly, and that a file
# written here reads back as the same numbers.
# ---------------------------------------------------------------------------

def selftest():
    failures = []

    def check(name, got, want):
        if got != want:
            failures.append("%s: got %r, wanted %r" % (name, got, want))

    def close(name, got, want, tol=1e-9):
        if abs(got - want) > tol:
            failures.append("%s: got %r, wanted %r" % (name, got, want))

    ramp = [float(i) for i in xrange(1, 101)]              # 1.0 .. 100.0

    # take_piece is 1-based and inclusive at both ends, so the numbers typed
    # into the Cut tab are the numbers in the file's first column.
    check("piece head", take_piece(ramp, 1, 10), [float(i) for i in xrange(1, 11)])
    check("piece tail", take_piece(ramp, 91, 100), [float(i) for i in xrange(91, 101)])
    check("piece one", take_piece(ramp, 50, 50), [50.0])
    check("piece whole", take_piece(ramp), ramp)
    for bad in ((0, 5), (1, 101), (60, 40)):
        try:
            take_piece(ramp, *bad)
            failures.append("piece %r should have been refused" % (bad,))
        except ValueError:
            pass

    # 100 into 3 comes out 33/34/33 rather than 33/33/34: the boundaries are
    # rounded from the exact thirds, so the odd point lands in the middle piece
    # instead of all the slack piling up at the end.
    check("split count", [len(p) for p in split_equal(ramp, 3)], [33, 34, 33])
    joined = []
    for part in split_equal(ramp, 7):
        joined.extend(part)
    check("split tiles", joined, ramp)
    check("chunks", [len(p) for p in split_every(ramp, 30)], [30, 30, 30, 10])
    check("chunks dropped", [len(p) for p in split_every(ramp, 30, False)],
          [30, 30, 30])

    check("resample same", resample(ramp, 100), ramp)
    check("resample ends", (resample(ramp, 7)[0], resample(ramp, 7)[-1]),
          (1.0, 100.0))
    close("resample middle", resample(ramp, 199)[1], 1.5)   # halfway to sample 2
    check("resample up length", len(resample(ramp, 1000)), 1000)

    check("count plain", parse_count("2500"), 2500)
    check("count blank", parse_count("  "), None)
    check("count percent", parse_count("25%", 0, 100), 25)
    check("count time", parse_count("2ms", 1e6), 2000)
    try:
        parse_count("2ms")
        failures.append("a time with no rate should have been refused")
    except ValueError:
        pass
    check("cycles plain", parse_cycles("50", 1000)[0], 50.0)
    close("cycles from Hz", parse_cycles("5kHz", 1000, 1e6)[0], 5.0)

    for shape in BUILD_SHAPES:
        made = build_waveform(shape, 64, {})
        check("%s length" % shape, len(made), 64)
        if max([abs(v) for v in made]) > 1e6:
            failures.append("%s came out unreasonably large" % (shape,))
    close("gaussian peak", max(build_waveform("Gaussian", 101, {})), 1.0)
    check("ramp ends", (build_waveform("Linear ramp", 11, {})[0],
                        build_waveform("Linear ramp", 11, {})[-1]), (0.0, 1.0))
    check("hann ends", (build_waveform("Hann", 9, {})[0],
                        build_waveform("Hann", 9, {})[-1]), (0.0, 0.0))
    carried = build_waveform("Hold (DC)", 400, {"cycles": "4"})
    close("carrier closes", carried[0], 0.0, 1e-9)

    library = OrderedDict([("ramp", ramp), ("flat", [2.0] * 10)])
    made, marks = assemble([{"source": "ramp", "first": "1", "last": "10",
                             "repeat": "2", "scale": "1", "offset": "0"},
                            {"source": "flat", "repeat": "1", "scale": "0.5",
                             "offset": "1", "gap": "5"},
                            {"source": "shape: Linear ramp", "points": "4",
                             "repeat": "1", "options": "start=0 end=3"}],
                           library, gap_level=-1.0)
    check("assembled length", len(made), 20 + 10 + 5 + 4)
    check("assembled head", made[:3], [1.0, 2.0, 3.0])
    check("assembled repeat", made[10], 1.0)
    check("assembled scaled", made[20], 2.0)
    check("assembled gap", made[30:35], [-1.0] * 5)
    check("assembled shape", made[-1], 3.0)
    check("assembled marks", [m[1] for m in marks],
          ["ramp[1..10]", "flat", "Linear ramp"])
    try:
        assemble([{"source": "nope"}], library)
        failures.append("an unknown source should have been refused")
    except ValueError:
        pass

    # A file written here has to read back as the same numbers, including the
    # index column being recognised rather than taken for samples.
    import tempfile
    handle, path = tempfile.mkstemp(suffix=".csv")
    os.close(handle)
    try:
        sample = build_waveform("Chirp", 500, {"c0": "2", "c1": "20"})
        write_csv(path, sample)
        columns, names = read_table(path)
        check("round trip columns", len(columns), 2)
        check("round trip header", names, None)
        index, unsure = value_column(columns, names)
        check("round trip column", (index, unsure), (1, False))
        check("round trip length", len(columns[1]), 500)
        worst = max([abs(a - b) for a, b in zip(columns[1], sample)])
        if worst > 1e-9:
            failures.append("round trip changed a value by %g" % (worst,))
        check("index column", columns[0][:3], [1.0, 2.0, 3.0])
    finally:
        os.remove(path)

    # The time,value writer: header recognised, volts picked, time axis at
    # index / rate, and a missing rate refused rather than guessed.
    handle, path = tempfile.mkstemp(suffix=".csv")
    os.close(handle)
    try:
        sample = build_waveform("Gaussian", 300, {})
        write_time_csv(path, sample, 500000.0, comment="selftest\ntwo lines")
        columns, names = read_table(path)
        check("time csv header", names, ["time_us", "voltage_V"])
        check("time csv column", value_column(columns, names), (1, False))
        check("time csv axis", columns[0][:3], [0.0, 2.0, 4.0])
        worst = max([abs(a - b) for a, b in zip(columns[1], sample)])
        if worst > 1e-9:
            failures.append("time csv changed a value by %g" % (worst,))
        try:
            write_time_csv(path, sample, 0.0)
            failures.append("time csv without a rate should have been refused")
        except ValueError:
            pass
    finally:
        os.remove(path)

    # The tolerant reader: one column, other delimiters, comments, a header.
    columns, names = table_from_lines(["# a comment", "1;0.5", "2;0.25"])
    check("semicolons", columns[1], [0.5, 0.25])
    columns, names = table_from_lines(["idx\tvolts", "1\t0.5", "2\t0.25"])
    check("header names", names, ["idx", "volts"])
    check("header column", value_column(columns, names), (1, False))
    columns, names = table_from_lines(["# AWG cache", "0.5", "0.25", "0.125"])
    check("one column", value_column(columns, names), (0, False))

    check("unique name", unique_name("a", {"a": 1, "a_2": 1}), "a_3")
    check("safe name", safe_name('a/b:c'), "a_b_c")
    check("fmt count", fmt_count(1234567), "1,234,567")

    # Supertime ramps: the plateau split, the offset rule, the file layout and
    # the endpoint table, on a trapezoid with the ILC drive's kind of offset.
    trap = ([0.02] * 5 + [0.02 + 2.0 * i / 20.0 for i in xrange(1, 21)]
            + [2.02] * 9 + [2.02 - 2.0 * i / 20.0 for i in xrange(1, 21)] + [0.02] * 5)
    tt = [2.0 * i for i in xrange(len(trap))]
    k = plateau_split(trap)
    check("plateau middle", k, 28)          # top = samples 24..33
    (tu, vu), (td, vd) = split_at_time(tt, trap, tt[k])
    check("split lengths", (len(vu), len(vd)), (28, len(trap) - 28))
    zu, off = remove_offset(vu, "first")
    close("up offset", off, 0.02)
    close("up first", zu[0], 0.0)
    close("up last", zu[-1], 2.0)
    zd, off = remove_offset(vd, "last")
    close("down last", zd[-1], 0.0)
    close("down first", zd[0], 2.0)
    ez = zero_end_sample(vu, "first")
    check("end sample only", (ez[0], ez[1]), (0.0, vu[1]))
    rt, rv = resample_grid(tu, zu, 10.0)
    check("regrid start", rt[0], tu[0])
    check("regrid step", rt[1] - rt[0], 10.0)
    close("regrid value", rv[1], zu[5])
    try:
        import tempfile
        path = os.path.join(tempfile.gettempdir(), "wf_editor_selftest_st.csv")
        write_supertime_csv(path, tu, zu)
        raw = open(path, "rb").read()
        check("no header", raw[:4], b"0,0\r")
        check("crlf only", raw.count(b"\n"), len(tu) - 1)
        check("no final newline", raw[-1:] != b"\n", True)
        back_t, back_v = read_timed(path)
        check("supertime round trip", back_v, zu)
        check("supertime times", back_t, tu)
        tpath = os.path.join(tempfile.gettempdir(), "wf_editor_selftest_eom.txt")
        open(tpath, "w").write("0.0\t0.0\n8.0\t10.0\n9.99\t12.0\n")
        rows = read_eom_table(tpath)
        close("units to volts", units_to_volts(rows, 5.0), 4.0)
        close("volts to units", volts_to_units(rows, 4.0), 5.0)
        close("units past the knee", units_to_volts(rows, 11.0), 8.995)
        close("volts past the knee", volts_to_units(rows, 8.995), 11.0)
        for p in (path, tpath):
            os.remove(p)
    except Exception as exc:
        failures.append("supertime files: %r" % (exc,))

    try:
        st, so, si = nested_targets(2.0, 100, 300, 50, 400, 200, 100, 9.0, 8.0)
        check("nested length", len(st), int(round((100 + 300 + 50 + 400 + 200 + 400 + 50 + 300 + 100) / 2.0)) + 1)
        close("outer level", max(so), 9.0)
        close("inner level", max(si), 8.0)
        close("outer ends at zero", so[-1], 0.0)
        ra = ramp_timing(st, so)
        close("outer rise ends", ra["rise"][1], 100 + 300 * 0.85, 12.0)   # 95% of a min-jerk ramp
        close("outer top starts", ra["plateau"][0], 100 + 300 * 0.92, 12.0)  # within 1% of the top
        lines, ok, angle, marks = series_pair(st, so, st, si, 40.0)
        check("nested pair passes", ok, True)
        close("total 180 deg", max(angle), 180.0)
        check("seven segments", len(marks), 7)
        # The same shape on both EOMs at once is the parallel case: both hit
        # 45 deg on the 90 deg point, which the check must refuse.
        lines, ok, angle, marks = series_pair(st, so, st, so, 0.0)
        check("parallel pair refused", ok, False)
        close("interp hold", interp_to([0.0, 1.0, 2.0], [0.0, 2.0], [0.0, 4.0])[1], 2.0)
    except Exception as exc:
        failures.append("series pair: %r" % (exc,))

    for line in failures:
        print("FAIL " + line)
    print("%d check(s) failed" % (len(failures),) if failures
          else "self-test passed")
    return 1 if failures else 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
