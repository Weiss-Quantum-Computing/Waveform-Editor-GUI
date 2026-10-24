"""Build the window and drive every panel of the editor, with nobody watching.

Not a substitute for looking at it, but it walks the paths that only exist once
the widgets are real: the tab routing, the preview redraw, the Assemble table's
insert and reorder and delete, and a file round trip through the actual
buttons. House habit is to run this after any change to the panel, and to
extend it with every feature.

    python tests/smoke_test.py

Runs on either Python, and needs no instruments. It never reads or writes the
real settings file: CONFIG_PATH points at an empty temp folder before the window
is built, so a saved folder, rate or ILC-header tick cannot change the result.
The window is parked off screen at +6000+200 rather than withdrawn - the wheel
checks find the plot with winfo_containing and the edge checks measure real
widths, and both need a mapped window.
"""
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import waveform_editor_gui as W

failures = []


def step(name, fn):
    try:
        fn()
        root.update()
    except Exception:
        failures.append(name + "\n" + traceback.format_exc())


# The App reads folder, rate, ILC header and geometry from CONFIG_PATH: give it
# a fresh one, so the test runs on the defaults whatever the user last saved.
config_folder = tempfile.mkdtemp()
real_config = W.CONFIG_PATH
W.CONFIG_PATH = os.path.join(config_folder, "config.json")

root = W.tk.Tk()
root.geometry("1010x730+6000+200")
app = W.App(root)
# Silence the dialogs: a smoke test must never sit waiting on a message box.
W.messagebox.showerror = lambda *a, **k: failures.append("showerror " + repr(a))
W.messagebox.showinfo = lambda *a, **k: failures.append("showinfo " + repr(a))
W.messagebox.askyesno = lambda *a, **k: True
W.messagebox.askokcancel = lambda *a, **k: True
W.messagebox.askyesnocancel = lambda *a, **k: True
root.update()
root.update_idletasks()

# --- build a shape --------------------------------------------------------
step("build gaussian", app.do_build)
step("shape switch", lambda: (app.shape.set("Chirp"), app.on_shape()))
step("build chirp", app.do_build)
step("shape switch 2", lambda: (app.shape.set("Multitone"), app.on_shape()))
step("build multitone", app.do_build)
step("set a rate", lambda: app.rate_text.set("1e6"))
step("length as a time", lambda: app.build_len.set("2ms"))
step("build note", app.on_build_change)
step("build timed", app.do_build)
step("carrier in Hz", lambda: (app.shape.set("Gaussian"), app.on_shape(),
                               app.shape_vars[1].set("20kHz"),
                               app.on_build_change()))
step("build carrier", app.do_build)
step("clear the rate", lambda: app.rate_text.set(""))

names = list(app.library)
if len(names) != 5:
    failures.append("expected five built waveforms, got %r" % (names,))

# --- preview --------------------------------------------------------------
step("preview refresh", app.refresh_preview)
step("plot draw", app.plot.draw)
step("hover", lambda: app.plot._hover((10, 0.5)))
step("hover clear", lambda: app.plot._hover(None))

# --- cut ------------------------------------------------------------------
step("tab cut", lambda: app.tabs.select(app.tab_frames["cut"]))
step("pick source", lambda: app.cut_source.set(names[0]))
step("cut change", app.on_cut_change)
step("span from the plot", lambda: app.on_plot_span((201, 800)))
step("take piece", app.do_take)
step("zoom to span", app.do_zoom_span)
step("show all", app.do_zoom_all)
step("clear span", lambda: app.on_plot_span(None))


# --- wheel zoom, right-drag pan, right-click copy ----------------------------
# The handlers are driven with synthetic events: the canvas is real, so the
# geometry is real, and the only thing faked is the mouse. `x_root`/`y_root`
# are what the application-wide wheel binding looks at to decide whether the
# pointer is over the plot at all.
class Fake(object):
    def __init__(self, **fields):
        self.__dict__.update(fields)


