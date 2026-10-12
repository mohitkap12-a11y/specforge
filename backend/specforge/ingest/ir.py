"""Intermediate representation (IR) shared by the PDF and DOCX pipelines.

A parsed spec is a flat list of nodes with stable keys:

    {family}@{version}/sec:5.1.13        section
    {family}@{version}/fig:312           figure / table
    {family}@{version}/sec:5.1.13/req:ab12cd34   normative statement

Stable keys survive a reparse; database ids do not.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Section:
    number: str
    title: str
    level: int
    page_from: int
    parent: str | None = None  # parent section number
    paragraphs: list[str] = field(default_factory=list)
    page_to: int | None = None

    def key(self, prefix: str) -> str:
        return f"{prefix}/sec:{self.number}"


@dataclass
class Table:
    label: str  # "312" (figure number as printed) or synthetic "p12t1" when uncaptioned
    caption: str
    header: list[str]
    rows: list[list[str]]
    page_from: int
    page_to: int
    section: str | None = None  # section number that contains the table
    captioned: bool = True
    parent_table: str | None = None  # label of the enclosing table (nested tables, DOCX)
    parent_cell: tuple[int, int] | None = None
    extractor: str = ""
    quality: float = 1.0
    continued_pages: int = 0
    bbox: list[float] | None = None

    def key(self, prefix: str) -> str:
        return f"{prefix}/fig:{self.label}"


@dataclass
class Requirement:
    text: str
    strength: str  # mandatory | prohibited | recommended | optional
    section: str | None
    table: str | None = None
    page: int | None = None
    normative_word: str = ""


@dataclass
class Reference:
    raw: str
    kind: str  # section | figure | spec
    target_family: str | None  # None => same document
    target_label: str  # "5.1.13" / "312" / "" for spec-only
    src_key: str  # key of the node whose text contains the reference
    page: int | None = None


@dataclass
class Excluded:
    page: int
    category: str  # header | footer | toc | pagenum
    text: str


@dataclass
class ParsedSpec:
    family: str
    version: str
    title: str
    fmt: str
    pages: int
    sections: list[Section]
    tables: list[Table]
    requirements: list[Requirement]
    references: list[Reference]
    excluded: list[Excluded]
    stats: dict[str, Any] = field(default_factory=dict)
    # data for audit (not stored): per-page raw non-whitespace char counts
    raw_char_count: int = 0
    accounted_char_count: int = 0
    notes: list[dict] = field(default_factory=list)  # parser-raised issues: {severity, kind, node_key, page, message}

    @property
    def prefix(self) -> str:
        return f"{self.family}@{self.version}"
