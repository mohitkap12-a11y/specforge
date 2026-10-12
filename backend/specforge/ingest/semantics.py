"""Format-independent semantic pass: normative statements and references."""
from __future__ import annotations

import hashlib

from .ir import ParsedSpec, Reference, Requirement
from .normalize import extract_references, norm_title, normative_info, split_sentences


def req_hash(sentence: str) -> str:
    return hashlib.sha1(norm_title(sentence).encode()).hexdigest()[:8]


def build_semantics(spec: ParsedSpec) -> None:
    reqs: list[Requirement] = []
    refs: list[Reference] = []
    prefix_sec = lambda n: f"sec:{n}"  # noqa: E731

    for s in spec.sections:
        src = prefix_sec(s.number)
        for para in s.paragraphs:
            for sent in split_sentences(para):
                info = normative_info(sent)
                if info:
                    reqs.append(Requirement(text=sent, strength=info[0], normative_word=info[1], section=s.number))
            for r in extract_references(para, spec.family):
                refs.append(Reference(raw=r.raw, kind=r.kind, target_family=r.family, target_label=r.label,
                                      src_key=src, page=s.page_from))
    for t in spec.tables:
        src = f"fig:{t.label}"
        texts = [c for row in t.rows for c in row if c]
        for cell in texts:
            for sent in split_sentences(cell):
                info = normative_info(sent)
                if info:
                    reqs.append(Requirement(text=sent, strength=info[0], normative_word=info[1], section=t.section,
                                            table=t.label, page=t.page_from))
            for r in extract_references(cell, spec.family):
                refs.append(Reference(raw=r.raw, kind=r.kind, target_family=r.family, target_label=r.label,
                                      src_key=src, page=t.page_from))
        for r in extract_references(t.caption, spec.family):
            refs.append(Reference(raw=r.raw, kind=r.kind, target_family=r.family, target_label=r.label,
                                  src_key=src, page=t.page_from))
    spec.requirements = reqs
    # de-duplicate references per (src, kind, family, label)
    seen: set = set()
    uniq: list[Reference] = []
    for r in refs:
        k = (r.src_key, r.kind, r.target_family, r.target_label)
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    spec.references = uniq