def plot_point(fx, fy):
    """Canvas pixel (fx, fy fractions across the axes) and its screen position."""
    left, top, right, bottom = app.plot._box()
    x = int(left + fx * (right - left))
    y = int(top + fy * (bottom - top))
    return x, y, app.plot.canvas.winfo_rootx() + x, app.plot.canvas.winfo_rooty() + y


def check(condition, message):
    if not condition:
        failures.append(message)


root.update_idletasks()
root.update()
plot = app.plot
n_shown = len(plot.samples)
check(n_shown > 10, "cut preview has %d samples, cannot test the mouse" % (n_shown,))
check(plot.view is None, "view not at the full record before zooming")
check(app.zoom_note.cget("text") == "", "zoom note set before any zoom")

# wheel in, pointer over the plot
x, y, rx, ry = plot_point(0.5, 0.5)
step("wheel in", lambda: plot._wheel(Fake(x=x, y=y, x_root=rx, y_root=ry,
                                           delta=120, state=0)))
check(plot.view is not None, "wheel over the plot did not zoom in")
if plot.view:
    lo, hi = plot.view
    check(1 <= lo < hi <= n_shown, "zoomed view out of range: %r" % (plot.view,))
    check(hi - lo < n_shown - 1, "zoomed view is not narrower: %r" % (plot.view,))
    check(abs((hi - lo) - (n_shown - 1) / 1.25) <= 2,
          "one notch should narrow by 1.25, got %r of %d" % (plot.view, n_shown))
check(app.zoom_note.cget("text") == W.ZOOM_NOTE,
      "zoom note after a wheel: %r" % (app.zoom_note.cget("text"),))

# the sample under the cursor stays under the cursor
anchor_before = plot.sample_at(x)
step("wheel in again", lambda: plot._wheel(Fake(x=x, y=y, x_root=rx, y_root=ry,
                                                 delta=120, state=0)))
check(abs(plot.sample_at(x) - anchor_before) <= 2,
      "zoom moved the sample under the cursor from %r to %r"
      % (anchor_before, plot.sample_at(x)))

# Shift+wheel keeps the x range (y autoscales here, so it is a no-op)
held = plot.view
step("shift wheel", lambda: plot._wheel(Fake(x=x, y=y, x_root=rx, y_root=ry,
                                              delta=120, state=0x0001)))
check(plot.view == held, "Shift+wheel changed the x range: %r -> %r"
      % (held, plot.view))

# a wheel somewhere else in the window is somebody else's
elsewhere = Fake(x=5, y=5, x_root=app.listbox.winfo_rootx() + 5,
                 y_root=app.listbox.winfo_rooty() + 5, delta=120, state=0)
step("wheel off the plot", lambda: plot._wheel(elsewhere))
check(plot.view == held, "a wheel over the library zoomed the plot")

# the view is held across a redraw of the same record
step("redraw while zoomed", app.refresh_preview)
check(plot.view == held, "redraw of the same record lost the view: %r -> %r"
      % (held, plot.view))
check(app.zoom_note.cget("text") == W.ZOOM_NOTE, "zoom note lost on a redraw")

# right-drag pans, by the width of the drag
lo, hi = plot.view
x1, y1, _, _ = plot_point(0.5, 0.5)
x2, y2, _, _ = plot_point(0.2, 0.5)
step("right press", lambda: plot._press3(Fake(x=x1, y=y1)))
step("right drag", lambda: plot._drag3(Fake(x=x2, y=y2)))
step("right release", lambda: plot._release3(Fake(x=x2, y=y2)))
check(plot.view is not None and plot.view[0] > lo,
      "a right-drag to the left did not pan forward: %r -> %r" % ((lo, hi), plot.view))
if plot.view:
    check(abs((plot.view[1] - plot.view[0]) - (hi - lo)) <= 2,
          "panning changed the width: %r -> %r" % ((lo, hi), plot.view))
# and it cannot leave the record
far = plot_point(0.5, 0.5)
step("pan off the end", lambda: (plot._press3(Fake(x=far[0], y=far[1])),
                                 plot._drag3(Fake(x=-5000, y=far[1])),
                                 plot._release3(Fake(x=-5000, y=far[1]))))
