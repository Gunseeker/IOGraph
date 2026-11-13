# IOGraph
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="iograph-2.0.0-dark.jpg">
  <img width="876" alt="IOGraph" src="iograph-2.0.0-light.jpg">
</picture>

### Turn your routine work into modern art

IOGraph — is an application that turns mouse movement into visual artifacts. The idea is simple: run IOGraph in the background while you go about your daily work. Later, return to the program and export an image generated from your activity.

IOGraph records only mouse movement. It does not record typing, clicks, files, applications, or screen content, and nothing is sent anywhere — everything happens locally on your computer. If you like, you can also place a screenshot of your screen behind the visualization to create a composite image.

You can save the resulting image and use it however you like.

### Raw-data import fork

Use **File → Load Raw Data...** (`Ctrl+O`) to select an exported CSV. **Append
to Current Session** is the default. **Replace Current Session** requires a
second confirmation. Tracking continues during validation, and successful
imports retain the previous tracking/paused state. Cancelled or invalid imports
leave the recording unchanged.

Recordings are separate segments: their elapsed milliseconds are added together,
without connecting movement lines or idle circles across boundaries. Pausing and
resuming live tracking also starts a new segment. Negative global coordinates
and blank-coordinate idle samples are preserved. Rendering uses the current
monitor configuration; CSV alone cannot recover a previous monitor geometry or
the original recording dates. Imported sessions therefore omit inferred dates
from their displayed period and export filename.

The importer accepts the original `x,y,time` header and an extended
`x,y,time,segment_start` header. The optional fourth column is `1` at a segment's
first sample and `0` elsewhere. This fork exports the extended format so segment
boundaries survive CSV round trips. Tools reading the original format can use
the first three columns, but discarding the fourth loses boundary information.
Coordinates must be signed 32-bit integer pixels; time is nonnegative integer
milliseconds in nondecreasing order. Invalid rows reject the entire file.
Imports run in a cancellable worker, with limits of **2 million data rows** and
**256 MiB per CSV**. Samples use lossless temporary disk storage; large sessions
require free disk space, and committing or saving them may briefly pause the UI.

Sessions, caches, instance locks, and settings use the separate **IOGraphRawData**
application identity (`iographica/IOGraphRawData` for settings). Your installed
IOGraph and its saved recordings are untouched. To transfer an existing
recording, export CSV from the installed app and load it in this fork. Automatic
update checks default to off; manual checks and any enabled automatic checks use
**Gunseeker/IOGraph**, never upstream. Existing upstream settings and downloaded
update metadata are not copied into the fork.

### Build and run the Windows fork

