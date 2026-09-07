"""bcf_export -- turn Findings into a BCF 2.1 issue package.

WHY THIS EXISTS
A Finding from geometry_engine is only useful once it reaches the tools the
trades actually work in (Navisworks, Solibri, BIMcollab, ...). BCF (BIM
Collaboration Format) is the vendor-neutral exchange for exactly that: a zip
of per-issue folders, each carrying a markup.bcf (what the issue is) and a
viewpoint.bcfv (where to look). Writing it ourselves with the stdlib's
ElementTree -- rather than a third-party BCF library -- keeps this block
auditable: every tag written here is a tag we chose to write, not one a
dependency chose for us.

WHAT BECOMES AN ISSUE
Only "clash" and "clearance" findings are exported. A "clear" pair is not a
problem to hand to a coordinator, and an "unjudged" pair is a gap in the
geometry, not a located issue with two GlobalIds and a place to stand a
camera -- exporting either as a BCF topic would train reviewers to distrust
the report.

THE CAMERA -- AND WHY IT IS OFTEN ABSENT
BCF's PerspectiveCamera exists so a reviewer's viewer can jump straight to
the clash. That is only honest when we actually know where the clash is.
Finding carries no centroid in its base contract (geometry_engine judges
pairs; it does not have to record where in space they touched), so this
block reads an *optional* ``centroid`` attribute off the finding via
``getattr`` -- present when a caller (or a future geometry_engine revision)
attaches one, silently absent otherwise. When absent we omit the whole
PerspectiveCamera element rather than point the camera at (0, 0, 0) or at
one element's un-averaged origin. A camera aimed at a made-up point sends a
reviewer looking at empty air and teaches them the export cannot be trusted;
no camera just means "open the model yourself," which is merely inconvenient.
"""
from __future__ import annotations

import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree as ET

# Kinds that are worth a coordinator's attention. "clear" and "unjudged" are
# deliberately excluded -- see module docstring.
_EXPORTABLE_KINDS = ("clash", "clearance")

BCF_VERSION_ID = "2.1"

# Schema-required camera fields that are NOT measurements. BCF's
# PerspectiveCamera needs a direction and up-vector to be schema-valid; we
# have no observed data for either (only a centroid, when one exists), so
# these are fixed, clearly-labelled defaults -- a "look down from above"
# convention -- never presented as anything the geometry engine measured.
_DEFAULT_CAMERA_DIRECTION = (0.0, 0.0, -1.0)
_DEFAULT_CAMERA_UP_VECTOR = (0.0, 1.0, 0.0)
_DEFAULT_FIELD_OF_VIEW = "60.0"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clause_text(finding: Any, rule_lookup: Any) -> str | None:
    """Text for the clause a clearance finding was judged against.

    This block does not import clearance_rules -- that module is owned and
    edited elsewhere, and a hard import here would make bcf_export break
    every time that file is mid-edit, for a feature (pretty clause prose)
    that is not this block's job to own. Instead a caller MAY pass
    ``rule_lookup`` (a dict or a callable of rule_id -> text) to supply the
    real clause. Without one, we still owe the reader something better than
    a bare code: we synthesise a clause sentence from data the Finding
    itself already carries (the rule_id and the required clearance it
    recorded), so the Description is never silently missing the citation a
    clearance finding is required to carry.
    """
    rule_id = getattr(finding, "rule_id", None)
    if not rule_id:
        return None
    if rule_lookup is not None:
        if isinstance(rule_lookup, dict):
            text = rule_lookup.get(rule_id)
        else:
            text = rule_lookup(rule_id)
        if text:
            return text
    required = getattr(finding, "required_clearance_m", None)
    if required is not None:
        return f"Rule {rule_id}: minimum required clearance {required:.3f} m."
    return f"Rule {rule_id}."


def _measurement_text(finding: Any) -> str:
    kind = finding.kind
    method = finding.method
    if kind == "clash":
        vol = getattr(finding, "penetration_volume_m3", None)
        if vol is not None:
            return (
                f"Clash between {finding.element_a} and {finding.element_b}: "
                f"penetration volume {vol:.4f} m3 (method: {method})."
            )
        return (
            f"Clash between {finding.element_a} and {finding.element_b}: "
            f"contact proven, penetration depth not measurable (method: {method})."
        )
    # clearance
    distance = getattr(finding, "distance_m", None)
    required = getattr(finding, "required_clearance_m", None)
    return (
        f"Clearance violation between {finding.element_a} and {finding.element_b}: "
        f"measured separation {distance:.4f} m, required {required:.4f} m "
        f"(method: {method})."
    )


def _proposed_move_text(finding: Any) -> str:
    """Text for the Comment element: a first-pass remediation suggestion.

    Deliberately phrased as a proposal, not an instruction -- this block
    only reports geometry, it never edits the model (that is model_clone's
    job), so the language must not imply the move has happened.
    """
    if finding.kind == "clash":
        return (
            f"Proposed: reroute {finding.element_b} clear of {finding.element_a} "
            f"to eliminate the interpenetration."
        )
    required = getattr(finding, "required_clearance_m", None)
    distance = getattr(finding, "distance_m", None)
    gap = None
    if required is not None and distance is not None:
        gap = required - distance
    if gap is not None and gap > 0:
        return (
            f"Proposed: increase separation between {finding.element_a} and "
            f"{finding.element_b} by at least {gap:.4f} m."
        )
    return (
        f"Proposed: increase separation between {finding.element_a} and "
        f"{finding.element_b} to meet the required clearance."
    )