check(plot.view is not None and plot.view[1] == n_shown,
      "panning past the end did not stop at the end: %r" % (plot.view,))
check(plot.view is not None and plot.view[0] >= 1, "panned before sample 1")

# a right-click that did not move copies the readout
try:
    root.clipboard_clear()
    root.clipboard_append("stale")
except Exception:
    pass
x, y, _, _ = plot_point(0.5, 0.5)
# Released one pixel over: still a click, and the readout is the one under
# the release, which is where the hover label was pointing.
step("right click", lambda: (plot._press3(Fake(x=x, y=y)),
                             plot._release3(Fake(x=x + 1, y=y))))
expect = plot.readout_at(x + 1, y)
if expect is None:
    failures.append("no readout under the right-click")
else:
    want = app.readout_text(*expect)
    check(want.startswith("sample "), "readout text is %r" % (want,))
    try:
        got = root.clipboard_get()
    except Exception:
        got = "<clipboard_get failed>"
    check(got == want, "clipboard has %r, hover readout is %r" % (got, want))
    check(app.hover.cget("text") == "copied " + want,
          "hover label after a copy: %r" % (app.hover.cget("text"),))

# wheel out past the whole record is the whole record
step("wheel out", lambda: [plot.zoom_at(x, 1 / 1.25) for _ in range(12)])
check(plot.view is None, "wheeling out did not come back to the full record")
check(app.zoom_note.cget("text") == "", "zoom note after wheeling all the way out")

# Show all is Home: it clears the view and the note
step("zoom for show all", lambda: plot.zoom_at(x, 4.0))
check(plot.view is not None, "zoom_at(4.0) did not zoom")
step("show all clears it", app.do_zoom_all)
check(plot.view is None, "Show all did not reset the view")
check(app.zoom_note.cget("text") == "", "Show all did not clear the zoom note")

# a different record starts at the full extent again
step("zoom then switch", lambda: (plot.zoom_at(x, 4.0),
                                  app.cut_source.set(names[1]),
                                  app.refresh_preview()))
check(plot.view is None, "a different record kept the old view: %r" % (plot.view,))
check(app.zoom_note.cget("text") == "", "zoom note survived a change of record")
step("back to the first", lambda: (app.cut_source.set(names[0]),
                                   app.refresh_preview()))
step("percent span", lambda: (app.cut_first.set("25%"), app.cut_last.set("75%"),
                              app.on_cut_change()))
step("take percent piece", app.do_take)
step("split equal", lambda: (app.cut_parts.set("5"), app.do_split_equal()))
step("split chunks", lambda: (app.cut_chunk.set("2500"), app.do_split_chunks()))

# The prior-leak guard: a span typed against one record must not survive being
# pointed at another. 1 to 500 carried across is not an error - it is a piece
# of something you did not mean, which is worse.
step("span on the first", lambda: (app.cut_source.set(names[0]),
                                   app.cut_first.set("1"),
                                   app.cut_last.set("500")))
step("point at another", lambda: app.cut_source.set(names[1]))
if app.cut_first.get() or app.cut_last.get():
    failures.append("changing the cut source left From/To at %r..%r"
                    % (app.cut_first.get(), app.cut_last.get()))

# --- assemble -------------------------------------------------------------
step("tab assemble", lambda: app.tabs.select(app.tab_frames["assemble"]))
step("add selected", app.do_seg_add_selected)
step("add row", app.do_seg_add)


def fill_row(index, **fields):
    for key, value in fields.items():
        app.seg_vars[index][key].set(value)


step("row 0", lambda: fill_row(0, source=names[0], first="1", last="500",
                               repeat="2"))
step("row 1 is a shape", lambda: fill_row(1, source=W.SHAPE_PREFIX + "Linear ramp",
                                          points="400"))
