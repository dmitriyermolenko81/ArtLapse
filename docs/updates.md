# Updates

ArtLapse checks the latest stable GitHub release in the background after
startup. Version 0.1.2 is displayed beside ArtLapse and in the window title.
The circular-arrow button beside RU / EN runs a manual check.
The compact prompt offers Download or Later. Download opens the complete
Windows ZIP in your browser. ArtLapse does not install it automatically.

Extract the whole ZIP to a new folder. Close the previous application before
copying its `data` folder into the new application folder to retain preferences.
Keep projects and original media at their existing paths.

The automatic check stays silent when offline. Manual checks explain a network
error or report that no newer Windows release is available. Editing and exporting
continue without internet access. Checks time out after eight seconds and cancel
on application close. No project, video, settings or log data is uploaded.
GitHub receives the request, IP address and application version.

## Publishing a compatible release

1. Increase the same version in `pyproject.toml` and `src/sculptcut/__init__.py`.
2. Build and verify a complete portable Windows x64 archive.
3. Publish a stable GitHub release with a tag such as `v0.1.2` and the exact
   asset filename `ArtLapse-0.1.2-Windows-x64.zip`.
4. Use the short asset label `ArtLapse for Windows`.

Replacing an archive under an existing tag does not trigger an update offer.
Versions are compared numerically. Drafts, prereleases, source archives and
older versions are not offered. The release API and Windows download URL must
belong to the official `dmitriyermolenko81/ArtLapse` repository.
