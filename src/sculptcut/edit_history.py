"""Bounded editing history; a failed replay never advances the cursor."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Edit:
    label: str
    undo: Callable[[], None]
    redo: Callable[[], None]


class EditHistory:
    def __init__(self, limit: int = 100) -> None:
        if limit < 1:
            raise ValueError("History limit must be positive")
        self.limit = limit
        self.entries: list[Edit] = []
        self.position = 0
        self._replaying = False
        self.changed: Callable[[], None] = lambda: None

    @property
    def undo_label(self) -> str:
        return self.entries[self.position - 1].label if self.position else ""

    @property
    def redo_label(self) -> str:
        return self.entries[self.position].label if self.position < len(self.entries) else ""

    def record(self, label: str, before: T, after: T, apply: Callable[[T], None]) -> None:
        if self._replaying or before == after:
            return
        del self.entries[self.position:]
        self.entries.append(Edit(label, lambda: apply(before), lambda: apply(after)))
        if len(self.entries) > self.limit:
            del self.entries[:len(self.entries) - self.limit]
        self.position = len(self.entries)
        self.changed()

    def undo(self) -> None:
        if self.position and not self._replaying:
            self._replaying = True
            try:
                self.entries[self.position - 1].undo()
            finally:
                self._replaying = False
            self.position -= 1
            self.changed()

    def redo(self) -> None:
        if self.position < len(self.entries) and not self._replaying:
            self._replaying = True
            try:
                self.entries[self.position].redo()
            finally:
                self._replaying = False
            self.position += 1
            self.changed()

    def clear(self) -> None:
        self.entries.clear()
        self.position = 0
        self.changed()