step("row 1 options", lambda: fill_row(1, options="start=0 end=1 reverse=on"))
step("add a third row", app.do_seg_add)
step("row 2", lambda: fill_row(2, source=names[1], points="300", scale="0.5",
                               offset="0.25", gap="100"))
step("running total", app.on_seg_change)
step("assemble preview", app.preview_assemble)
step("assemble", app.do_assemble)
step("duplicate row", lambda: app.do_seg_dup(0))
step("move row down", lambda: app.do_seg_down(0))
step("move row up", lambda: app.do_seg_up(1))
step("delete row", lambda: app.do_seg_del(0))
step("assemble again", app.do_assemble)

# --- modify ---------------------------------------------------------------
step("tab modify", lambda: app.tabs.select(app.tab_frames["modify"]))
step("modify source", lambda: app.mod_source.set(names[0]))
step("scale and offset", lambda: (app.mod_scale.set("2.5"),
                                  app.mod_offset.set("-0.5"), app.do_scale()))
step("normalise", app.do_normalise)
step("stretch to 0..1", app.do_unipolar)
step("invert", app.do_invert)
step("reverse", app.do_reverse)
step("resample", lambda: (app.mod_points.set("777"), app.do_resample()))
step("clip", app.do_clip)
step("in place", lambda: (app.mod_replace.set(True), app.do_invert()))
step("back to copies", lambda: app.mod_replace.set(False))

# --- values ---------------------------------------------------------------
step("tab values", lambda: app.tabs.select(app.tab_frames["values"]))
step("load values", lambda: (app.listbox.selection_clear(0, "end"),
                             app.listbox.selection_set(0), app.on_select(),
                             app.do_values_load()))
step("use values", lambda: (app.values_name.set("typed_back"),
                            app.do_values_use()))

# --- files ----------------------------------------------------------------
folder = tempfile.mkdtemp()


def _save_all(target):
    W.filedialog.askdirectory = lambda **k: target
    app.do_save_all()


def _load_folder(target):
    W.filedialog.askdirectory = lambda **k: target
    app.do_load_folder()


# Nothing built in this window has been written yet, so everything should be
# flagged unsaved - and nothing should be, straight after Save all.
if not [n for n in app.library if n not in app.saved]:
    failures.append("nothing was flagged unsaved before saving")
step("save all", lambda: _save_all(folder))
written = sorted(os.listdir(folder))
if not written:
    failures.append("save all wrote nothing")
still = [n for n in app.library if n not in app.saved]
if still:
    failures.append("still flagged unsaved after Save all: %r" % (still[:5],))
# With the ILC header box ticked (and a rate set), Save all writes the
# time_us,voltage_V layout with a # header - the file EOM-ILC reads.
ilc_folder = tempfile.mkdtemp()
step("ILC header save all", lambda: (app.ilc_header.set(True),
                                     app.rate_text.set("500000"),
                                     _save_all(ilc_folder)))
ilc_written = sorted(os.listdir(ilc_folder))
if not ilc_written:
    failures.append("ILC-header save all wrote nothing")
else:
    with open(os.path.join(ilc_folder, ilc_written[0])) as handle:
        head = [handle.readline() for _ in range(6)]
    if not head[0].startswith("# "):
        failures.append("ILC-header file does not start with a # comment")
    if "time_us,voltage_V\n" not in head:
        failures.append("ILC-header file has no time_us,voltage_V header line")
    body = [line for line in head if line and line[0].isdigit()]
    if body and not body[0].startswith("0.000000,"):
        failures.append("ILC-header time axis does not start at 0: %r" % (body[0],))
for entry in os.listdir(ilc_folder):
    os.remove(os.path.join(ilc_folder, entry))
os.rmdir(ilc_folder)
step("ILC header off again", lambda: (app.ilc_header.set(False),
                                      app.rate_text.set("")))
step("modify in place makes it unsaved again",
     lambda: (app.mod_source.set(sorted(app.library)[0]),
              app.mod_replace.set(True), app.do_invert(),
              app.mod_replace.set(False)))
if sorted(app.library)[0] in app.saved:
    failures.append("an in-place change left the waveform flagged as saved")

