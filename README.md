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
**256 MiB per CSV** to bound memory use. Large sessions still require sufficient
RAM; committing and saving them may briefly pause the UI.

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
.\.venv\Scripts\python.exe -m unittest discover -s scripts -p test_raw_import.py -v
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
