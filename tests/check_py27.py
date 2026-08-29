"""Grammar-check the editor against Python 2.7 from a machine that has no 2.7.

`lib2to3` is gone from Python 3.13, and the parso Anaconda ships dropped its
2.7 grammar. The recipe that does work here is parso 0.5.2 in a --target
directory, which leaves the system parso alone:

    /c/ProgramData/anaconda3/python.exe -m pip install --target ./_p2 "parso==0.5.2"
    /c/ProgramData/anaconda3/python.exe tests/check_py27.py ./_p2

A clean parse is SYNTAX ONLY. It is not a runtime test, and it does not know
anything about a name that exists on 3 and not on 2 - the grep at the end is
there to catch the common ones by eye.
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(os.path.dirname(HERE), "waveform_editor_gui.py")

parso_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "_p2")
sys.path.insert(0, os.path.abspath(parso_dir))
try:
    import parso
except ImportError:
    sys.exit("parso not found. Install it with:\n"
             "  python -m pip install --target %s \"parso==0.5.2\"" % (parso_dir,))

raw = io.open(TARGET, "rb").read()
text = raw.decode("utf-8")
problems = 0

print("%s: %d bytes, %d lines" % (os.path.basename(TARGET), len(raw),
                                  text.count("\n")))

# The file declares `coding: ascii`, and Python 2 refuses to import it over a
# single stray byte.
try:
    raw.decode("ascii")
    print("ascii            ok")
except UnicodeDecodeError as exc:
    print("ascii            FAILED - %s" % (exc,))
    problems += 1

crlf, lf = raw.count(b"\r\n"), raw.count(b"\n")
print("line endings     %s (%d CRLF of %d LF)"
      % ("ok" if crlf == lf else "MIXED", crlf, lf))
if crlf != lf:
    problems += 1

tabs = [i + 1 for i, line in enumerate(text.splitlines()) if line[:1] == "\t"]
print("tab indentation  %s" % ("ok" if not tabs else "lines %s" % (tabs[:10],)))
if tabs:
    problems += 1

grammar = parso.load_grammar(version="2.7")
errors = list(grammar.iter_errors(grammar.parse(text)))
print("2.7 grammar      %s" % ("ok" if not errors else "%d error(s)"
                               % (len(errors),)))
for err in errors[:40]:
    print("    line %s: %s" % (err.start_pos[0], err.message))
problems += len(errors)

# Names that parse under 2.7 but only exist on 3. Reported rather than failed:
# every one of them here is inside a try/except or a version shim, and the
# point is to notice a new one arriving.
print()
for label, needle in (("nonlocal", "nonlocal "),
                      ("yield from", "yield from"),
                      ("f-string", 'f"'),
                      ("tkinter (lowercase)", "import tkinter"),
                      ("trace_add", "trace_add"),
                      ("exist_ok", "exist_ok"),
                      ("subprocess.run", "subprocess.run"),
                      ("math.inf", "math.inf"),
                      ("format_map", "format_map")):
    hits = [i + 1 for i, line in enumerate(text.splitlines()) if needle in line]
    if hits:
        print("3-only name %-22s lines %s" % (label + ":", hits[:8]))

sys.exit(1 if problems else 0)