step("empty the library", lambda: (app.library.clear(), app.origin.clear(),
                                   app.refresh_library()))
step("load folder", lambda: _load_folder(folder))
root.update()
if len(app.library) != len(written):
    failures.append("reloaded %d of %d files" % (len(app.library), len(written)))

# Every reloaded record has to match what was written, to the digits written.
if app.library:
    sample = sorted(app.library)[0]
    path = os.path.join(folder, sample + ".csv")
    columns, colnames = W.read_table(path)
    if len(columns) != 2:
        failures.append("%s came back with %d columns" % (path, len(columns)))
    elif max([abs(a - b) for a, b
              in zip(columns[1], app.library[sample])]) > 1e-9:
        failures.append("%s did not round trip" % (path,))

# --- a file this program did not write ------------------------------------
# Headed time_s,CH1_V,CH2_V, which is what Scope Grab writes. Picking column 0
# here would upload the TIME AXIS as the waveform - and a time axis normalises
# into a clean ramp, so it looks like a plausible record rather than a mistake.
odd = os.path.join(folder, "scope_capture.csv")
handle = open(odd, "w")
handle.write("time_s,CH1_V,CH2_V\n")
for i in range(200):
    handle.write("%g,%g,%g\n" % (i * 1e-6, i / 200.0, 1 - i / 200.0))
handle.close()
step("load a headed file", lambda: app.load_one(odd, ask=False))
if "scope_capture" in app.library:
    got = app.library["scope_capture"]
    if abs(got[0]) > 1e-12 or abs(got[-1] - 199 / 200.0) > 1e-12:
        failures.append("headed file picked the wrong column: %r" % (got[:3],))
else:
    failures.append("headed file did not load")

# --- a timed file keeps its own grid (WE-1) -------------------------------
# An ILC drive on the 2 us grid loaded while the rate box says 1 MHz: an
# ILC-header save must not quietly re-time it onto 1 us. The question is
# answered Yes (own grid), No (the box), Cancel (nothing written).
timed = os.path.join(folder, "drive_2us.csv")
with open(timed, "w") as fh:
    fh.write("# smoke drive\ntime_us,voltage_V\n")
    for i in range(50):
        fh.write("%.6f,%.6f\n" % (2.0 * i, i / 50.0))
asked = []


def _answer(value):
    def ask(*a, **k):
        asked.append(a)
        return value
    return ask


def _save_timed(answer, rate_box, path):
    W.messagebox.askyesnocancel = _answer(answer)
    app.rate_text.set(rate_box)
    app.write_one(path, "drive_2us")


def _time_step(path):
    columns, names = W.read_table(path)
    return columns[0][1] - columns[0][0]


step("load a timed file", lambda: (app.rate_text.set("1e6"), app.load_one(timed, ask=False)))
check(abs(app.wave_rate.get("drive_2us", 0) - 500000.0) < 1e-3,
      "timed file did not keep its 2 us grid: %r" % (app.wave_rate.get("drive_2us"),))
step("ILC header on", lambda: app.ilc_header.set(True))
own = os.path.join(folder, "timed_own.txt")
step("save timed, own grid", lambda: _save_timed(True, "1e6", own))
check(len(asked) == 1, "a rate mismatch did not ask (%d questions)" % (len(asked),))
check(os.path.exists(own) and abs(_time_step(own) - 2.0) < 1e-9,
      "Yes did not write the file on its own 2 us grid")
boxed = os.path.join(folder, "timed_box.txt")
step("save timed, box rate", lambda: _save_timed(False, "1e6", boxed))
check(os.path.exists(boxed) and abs(_time_step(boxed) - 1.0) < 1e-9,
      "No did not write the file at the rate box (1 us)")
