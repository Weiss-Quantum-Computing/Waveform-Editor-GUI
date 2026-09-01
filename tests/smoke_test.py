"""Build the window and drive every panel of the editor, with nobody watching.

Not a substitute for looking at it, but it walks the paths that only exist once
the widgets are real: the tab routing, the preview redraw, the Assemble table's
insert and reorder and delete, and a file round trip through the actual
buttons. House habit is to run this after any change to the panel, and to
extend it with every feature.

    python tests/smoke_test.py

Runs on either Python, and needs no instruments. A window flashes up while it
runs.
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


root = W.tk.Tk()
root.geometry("1010x730")
app = W.App(root)
# Silence the dialogs: a smoke test must never sit waiting on a message box.
W.messagebox.showerror = lambda *a, **k: failures.append("showerror " + repr(a))
W.messagebox.showinfo = lambda *a, **k: failures.append("showinfo " + repr(a))
W.messagebox.askyesno = lambda *a, **k: True
W.messagebox.askokcancel = lambda *a, **k: True
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

if failures:
    for item in failures:
        print("FAIL " + item)
    print("%d smoke failure(s)" % (len(failures),))
    sys.exit(1)
print("smoke test passed")
