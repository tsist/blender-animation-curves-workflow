"""Host-safe contract for the persistent multi-event animation project.

This module deliberately imports no bpy, performs no file access and evaluates
no code.  It only normalizes bounded JSON-safe requests so the Blender-side
project core can reuse the existing single-event validator without duplicating
its native policy.
"""
import copy
import json
import math
import struct

from .clip_contract import SCHEMA as CLIP_SCHEMA, normalize_request as normalize_clip
from .contract import WorkflowError

SCHEMA = "animation-event-project-v1"
ACTIONS = {"inspect", "capture", "adopt", "retime", "set_relations", "set_lock"}
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_EVENTS = 64
MAX_RELATIONS = 128
MAX_SPATIAL_LOCKS = 64
MAX_EVENT_ID = 128


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
    return result


def _f32(value, label):
    number = _number(value, label)
    try:
        native = struct.unpack("f", struct.pack("f", number))[0]
    except (OverflowError, struct.error):
        _fail(label + " is outside the native float32 range")
    if not math.isfinite(native) or native != number:
        _fail(label + " must be exactly representable as a native float32 frame")
    return number


def _bool(value, label):
    if type(value) is not bool:
        _fail(label + " must be boolean")
    return value


def _array(value, label, maximum, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        _fail(label + " requires " + str(minimum) + ".." + str(maximum) + " items")
    return value


def normalize_ref(value, label):
    """Normalize one explicit geometry reference.

    Bone refs use a point in evaluated pose-bone local space; vertex refs use a
    native vertex index on the evaluated mesh.  No RNA paths or expressions.
    """
    if not isinstance(value, dict):
        _fail(label + " must be an object")
    has_bone = "bone" in value
    has_vertex = "vertex" in value
    if has_bone == has_vertex:
        _fail(label + " requires exactly one of bone or vertex")
    if has_bone:
        _object(value, {"object", "bone", "local"},
                {"object", "bone", "local"}, label)
        local = _array(value["local"], label + ".local", 3, 3)
        return {
            "object": _text(value["object"], label + ".object", 256),
            "bone": _text(value["bone"], label + ".bone", 256),
            "local": [_number(item, label + ".local") for item in local],
        }
    _object(value, {"object", "vertex"}, {"object", "vertex"}, label)
    vertex = value["vertex"]
    if type(vertex) is not int or vertex < 0:
        _fail(label + ".vertex must be a nonnegative native integer")
    return {
        "object": _text(value["object"], label + ".object", 256),
        "vertex": vertex,
    }


def normalize_contact_intent(value):
    _object(value, {"a", "b", "max_distance"},
            {"a", "b", "max_distance"}, "contact_intent")
    distance = _number(value["max_distance"], "contact_intent.max_distance")
    if distance <= 0.0:
        _fail("contact_intent.max_distance must be positive")
    return {
        "a": normalize_ref(value["a"], "contact_intent.a"),
        "b": normalize_ref(value["b"], "contact_intent.b"),
        "max_distance": distance,
    }


def normalize_spatial_locks(value):
    rows = _array(value, "spatial_locks", MAX_SPATIAL_LOCKS)
    out = []
    seen = set()
    for index, row in enumerate(rows):
        label = "spatial_locks[" + str(index) + "]"
        _object(row, {"frame", "point", "world", "tolerance"},
                {"frame", "point", "world", "tolerance"}, label)
        frame = _f32(row["frame"], label + ".frame")
        key = round(frame, 9)
        if key in seen:
            _fail("spatial_locks contains a duplicate frame")
        seen.add(key)
        world = _array(row["world"], label + ".world", 3, 3)
        tolerance = _number(row["tolerance"], label + ".tolerance")
        if tolerance < 0.0:
            _fail(label + ".tolerance cannot be negative")
        out.append({
            "frame": frame,
            "point": normalize_ref(row["point"], label + ".point"),
            "world": [_number(item, label + ".world") for item in world],
            "tolerance": tolerance,
        })
    return out


def _sync_conflicts(relations):
    parent = {}
    potential = {}

    def find(node):
        if node not in parent:
            parent[node] = node
            potential[node] = 0.0
            return node
        if parent[node] == node:
            return node
        root = find(parent[node])
        potential[node] += potential[parent[node]]
        parent[node] = root
        return root

    for relation in relations:
        if relation["type"] != "sync":
            continue
        left, right = relation["a"], relation["b"]
        root_left, root_right = find(left), find(right)
        wanted = float(relation["offset"])
        if root_left == root_right:
            actual = potential[left] - potential[right]
            if abs(actual - wanted) > 1e-9:
                _fail("Contradictory sync relations for " + left + "/" + right)
            continue
        parent[root_left] = root_right
        potential[root_left] = wanted - potential[left] + potential[right]


def _after_cycle(relations):
    edges = {}
    nodes = set()
    for relation in relations:
        if relation["type"] != "after":
            continue
        before, after = relation["before"], relation["after"]
        nodes.update((before, after))
        edges.setdefault(before, set()).add(after)
    visiting = set()
    visited = set()

    def visit(node):
        if node in visited:
            return
        if node in visiting:
            _fail("Relation graph contains an after-cycle at " + node)
        visiting.add(node)
        for child in sorted(edges.get(node, ())):
            visit(child)
        visiting.remove(node)
        visited.add(node)

    for node in sorted(nodes):
        visit(node)


def normalize_relations(value, event_ids=None):
    rows = _array(value, "relations", MAX_RELATIONS)
    out = []
    seen = set()
    for index, row in enumerate(rows):
        label = "relations[" + str(index) + "]"
        if not isinstance(row, dict):
            _fail(label + " must be an object")
        kind = row.get("type")
        if kind == "after":
            _object(row, {"type", "before", "after", "min_gap"},
                    {"type", "before", "after", "min_gap"}, label)
            before = _text(row["before"], label + ".before", MAX_EVENT_ID)
            after = _text(row["after"], label + ".after", MAX_EVENT_ID)
            if before == after:
                _fail(label + " cannot relate an event to itself")
            min_gap = _number(row["min_gap"], label + ".min_gap")
            if min_gap < 0.0:
                _fail(label + ".min_gap cannot be negative")
            normalized = {"type": "after", "before": before, "after": after,
                          "min_gap": min_gap}
            key = ("after", before, after)
        elif kind == "sync":
            _object(row, {"type", "a", "b", "offset", "tolerance"},
                    {"type", "a", "b", "offset", "tolerance"}, label)
            left = _text(row["a"], label + ".a", MAX_EVENT_ID)
            right = _text(row["b"], label + ".b", MAX_EVENT_ID)
            if left == right:
                _fail(label + " cannot sync an event with itself")
            offset = _number(row["offset"], label + ".offset")
            tolerance = _number(row["tolerance"], label + ".tolerance")
            if tolerance < 0.0:
                _fail(label + ".tolerance cannot be negative")
            normalized = {"type": "sync", "a": left, "b": right,
                          "offset": offset, "tolerance": tolerance}
            key = ("sync", min(left, right), max(left, right))
        else:
            _fail(label + ".type must be after or sync")
        if key in seen:
            _fail(label + " duplicates another relation")
        seen.add(key)
        if event_ids is not None:
            names = ({normalized["before"], normalized["after"]}
                     if kind == "after" else {normalized["a"], normalized["b"]})
            missing = names - set(event_ids)
            if missing:
                _fail(label + " references unknown event ids: "
                      + repr(sorted(missing)))
        out.append(normalized)
    _sync_conflicts(out)
    _after_cycle(out)
    return out


def normalize_request(value):
    """Validate and deep-copy one public multi-event project request."""
    if not isinstance(value, dict):
        _fail("request must be an object")
    action = value.get("action")
    if action not in ACTIONS:
        _fail("request.action must be one of " + ", ".join(sorted(ACTIONS)))
    common = {"schema", "action", "context"}
    allowed = set(common)
    required = set(common)
    if action == "inspect":
        allowed.add("event_id")
    elif action in {"capture", "adopt"}:
        allowed.update({"event_id", "clip", "lock_contact",
                        "contact_intent", "spatial_locks"})
        required.update({"event_id", "clip"})
    elif action == "retime":
        allowed.update({"event_id", "clip", "contact_intent", "spatial_locks"})
        required.update({"event_id", "clip"})
    elif action == "set_relations":
        allowed.add("relations")
        required.add("relations")
    else:
        allowed.update({"event_id", "locked"})
        required.update({"event_id", "locked"})
    _object(value, allowed, required, "request")
    out = copy.deepcopy(value)
    if out["schema"] != SCHEMA:
        _fail("request.schema must be " + SCHEMA)
    _object(out["context"], {"scene", "view_layer"},
            {"scene", "view_layer"}, "context")
    _text(out["context"]["scene"], "context.scene", 256)
    _text(out["context"]["view_layer"], "context.view_layer", 256)

    if "event_id" in out:
        out["event_id"] = _text(out["event_id"], "event_id", MAX_EVENT_ID)
    if "clip" in out:
        clip = normalize_clip(out["clip"])
        expected = "capture" if action in {"capture", "adopt"} else "retime"
        if clip["action"] != expected:
            _fail("clip.action must be " + expected + " for project " + action)
        if clip["context"] != out["context"]:
            _fail("clip.context must exactly match the project context")
        if clip["event"]["id"] != out["event_id"]:
            _fail("clip.event.id must match project event_id")
        out["clip"] = clip
    if "lock_contact" in out:
        out["lock_contact"] = _bool(out["lock_contact"], "lock_contact")
    if "locked" in out:
        out["locked"] = _bool(out["locked"], "locked")
    if "contact_intent" in out:
        out["contact_intent"] = normalize_contact_intent(out["contact_intent"])
    if "spatial_locks" in out:
        out["spatial_locks"] = normalize_spatial_locks(out["spatial_locks"])
    if "relations" in out:
        out["relations"] = normalize_relations(out["relations"])
    return out


def _pairs(rows):
    out = {}
    for key, value in rows:
        if key in out:
            raise WorkflowError("INVALID_REQUEST",
                                "Request JSON contains duplicate field: " + key)
        out[key] = value
    return out


def loads(text):
    """Strict JSON entry point used by hosts that receive request text."""
    if not isinstance(text, str) or not text:
        _fail("request text must be a nonempty string")
    if len(text.encode("utf8")) > MAX_REQUEST_BYTES:
        _fail("request text exceeds its byte budget")
    try:
        value = json.loads(text, object_pairs_hook=_pairs)
    except WorkflowError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise WorkflowError("INVALID_REQUEST",
                            "Request text is not valid JSON") from exc
    return normalize_request(value)


__all__ = [
    "SCHEMA", "ACTIONS", "MAX_EVENTS", "MAX_RELATIONS", "MAX_SPATIAL_LOCKS",
    "normalize_ref", "normalize_contact_intent", "normalize_spatial_locks",
    "normalize_relations", "normalize_request", "loads",
]
