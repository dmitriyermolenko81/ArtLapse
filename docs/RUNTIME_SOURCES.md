# Runtime sources and replacement / Исходники компонентов

The full Windows ZIP is self-contained. The separate third-party source archive
is optional for users and provides sources for rebuilding/replacing the libraries:
https://github.com/dmitriyermolenko81/ArtLapse/releases/download/v0.1.0/ArtLapse-0.1.0-ThirdParty-Sources.zip

ArtLapse 0.1.2 uses the same dependency binaries as 0.1.0. This existing source
archive continues to apply; the new Windows ZIP includes the retained notices.

## Correspondence

- Qt DLLs/plugins: Qt6.8.3, matching the installed PySide6 distribution by SHA256. Included sources: qtbase, qtdeclarative, qtmultimedia, qtsvg, qtimageformats, qttranslations, qtwebengine(PDF)6.8.3.
- PySide/Shiboken6.8.3: pyside-setup-everywhere-src-6.8.3.tar.xz. Qt6.8.3 meta repository and coin provisioning scripts are included. ArtLapse does not modify vendor library source code.
- Qt FFmpeg DLLs: upstream7.1; version and actual configuration recorded from DLLs. Complete sources and matching Qt6.8.3 Windows provisioning scripts are included.
- OpenCV FFmpeg wrapper: upstream MD5e5c6936240201064b15bcecf1816e8f4, checked against the delivered DLL. Build repository ea9240e39bc0d6a69d2b1f0ba4513bdc7612a41e and its pinned OpenCV source e9f1da7e8e977a65b8bf8fe7ea8b92eef9171f19 are included. Upstream scripts pin FFmpeg4.4.6, libvpx1.15.2, AOM3.12.1, OpenH2641.8.0; all those sources are included. A separate Cisco OpenH264 binary is not distributed.
- CLI FFmpeg: own8.1.2-ArtLapse-202 build; complete verified source kit includes linked dependency sources, patches, actual configuration and build scripts as ArtLapse-FFmpeg-202-source.tar.
- Python3.10.6, NumPy2.2.6, OpenCV-Python4.12.0.88, Pydantic2.12.5/core2.41.5, PyInstaller6.22.0 and companion packages: sources and retained notices. CPython external dependency tags are recorded.
- Mesa11.2.2 software rasterizer/LLVM, OpenSSL1.1.1n/3.6.4 and libffi3.3 notices are preserved. SQLite is public domain. Python and NumPy/OpenCV retain their bundled component notices.

## Rebuilding

Qt/PySide: use MSVC2022, CMake and Ninja with the supplied version6.8.3 sources.
The included qt5-6.8.3/coin provisioning scripts record FFmpeg configuration.
For PySide install its requirements, run
`python setup.py build --qtpaths=C:/Qt/6.8.3/bin/qtpaths.exe --parallel=8`,
then `python create_wheels.py --build-dir=... --no-examples`.
Upstream instructions: https://doc.qt.io/qtforpython-6.8/building_from_source/windows.html.

For OpenCV's FFmpeg wrapper use the included ffmpeg/build_via_docker.sh,
ffmpeg/download_src.sh and ffmpeg/make_mingw.sh with the pinned archives above;
the OpenCV submodule is supplied as a complete source archive.
For CLI FFmpeg follow BUILDING.md and packaging/ffmpeg in its complete source kit.
Python and the Python packages retain their own standard build instructions.

Bit-for-bit reproduction on a second machine is not claimed. The kit provides
unmodified vendor-version sources, actual configurations, upstream build scripts,
notices and SHA256 manifests.

Replacement: [LIBRARY_REPLACEMENT.md](LIBRARY_REPLACEMENT.md). ArtLapse permits
LGPL library replacement and reverse engineering to debug those modifications;
there is no library signature lock. The source archive is downloadable from
the publisher's 0.1.0 release at no charge, linked from the current release.