def _build_markup_xml(finding: Any, guid: str, model_name: str, rule_lookup: Any) -> bytes:
    topic_type = "Clash" if finding.kind == "clash" else "Clearance"
    title = f"{model_name}: {topic_type} - {finding.element_a} / {finding.element_b}"

    description_parts = [_measurement_text(finding)]
    clause = _clause_text(finding, rule_lookup)
    if clause:
        description_parts.append(clause)
    description = " ".join(description_parts)

    markup = ET.Element("Markup")
    topic = ET.SubElement(
        markup, "Topic",
        {"Guid": guid, "TopicType": topic_type, "TopicStatus": "Open"},
    )
    ET.SubElement(topic, "Title").text = title
    ET.SubElement(topic, "CreationDate").text = _iso_now()
    ET.SubElement(topic, "Description").text = description

    comment = ET.SubElement(markup, "Comment", {"Guid": str(uuid.uuid4())})
    ET.SubElement(comment, "Date").text = _iso_now()
    ET.SubElement(comment, "Comment").text = _proposed_move_text(finding)

    return ET.tostring(markup, encoding="UTF-8", xml_declaration=True)


def _build_viewpoint_xml(finding: Any, guid: str) -> bytes:
    info = ET.Element("VisualizationInfo", {"Guid": guid})
    components = ET.SubElement(info, "Components")
    selection = ET.SubElement(components, "Selection")
    ET.SubElement(selection, "Component", {"IfcGuid": finding.element_a})
    ET.SubElement(selection, "Component", {"IfcGuid": finding.element_b})

    # See module docstring: no camera unless we have a real centroid.
    centroid = getattr(finding, "centroid", None)
    if centroid is not None:
        x, y, z = centroid
        camera = ET.SubElement(info, "PerspectiveCamera")
        viewpoint = ET.SubElement(camera, "CameraViewPoint")
        ET.SubElement(viewpoint, "X").text = repr(float(x))
        ET.SubElement(viewpoint, "Y").text = repr(float(y))
        ET.SubElement(viewpoint, "Z").text = repr(float(z))

        direction = ET.SubElement(camera, "CameraDirection")
        dx, dy, dz = _DEFAULT_CAMERA_DIRECTION
        ET.SubElement(direction, "X").text = repr(dx)
        ET.SubElement(direction, "Y").text = repr(dy)
        ET.SubElement(direction, "Z").text = repr(dz)

        up = ET.SubElement(camera, "CameraUpVector")
        ux, uy, uz = _DEFAULT_CAMERA_UP_VECTOR
        ET.SubElement(up, "X").text = repr(ux)
        ET.SubElement(up, "Y").text = repr(uy)
        ET.SubElement(up, "Z").text = repr(uz)

        ET.SubElement(camera, "FieldOfView").text = _DEFAULT_FIELD_OF_VIEW

    return ET.tostring(info, encoding="UTF-8", xml_declaration=True)


def _build_version_xml() -> bytes:
    version = ET.Element("Version", {"VersionId": BCF_VERSION_ID})
    return ET.tostring(version, encoding="UTF-8", xml_declaration=True)


def export_bcf(
    findings: list[Any],
    out_path: str | Path,
    model_name: str,
    viewpoints: bool = True,
    rule_lookup: dict[str, str] | Callable[[str], str] | None = None,
) -> Path:
    """Write ``findings`` as a BCF 2.1 package (a .bcfzip) at ``out_path``.

    Only clash/clearance findings become issues (see module docstring).
    ``viewpoints`` toggles whether viewpoint.bcfv is written per issue at
    all; when it is, the camera inside it is still conditional on the
    finding actually carrying a centroid.
    """
    out_path = Path(out_path)
    exportable = [f for f in findings if f.kind in _EXPORTABLE_KINDS]

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bcf.version", _build_version_xml())
        for finding in exportable:
            issue_guid = str(uuid.uuid4())
            folder = issue_guid
            markup_xml = _build_markup_xml(finding, issue_guid, model_name, rule_lookup)
            zf.writestr(f"{folder}/markup.bcf", markup_xml)
            if viewpoints:
                viewpoint_xml = _build_viewpoint_xml(finding, str(uuid.uuid4()))
                zf.writestr(f"{folder}/viewpoint.bcfv", viewpoint_xml)

    # Self-check: a writer that silently drops a required member is worse
    # than one that raises, because a broken export otherwise looks fine
    # until it is opened in a real BCF viewer, far from here.
    validate_bcf_zip(out_path)
    return out_path


def validate_bcf_zip(path: str | Path) -> None:
    """Structural validation of a .bcfzip against the minimum this block
    promises: a bcf.version declaring 2.1, and every issue folder holding a
    markup.bcf.

    This is not a courtesy check for tests -- ``export_bcf`` calls it on
    every write. That is what makes the mutation-probe test meaningful: it
    proves this function actually rejects a broken archive, rather than the
    suite merely trusting that "if export_bcf ran, the zip must be fine."
    """
    path = Path(path)
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        if "bcf.version" not in names:
            raise ValueError("invalid BCF archive: bcf.version is missing")
        version_root = ET.fromstring(zf.read("bcf.version"))
        if version_root.get("VersionId") != BCF_VERSION_ID:
            raise ValueError(
                f"invalid BCF archive: bcf.version does not declare "
                f"VersionId={BCF_VERSION_ID!r}"
            )
        issue_dirs = sorted({n.split("/", 1)[0] for n in names if "/" in n})
        for folder in issue_dirs:
            if f"{folder}/markup.bcf" not in names:
                raise ValueError(f"invalid BCF archive: {folder} is missing markup.bcf")