From the repository root in PowerShell, with Python 3.11 or newer installed:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller pillow
.\.venv\Scripts\python.exe -m compileall iograph
.\.venv\Scripts\python.exe scripts/smoke_regression.py
.\.venv\Scripts\python.exe scripts/i18n_missing.py --strict
.\.venv\Scripts\python.exe -m unittest discover -s scripts -p 'test_*.py' -v
.\scripts\build_windows.ps1
```

Launch `dist\raw-data\IOGraph\IOGraph.exe`. Keep its `runtime` folder alongside
the executable. To share or move it, extract the generated
`dist\IOGraph-windows-<VERSION>-raw-data.zip` into a new folder and run
`IOGraph.exe` there. The build reads the checkout's `VERSION` and appends
`-raw-data` in the package, without changing the source version file or creating
a release tag. The current checkout is `2.0.3-rc.004`; its output ZIP is
`IOGraph-windows-2.0.3-rc.004-raw-data.zip`. Build files are under `build\raw-data`
and outputs under `dist`; both are ignored by Git.

The script follows the existing Windows release's PyInstaller onedir layout,
resources, Qt collection, and Windows version resource. It also removes a
conflicting `icuuc.dll` sometimes collected from bundled Python development
runtimes, allowing Qt to use Windows' ICU implementation.

Alternatively, run the **Build Windows Fork** workflow manually on this branch
in your fork and download its **IOGraph-Windows-RawData** artifact. It runs the
checks and packages a ZIP without publishing a release or creating a tag. The
existing release workflows are unchanged.

### Long-session memory and performance audit

The original tuple list grew with every movement sample. That was retained
recording history, not evidence of a leak, but full-session dictionary lists,
render snapshots, CSV strings, and restore buffers caused large additional peaks.
Repeated imports also left their parented dialogs alive until the window closed.
Preview workers could queue full images while the GUI was busy. Raster buffers
and the live idle-sample coalescing were bounded; the audit did not establish a
continuous leak in Qt's drawing engine or in the fixed tracking/UI timers.

The fork now keeps samples in a temporary packed file with one mutable tail in
RAM. Every movement, idle endpoint, global coordinate, integer timestamp and
segment boundary is preserved. Renderers read immutable, bounded-batch snapshots
of that store. Appending constructs a new store before swapping it in, so a
failed import leaves the original recording intact. Reset/replacement releases
old storage once any in-flight render completes. The store uses double-precision
coordinates and signed 64-bit elapsed milliseconds; CSV timestamp offset rules
are unchanged.

CSV parsing/export and session save/restore now stream samples. Session chunks
are staged under unique generation names; the manifest is flushed and replaced
atomically before obsolete chunks are removed. Failed writes preserve the prior
manifest/chunks, and incomplete restores no longer silently drop missing chunks.
An incomplete restore protects existing saved files from subsequent saves until
an explicit reset/replacement; export any new recording first. A failed save
prevents a normal application exit from discarding the current recording.

Import dialogs are explicitly deleted, preview delivery keeps only the latest
pending image, completed workers release their snapshots, and render workers
finish/cancel safely on exit. Application-wide screen/theme connections use
bound QObject methods so destroyed windows can be collected. Repeated desktop
snapshot requests are coalesced, and disabling the background releases its image.

Controlled measurements used Python 3.12.14 / Qt 6.12 on Windows. The baseline
was commit `cc63c59`, with the same 200,000 movement samples (about 1.83 hours at
33 ms). The table reports peak **Python allocations traced by tracemalloc**, in
decimal MB, including the live datasets at each phase; it is not total app RAM.

| Phase | Before, MB | After, MB |
| --- | ---: | ---: |
| Record | 30.43 | 0.02 |
| Two render snapshots | 33.75 | 0.13 |
| Session save | 73.93 | 0.39 |
| Session restore | 112.62 | 0.22 |
| CSV export | 53.95 | 0.40 |
| CSV parse | 86.14 | 0.20 |
| Append | 110.13 | 0.38 |

A 2.7-million-sample simulation (24.75 hours of continuous movement) verified
every coordinate and timestamp, using 67.5 MB of temporary sample storage and
about 0.35 MB peak traced Python allocation during read-back. A separate native
Windows Qt simulation of 20,000 actual tracking ticks and repeated redraws
produced identical CSV and rendered-preview SHA-256 hashes before/after, with
GDI/USER counts constant at 2/9 in both processes. This is a controlled simulation,
not a days-long real-world soak or proof that every graphics driver is leak-free.

The automated suite covers concurrent snapshots, resource release, idle updates,
repeated imports/redraws, pixel parity, cancellation, disk-write/restore failures,
atomic saves, shutdown, image export, and the existing CSV tests. Run the bounded
profiling workloads separately when needed:

```powershell
.\.venv\Scripts\python.exe scripts/profile_memory.py --output build/memory-audit/after.json
.\.venv\Scripts\python.exe scripts/profile_memory.py --samples 2700000 --record-only --output build/memory-audit/one-day.json
```

Tradeoffs: complete history still grows on disk (25 bytes per packed sample,
about 65 MB/day at 33 ms), and imports may temporarily need both old and new
stores plus staged session files. Full-history rendering/saving remains O(n).
Disk-backed CSV parsing and append were slower in the traced 200,000-row workload
(about 4.0 vs 3.0 seconds parsing, and 1.5 vs 0.3 seconds appending); snapshot
creation and reset became cheaper. Disk cache, Qt buffers, Python allocator
high-water marks, and segment/chunk metadata can still consume memory. The
compatibility APIs `export_raw_samples()` and `export_csv_text()` still materialize
their requested outputs; production save/render/import paths use streaming views.
Temporary storage is not a new crash-recovery format: the existing session-save
triggers are retained, and a power failure can still lose changes since the last
successful session save. No samples are sampled away or deleted to reduce RAM.
