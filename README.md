# Waveform Editor GUI

Build arbitrary waveforms, cut them into pieces, reassemble the pieces, and
save the result as CSV. It is the BK4063B AWG GUI's waveform building with the
instrument taken out — the same shape library, the same segment assembler, the
same preview — but nothing here talks to hardware, and the only thing it
produces is a file.

Written for **Python 2.7 on Windows XP**, using nothing but the standard
library. No numpy, no matplotlib, nothing to install: the preview is drawn by
hand on a Tk canvas. The same file runs unchanged on Python 3, which is how it
gets tested away from the XP machine.

```
pythonw waveform_editor_gui.py          # no console window
python  waveform_editor_gui.py --selftest
```

## The file format

In and out: **two columns, `index,value`, the index counting from 1, and
nothing else in the file.**

```
1,0
2,7.545898248e-07
3,1.51169371e-06
```

Values are written to ten significant figures, so a record saved here and read
back is the same record — measured round-trip error on a 106,020-point capture
is exactly zero.

Tick **ILC header (time_us,voltage_V)** under the library buttons and both
`Save CSV...` and `Save all...` write the other layout instead:
`time_us,voltage_V` with `#` comment lines above it, which is what the EOM-ILC
panel reads as a target or as a seed drive. It needs the sample rate box
(500kHz for the 2 µs grid): the time column is index / rate, from 0, and a
save with the box ticked and no rate set is refused with the reason rather
than guessed. The default `index,value` file is refused by that panel on
purpose — with no time axis it would read the index as seconds — so a record
that is going back to the ILC goes out with the box ticked. The setting is
remembered between launches.

The reader is deliberately looser than the writer, because the files it gets
pointed at were written by other things:

- one column of bare samples, or two, or many;
- commas, semicolons, tabs or whitespace between them;
- a header row from a spreadsheet (`time_us,voltage_V`);
- `#` comment lines — so the AWG GUI's own `Waveforms/*.csv` cache files, which
  carry two comment lines and a single column, load here directly.

Which column holds the samples is worked out rather than assumed. The
`index,value` pair this program writes is recognised outright, so reloading its
own output never asks anything; `time_us,voltage_V` picks the volts, not the
time axis; anything genuinely ambiguous puts up a column chooser rather than
guessing. Getting this wrong is quiet and nasty — a time axis read as samples
normalises into a clean ramp, so it looks like a plausible waveform rather than
like a mistake.

## The panel

**Library** (left) holds everything this session has built, cut or loaded. A
`*` in the left margin means that one exists only in this window — it clears
when the waveform is written out and comes back if it is changed in place, and
it is what the close prompt lists. Nothing you save is touched by `Remove`.
`Load folder...` pulls in every CSV in one place, `Save all...` writes the whole
library back out — which is what turns a working folder into a persistent
library without any hidden state.

**Preview** always shows whatever the open tab is about: the shape you are
typing, the source you are cutting with the span shaded, the record you are
assembling with its segment boundaries marked. Long records are drawn the way a
scope draws them — one vertical line per pixel column spanning that column's
min and max — so a 106,020-point record renders in 4 ms and shows what is
actually in it, which a polyline squeezed into 700 pixels does neither.
Hovering reads out the sample under the cursor.

### Build a shape

Sixteen shapes, ported from the AWG GUI sample for sample so a record built
there and here is the same record: Gaussian, Blackman-Harris, Hann, Tukey
flat-top, Sech, Sinc, square, trapezoid, tanh flat-top, DC hold, linear,
exponential and smoothstep ramps, chirp, multitone, Gaussian derivative. Any of
them can be multiplied by a carrier to make a burst.

The note line says what the record will come to and what it cannot resolve — a
carrier with fewer than two points per cycle is aliased and says so before you
build it, not after. The preview updates as you type; past 40,000 points it is
drawn shorter, which shows the same curve because a shape is specified in
fractions of its record.

### Cut into pieces

The part the AWG GUI does not have. **Drag across the preview** to pick a span;
the `From` and `To` boxes follow, and so does the shaded region and the piece
drawn in red over the top. `Zoom to span` narrows the view so the ends can be
placed exactly. Click the preview to clear.

