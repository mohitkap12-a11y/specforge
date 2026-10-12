"""Cross-command workflow scenarios: the multi-step flows a test-script developer writes by hand.

A single-command scenario ("a Set Features was submitted") says little about a device.  Functional tests chain
commands: configure a feature then read it back, create the queues before using them, attach a namespace before
writing to it.  This module holds those flows as *recipes* over command names and field abbreviations.  Nothing
numeric is hard-coded: opcodes, dwords, bit ranges and enumerated values are resolved from the catalog, so a recipe
whose commands or fields are not in the parsed specs is skipped, never guessed.  Every generated predicate is run
through `validate_predicate` before it becomes a catalog item.

Each workflow is a catalog item of kind `workflow` (a coverage denominator entry) carrying the finished predicate; the
template generator turns it into a scenario.  Observability is `stimulus`: the log holds submissions only, so the
sequence can be shown to have been driven but the device's responses cannot be checked.
"""
from __future__ import annotations

import re
from typing import Any

from .ingest.normalize import norm_title
from .scenarios import validate_predicate

# ----------------------------------------------------------------------------- recipe vocabulary
# Value specs for a field condition:  int -> eq,  Like(regex) -> the catalog value whose description matches,
# Var(name) -> the foreach value (bound) or a value captured by an earlier step.


class Like:
    def __init__(self, rx: str):
        self.rx = re.compile(rx, re.I)


class Var:
    def __init__(self, name: str):
        self.name = name


def S(cmd: str, note: str, where: dict | None = None, cap: dict | None = None, via: str | None = None, rsvd: bool = False) -> dict:
    """One step: command name, what the step is for, field conditions {abbr: spec}, captures {var: abbr}."""
    return {"cmd": cmd, "note": note, "where": where or {}, "cap": cap or {}, "via": via, "rsvd": rsvd}


CTRL = Like(r"controller data structure")
NSID = Like(r"^identify namespace data structure")
ACTIVE = Like(r"active namespace id list")
RANGE = ("SLBA", "NLB")        # fields that make a Read address the range a Write/Write Zeroes touched