cancelled = os.path.join(folder, "timed_cancel.txt")
step("save timed, cancel", lambda: _save_timed(None, "1e6", cancelled))
check(not os.path.exists(cancelled), "Cancel still wrote the file")
del asked[:]
agreed = os.path.join(folder, "timed_agree.txt")
step("save timed, box agrees", lambda: _save_timed(None, "500000", agreed))
check(not asked and os.path.exists(agreed), "a matching rate box asked or refused")
no_box = os.path.join(folder, "timed_nobox.txt")
step("save timed, box empty", lambda: _save_timed(None, "", no_box))
check(not asked and os.path.exists(no_box) and abs(_time_step(no_box) - 2.0) < 1e-9,
      "with the box empty the file's own grid was not used")
step("piece keeps the grid", lambda: (app.cut_source.set("drive_2us"),
                                      app.cut_first.set("1"), app.cut_last.set("20"),
                                      app.cut_name.set("drive_2us_piece"), app.do_take()))
check(abs(app.wave_rate.get("drive_2us_piece", 0) - 500000.0) < 1e-3,
      "a piece of a timed record lost its grid")
step("ILC header off, tidy up", lambda: (app.ilc_header.set(False), app.rate_text.set(""),
                                         setattr(W.messagebox, "askyesnocancel",
                                                 lambda *a, **k: True)))
for entry in ("timed_own.txt", "timed_box.txt", "timed_agree.txt", "timed_nobox.txt"):
    if os.path.exists(os.path.join(folder, entry)):
        os.remove(os.path.join(folder, entry))

# --- Save all refuses two names that are one file (WE-3) --------------------
# On Windows `Ramp` and `ramp` are the same file: the second must not silently
# replace the first with both flagged saved.
told = []
real_showinfo = W.messagebox.showinfo
dup_folder = tempfile.mkdtemp()
step("two names, one file", lambda: (app.add_wave("Ramp", [0.0, 1.0], "case test"),
                                     app.add_wave("ramp", [1.0, 0.0], "case test")))
if os.path.normcase("A") == "a" and "Ramp" in app.library and "ramp" in app.library:
    W.messagebox.showinfo = lambda *a, **k: told.append(a)
    step("save all with a case clash", lambda: _save_all(dup_folder))
    W.messagebox.showinfo = real_showinfo
    check(not os.listdir(dup_folder), "Save all wrote files despite a name clash: %r"
          % (os.listdir(dup_folder),))
    check(len(told) == 1, "Save all did not say why it refused (%d messages)" % (len(told),))
    check("Ramp" not in app.saved and "ramp" not in app.saved,
          "a refused Save all flagged the clashing names saved")
step("drop the case test", lambda: [(app.library.pop(n, None), app.saved.discard(n))
                                    for n in ("Ramp", "ramp")] and app.refresh_library())
os.rmdir(dup_folder)

# --- supertime ramps ------------------------------------------------------
# An ILC-style drive with a DC offset, through the whole tab: read, plateau
# split, both files written, preview, and the endpoint table.
drive = os.path.join(folder, "drive_SMOKE_i00.csv")
with open(drive, "w") as fh:
    fh.write("# smoke drive\ntime_us,voltage_V\n")
    for i in range(600):
        t = 2.0 * i
        if t < 200:
            v = 0.02
        elif t < 600:
            v = 0.02 + 9.0 * (t - 200) / 400.0
        elif t < 700:
            v = 9.02
        elif t < 1100:
            v = 9.02 - 9.0 * (t - 700) / 400.0
        else:
            v = 0.02
        fh.write("%g,%.6f\n" % (t, v))
table = os.path.join(folder, "EOM_SMOKE.txt")
with open(table, "w") as fh:
    fh.write("-0.03\t0.012\n0.79\t1\n8.30\t10\n9.99\t12\n")
step("tab supertime", lambda: app.tabs.select(app.tab_frames["supertime"]))
step("supertime read", lambda: (app.st_file.set(drive), app.st_read()))
if not app.st_split.get():
    failures.append("supertime read did not suggest a split")
step("supertime plateau", app.st_find_plateau)
step("supertime preview", app.st_preview)
step("supertime write", lambda: (app.folder.set(folder), app.st_stem.set("SMOKE"),
                                 app.st_write()))
