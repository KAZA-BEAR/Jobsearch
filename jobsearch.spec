# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the EU Robotics & Mechatronics Job Search desktop app.

Build on Windows with:
    pyinstaller jobsearch.spec --clean --noconfirm

Output: dist\\EUJobSearch.exe  (single file, no console window)
"""

from PyInstaller.utils.hooks import collect_dynamic_libs

block_cipher = None

# tls_client (a jobspy dependency) loads a native DLL/so/dylib via
# ctypes.LoadLibrary at a path relative to its own __file__ — PyInstaller's
# static import analysis can't see that, so without this the frozen .exe
# crashes on startup with "Failed to load dynlib/dll ...tls-client-64.dll".
tls_client_libs = collect_dynamic_libs('tls_client')

a = Analysis(
    ['job_gui.py'],
    pathex=['.'],
    binaries=tls_client_libs,
    # profile.json is bundled so fit scoring works out of the box; a copy in
    # %APPDATA%\EUJobSearch takes precedence once you add one there.
    datas=[('profile.json', '.')] if __import__('os').path.exists('profile.json') else [],
    hiddenimports=[
        # Local modules imported indirectly through the GUI
        'linkedin_jobs',
        'eu_student_jobs',
        'robotics_track',
        'us_asia_jobs',
        'company_radar',
        'ats_prompts',
        'ats_latex',
        'ats_regions',
        'ats_pipeline',
        'ats_checks',
        'ats_plan',
        'ats_embed',
        'jobspy_provider',
        'ba_jobsuche',
        'personio_jobs',
        'workday_jobs',
        'research_jobs',
        'fit_score',
        'daily_sweep',
        'system_checks',
        'xml.etree.ElementTree',
        'email.utils',
        # requests pulls these in dynamically
        'requests',
        'urllib3',
        'charset_normalizer',
        'idna',
        'certifi',
        # Tkinter submodules PyInstaller sometimes misses
        'tkinter',
        'tkinter.ttk',
        'tkinter.filedialog',
        'tkinter.messagebox',
        # stdlib used at runtime only
        'sqlite3',
        'csv',
        'json',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # numpy/pandas/jobspy are NOT excluded: the "Web search (JobSpy)" tab
        # needs them bundled to work in the built .exe (adds ~150 MB — see
        # install_jobspy.py, which build.bat runs into .venv before this
        # spec's Analysis step picks them up). Everything below genuinely
        # is never imported, so it's still trimmed.
        'matplotlib', 'scipy', 'PIL',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
        'notebook', 'IPython', 'pytest', 'setuptools',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='EUJobSearch',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=['vcruntime140.dll', 'python3*.dll'],
    runtime_tmpdir=None,
    console=False,          # no black terminal window behind the GUI
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico' if __import__('os').path.exists('icon.ico') else None,
    version='version_info.txt' if __import__('os').path.exists('version_info.txt') else None,
)