RECIPES: list[dict[str, Any]] = [
    # ---- feature configuration round trip: the bread and butter of Admin tests
    {"id": "feature-roundtrip", "foreach": ("Set Features", "FID"),
     "title": "Set Features {v} then Get Features returns the current value",
     "steps": [S("Set Features", "Configure the feature", {"FID": Var("v")}, {"fid": "FID"}),
               S("Get Features", "Read back the same feature (current value)", {"FID": Var("fid"), "SEL": Like(r"^current")})]},
    {"id": "feature-saved", "foreach": ("Set Features", "FID"),
     "title": "Set Features {v} with Save, then Get Features returns the saved value",
     "steps": [S("Set Features", "Configure the feature and ask the controller to save it", {"FID": Var("v"), "SV": 1}, {"fid": "FID"}),
               S("Get Features", "Read the saved value of the same feature", {"FID": Var("fid"), "SEL": Like(r"^saved")})]},
    {"id": "feature-capabilities", "foreach": ("Set Features", "FID"),
     "title": "Query {v} capabilities with Get Features before changing it with Set Features",
     "steps": [S("Get Features", "Discover what the feature supports", {"FID": Var("v"), "SEL": Like(r"supported capabilities")}, {"fid": "FID"}),
               S("Set Features", "Change the feature only after discovering its capabilities", {"FID": Var("fid")})]},
    # ---- identify / enumeration
    {"id": "enumerate", "title": "Enumerate the controller: Identify Controller, the active namespace list, then a namespace",
     "steps": [S("Identify", "Identify the controller", {"CNS": CTRL}), S("Identify", "List active namespaces", {"CNS": ACTIVE}),
               S("Identify", "Identify a namespace", {"CNS": NSID})]},
    {"id": "init-then-io", "title": "Identify the controller and a namespace, then Write and Read",
     "steps": [S("Identify", "Identify the controller", {"CNS": CTRL}), S("Identify", "Identify the namespace", {"CNS": NSID}),
               S("Write", "Write data to the namespace"), S("Read", "Read the data back")]},
    # ---- I/O queues
    {"id": "queue-count", "title": "Set the Number of Queues, create the completion then the submission queue, then do I/O",
     "steps": [S("Set Features", "Request the number of I/O queues", {"FID": Like(r"number of queues")}),
               S("Create I/O Completion Queue", "Create the completion queue first", cap={"qid": "QID"}),
               S("Create I/O Submission Queue", "Create the submission queue bound to that completion queue", {"CQID": Var("qid")}),
               S("Write", "Write through the new queue pair"), S("Read", "Read it back")]},
    {"id": "queue-create", "title": "Create I/O Completion Queue, then I/O Submission Queue, then issue I/O",
     "steps": [S("Create I/O Completion Queue", "Create the completion queue first", cap={"qid": "QID"}),
               S("Create I/O Submission Queue", "Create the submission queue bound to that completion queue", {"CQID": Var("qid")}),
               S("Write", "Write through the new queue pair"), S("Read", "Read it back")]},
    {"id": "queue-teardown", "title": "I/O queue lifecycle: create both queues, do I/O, delete the submission then the completion queue",
     "steps": [S("Create I/O Completion Queue", "Create the completion queue", cap={"cqid": "QID"}),
               S("Create I/O Submission Queue", "Create the submission queue", {"CQID": Var("cqid")}, {"sqid": "QID"}),
               S("Write", "Write through the new queue pair"), S("Read", "Read it back"),
               S("Delete I/O Submission Queue", "Delete the submission queue first", {"QID": Var("sqid")}),
               S("Delete I/O Completion Queue", "Then delete the completion queue", {"QID": Var("cqid")})]},
    {"id": "abort-sq", "title": "Create an I/O Submission Queue, then Abort a command on that queue",
     "steps": [S("Create I/O Submission Queue", "Create the queue", cap={"sqid": "QID"}),
               S("Abort", "Abort a command on that submission queue", {"SQID": Var("sqid")})]},
    # ---- namespace management
    {"id": "ns-lifecycle", "title": "Namespace lifecycle: create, attach, identify, write and read, detach, delete",
     "steps": [S("Namespace Management", "Create a namespace", {"SEL": Like(r"create")}),
               S("Namespace Attachment", "Attach it to the controller", {"SEL": Like(r"attach")}),
               S("Identify", "Identify the new namespace", {"CNS": NSID}),
               S("Write", "Write to it"), S("Read", "Read it back"),
               S("Namespace Attachment", "Detach it", {"SEL": Like(r"detach")}),
               S("Namespace Management", "Delete it", {"SEL": Like(r"delete")})]},
    # ---- data path
    {"id": "write-read", "title": "Write then Read back the same range", "link": ("Write", "Read"),
     "steps": [S("Write", "Write data"), S("Read", "Read the same range back")]},
    {"id": "write-flush-read", "title": "Write, Flush, then Read back the same range", "link": ("Write", "Read"),
     "steps": [S("Write", "Write data"), S("Flush", "Flush to non-volatile media"), S("Read", "Read the same range back")]},
    {"id": "write-fua-read", "title": "Write with Force Unit Access, then Read with Force Unit Access", "link": ("Write", "Read"),
     "steps": [S("Write", "Write with FUA set", {"FUA": 1}), S("Read", "Read with FUA set", {"FUA": 1})]},
    {"id": "zeroes-read", "title": "Write Zeroes then Read back the same range", "link": ("Write Zeroes", "Read"),
     "steps": [S("Write Zeroes", "Zero a range"), S("Read", "Read the range back")]},
    {"id": "dsm-read", "title": "Write, Dataset Management, then Read the range", "link": ("Write", "Read"),
     "steps": [S("Write", "Write data"), S("Dataset Management", "Deallocate / set attributes on the range"), S("Read", "Read the range back")]},
    # ---- logs
    {"id": "smart-delta", "title": "Read the SMART log, run Write and Read, read the SMART log again",
     "steps": [S("Get Log Page", "Baseline SMART / Health log", {"LID": Like(r"smart")}), S("Write", "Generate write traffic"),
               S("Read", "Generate read traffic"), S("Get Log Page", "SMART / Health log after the traffic", {"LID": Like(r"smart")})]},
    {"id": "aec-log", "title": "Enable asynchronous events, then read a log page with Retain Asynchronous Event",
     "steps": [S("Set Features", "Configure Asynchronous Event Configuration", {"FID": Like(r"asynchronous event")}),
               S("Get Log Page", "Read the log without clearing the event", {"RAE": 1})]},
    {"id": "invalid-then-errlog", "foreach_cmd": ["Identify", "Set Features", "Get Features"],
     "title": "{c}: set a reserved field, then read the Error Information log",
     "steps": [S("{c}", "Submit with a reserved field set; the controller is expected to fail it with Invalid Field", rsvd=True),
               S("Get Log Page", "Read the Error Information log page", {"LID": Like(r"error information")})]},
    # ---- NVMe-MI
    {"id": "mi-structures", "title": "NVMe-MI: read the controller list, then controller information",
     "steps": [S("Read NVMe-MI Data Structure", "Read the controller list", {"DTYP": Like(r"controller list")}),
               S("Read NVMe-MI Data Structure", "Read controller information", {"DTYP": Like(r"controller information")})]},
    {"id": "mi-health", "title": "NVMe-MI: subsystem health poll, then controller health poll",
     "steps": [S("NVM Subsystem Health Status Poll", "Poll the subsystem"), S("Controller Health Status Poll", "Poll the controller")]},
    {"id": "mi-config", "title": "NVMe-MI: Configuration Set, then Configuration Get",
     "steps": [S("Configuration Set", "Change a configuration"), S("Configuration Get", "Read it back")]},
    {"id": "mi-identify-parity", "title": "Identify Controller out-of-band over NVMe-MI and in-band over PCIe", "order": "any",
     "steps": [S("Identify", "Out-of-band Identify tunnelled in an NVMe-MI message", {"CNS": CTRL}, via="mi"),
               S("Identify", "In-band Identify on the Admin queue", {"CNS": CTRL})]},
]