`From`/`To` are 1-based and inclusive, so the numbers you type are the numbers
in the file's first column — 1 to 100 is the first hundred samples.

Below that, two ways to cut the whole thing up at once: into *N* equal pieces
(boundaries rounded from the exact fractions, so the pieces tile the source
exactly rather than leaving the remainder in the last one), or into fixed-length
chunks. Both name the pieces `source_1`, `source_2`, ...

### Assemble

Pieces of other waveforms, and freshly built shapes, laid end to end. One row
per segment:

| column | meaning |
| --- | --- |
| Source | a library waveform, or `shape: <name>` to build one here |
| From / To | a span of that waveform; blank takes the whole thing |
| Points | resample the piece to this length; on a `shape:` row, how long to build it |
| Rep | how many times to lay it down |
| Scale / Offset | multiply, then add |
| Options | `reverse=on`, plus a shape's own `key=value` settings |
| Gap after | samples of the gap level to leave before the next segment |

The `shape:` prefix is not decoration — without it a waveform saved as
`Gaussian` and the Gaussian builder are the same string, and a row would
silently mean whichever the lookup tried first.

### Modify

Scale and offset, normalise to ±1, stretch to 0..1, invert, reverse, resample
to a point count, clip to a range. Every one of them makes a new waveform by
default; tick *change it in place* to overwrite the source instead.

### Values

The selected record's numbers, one per line, to read and edit by hand — the
thing that is otherwise impossible once a record exists. Capped at 20,000 lines,
because a Tk text widget holding a line per sample is comfortable there and
unusable at a million.

## Sample rate

Optional, in the top bar. Leave it blank and everything is in samples. Set one
and two things change: every count box also takes a time (`2ms`, `500us`,
`200µs`), every carrier box also takes a frequency (`40kHz`), and the preview's
x axis becomes a time axis with the grid stepping in round times rather than
round sample numbers.

Percentages work either way: `25%` and `75%` in the Cut tab's From/To is the
middle half of the record.

## Windows XP and Python 2.7 notes

- **Standard library only.** numpy and matplotlib for 2.7 are wheels that may or
  may not be on an XP machine, and the program is worth nothing if it will not
  start.
- **Courier New**, not Consolas, for the monospaced text — Consolas ships with
  Vista and Office 2007, so on a bare XP install it falls back to something
  proportional and the columns of numbers stop lining up.
- Settings (folder, sample rate, window size) live in
  `%APPDATA%\Waveform-Editor-GUI\config.json`.
- Two million points is the cap. Measured on a modern machine, a million points
  builds in 0.4 s, writes in 0.9 s (21 MB) and reads back in 3 s, and the list
  itself holds about 32 MB — an XP-era machine is several times slower and has
  far less to spare.

## Verification

Three checks, all run before this was handed over. The first two run anywhere;
the third is what stands in for a 2.7 interpreter on a machine that has none.

```bash
python waveform_editor_gui.py --selftest
```

Checks the arithmetic a preview cannot show you is wrong: the 1-based inclusive
slicing, the resampler's endpoints, that a split tiles its source exactly, that
every shape builds at the length asked for, the assembler's segment placement,
and that a file written here reads back as the same numbers with the index
column recognised rather than taken for samples.

The GUI smoke test (`smoke.py`, kept with the session notes) builds the window
and drives every panel — every tab, the plot redraw, the Assemble table's
insert/reorder/delete, save-all, load-folder, and a `time_s,CH1_V,CH2_V` file
loading with the right column picked.

For the 2.7 grammar, `lib2to3` is gone from Python 3.13 and the parso Anaconda
ships dropped its 2.7 grammar. The recipe that works here:

```bash
/c/ProgramData/anaconda3/python.exe -m pip install --target ./_p2 "parso==0.5.2"
```

then `sys.path.insert(0, './_p2')`, `parso.load_grammar(version='2.7')` and
`grammar.iter_errors(tree)`. Current state: 0 grammar errors, pure ASCII,
uniform CRLF, no tab-indented lines.

**A clean parse is syntax only.** It is not a runtime test, and this has not yet
been run on an actual Python 2.7 / Windows XP machine. That is the one thing
still outstanding.