for half in ("SMOKE_up.csv", "SMOKE_down.csv"):
    path = os.path.join(folder, half)
    if not os.path.exists(path):
        failures.append("supertime did not write %s" % (half,))
        continue
    raw = open(path, "rb").read()
    if raw.startswith(b"time") or b"#" in raw or not raw.count(b"\r\n"):
        failures.append("%s is not the headerless CRLF layout" % (half,))
    t_back, v_back = W.read_timed(path)
    end = v_back[0] if half.endswith("up.csv") else v_back[-1]
    if abs(end) > 1e-12:
        failures.append("%s: the anchor sample is %g, not 0" % (half, end))
step("supertime regrid", lambda: (app.st_step.set("10"), app.st_down_from_zero.set(True),
                                  app.st_stem.set("SMOKE10"), app.st_write(),
                                  app.st_step.set(""), app.st_down_from_zero.set(False)))
t10, _ = W.read_timed(os.path.join(folder, "SMOKE10_down.csv"))
if t10[0] != 0 or t10[1] - t10[0] != 10:
    failures.append("regridded down ramp is %r..: not a 10 us grid from 0" % (t10[:3],))
step("supertime endpoints", lambda: (app.st_table.set(table), app.st_zero.set("0V"),
                                     app.st_end.set("10.97"), app.st_endpoints()))
if "stroke" not in app.st_note.cget("text"):
    failures.append("endpoints did not report a stroke: %r" % (app.st_note.cget("text"),))
step("supertime endpoints in volts", lambda: (app.st_end.set("9.16V"), app.st_endpoints()))
step("supertime endpoints in units", lambda: (app.st_zero.set("0.05"), app.st_end.set("10.97"),
                                              app.st_endpoints()))

# --- modify: zero a point ---------------------------------------------------
step("modify zero first", lambda: (app.tabs.select(app.tab_frames["modify"]),
                                   app.mod_source.set(app.list_names[0]), app.mod_replace.set(False),
                                   app.mod_name.set("zeroed_first"), app.do_zero_point("first")))
if "zeroed_first" not in app.library or abs(app.library["zeroed_first"][0]) > 1e-12:
    failures.append("zero first point did not zero the first point")

# --- series pair --------------------------------------------------------------
# Two nested ILC-style drives written to disk, then the whole tab: read,
# check, preview, four files, and the target builder.
ts, so, si = W.nested_targets(2.0, 200, 600, 100, 800, 300, 200, 9.0, 8.0)
outer = os.path.join(folder, "drive_SMOKE_outer.csv")
inner = os.path.join(folder, "drive_SMOKE_inner.csv")
for path, values, off in ((outer, so, 0.02), (inner, si, 0.08)):
    with open(path, "w") as fh:
        fh.write("# smoke series drive\ntime_us,voltage_V\n")
        for tt, vv in zip(ts, values):
            fh.write("%g,%.6f\n" % (tt, vv + off))
step("tab series", lambda: app.tabs.select(app.tab_frames["series"]))
step("series check", lambda: (app.se_file_a.set(outer), app.se_file_b.set(inner),
                              app.se_guard.set("50"), app.se_check()))
report = app.se_report.get("1.0", "end")
if "series pair OK" not in report:
    failures.append("nested drives did not pass the series check: %r" % (report,))
step("series swapped", lambda: (app.se_file_a.set(inner), app.se_file_b.set(outer), app.se_check()))
if "NOT OK" not in app.se_report.get("1.0", "end"):
    failures.append("swapped drives passed the series check")
step("series suggest guard", lambda: (app.se_file_a.set(outer), app.se_file_b.set(inner),
                                      app.se_tol.set("0.2"), app.se_suggest()))
if not app.se_guard.get().strip() or "guard suggested" not in app.se_report.get("1.0", "end"):
    failures.append("guard suggestion did not fill the box: %r" % (app.se_guard.get(),))
step("series check as parallel", lambda: app.se_parallel())
if "NOT OK" not in app.se_report.get("1.0", "end"):
    failures.append("nested drives passed the parallel check")
