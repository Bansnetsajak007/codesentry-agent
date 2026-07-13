"""Unified-diff parsing built on unidiff, returning per-file changes with the
added/removed lines and their line numbers, across diffs that may touch multiple
languages in a single change set."""

from __future__ import annotations

from dataclasses import dataclass, field

from unidiff import PatchSet


@dataclass(frozen=True)
class DiffLine:
    """A single added or removed line with its 1-based line number (target-side for
    additions, source-side for removals)."""

    line_number: int
    content: str


@dataclass
class FileDiff:
    """The change to one file: its path plus the added and removed lines and the raw
    unified-diff text for that file."""

    path: str
    added: list[DiffLine] = field(default_factory=list)
    removed: list[DiffLine] = field(default_factory=list)
    raw: str = ""

    @property
    def changed_line_numbers(self) -> set[int]:
        """Line numbers touched on either side of the change."""
        return {line.line_number for line in self.added} | {
            line.line_number for line in self.removed
        }


def parse_diff(diff_text: str) -> list[FileDiff]:
    """Parse a unified diff into per-file change records. Malformed input raises
    unidiff.UnidiffParseError."""

    patch = PatchSet(diff_text)
    file_diffs: list[FileDiff] = []
    for patched_file in patch:
        added: list[DiffLine] = []
        removed: list[DiffLine] = []
        for hunk in patched_file:
            for line in hunk:
                if line.is_added and line.target_line_no is not None:
                    added.append(DiffLine(line.target_line_no, line.value.rstrip("\n")))
                elif line.is_removed and line.source_line_no is not None:
                    removed.append(DiffLine(line.source_line_no, line.value.rstrip("\n")))
        file_diffs.append(
            FileDiff(
                path=patched_file.path,
                added=added,
                removed=removed,
                raw=str(patched_file),
            )
        )
    return file_diffs
