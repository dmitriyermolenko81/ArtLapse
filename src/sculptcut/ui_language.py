"""Qt language installation and in-place widget retranslation without rebuilding state."""

from PySide6.QtCore import QCoreApplication, QLibraryInfo, QLocale, QSignalBlocker, QTranslator
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QLabel,
    QLineEdit,
    QWidget,
)

from sculptcut.i18n import language, set_language, tr

_translators: list[QTranslator] = []


def install_language(value: str) -> None:
    set_language(value)
    QLocale.setDefault(QLocale("ru_RU" if value == "ru" else "en_US"))
    app = QCoreApplication.instance()
    if app is None:
        return
    for translator in _translators:
        app.removeTranslator(translator)
    _translators.clear()
    if value == "ru":
        for name in ("qtbase_ru", "qtmultimedia_ru"):
            translator = QTranslator(app)
            if translator.load(name, QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
                app.installTranslator(translator)
                _translators.append(translator)


def retranslate_windows() -> None:
    """Only translate presentation properties; never alter input values or model data."""
    for root in QApplication.topLevelWidgets():
        root.setLocale(QLocale("ru_RU" if language() == "ru" else "en_US"))
        for obj in [root, *root.findChildren(QWidget), *root.findChildren(QAction)]:
            properties = ["toolTip", "statusTip", "accessibleName", "accessibleDescription"]
            if isinstance(obj, QWidget):
                properties.append("windowTitle")
            if isinstance(obj, (QLabel, QAbstractButton, QAction)):
                properties.append("text")
            if isinstance(obj, (QLineEdit, QComboBox)):
                properties.append("placeholderText")
            if isinstance(obj, QAbstractSpinBox):
                properties.extend(("suffix", "prefix"))
            blocker = QSignalBlocker(obj)
            for prop in properties:
                getter = getattr(obj, prop, None)
                setter = getattr(obj, "set" + prop[0].upper() + prop[1:], None)
                if not callable(getter) or not callable(setter):
                    continue
                text = getter()
                if not isinstance(text, str) or not text:
                    continue
                # Retain the exact source for ambiguous reverse translations.
                cache = obj.property("languageProperties") or {}
                original, previous = cache.get(prop, (text, text))
                source = original if text == previous else text
                translated = tr(source)
                cache[prop] = (source, translated)
                obj.setProperty("languageProperties", cache)
                if text != translated:
                    setter(translated)
            if isinstance(obj, QComboBox):
                for index in range(obj.count()):
                    obj.setItemText(index, tr(obj.itemText(index)))
            del blocker
            if isinstance(obj, QWidget):
                QWidget.update(obj)