step("series preview", lambda: (app.se_file_a.set(outer), app.se_file_b.set(inner), app.se_preview()))
if "drive_SMOKE_outer" not in app.library:
    failures.append("series preview did not add the outer drive")
step("series write", lambda: (app.folder.set(folder), app.se_stem.set("SMOKESER"), app.se_write()))
for which in ("outer_up", "outer_down", "inner_up", "inner_down"):
    path = os.path.join(folder, "SMOKESER_%s.csv" % which)
    if not os.path.exists(path):
        failures.append("series write missed %s" % which)
        continue
    raw = open(path, "rb").read()
    if not raw[:1].isdigit() or b"\r\n" not in raw or raw.endswith(b"\n"):
        failures.append("%s is not a headerless CRLF Supertime file" % which)
t_iu, v_iu = W.read_timed(os.path.join(folder, "SMOKESER_inner_up.csv"))
if abs(v_iu[0]) > 1e-9:
    failures.append("inner up ramp does not start at zero: %r" % (v_iu[0],))
step("series build targets", lambda: (app.se_target["step"].set("2"), app.se_build()))
if "series_outer_target" not in app.library or "series_inner_target" not in app.library:
    failures.append("target builder did not add both targets")
if not app.ilc_header.get():
    failures.append("target builder did not tick the ILC header")

# --- nothing runs off the right edge at the minimum window size --------------
def _overflow():
    root.geometry("880x620")
    root.update_idletasks()
    root.update()
    bad = []
    for key, frame in app.tab_frames.items():
        app.tabs.select(frame)
        root.update_idletasks()
        root.update()
        for row in frame.winfo_children():
            width = row.winfo_width()
            if isinstance(row, W.FlowFrame):
                for child, _, _ in row._flow or []:
                    if child.winfo_x() + child.winfo_reqwidth() > width + 1:
                        bad.append("%s: %s past the edge" % (key, child.winfo_class()))
            elif row.winfo_reqwidth() > width + 1:
                bad.append("%s: %s wants %d px of %d" % (key, row.winfo_class(),
                                                        row.winfo_reqwidth(), width))
    return bad
step("minimum window size", lambda: None)
for line in _overflow():
    failures.append(line)

# --- library column width ---------------------------------------------------
long_name = "drive_P92PX1H_i15_up_with_a_long_name"
step("long library name", lambda: app.add_wave(long_name, [0.0, 0.5, 1.0], "width test"))
shown = [app.listbox.get(i) for i in range(app.listbox.size())]
if not any(long_name in s for s in shown):
    failures.append("long name is clipped in the library: %r" % (shown,))
if app.listbox.cget("width") < len(long_name) + 11:
    failures.append("library column did not widen: %r" % (app.listbox.cget("width"),))

# --- housekeeping ---------------------------------------------------------
def _rename(new):
    app.ask_text = lambda *a, **k: new
    app.do_rename()


step("rename", lambda: _rename("renamed_one"))
step("duplicate", app.do_duplicate)
step("remove", app.do_remove)
step("resize the plot", lambda: (app.plot.canvas.configure(width=900,
                                                           height=300),
                                 root.update_idletasks(), app.plot.draw()))
step("back to the build tab", lambda: app.tabs.select(app.tab_frames["build"]))
step("final refresh", app.refresh_preview)

root.update()
root.destroy()

for entry in os.listdir(folder):
    os.remove(os.path.join(folder, entry))
os.rmdir(folder)

if W.CONFIG_PATH == real_config or not W.CONFIG_PATH.startswith(config_folder):
    failures.append("CONFIG_PATH was not the sandboxed one: %r" % (W.CONFIG_PATH,))
for entry in os.listdir(config_folder):
    os.remove(os.path.join(config_folder, entry))
os.rmdir(config_folder)

if failures:
    for item in failures:
        print("FAIL " + item)
    print("%d smoke failure(s)" % (len(failures),))
    sys.exit(1)
print("smoke test passed")
