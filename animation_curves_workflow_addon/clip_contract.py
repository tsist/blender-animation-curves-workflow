"""Host-safe request contract for one localized clip event.

This module is deliberately free of bpy, file access, dynamic imports of the
Blender package, and code evaluation.  It only normalizes explicit JSON-safe
input and can be imported by host-side CLI code before Blender starts.
"""
import copy
import math

from .contract import WorkflowError

SCHEMA = "animation-clip-event-v1"
ACTIONS = {"capture", "retime"}
PARTICIPANT_FIELDS = ("rigs", "roots", "lights", "materials")
OBJECT_FIELD = "objects"
_MAX_NAMES = {"rigs": 8, "roots": 8, "lights": 16, "materials": 16,
              OBJECT_FIELD: 8}


def _fail(message):
    raise WorkflowError("INVALID_REQUEST", message)


def _object(value, allowed, required, label):
    if not isinstance(value, dict):
        _fail(label + " must be an object")
    missing = set(required) - value.keys()
    unknown = value.keys() - set(allowed)
    if missing or unknown:
        _fail(label + ": missing=" + repr(sorted(missing))
              + ", unknown=" + repr(sorted(unknown)))


def _text(value, label, maximum=512):
    if (not isinstance(value, str) or not value or len(value) > maximum
            or "\x00" in value):
        _fail(label + " must be a nonempty bounded string")
    return value


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(label + " must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        _fail(label + " exceeds the supported finite numeric range")
    if not math.isfinite(result):
        _fail(label + " must be a finite number")
    return value


def _names(value, label, maximum):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        _fail(label + " requires 1.." + str(maximum) + " explicit names")
    out = []
    seen = set()
    for item in value:
        name = _text(item, label + " name", 256)
        if name in seen:
            _fail(label + " contains duplicate name: " + name)
        seen.add(name)
        out.append(name)
    return out


def normalize_request(value):
    """Validate and deep-copy one public clip-event request.

    The returned value is a plain JSON-safe dict.  Caller-owned input is never
    mutated.  Capture has no target_contact; retime requires one.
    """
    _object(value, {"schema", "action", "context", "event", "participants",
                    "locks"},
            {"schema", "action", "context", "event", "participants", "locks"},
            "request")
    out = copy.deepcopy(value)
    if out["schema"] != SCHEMA:
        _fail("request.schema must be " + SCHEMA)
    if out["action"] not in ACTIONS:
        _fail("request.action must be capture or retime")

    _object(out["context"], {"scene", "view_layer"},
            {"scene", "view_layer"}, "context")
    _text(out["context"]["scene"], "context.scene", 256)
    _text(out["context"]["view_layer"], "context.view_layer", 256)

    event_allowed = {"id", "start", "contact", "end"}
    event_required = {"id", "start", "contact", "end"}
    if out["action"] == "retime":
        event_allowed = event_allowed | {"target_contact"}
        event_required = event_required | {"target_contact"}
    _object(out["event"], event_allowed,
            event_required, "event")
    _text(out["event"]["id"], "event.id", 128)
    for field in ("start", "contact", "end"):
        _number(out["event"][field], "event." + field)
    if not out["event"]["start"] < out["event"]["contact"] < out["event"]["end"]:
        _fail("event requires start < contact < end")
    if out["action"] == "retime":
        target = _number(out["event"]["target_contact"],
                         "event.target_contact")
        if not out["event"]["start"] < target < out["event"]["end"]:
            _fail("event.target_contact must remain strictly inside the event window")

    object_mode = (isinstance(out["participants"], dict)
                   and OBJECT_FIELD in out["participants"])
    allowed = set(PARTICIPANT_FIELDS)
    if object_mode:
        allowed.add(OBJECT_FIELD)
    _object(out["participants"], allowed, set(PARTICIPANT_FIELDS),
            "participants")
    if object_mode:
        out["participants"][OBJECT_FIELD] = _names(
            out["participants"][OBJECT_FIELD], "participants.objects",
            _MAX_NAMES[OBJECT_FIELD])
        for field in PARTICIPANT_FIELDS:
            if not isinstance(out["participants"][field], list) \
                    or len(out["participants"][field]) != 0:
                _fail("participants." + field
                      + " must be empty in an explicit objects request")
            out["participants"][field] = []
        scene_names = out["participants"][OBJECT_FIELD]
    else:
        for field in PARTICIPANT_FIELDS:
            out["participants"][field] = _names(
                out["participants"][field], "participants." + field,
                _MAX_NAMES[field])
        scene_names = (out["participants"]["rigs"]
                       + out["participants"]["roots"]
                       + out["participants"]["lights"])
    if len(scene_names) != len(set(scene_names)):
        if object_mode:
            _fail("objects must name distinct scene objects")
        _fail("rigs/roots/lights must name distinct scene objects")

    if not isinstance(out["locks"], list) or len(out["locks"]) > 64:
        _fail("locks requires 0..64 explicit frames")
    locks = []
    seen_locks = set()
    start, end = out["event"]["start"], out["event"]["end"]
    for frame in out["locks"]:
        frame = _number(frame, "lock frame")
        key = float(frame)
        if key in seen_locks:
            _fail("locks contains a duplicate frame")
        seen_locks.add(key)
        if start <= key <= end:
            _fail("lock frames must stay outside the edited event window")
        locks.append(frame)
    out["locks"] = locks
    return out


__all__ = ["SCHEMA", "normalize_request"]