# ----------------------------------------------------------------------------- catalog access

def _cmd_norm(name: str) -> str:
    return re.sub(r"\s+command$", "", norm_title(name))


def _order(a: dict) -> tuple:
    return (str(type(a["dw"])), a["dw"], -a["lo"])


def _text(v: dict) -> str:
    d = v.get("desc") or ""
    return "" if d in ("cleared", "set") else (d if len(d) <= 48 else d[:45].rstrip() + "...")


class _Skip(Exception):
    """The recipe cannot be built from this catalog."""


class _Cat:
    def __init__(self, items: dict[str, dict]):
        self.by_name: dict[str, list[dict]] = {}
        self.fields: dict[tuple[str, str], list[dict]] = {}
        self.values: dict[str, list[dict]] = {}
        self.reserved: dict[str, list[dict]] = {}
        self.cmds: dict[str, dict] = {}
        self.described: dict[str, list[dict]] = {}       # command -> every described bit range (field + reserved), for validation
        for it in items.values():
            a = it["attrs"]
            if it["kind"] == "command":
                c = {**a, "key": it["key"]}
                self.cmds[it["key"]] = c
                self.by_name.setdefault(_cmd_norm(a["name"]), []).append(c)
            elif it["kind"] == "field":
                self.fields.setdefault((a["command"], (a.get("abbr") or "").upper()), []).append({**a, "key": it["key"]})
                self.described.setdefault(a["command"], []).append(a)
            elif it["kind"] == "reserved":
                self.reserved.setdefault(a["command"], []).append(a)
                self.described.setdefault(a["command"], []).append(a)
            elif it["kind"] == "value" and not a.get("reserved_value"):
                self.values.setdefault(a["field"], []).append(a)

    def command(self, name: str, via: str | None) -> dict | None:
        cands = self.by_name.get(_cmd_norm(name), [])
        cands = [c for c in cands if c["queue"] == "admin"] if via == "mi" else ([c for c in cands if c["interface"] != "mi"] or cands)
        return min(cands, key=lambda c: c["key"]) if cands else None

    def field(self, cmd: dict, abbr: str) -> list[dict]:
        return sorted(self.fields.get((cmd["key"], abbr.upper()), []), key=_order)

    def value(self, f: dict, spec: Any) -> tuple[int, str]:
        if isinstance(spec, int):
            return spec, ""
        hits = [v for v in self.values.get(f["key"], []) if v["vlo"] == v["vhi"] and spec.rx.search(_text(v))]
        if not hits:
            raise _Skip(f"no value of {f['name']} matches /{spec.rx.pattern}/")
        return hits[0]["vlo"], _text(hits[0])


