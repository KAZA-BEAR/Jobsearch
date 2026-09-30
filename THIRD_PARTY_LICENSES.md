# Third-party licenses

This project's own source code is licensed under the
[PolyForm Noncommercial License 1.0.0](LICENSE). The libraries it uses keep their
own licenses, listed below. **The noncommercial restriction applies only to this
project's code, not to these libraries.** Each library can still be used under its
own terms.

Versions are the ones installed in the build environment (`.venv`) at the time of
writing. Check each project for its current license text.

## Runtime: required

Installed by `pip install -r requirements.txt`.

| Library | Version | License | Source |
|---|---|---|---|
| requests | 2.34.2 | Apache-2.0 | https://github.com/psf/requests |
| urllib3 | 2.8.0 | MIT | https://github.com/urllib3/urllib3 |
| charset-normalizer | 3.5.1 | MIT | https://github.com/jawah/charset_normalizer |
| idna | 3.20 | BSD-3-Clause | https://github.com/kjd/idna |
| certifi | 2026.7.22 | MPL-2.0 | https://github.com/certifi/python-certifi |

## Runtime: optional (JobSpy search engine)

Installed by `python install_jobspy.py`. The Windows `.exe` bundles these packages too.

| Library | Version | License | Source |
|---|---|---|---|
| python-jobspy | 1.1.82 | MIT | https://github.com/cullenwatson/JobSpy |
| numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | https://github.com/numpy/numpy |
| pandas | 3.0.6 | BSD-3-Clause | https://github.com/pandas-dev/pandas |
| python-dateutil | 2.9.0.post0 | Apache-2.0 OR BSD-3-Clause | https://github.com/dateutil/dateutil |
| tzdata | 2026.4 | Apache-2.0 | https://github.com/python/tzdata |
| six | 1.17.0 | MIT | https://github.com/benjaminp/six |
| beautifulsoup4 | 4.15.0 | MIT | https://www.crummy.com/software/BeautifulSoup/ |
| soupsieve | 2.10 | MIT | https://github.com/facelessuser/soupsieve |
| pydantic | 2.13.5 | MIT | https://github.com/pydantic/pydantic |
| pydantic-core | 2.46.5 | MIT | https://github.com/pydantic/pydantic-core |
| annotated-types | 0.8.0 | MIT | https://github.com/annotated-types/annotated-types |
| typing-inspection | 0.4.4 | MIT | https://github.com/pydantic/typing-inspection |
| typing-extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| tls-client | 1.0.1 | MIT | https://github.com/FlorianREGAZ/Python-Tls-Client |
| markdownify | 1.2.3 | MIT | https://github.com/matthewwithanm/python-markdownify |
| regex | 2026.9.10 | Apache-2.0 AND CNRI-Python | https://github.com/mrabarnett/mrab-regex |

## Build tools (not part of the app's own code)

| Tool | Version | License | Source |
|---|---|---|---|
| PyInstaller | 6.22.3 | GPL-2.0-or-later with the PyInstaller bootloader exception | https://github.com/pyinstaller/pyinstaller |
| pyinstaller-hooks-contrib | 2026.7 | GPL-2.0-or-later / Apache-2.0 (dual) | https://github.com/pyinstaller/pyinstaller-hooks-contrib |
| altgraph | 0.17.5 | MIT | https://github.com/ronaldoussoren/altgraph |
| pefile | 2024.8.26 | MIT | https://github.com/erocarrera/pefile |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause | https://github.com/enthought/pywin32-ctypes |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging |

The PyInstaller bootloader exception allows the built `EUJobSearch.exe` to be
distributed under this project's license. The GPL does not extend to the app.

## Python runtime and standard library

The app uses only the Python standard library beyond the packages above. That
includes `tkinter`, `sqlite3`, `urllib`, `xml`, `json`, `csv` and `concurrent`. The
Windows `.exe` bundles these components:

| Component | License | Source |
|---|---|---|
| CPython | PSF-2.0 | https://docs.python.org/3/license.html |
| Tcl/Tk (via `tkinter`) | Tcl/Tk license (BSD-style) | https://www.tcl.tk/software/tcltk/license.html |
| SQLite (via `sqlite3`) | Public domain | https://www.sqlite.org/copyright.html |
| OpenSSL (via `ssl`) | Apache-2.0 | https://www.openssl.org/source/license.html |
| libffi, zlib, bzip2, xz/liblzma, libexpat | MIT / Zlib / BSD-style / 0BSD / MIT | bundled with CPython |

## External programs (not bundled)

The app can call these programs, but it does not ship or link them. You install
them separately under their own terms:

| Program | License | Source |
|---|---|---|
| LM Studio | Proprietary (LM Studio terms of use) | https://lmstudio.ai |
| MiKTeX / TeX Live (`pdflatex`) | Various free licenses (mostly LPPL / GPL) | https://miktex.org, https://tug.org/texlive |
| Language models run in LM Studio | Each model's own license | see each model card |

## External services and data

Job listings come from third-party APIs and websites, such as the Bundesagentur
für Arbeit, EURES, EURAXESS, Personio, Workday, LinkedIn/Indeed via JobSpy,
JSearch and Apify. Your use of each one is governed by that service's terms of
service. This license does not grant any rights to their data.
