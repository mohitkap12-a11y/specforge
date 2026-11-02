"""Spec identity detection is protocol-agnostic: CXL, PCIe, OCP, MFND, DMTF ... are tagged as themselves, never as NVMe."""
import pytest

from specforge.catalog import _opcode_queue
from specforge.ingest.families import detect_identity, family_name, protocol_of
from specforge.ingest.normalize import extract_references


@pytest.mark.parametrize("text,filename,expected", [
    ("NVM Express® Base Specification\nRevision 2.4\n\fSee also the NVM Command Set Specification", "", ("nvme-base", "2.4")),
    ("NVM Express® Management Interface Specification\nRevision 2.0", "", ("nvme-mi", "2.0")),
    ("NVM Express® NVMe® over PCIe® Transport Specification\nRevision 1.1", "", ("nvme-pcie", "1.1")),
    ("NVM Express\nRevision 1.4\nJune 10, 2019", "", ("nvme-base", "1.4")),
    # "Base Specification" used to be read as NVMe's
    ("PCI Express® Base Specification\nRevision 6.0\nVersion 1.0\n", "", ("pcie-base", "6.0")),
    ("Compute Express Link™\nSpecification\nRevision 3.1, Version 1.0\nAugust 2023", "", ("cxl", "3.1")),
    # OCP / MFND titles say "NVMe" and their pages cite NVMe-MI and the Base Specification
    ("Open Compute Project\nDatacenter NVMe® SSD Specification\nVersion 2.5\nRequires NVMe-MI and the NVM Express Base Specification", "",
     ("ocp-datacenter-nvme-ssd", "2.5")),
    ("OCP\nMulti-Function NVMe Device (MFND)\nSpecification\nVersion 1.0\nNVM Express Management Interface", "", ("mfnd", "1.0")),
    ("Management Component Transport Protocol (MCTP)\nBase Specification\nVersion 1.3.1\nDSP0236", "", ("dmtf-mctp", "1.3.1")),
    # protocols / specs not in the registry: the protocol plus the title, or the title alone, never NVMe or unknown
    ("Open Compute Project\nStorage Hardware Management\nSpecification\nRevision 1.1\nrefers to NVM Express Management Interface", "",
     ("ocp-storage-hardware-management", "1.1")),
    ("Universal Flash Storage (UFS)\nVersion 4.0\nJEDEC Standard JESD220F", "", ("ufs", "4.0")),
    ("Acme Widget Protocol Specification\nRevision 0.9\nThis builds on the NVM Express Base Specification.", "", ("acme-widget-protocol", "0.9")),
    # file name only
    ("", "NVM-Express-NVM-Command-Set-Specification-Revision-1.3-Ratified.pdf", ("nvme-nvm-cs", "1.3")),
    ("", "CXL-3.1-Specification.pdf", ("cxl", "3.1")),
    ("", "PCIe_Base_r6.0.pdf", ("pcie-base", "6.0")),
    ("", "notes.pdf", (None, None)),
    # page 1 is a cover image: the spec named in every page header
    ("\fPCI Express Base Specification Revision 5.0\fPCI Express Base Specification Revision 5.0 (NVMe)", "", ("pcie-base", "5.0")),
])
def test_detect_identity(text, filename, expected):
    assert detect_identity(text, filename) == expected


def test_title_metadata_is_used():
    assert detect_identity("", "spec.pdf", "Compute Express Link Specification") == ("cxl", None)
    assert detect_identity("", "spec.pdf", "Microsoft Word - spec.docx") == (None, None)


def test_protocol_and_names():
    assert protocol_of("nvme-base") == "nvme" and protocol_of("cxl") == "cxl" and protocol_of("ocp-foo") == "ocp"
    assert protocol_of("acme-widget") is None and protocol_of(None) is None
    assert family_name("pcie-base") == "PCI Express Base"
    assert family_name("ocp-storage-hardware-management") == "Open Compute Project Storage Hardware Management"


def test_unqualified_base_specification_is_nvme_only_in_nvme_context():
    text = "as defined in section 7.5 of the Base Specification"
    assert [r.family for r in extract_references(text, "nvme-nvm-cs")] == ["nvme-base"]
    assert [r.family for r in extract_references(text, "mfnd")] == ["nvme-base"]
    assert [r.family for r in extract_references(text, "cxl")] == [None]
    pcie = extract_references("as defined in section 7.5 of the PCI Express® Base Specification", "cxl")
    assert [(r.family, r.label) for r in pcie] == [("pcie-base", "7.5")]
    assert [r.family for r in extract_references("see section 3 of the NVM Express® Base Specification", "cxl")] == ["nvme-base"]


def test_non_nvme_opcode_tables_get_their_own_command_space():
    t = {"caption": "Figure 8-93. CXL Memory Device Command Opcodes"}
    assert _opcode_queue({"family": "cxl"}, t) == ("cxl", "cmd", "cxl")
    assert _opcode_queue({"family": "cxl"}, {"caption": "Out-of-Band Management Opcodes"}) is None     # still not commands
    assert _opcode_queue({"family": "cxl"}, {"caption": "Control Primitive Opcodes"}) is None
    assert _opcode_queue({"family": "nvme-base"}, {"caption": "Opcodes for Admin Commands"}) == ("pcie", "admin", "base")
    assert _opcode_queue({"family": "ocp-datacenter-nvme-ssd"}, {"caption": "Vendor Specific Admin Command Opcodes"}) == ("pcie", "admin", "base")
