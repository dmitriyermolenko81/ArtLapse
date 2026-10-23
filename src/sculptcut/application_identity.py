"""Stable Windows taskbar identity, independent of the portable folder's path."""

import ctypes
import logging
import sys

from PySide6.QtCore import QCoreApplication

APP_USER_MODEL_ID = "ArtLapse.Desktop"


def configure_application_identity() -> None:
    """Run before QApplication creates any native windows (also in diagnostics)."""
    QCoreApplication.setApplicationName("ArtLapse")
    if sys.platform != "win32":
        return
    try:
        shell = ctypes.WinDLL("shell32")
        set_id = shell.SetCurrentProcessExplicitAppUserModelID
        set_id.argtypes = [ctypes.c_wchar_p]
        set_id.restype = ctypes.c_long
        result = set_id(APP_USER_MODEL_ID)
        if result < 0:
            raise OSError(f"SetCurrentProcessExplicitAppUserModelID: HRESULT {result:#x}")
    except OSError:
        logging.getLogger("artlapse").warning(
            "Could not set Windows taskbar identity", exc_info=True,
            extra={"event": "taskbar_identity_failed"},
        )


def windows_application_id() -> str | None:
    """Read the actual process identity for the portable diagnostic report."""
    if sys.platform != "win32":
        return None
    shell = ctypes.WinDLL("shell32")
    get_id = shell.GetCurrentProcessExplicitAppUserModelID
    get_id.argtypes = [ctypes.POINTER(ctypes.c_wchar_p)]
    get_id.restype = ctypes.c_long
    value = ctypes.c_wchar_p()
    if get_id(ctypes.byref(value)) < 0:
        return None
    try:
        return value.value
    finally:
        free = ctypes.WinDLL("ole32").CoTaskMemFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = None
        free(ctypes.cast(value, ctypes.c_void_p))
