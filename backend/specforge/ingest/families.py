"""Which specification a document is: a protocol-agnostic registry of spec families, and identity detection.

A family id is "<protocol>-<spec>" ("nvme-base", "pcie-base", "ocp-datacenter-nvme-ssd") or just "<protocol>" when the
protocol has one main specification ("cxl", "mfnd").  Detection never falls back to NVMe: a document is tagged with the
family its title page names, else with its protocol plus a slug of its title, else with a slug of its title alone, and
only when there is no title at all is it left unknown (the caller's default).

Adding a protocol or a specification is one entry in PROTOCOLS / FAMILIES below; nothing else in SpecForge lists them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePath

_TM = r"[\s®™©*]*"           # ® ™ © and footnote stars a title puts between words
_SP = r"[\s®™©*]+"


def _rx(*words: str) -> str:
    """'NVM Express', 'Base' -> a pattern tolerant of trademark signs and line breaks between the words."""
    return _SP.join(words)


NVME = r"(?:NVM" + _TM + r"\s*Express|NVMe)" + _TM


@dataclass(frozen=True)
class Protocol:
    id: str
    name: str
    patterns: tuple[str, ...]          # the protocol / its standards body named in a title
    words: tuple[str, ...] = ()        # words of the protocol's own name, left out of a generated family slug
    default: str | None = None         # the family a title naming only the protocol means ("NVM Express Revision 1.4")


@dataclass(frozen=True)
class Family:
    id: str
    name: str
    protocol: str
    titles: tuple[str, ...]            # patterns that name this specification in its own title
    refs: tuple[str, ...] = ()         # how other documents name it in running text (lower case, words split by spaces)
    short: tuple[str, ...] = ()        # short names a user or an agent may ask for it by
    _compiled: tuple[re.Pattern, ...] = field(default=(), repr=False, compare=False)


PROTOCOLS: list[Protocol] = [
    # NVMe-based specifications from other bodies first: their titles also say "NVMe"
    Protocol("mfnd", "Multi-Function NVMe Device", (r"\bMFND\b", _rx(r"Multi[- ]?Function", "NVMe")), ("multi", "function", "nvme", "device", "mfnd")),
    Protocol("ocp", "Open Compute Project", (_rx("Open", "Compute", "Project"), r"\bOCP\b"), ("open", "compute", "project", "ocp")),
    Protocol("cxl", "Compute Express Link", (_rx("Compute", "Express", "Link"), r"\bCXL\b"), ("compute", "express", "link", "cxl")),
    Protocol("nvme", "NVM Express", (NVME,), ("nvm", "express", "nvme"), "nvme-base"),
    Protocol("pcie", "PCI Express", (_rx("PCI", "Express"), r"\bPCIe\b", r"\bPCI-SIG\b", _rx("PCI", "Firmware")), ("pci", "express", "pcie", "sig"), "pcie-base"),
    Protocol("dmtf", "DMTF", (r"\bDMTF\b", _rx("Distributed", "Management", "Task", "Force"), r"\bDSP\d{4}\b"), ("dmtf",)),
    Protocol("snia", "SNIA", (r"\bSNIA\b", _rx("Storage", "Networking", "Industry", "Association")), ("snia",)),
    Protocol("tcg", "Trusted Computing Group", (_rx("Trusted", "Computing", "Group"), r"\bTCG\b"), ("trusted", "computing", "group", "tcg")),
    Protocol("jedec", "JEDEC", (r"\bJEDEC\b", r"\bJES?D\d+"), ("jedec",)),
    Protocol("sff", "SNIA SFF", (r"\bSFF-\d{4}\b", r"\bSFF-TA-\d{4}\b"), ("sff",)),
    Protocol("uefi", "UEFI", (r"\bUEFI\b", _rx("Unified", "Extensible", "Firmware", "Interface")), ("uefi", "unified", "extensible", "firmware", "interface")),
    Protocol("acpi", "ACPI", (r"\bACPI\b", _rx("Advanced", "Configuration", "and", "Power", "Interface")), ("acpi", "advanced", "configuration", "power", "interface")),
    Protocol("scsi", "SCSI / T10", (r"\bT10\b", r"\bSCSI\b", r"\bINCITS\b"), ("scsi", "t10", "incits")),
    Protocol("sata", "Serial ATA", (_rx("Serial", "ATA"), r"\bSATA\b", r"\bATA/ATAPI\b", r"\bACS-\d\b"), ("serial", "ata", "sata")),
    Protocol("usb", "USB", (r"\bUSB\b", _rx("Universal", "Serial", "Bus")), ("usb", "universal", "serial", "bus")),
    Protocol("ufs", "Universal Flash Storage", (_rx("Universal", "Flash", "Storage"), r"\bUFS\b"), ("universal", "flash", "storage", "ufs")),
]

FAMILIES: list[Family] = [
    # ---- NVM Express
    Family("nvme-mi", "NVMe-MI", "nvme", (NVME + r"\s*Management" + _SP + "Interface", r"\bNVMe[- ]?MI\b"),
           refs=("nvm express management interface specification", "nvme management interface specification", "nvme-mi specification"),
           short=("mi", "nvme mi", "nvmemi", "nvme-mi", "management interface")),
    Family("nvme-zns-cs", "Zoned Namespace Command Set", "nvme", (_rx("Zoned", r"Namespaces?"),),
           refs=("nvm express zoned namespace command set specification", "zoned namespace command set specification"), short=("zns",)),
    Family("nvme-kv-cs", "Key Value Command Set", "nvme", (_rx(r"Key[\s-]*Value", "Command", "Set"),),
           refs=("nvm express key value command set specification", "key value command set specification"), short=("kv", "key value")),
    Family("nvme-slm-cs", "Subsystem Local Memory Command Set", "nvme", (_rx("Subsystem", "Local", "Memory"),),
           refs=("nvm express subsystem local memory command set specification", "subsystem local memory command set specification"), short=("slm",)),
    Family("nvme-cp-cs", "Computational Programs Command Set", "nvme", (_rx("Computational", "Programs"),),
           refs=("nvm express computational programs command set specification", "computational programs command set specification"), short=("cp", "computational programs")),
    Family("nvme-nvm-cs", "NVM Command Set", "nvme", (r"\bNVM" + _SP + "Command" + _SP + "Set",),
           refs=("nvm express nvm command set specification", "nvme nvm command set specification", "nvm command set specification"),
           short=("nvm", "nvm cs", "nvmcs", "nvm command set", "command set")),
    Family("nvme-pcie", "PCIe Transport", "nvme", (r"(?:PCIe|PCI" + _SP + "Express)" + _TM + r"\s*Transport",),
           refs=("nvm express pcie transport specification", "nvme over pcie transport specification", "pcie transport specification"), short=("pcie transport",)),
    Family("nvme-tcp", "TCP Transport", "nvme", (r"\bTCP" + _SP + "Transport",),
           refs=("nvm express tcp transport specification", "tcp transport specification"), short=("tcp",)),
    Family("nvme-rdma", "RDMA Transport", "nvme", (r"\bRDMA" + _SP + "Transport",),
           refs=("nvm express rdma transport specification", "rdma transport specification"), short=("rdma",)),
    Family("nvme-boot", "NVMe Boot", "nvme", (NVME + r"\s*Boot" + _SP + "Specification",),
           refs=("nvm express boot specification", "nvme boot specification"), short=("boot", "nvme boot")),
    Family("nvme-base", "NVMe Base", "nvme", (NVME + r"\s*Base" + _SP + "Specification",),
           refs=("nvm express base specification", "nvme base specification"), short=("base", "nvme", "nvme base")),
    # ---- OCP / MFND (NVMe devices specified outside NVM Express)
    Family("mfnd", "Multi-Function NVMe Device (MFND)", "mfnd", (r"\bMFND\b", _rx(r"Multi[- ]?Function", "NVMe", "Device")),
           refs=("multi-function nvme device specification", "mfnd specification"), short=("mfnd", "multi function nvme device", "multi-function nvme device")),
    Family("ocp-datacenter-nvme-ssd", "OCP Datacenter NVMe SSD", "ocp", (_rx("Datacenter", r"NVMe", "SSD"),),
           refs=("datacenter nvme ssd specification", "ocp datacenter nvme ssd specification"), short=("ocp", "datacenter ssd", "dc ssd", "ocp ssd")),
    Family("ocp-nvme-cloud-ssd", "OCP NVMe Cloud SSD", "ocp", (_rx("NVMe", "Cloud", "SSD"),),
           refs=("nvme cloud ssd specification",), short=("cloud ssd",)),
    Family("ocp-hyperscale-nvme-boot-ssd", "OCP Hyperscale NVMe Boot SSD", "ocp", (_rx("Hyperscale", "NVMe", "Boot", "SSD"),),
           refs=("hyperscale nvme boot ssd specification",), short=("boot ssd", "hyperscale boot ssd")),
    # ---- CXL
    Family("cxl", "Compute Express Link (CXL)", "cxl", (_rx("Compute", "Express", "Link"), r"\bCXL\b" + _TM + r"\s*Specification"),
           refs=("compute express link specification", "cxl specification"), short=("cxl", "compute express link")),
    # ---- PCI Express / PCI-SIG
    Family("pcie-cem", "PCI Express Card Electromechanical", "pcie", (_rx("Card", "Electromechanical"), r"\bCEM" + _SP + "Specification"),
           refs=("pci express card electromechanical specification", "cem specification"), short=("cem",)),
    Family("pcie-firmware", "PCI Firmware", "pcie", (_rx("PCI", "Firmware", "Specification"),), refs=("pci firmware specification",), short=("pci firmware",)),
    Family("pcie-base", "PCI Express Base", "pcie", (r"(?:PCI" + _SP + "Express|PCIe)" + _TM + r"\s*Base" + _SP + "Specification",
                                                     r"(?:PCI" + _SP + "Express|PCIe)" + _TM + r"\s*Specification"),
           refs=("pci express base specification", "pcie base specification", "pci express specification", "pcie specification"),
           short=("pcie base", "pci express", "pci express base", "pcie spec")),
    # ---- DMTF
    Family("dmtf-mctp", "MCTP", "dmtf", (_rx("Management", "Component", "Transport", "Protocol"), r"\bMCTP\b"),
           refs=("mctp base specification", "management component transport protocol base specification"), short=("mctp",)),
    Family("dmtf-pldm", "PLDM", "dmtf", (_rx("Platform", "Level", "Data", "Model"), r"\bPLDM\b"), refs=("pldm base specification",), short=("pldm",)),
    Family("dmtf-spdm", "SPDM", "dmtf", (_rx("Security", "Protocol", "and", "Data", "Model"), r"\bSPDM\b"),
           refs=("security protocol and data model specification", "spdm specification"), short=("spdm",)),
    Family("dmtf-redfish", "Redfish", "dmtf", (r"\bRedfish\b",), short=("redfish",)),
    # ---- others with a well known main specification
    Family("tcg-opal", "TCG Opal SSC", "tcg", (r"\bOpal\b",), refs=("opal ssc specification", "tcg opal specification"), short=("opal",)),
    Family("tcg-storage-core", "TCG Storage Architecture Core", "tcg", (_rx("Storage", "Architecture", "Core"),), refs=("tcg storage architecture core specification",)),
    Family("snia-swordfish", "SNIA Swordfish", "snia", (r"\bSwordfish\b",), short=("swordfish",)),
]

PROTOCOL_BY_ID = {p.id: p for p in PROTOCOLS}
FAMILY_BY_ID = {f.id: f for f in FAMILIES}
_FAMILY_RX = [(f, [re.compile(p, re.I) for p in f.titles]) for f in FAMILIES]
_PROTOCOL_RX = [(p, [re.compile(x, re.I) for x in p.patterns]) for p in PROTOCOLS]

# Protocols whose documents mean NVM Express when they say just "the Base Specification" or "the Management Interface
# Specification"; a CXL or PCIe document saying "Base Specification" means its own, never NVMe's.
NVME_CONTEXT = {"nvme", "ocp", "mfnd"}


def protocol_of(family: str | None) -> str | None:
    """'nvme-base' -> 'nvme', 'cxl' -> 'cxl', 'ocp-foo-bar' -> 'ocp'; None when the family names no known protocol."""
    if not family:
        return None
    if f := FAMILY_BY_ID.get(family):
        return f.protocol
    head = family.split("-", 1)[0]
    return head if head in PROTOCOL_BY_ID else None


def family_name(family: str) -> str:
    """Display name: the registry's, else the protocol's name plus the rest of the id ('ocp-storage-mgmt' -> 'Open Compute
    Project Storage Mgmt'), else the id."""
    if f := FAMILY_BY_ID.get(family):
        return f.name
    p = protocol_of(family)
    if p:
        rest = family[len(p):].strip("-").replace("-", " ").title()
        return f"{PROTOCOL_BY_ID[p].name} {rest}".strip()
    return family


def family_names() -> dict[str, str]:
    return {f.id: f.name for f in FAMILIES}


def short_aliases() -> dict[str, list[str]]:
    """short name -> the families it may mean, most likely first (one short name can fit two specs: 'pcie')."""
    out: dict[str, list[str]] = {}
    for f in FAMILIES:
        for s in f.short:
            out.setdefault(s, []).append(f.id)
    out.setdefault("pcie", []).extend(["pcie-base", "nvme-pcie"])
    return out


def reference_aliases() -> dict[str, str]:
    """lower-case name of a specification in running text -> family."""
    out: dict[str, str] = {}
    for f in FAMILIES:
        for r in f.refs:
            out.setdefault(r, f.id)
    return out


# Unqualified names that mean an NVM Express spec only inside an NVMe-context document (see NVME_CONTEXT).
CONTEXT_REFERENCE_ALIASES: dict[str, str] = {
    "base specification": "nvme-base",
    "management interface specification": "nvme-mi",
}


# ----------------------------------------------------------------------------- detection

_STOP = {"the", "of", "and", "for", "a", "an", "to", "in", "on", "specification", "specifications", "spec", "standard",
         "revision", "rev", "version", "ver", "draft", "ratified", "final", "release", "document", "technical", "proposal",
         "working", "group", "approved", "publication", "copyright", "all", "rights", "reserved", "inc", "pdf", "docx"}
_TITLE_WORD = re.compile(r"\b(specifications?|standard|protocol|architecture|binding|requirements|interface|guide)\b", re.I)
_VERSION_RE = re.compile(r"\b(?:Revision|Rev\.?|Version|Ver\.?|Release)\s*:?\s*(\d+(?:\.\d+)*[a-z]?)\b", re.I)
_VERSION_V = re.compile(r"(?<![A-Za-z0-9])[vVrR](\d+\.\d+(?:\.\d+)*[a-z]?)\b")     # "v2.5", "r6.0"
_VERSION_BARE = re.compile(r"(?<![\d.])(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?[a-z]?)(?![\d.])")   # "cxl-3.1.pdf"; never a date
_NOISE_LINE = re.compile(r"^(?:\W*|\d+|page\s+\d+.*|copyright.*|©.*|all rights reserved.*|.*\b(?:19|20)\d\d[./-]\d\d?[./-]\d\d?\b.*)$", re.I)


def _earliest(text: str, compiled) -> tuple[int, int, object] | None:
    """(position, -length, entry) of the entry whose pattern matches earliest in text (a longer match breaks a tie)."""
    best = None
    for entry, rxs in compiled:
        for rx in rxs:
            m = rx.search(text)
            if m:
                k = (m.start(), -(m.end() - m.start()), entry)
                if best is None or k[:2] < best[:2]:
                    best = k
    return best


def match_family(text: str) -> str | None:
    """The registered family a title names (earliest mention wins: a title page names its own spec first)."""
    b = _earliest(text, _FAMILY_RX)
    return b[2].id if b else None


def match_protocol(text: str) -> str | None:
    b = _earliest(text, _PROTOCOL_RX)
    return b[2].id if b else None


def _slug_words(title: str, drop: set[str]) -> list[str]:
    title = _VERSION_RE.sub(" ", title)
    title = _VERSION_V.sub(" ", title)
    words = re.findall(r"[a-z0-9]+", title.lower())
    return [w for w in words if w not in _STOP and w not in drop and not re.fullmatch(r"\d+", w)][:6]


def title_of(page_text: str) -> tuple[str, bool]:
    """(title, found) of a document from its first page: the first line naming it a specification / standard /
    protocol ..., with the short lines just above it (titles wrap; a "Version 4.0" line between is skipped).  found is
    False when no line says so and the first line is returned instead."""
    lines = [ln.strip() for ln in page_text.splitlines()]
    lines = [ln for ln in lines if ln and not _NOISE_LINE.match(ln)][:20]
    for i, ln in enumerate(lines):
        m = _TITLE_WORD.search(ln)
        if m:
            above: list[str] = []
            j = i - 1
            while j >= 0 and len(above) < 2 and len(lines[j]) <= 60:
                if not _VERSION_RE.fullmatch(lines[j].strip(" ,.")) and not _VERSION_RE.match(lines[j]):
                    above.insert(0, lines[j])
                j -= 1
            return re.sub(r"\s+", " ", " ".join(above + [ln[:m.end()]])).strip(), True
    return (lines[0] if lines else ""), False


def generic_family(title: str, protocol: str | None) -> str | None:
    """A family id for a specification not in the registry: '<protocol>-<title words>', '<protocol>', or '<title words>'."""
    drop = (set(PROTOCOL_BY_ID[protocol].words) if protocol else set()) | set(PROTOCOL_BY_ID)  # no "jedec" / "dmtf" in a slug
    words = _slug_words(title, drop)
    if protocol:
        if set(words) <= {"base"} and PROTOCOL_BY_ID[protocol].default:
            return PROTOCOL_BY_ID[protocol].default
        return "-".join([protocol, *words])
    return "-".join(words) or None


def _version(*texts: str, filename: str = "") -> str | None:
    for t in texts:
        if t and (m := _VERSION_RE.search(t) or _VERSION_V.search(t)):
            return m.group(1)
    if filename and (m := _VERSION_BARE.search(filename)):
        return m.group(1)
    return None


def _frequent_family(text: str) -> str | None:
    """The registered family named most often (headers and footers repeat a document's own title on every page)."""
    counts: dict[str, int] = {}
    first: dict[str, int] = {}
    for f, rxs in _FAMILY_RX:
        for rx in rxs:
            for m in rx.finditer(text):
                counts[f.id] = counts.get(f.id, 0) + 1
                first[f.id] = min(first.get(f.id, m.start()), m.start())
    if not counts:
        return None
    return max(counts, key=lambda k: (counts[k], -first[k]))


def detect_identity(first_pages_text: str, filename: str = "", title: str = "") -> tuple[str | None, str | None]:
    """(family, version) of a specification.

    first_pages_text: text of page 1, optionally followed by a form-feed and pages 2-3; title: the file's own title
    metadata, when it has one.  The title on page 1 decides, then the title metadata: a registered spec they name, else the
    protocol they name plus a slug of the title.  Then the file name, then the rest of page 1, then (page 1 has no text)
    the spec pages 2-3 name most often, then a slug of the title alone.  Pages 2-3 are a last resort: they cite other
    specifications ("as defined in the NVM Express Base Specification"), which is not what the document is."""
    parts = first_pages_text.split("\f")
    page1_raw = parts[0][:3000]
    page1 = re.sub(r"\s+", " ", page1_raw).strip()
    rest = re.sub(r"\s+", " ", " ".join(parts[1:])[:6000]).strip()
    stem = PurePath(filename).stem if filename else ""
    fname = re.sub(r"[-_]+", " ", stem)
    meta = re.sub(r"\s+", " ", title or "").strip()
    if re.match(r"^(microsoft word|untitled|document\d*)\b", meta, re.I) or meta.lower() == stem.lower():
        meta = ""
    head, found = title_of(page1_raw) if page1 else ("", False)

    def generic(text: str) -> str | None:
        proto = match_protocol(text) if text else None
        return generic_family(head or meta or fname, proto) if proto else None

    fam = (match_family(head) or match_family(meta)             # the title names a registered spec
           or generic(head) or generic(meta)                     # ... or at least its protocol
           or match_family(fname) or (generic_family(fname, p) if (p := match_protocol(fname)) else None)
           # no title line: anything on page 1 (when there is one, the rest of page 1 only cites other specs:
           # "builds on the NVM Express Base Specification")
           or (None if found else match_family(page1) or generic(page1)))
    if fam is None and not page1:                                # no text on page 1 (a cover image)
        fam = _frequent_family(rest)
    if fam is None and (head or meta):
        fam = generic_family(head or meta, None)
    ver = _version(page1, meta, fname, filename=fname) or _version(rest)
    return fam, ver