# ----------------------------------------------------------------------------- resolution

def _bits(f: dict) -> dict:
    return {"dw": f["dw"], "hi": f["hi"], "lo": f["lo"]}


def _resolve(cat: _Cat, recipe: dict, subst: dict[str, str], bound: dict[str, tuple[int, str]]) -> tuple[list[dict], list[str], str]:
    """Recipe -> predicate steps, one readable line per step, and a closing note on how steps are linked.  Raises _Skip when the catalog cannot support it."""
    steps: list[dict] = []
    lines: list[str] = []
    captured: set[str] = set()
    uses: list[tuple[dict, dict, str]] = []        # (step, condition, variable) so unresolvable links can be dropped
    for sp in recipe["steps"]:
        name = sp["cmd"].format(**subst)
        cmd = cat.command(name, sp["via"])
        if cmd is None:
            raise _Skip(f"command '{name}' is not in the catalog")
        mi = sp["via"] == "mi"
        if mi and not any(c["interface"] == "mi" for c in cat.cmds.values()):
            raise _Skip("no NVMe-MI specification in the set")
        step: dict[str, Any] = {"command": cmd["key"], "interface": "mi" if mi else cmd["interface"], "queue": "admin" if mi else cmd["queue"],
                                "cs": cmd["cs"] if cmd["queue"] == "io" else None, "opcode": cmd["opcode"], "fields": []}
        conds: list[str] = []
        for abbr, spec in sp["where"].items():
            fl = cat.field(cmd, abbr)
            if isinstance(spec, Var) and spec.name not in bound:
                if fl:         # a link to an earlier capture; without the field the flow keeps its order but loses the link
                    cond = {**_bits(fl[0]), "op": "eq", "value": {"var": spec.name}}
                    step["fields"].append(cond)
                    uses.append((step, cond, spec.name))
                    conds.append(f"{abbr} = the {spec.name} captured above")
                continue
            if not fl:
                raise _Skip(f"{name} has no field {abbr}")
            val, text = bound[spec.name] if isinstance(spec, Var) else cat.value(fl[0], spec)
            step["fields"].append({**_bits(fl[0]), "op": "eq", "value": val})
            conds.append(f"{abbr} = {text or hex(val)}")
        if sp["rsvd"]:
            rs = sorted(cat.reserved.get(cmd["key"], []), key=_order)
            if not rs:
                raise _Skip(f"{name} has no reserved bit range")
            step["fields"].append({**_bits(rs[0]), "op": "ne", "value": 0})
            conds.append(f"reserved {rs[0]['dw']}[{rs[0]['hi']}:{rs[0]['lo']}] != 0")
        for var, abbr in sp["cap"].items():
            fl = cat.field(cmd, abbr)
            if fl:
                step.setdefault("capture", {})[var] = _bits(fl[0])
                captured.add(var)
        step["note"] = sp["note"]
        steps.append(step)
        lines.append(f"{name}{' (out-of-band, over NVMe-MI)' if mi else ''}" + (f" [{'; '.join(conds)}]" if conds else "") + f": {sp['note']}")
    for st, cond, var in uses:
        if var not in captured:
            st["fields"].remove(cond)
    return steps, lines, _link_range(cat, steps, *recipe["link"]) if recipe.get("link") else ""


def _link_range(cat: _Cat, steps: list[dict], first: str, second: str) -> str:
    """Make the second command address the range the first one touched: capture SLBA / NLB (whichever the catalog describes
    on both commands, with equal widths) from the first and require the same values on the second."""
    a = next(s for s in steps if cat.cmds[s["command"]]["name"] == first)
    b = next(s for s in reversed(steps) if cat.cmds[s["command"]]["name"] == second)
    linked = []
    for abbr in RANGE:
        fa, fb = cat.field(cat.cmds[a["command"]], abbr), cat.field(cat.cmds[b["command"]], abbr)
        pairs = [(x, y) for x, y in zip(fa, fb) if x["hi"] - x["lo"] == y["hi"] - y["lo"]] if len(fa) == len(fb) else []
        for k, (x, y) in enumerate(pairs):
            var = f"{abbr.lower()}{k}"
            a.setdefault("capture", {})[var] = _bits(x)
            b["fields"].append({**_bits(y), "op": "eq", "value": {"var": var}})
        if pairs:
            linked.append(abbr)
    return f"The {second} must address the same {' and '.join(linked)} as the {first}." if linked else ""


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _instances(cat: _Cat, r: dict) -> list[tuple[str, dict[str, str], dict[str, tuple[int, str]]]]:
    """Recipe -> (key suffix, text substitutions, bound variables) per concrete workflow."""
    if "foreach" in r:                  # one workflow per enumerated value of a field (e.g. every Feature Identifier)
        cname, abbr = r["foreach"]
        cmd = cat.command(cname, None)
        fl = cat.field(cmd, abbr) if cmd else []
        vals = [v for v in cat.values.get(fl[0]["key"], []) if v["vlo"] == v["vhi"] and _text(v)] if fl else []
        return [(f"{v['vlo']:02X}h", {"v": f"{_text(v)} ({abbr} {v['vlo']:#04x})"}, {"v": (v["vlo"], _text(v))})
                for v in sorted(vals, key=lambda v: v["vlo"])]
    if "foreach_cmd" in r:
        return [(_slug(c), {"c": c}, {}) for c in r["foreach_cmd"]]
    return [("", {}, {})]


def build_workflow_items(items: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """Return (catalog items of kind 'workflow', issues).  `items` maps catalog key -> {key, kind, attrs}."""
    cat = _Cat(items)
    out: list[dict] = []
    issues: list[dict] = []
    for r in RECIPES:
        built, why = 0, ""
        for suffix, subst, bound in _instances(cat, r):
            try:
                steps, lines, link_note = _resolve(cat, r, subst, bound)
            except _Skip as e:
                why = why or str(e)
                continue
            pred = {"scope": "test", "order": r.get("order", "strict"), "steps": steps}
            errs = validate_predicate(pred, cat.cmds, cat.described)
            if errs:
                issues.append({"severity": "warning", "kind": "workflow_invalid", "key": f"wf:{r['id']}",
                               "message": f"Workflow '{r['id']}' dropped: {'; '.join(errs[:3])}"})
                continue
            desc = ("Test flow" + (" (any order)" if r.get("order") == "any" else "") + ", all steps within one test:\n" +
                    "\n".join(f"{n}. {ln}" for n, ln in enumerate(lines, 1)) + (f"\n{link_note}" if link_note else ""))
            out.append({"key": f"wf:{r['id']}" + (f":{suffix}" if suffix else ""), "kind": "workflow", "parent_key": None,
                        "label": r["title"].format(**subst), "observability": "stimulus",
                        "attrs": {"recipe": r["id"], "commands": sorted({s["command"] for s in steps}), "steps": len(steps),
                                  "predicate": pred, "description": desc}})
            built += 1
        if not built:
            issues.append({"severity": "info", "kind": "workflow_skipped", "key": f"wf:{r['id']}",
                           "message": f"Workflow '{r['id']}' not generated" + (f": {why}" if why else "")})
    return out, issues
