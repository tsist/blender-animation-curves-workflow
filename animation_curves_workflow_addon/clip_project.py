"""Persistent multi-event animation project on the existing clip-event core.

The canonical store remains Scene.acw_clip_event_state.  Each project event
contains one validated legacy single-event record plus explicit contact-lock
and geometry metadata.  Native key edits still go through clip_event/core;
this module only adds book commit, adoption, relationship checks and the
project-level late-failure rollback envelope.
"""
import copy
import hashlib
import json
import math

import bpy
from mathutils import Vector

from . import clip_event, event_ui_state, native
from . import clip_project_contract as contract
from .clip_contract import SCHEMA as CLIP_SCHEMA
from .contract import WorkflowError

BOOK_SCHEMA = "animation-event-project-state-v1"
REVIEW_SCHEMA = "animation-event-project-review-v1"
RESULT_SCHEMA = "animation-event-project-result-v1"
INSPECT_SCHEMA = "animation-event-project-inspection-v1"
PROPERTY = clip_event.RECORD_PROPERTY
BOOK_MAX_BYTES = 32 * 1024 * 1024
MAX_EVENTS = contract.MAX_EVENTS
MAX_TOTAL_BINDINGS = 32768
EPSILON = 1e-6


def _fail(code, message, details=None):
    raise WorkflowError(code, message, details or {})


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise WorkflowError("INVALID_REQUEST",
                            "Project value is not finite JSON-safe data") from exc


def _sha(value):
    return hashlib.sha256(_canonical(value).encode("utf8")).hexdigest()


def _checkpoint(callback):
    if callback is not None and callback() is False:
        _fail("CANCELLED", "Project edit cancelled")


def _pairs(rows):
    out = {}
    for key, value in rows:
        if key in out:
            _fail("CONFLICT", "Project record has duplicate JSON fields")
        out[key] = value
    return out


def _resolve_context(context, request):
    scene = bpy.data.scenes.get(request["context"]["scene"])
    if scene is None:
        _fail("NOT_FOUND", "Scene does not exist: " + request["context"]["scene"])
    layer = scene.view_layers.get(request["context"]["view_layer"])
    if layer is None:
        _fail("NOT_FOUND",
              "View layer does not exist: " + request["context"]["view_layer"])
    if context.scene != scene or context.view_layer != layer:
        _fail("CONFLICT",
              "Project operation requires the explicitly named current scene/view layer")
    native.local(scene)
    return scene, layer


def _read_property(scene):
    if PROPERTY not in scene:
        return None
    value = scene.get(PROPERTY)
    if not isinstance(value, str) or not value:
        _fail("CONFLICT", "Animation event store is not a nonempty string")
    if len(value.encode("utf8")) > BOOK_MAX_BYTES:
        _fail("UNSUPPORTED", "Animation event store exceeds its byte budget")
    return value


def _write_property(scene, text):
    try:
        scene[PROPERTY] = text
    except Exception as exc:
        raise WorkflowError("CONFLICT",
                            "Could not write the animation event store") from exc
    if scene.get(PROPERTY) != text:
        _fail("VALIDATION_FAILED",
              "Animation event store readback differs from the write")


def _restore_property(scene, prior):
    if prior is None:
        if PROPERTY in scene:
            del scene[PROPERTY]
    else:
        scene[PROPERTY] = prior


def _new_book(context):
    seed = {"schema": BOOK_SCHEMA, "context": copy.deepcopy(context)}
    return {
        "schema": BOOK_SCHEMA,
        "id": _sha(seed)[:32],
        "revision": 1,
        "context": copy.deepcopy(context),
        "events": {},
        "relations": [],
    }


def _event_record(entry):
    return entry["record"]


def _record_event(record):
    return next(iter(record["events"].values()))


def _record_event_times(record):
    event = _record_event(record)
    return {
        "id": event["id"],
        "start": float(event["frame"]),
        "contact": float(event["contact"]),
        "end": float(event["end"]),
    }


def _record_summary(record):
    base = clip_event._record_summary(record)
    domains = {}
    actions = set()
    owners = set()
    for target in record["targets"].values():
        domains[target["domain"]] = domains.get(target["domain"], 0) + 1
        actions.add(target["action_name"])
        owners.add((target["owner"]["type"], target["owner"]["name"]))
    base.update({
        "targets_by_domain": dict(sorted(domains.items())),
        "actions": sorted(actions),
        "owners": [{"type": kind, "name": name}
                   for kind, name in sorted(owners)],
    })
    return base


def _entry_summary(event_id, entry, status=None):
    record = entry["record"]
    times = _record_event_times(record)
    record_summary = _record_summary(record)
    row = {
        "event_id": event_id,
        "start": times["start"],
        "contact": times["contact"],
        "end": times["end"],
        "locked": bool(entry["locked"]),
        "roles": copy.deepcopy(record["participants"]),
        "actions": record_summary["actions"],
        "owners": record_summary["owners"],
        "record": record_summary,
        "contact_intent": copy.deepcopy(entry["contact_intent"]),
        "spatial_lock_count": len(entry["spatial_locks"]),
        "status": None if status is None else status.get("status"),
        "contact_known": None if status is None else bool(status.get("contact_known")),
        "drift_targets": None if status is None else list(
            status.get("drift_targets", [])),
        "identity_changed": None if status is None else bool(
            status.get("identity_changed")),
        "status_error": None if status is None else status.get("error"),
    }
    if status is not None:
        row["diagnostics"] = copy.deepcopy(status.get("diagnostics"))
    return row


def _book_summary(book):
    total_bindings = sum(len(entry["record"]["bindings"])
                         for entry in book["events"].values())
    return {
        "schema": book["schema"],
        "id": book["id"],
        "revision": book["revision"],
        "context": copy.deepcopy(book["context"]),
        "event_count": len(book["events"]),
        "event_ids": list(book["events"]),
        "relation_count": len(book["relations"]),
        "bindings": total_bindings,
        "digest": _sha(book),
    }


def _check_event_record(event_id, record, book_context):
    if not isinstance(record, dict):
        _fail("CONFLICT", "Project event record is not an object",
              {"event_id": event_id})
    try:
        event_ui_state.check_clip_state(record)
        clip_event._decode_record_structure(record)
    except WorkflowError as exc:
        raise WorkflowError("CONFLICT",
                            "Project event contains an invalid legacy record",
                            {"event_id": event_id, "error": str(exc),
                             "code": exc.code}) from exc
    event = _record_event(record)
    if event["id"] != event_id:
        _fail("CONFLICT", "Project event key differs from its record event id",
              {"event_id": event_id, "record_id": event["id"]})
    if record["context"] != book_context:
        _fail("CONFLICT", "Project event record context differs from the book",
              {"event_id": event_id})
    return record


def _validate_book(book, expected_context=None):
    if not isinstance(book, dict):
        _fail("CONFLICT", "Project store is not an object")
    fields = {"schema", "id", "revision", "context", "events", "relations"}
    if set(book) != fields:
        _fail("CONFLICT", "Project store fields are missing or unknown",
              {"missing": sorted(fields - set(book)),
               "unknown": sorted(set(book) - fields)})
    if book["schema"] != BOOK_SCHEMA:
        _fail("CONFLICT", "Unknown project store schema",
              {"schema": book["schema"]})
    if (not isinstance(book["id"], str) or len(book["id"]) != 32
            or any(char not in "0123456789abcdef" for char in book["id"])):
        _fail("CONFLICT", "Project store id must be 32 lowercase hex characters")
    if type(book["revision"]) is not int or not 1 <= book["revision"] <= 1000000:
        _fail("CONFLICT", "Project store revision is out of range")
    if (not isinstance(book["context"], dict)
            or set(book["context"]) != {"scene", "view_layer"}
            or not all(isinstance(book["context"][key], str)
                       and book["context"][key]
                       and len(book["context"][key]) <= 256
                       and "\x00" not in book["context"][key]
                       for key in ("scene", "view_layer"))):
        _fail("CONFLICT", "Project store context is malformed")
    if expected_context is not None and book["context"] != expected_context:
        _fail("CONFLICT", "Project store context differs from the request")
    if not isinstance(book["events"], dict) or len(book["events"]) > MAX_EVENTS:
        _fail("CONFLICT", "Project event inventory is malformed or over budget")
    total_bindings = 0
    for event_id, entry in book["events"].items():
        if (not isinstance(event_id, str) or not event_id
                or len(event_id) > 128 or "\x00" in event_id):
            _fail("CONFLICT", "Project event id is malformed")
        if not isinstance(entry, dict) or set(entry) != {
                "record", "locked", "contact_intent", "spatial_locks"}:
            _fail("CONFLICT", "Project event metadata is malformed",
                  {"event_id": event_id})
        _check_event_record(event_id, entry["record"], book["context"])
        if type(entry["locked"]) is not bool:
            _fail("CONFLICT", "Project contact-time lock must be boolean",
                  {"event_id": event_id})
        try:
            if entry["contact_intent"] is not None:
                entry["contact_intent"] = contract.normalize_contact_intent(
                    entry["contact_intent"])
            entry["spatial_locks"] = contract.normalize_spatial_locks(
                entry["spatial_locks"])
        except WorkflowError as exc:
            raise WorkflowError(
                "CONFLICT",
                "Project event geometry metadata is malformed",
                {"event_id": event_id, "error": str(exc),
                 "code": exc.code}) from exc
        total_bindings += len(entry["record"]["bindings"])
    if total_bindings > MAX_TOTAL_BINDINGS:
        _fail("UNSUPPORTED", "Project binding budget exceeded",
              {"bindings": total_bindings})
    try:
        book["relations"] = contract.normalize_relations(
            book["relations"], set(book["events"]))
    except WorkflowError as exc:
        raise WorkflowError(
            "CONFLICT", "Project relations are malformed",
            {"error": str(exc), "code": exc.code}) from exc
    return book


def _encode_book(book):
    _validate_book(book)
    text = _canonical(book)
    if len(text.encode("utf8")) > BOOK_MAX_BYTES:
        _fail("UNSUPPORTED", "Project store exceeds its byte budget")
    return text


def _decode_book_text(text, context):
    if len(text.encode("utf8")) > BOOK_MAX_BYTES:
        _fail("UNSUPPORTED", "Project store exceeds its byte budget")
    try:
        value = json.loads(text, object_pairs_hook=_pairs)
    except WorkflowError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise WorkflowError("CONFLICT",
                            "Project store is not valid JSON") from exc
    if not isinstance(value, dict):
        _fail("CONFLICT", "Project store is not a JSON object")
    return _validate_book(value, context)


def _load_book(scene, context):
    raw = _read_property(scene)
    if raw is None:
        return _new_book(context), None, False
    if len(raw.encode("utf8")) > BOOK_MAX_BYTES:
        _fail("UNSUPPORTED", "Animation event store exceeds its byte budget")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
    except WorkflowError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise WorkflowError("CONFLICT",
                            "Animation event store is not valid JSON") from exc
    if not isinstance(value, dict):
        _fail("CONFLICT", "Animation event store is not a JSON object")
    schema = value.get("schema")
    if schema == event_ui_state.CLIP_SCHEMA:
        record = clip_event._decode_record(raw)
        if record["context"] != context:
            _fail("CONFLICT",
                  "Legacy clip record context differs from the project request")
        event_id = _record_event(record)["id"]
        book = _new_book(context)
        book["events"][event_id] = {
            "record": record,
            "locked": False,
            "contact_intent": None,
            "spatial_locks": [],
        }
        _validate_book(book, context)
        return book, raw, True
    if schema == BOOK_SCHEMA:
        return _decode_book_text(raw, context), raw, False
    _fail("CONFLICT", "Unknown animation event store schema",
          {"schema": schema})


def _clip_capture_request(record):
    return {
        "schema": CLIP_SCHEMA,
        "action": "capture",
        "context": copy.deepcopy(record["context"]),
        "event": _record_event_times(record),
        "participants": copy.deepcopy(record["participants"]),
        "locks": copy.deepcopy(record["locks"]),
    }


def _event_status(context, event_id, entry, diagnostics=False):
    record = entry["record"]
    request = _clip_capture_request(record)
    times = _record_event_times(record)
    status = {
        "event_id": event_id,
        "status": "stale",
        "contact_known": False,
        "drift_targets": [],
        "identity_changed": False,
        "error": None,
        "diagnostics": None,
    }
    try:
        resolved = clip_event._resolve_participants(context, request)
        clip_event._collect_window(resolved, request)
        current = {}
        for channel in resolved["channels"]:
            current[channel["target"]["id"]] = clip_event._window_semantics(channel)
        try:
            clip_event._match_record_targets(record, resolved)
            identity_changed = False
        except WorkflowError as exc:
            identity_changed = True
            status["error"] = str(exc)
        drift = event_ui_state.compare_clip_state(record, current)
        drift_targets = [row["target"] for row in drift
                         if row["status"] != "unchanged"]
        status.update({
            "status": "ready" if not drift_targets and not identity_changed
            else "stale",
            "contact_known": not identity_changed,
            "drift_targets": drift_targets,
            "identity_changed": identity_changed,
            "resolved": resolved,
            "dragged_targets": {row["target"]: row for row in drift
                                if row["status"] != "unchanged"},
        })
    except WorkflowError as exc:
        status["error"] = str(exc)
        if exc.details:
            status["error_details"] = copy.deepcopy(exc.details)
    if diagnostics and status.get("resolved") is not None:
        try:
            status["diagnostics"] = _diagnostics(
                context, resolved["scene"], resolved["layer"], times,
                entry["contact_intent"], entry["spatial_locks"], require=False)
        except WorkflowError as exc:
            status["diagnostics"] = {
                "status": "error", "error": str(exc),
                "contact_intent": None, "spatial_locks": [],
            }
    return status


def _all_statuses(context, book, diagnostics=False):
    return {
        event_id: _event_status(context, event_id, entry, diagnostics)
        for event_id, entry in book["events"].items()
    }


def _object_in_scope(scene, layer, name, role):
    obj = bpy.data.objects.get(name)
    if obj is None:
        _fail("NOT_FOUND", role + " object does not exist: " + name)
    if scene.objects.get(name) != obj or layer.objects.get(name) != obj:
        _fail("CONFLICT", role + " object is outside the requested scene/view layer: "
              + name)
    return obj


def _ref_point(scene, layer, depsgraph, ref, role):
    obj = _object_in_scope(scene, layer, ref["object"], role)
    evaluated = obj.evaluated_get(depsgraph)
    if evaluated is None:
        _fail("CONFLICT", role + " object has no evaluated state", {"object": obj.name})
    if "bone" in ref:
        if obj.type != "ARMATURE":
            _fail("INVALID_REQUEST", role + " bone reference requires an ARMATURE",
                  {"object": obj.name, "type": obj.type})
        pose_bone = evaluated.pose.bones.get(ref["bone"])
        if pose_bone is None:
            _fail("NOT_FOUND", role + " pose bone does not exist: " + ref["bone"],
                  {"object": obj.name})
        local = Vector(tuple(float(value) for value in ref["local"]))
        point = evaluated.matrix_world @ pose_bone.matrix @ local
        return [float(value) for value in point]
    if obj.type != "MESH":
        _fail("INVALID_REQUEST", role + " vertex reference requires a MESH",
              {"object": obj.name, "type": obj.type})
    mesh = evaluated.to_mesh()
    if mesh is None:
        _fail("CONFLICT", role + " mesh has no evaluated data", {"object": obj.name})
    try:
        index = int(ref["vertex"])
        if not 0 <= index < len(mesh.vertices):
            _fail("NOT_FOUND", role + " vertex index is outside the evaluated mesh",
                  {"object": obj.name, "vertex": index,
                   "vertices": len(mesh.vertices)})
        point = evaluated.matrix_world @ mesh.vertices[index].co
        return [float(value) for value in point]
    finally:
        evaluated.to_mesh_clear()


def _with_frame(context, scene, frame, callback):
    original_current = int(scene.frame_current)
    original_subframe = float(scene.frame_subframe)
    whole = int(math.floor(float(frame)))
    subframe = float(frame) - whole
    try:
        scene.frame_set(whole, subframe=subframe)
        depsgraph = context.evaluated_depsgraph_get()
        return callback(depsgraph)
    finally:
        scene.frame_set(original_current, subframe=original_subframe)


def _diagnostics(context, scene, layer, times, contact_intent, spatial_locks,
                 require=False):
    result = {
        "status": "not_configured",
        "contact_intent": None,
        "spatial_locks": [],
    }
    if contact_intent is None and not spatial_locks:
        return result
    contact_row = None
    spatial_rows = []
    errors = []
    if contact_intent is not None:
        try:
            def evaluate_contact(depsgraph):
                point_a = _ref_point(scene, layer, depsgraph,
                                     contact_intent["a"], "contact A")
                point_b = _ref_point(scene, layer, depsgraph,
                                     contact_intent["b"], "contact B")
                distance = (Vector(point_a) - Vector(point_b)).length
                return {
                    "frame": float(times["contact"]),
                    "a": copy.deepcopy(contact_intent["a"]),
                    "b": copy.deepcopy(contact_intent["b"]),
                    "point_a": point_a,
                    "point_b": point_b,
                    "distance": float(distance),
                    "max_distance": float(contact_intent["max_distance"]),
                    "within": bool(distance
                                   <= float(contact_intent["max_distance"]) + EPSILON),
                }
            contact_row = _with_frame(
                context, scene, float(times["contact"]), evaluate_contact)
            if not contact_row["within"]:
                errors.append({
                    "kind": "contact_intent",
                    "message": "Contact intent distance exceeds max_distance",
                    "distance": contact_row["distance"],
                    "max_distance": contact_row["max_distance"],
                })
        except WorkflowError as exc:
            if require:
                raise
            errors.append({"kind": "contact_intent", "code": exc.code,
                           "message": str(exc)})
    for index, lock in enumerate(spatial_locks):
        try:
            def evaluate_lock(depsgraph, lock=lock):
                point = _ref_point(scene, layer, depsgraph, lock["point"],
                                   "spatial lock " + str(index))
                distance = (Vector(point) - Vector(lock["world"])).length
                return {
                    "index": index,
                    "frame": float(lock["frame"]),
                    "point": copy.deepcopy(lock["point"]),
                    "actual": point,
                    "world": copy.deepcopy(lock["world"]),
                    "distance": float(distance),
                    "tolerance": float(lock["tolerance"]),
                    "within": bool(distance <= float(lock["tolerance"]) + EPSILON),
                }
            row = _with_frame(
                context, scene, float(lock["frame"]), evaluate_lock)
            spatial_rows.append(row)
            if not row["within"]:
                errors.append({
                    "kind": "spatial_lock",
                    "index": index,
                    "frame": row["frame"],
                    "message": "Spatial lock distance exceeds tolerance",
                    "distance": row["distance"],
                    "tolerance": row["tolerance"],
                })
        except WorkflowError as exc:
            if require:
                raise
            spatial_rows.append({
                "index": index, "frame": float(lock.get("frame", 0.0)),
                "status": "error", "code": exc.code, "message": str(exc),
            })
            errors.append({"kind": "spatial_lock", "index": index,
                           "code": exc.code, "message": str(exc)})
    result.update({
        "contact_intent": contact_row,
        "spatial_locks": spatial_rows,
        "errors": errors,
        "status": "passed" if not errors else "failed",
    })
    if errors and require:
        _fail("CONFLICT", "Project geometry diagnostic failed",
              {"errors": errors})
    return result


def _relation_checks(relations, contacts, overrides=None):
    overrides = overrides or {}
    rows = []
    errors = []
    for index, relation in enumerate(relations):
        row = {"index": index, "relation": copy.deepcopy(relation)}
        if relation["type"] == "after":
            before = overrides.get(relation["before"], contacts.get(relation["before"]))
            after = overrides.get(relation["after"], contacts.get(relation["after"]))
            if before is None or after is None:
                row.update({"status": "unknown",
                            "missing": [event_id for event_id, value in (
                                (relation["before"], before),
                                (relation["after"], after)) if value is None]})
                errors.append({"index": index, "code": "UNKNOWN_CONTACT",
                               "message": "Relation references an event whose "
                                          "contact key is missing or moved",
                               "relation": copy.deepcopy(relation)})
            else:
                gap = float(after) - float(before)
                ok = gap + EPSILON >= float(relation["min_gap"])
                row.update({"status": "passed" if ok else "failed",
                            "before_contact": float(before),
                            "after_contact": float(after),
                            "gap": gap,
                            "min_gap": float(relation["min_gap"])})
                if not ok:
                    errors.append({"index": index, "code": "RELATION_VIOLATED",
                                   "message": "after relation minimum gap is violated",
                                   "relation": copy.deepcopy(relation),
                                   "gap": gap,
                                   "min_gap": float(relation["min_gap"])})
        else:
            left = overrides.get(relation["a"], contacts.get(relation["a"]))
            right = overrides.get(relation["b"], contacts.get(relation["b"]))
            if left is None or right is None:
                row.update({"status": "unknown",
                            "missing": [event_id for event_id, value in (
                                (relation["a"], left),
                                (relation["b"], right)) if value is None]})
                errors.append({"index": index, "code": "UNKNOWN_CONTACT",
                               "message": "Relation references an event whose "
                                          "contact key is missing or moved",
                               "relation": copy.deepcopy(relation)})
            else:
                delta = float(left) - float(right)
                ok = abs(delta - float(relation["offset"])) \
                    <= float(relation["tolerance"]) + EPSILON
                row.update({"status": "passed" if ok else "failed",
                            "a_contact": float(left),
                            "b_contact": float(right),
                            "delta": delta,
                            "offset": float(relation["offset"]),
                            "tolerance": float(relation["tolerance"])})
                if not ok:
                    errors.append({"index": index, "code": "RELATION_VIOLATED",
                                   "message": "sync relation tolerance is violated",
                                   "relation": copy.deepcopy(relation),
                                   "delta": delta,
                                   "offset": float(relation["offset"]),
                                   "tolerance": float(relation["tolerance"])})
        rows.append(row)
    return {"rows": rows, "errors": errors,
            "status": "passed" if not errors else "failed"}


def _require_relations(relations, contacts, overrides=None):
    checked = _relation_checks(relations, contacts, overrides)
    if checked["errors"]:
        _fail("CONFLICT", "Project relations are not satisfied by actual contact times",
              {"relations": checked["errors"]})
    return checked


def _contact_map(context, book, statuses, overrides=None):
    overrides = overrides or {}
    contacts = {}
    for event_id, entry in book["events"].items():
        if event_id in overrides:
            contacts[event_id] = float(overrides[event_id])
            continue
        status = statuses.get(event_id)
        contacts[event_id] = (float(_record_event(entry["record"])["contact"])
                              if status is not None and status.get("contact_known")
                              else None)
    return contacts


def _seal_review(review):
    body = {key: value for key, value in review.items() if key != "review_sha256"}
    review["review_sha256"] = _sha(body)
    return review


def _status_public(status):
    return {
        "event_id": status.get("event_id"),
        "status": status.get("status"),
        "contact_known": bool(status.get("contact_known")),
        "drift_targets": list(status.get("drift_targets", [])),
        "identity_changed": bool(status.get("identity_changed")),
        "error": status.get("error"),
        "diagnostics": copy.deepcopy(status.get("diagnostics")),
    }


def _capture_proposed_book(context, scene, layer, book, request):
    action = request["action"]
    event_id = request["event_id"]
    clip_request = request["clip"]
    if action == "capture":
        if event_id in book["events"]:
            _fail("CONFLICT", "Capture conflicts with an existing project event id",
                  {"event_id": event_id})
        existing = None
    else:
        if event_id not in book["events"]:
            _fail("NOT_FOUND", "Adopt requires an existing project event id",
                  {"event_id": event_id})
        existing = book["events"][event_id]
    with clip_event._selected_record_view(scene, None):
        legacy = clip_event.review(context, clip_request)
    proposed_record = legacy["record"]
    event = _record_event(proposed_record)
    if event["id"] != event_id:
        _fail("CONFLICT", "Proposed record event id differs from project event id")
    if existing is None:
        locked = bool(request.get("lock_contact", False))
        intent = request.get("contact_intent")
        spatial = request.get("spatial_locks", [])
    else:
        old_times = _record_event_times(existing["record"])
        new_times = _record_event_times(proposed_record)
        locked = (bool(request["lock_contact"])
                  if "lock_contact" in request else bool(existing["locked"]))
        if (existing["locked"] and new_times["contact"] != old_times["contact"]
                and locked):
            _fail("CONFLICT",
                  "Adopt cannot move the contact of an explicitly locked event",
                  {"event_id": event_id, "contact": old_times["contact"],
                   "target_contact": new_times["contact"]})
        intent = (request["contact_intent"] if "contact_intent" in request
                  else existing["contact_intent"])
        spatial = (request["spatial_locks"] if "spatial_locks" in request
                   else existing["spatial_locks"])
    diagnostics = _diagnostics(
        context, scene, layer, _record_event_times(proposed_record),
        intent, spatial, require=True)
    proposed = copy.deepcopy(book)
    proposed["events"][event_id] = {
        "record": proposed_record,
        "locked": locked,
        "contact_intent": copy.deepcopy(intent),
        "spatial_locks": copy.deepcopy(spatial),
    }
    proposed["revision"] = int(book["revision"]) + 1
    _validate_book(proposed)
    if action == "adopt":
        statuses = _all_statuses(context, book)
        contacts = _contact_map(
            context, book, statuses,
            {event_id: _record_event_times(proposed_record)["contact"]})
        relation_checks = _require_relations(
            proposed["relations"], contacts)
    else:
        relation_checks = {"rows": [], "errors": [], "status": "passed"}
    return proposed, legacy, diagnostics, relation_checks


def _scene_of(context):
    scene = context.scene
    if scene is None:
        _fail("NOT_FOUND", "Current context has no scene")
    return scene


def _ensure_clip_context(context, clip_request):
    scene = _scene_of(context)
    _resolve_context(context, clip_request)
    return scene


def _static_proposed_book(context, book, request):
    action = request["action"]
    proposed = copy.deepcopy(book)
    if action == "set_relations":
        proposed["relations"] = contract.normalize_relations(
            request["relations"], set(proposed["events"]))
        statuses = _all_statuses(context, book)
        contacts = _contact_map(context, book, statuses)
        relation_checks = _require_relations(proposed["relations"], contacts)
    elif action == "set_lock":
        event_id = request["event_id"]
        if event_id not in proposed["events"]:
            _fail("NOT_FOUND", "set_lock requires an existing project event id",
                  {"event_id": event_id})
        proposed["events"][event_id]["locked"] = bool(request["locked"])
        relation_checks = {"rows": [], "errors": [], "status": "passed"}
    else:
        _fail("INVALID_REQUEST", "Not a static project action")
    proposed["revision"] = int(book["revision"]) + 1
    _validate_book(proposed)
    return proposed, relation_checks


def _owner_key(target_or_channel):
    if "target" in target_or_channel:
        owner = target_or_channel["target"]["owner"]
    else:
        owner = target_or_channel["owner"]
    return (owner["type"], owner["name"])


def _owner_map(resolved):
    out = {}
    for channel in resolved["channels"]:
        key = _owner_key(channel)
        if key in out:
            continue
        out[key] = {
            "owner": channel["owner"],
            "action": channel["action"],
            "slot": channel["slot"],
        }
    return out


def _frame_binding(record, binding):
    event = _record_event(record)
    return float(event["frame"]) + float(binding["offset"])


def _analyze_other_events(context, book, selected_id, selected_status,
                          target_contact):
    selected_entry = book["events"][selected_id]
    selected_times = _record_event_times(selected_entry["record"])
    selected_resolved = selected_status.get("resolved")
    if selected_resolved is None:
        return []
    selected_owners = _owner_map(selected_resolved)
    selected_targets = {
        channel["target"]["id"]: channel
        for channel in selected_resolved["channels"]
    }
    rows = []
    for event_id, entry in book["events"].items():
        if event_id == selected_id:
            continue
        record = entry["record"]
        other_status = _event_status(context, event_id, entry)
        affected = []
        overlap = []
        identity_mismatch = []
        missing = []
        for target_id, target in record["targets"].items():
            key = (target["owner"]["type"], target["owner"]["name"])
            if key not in selected_owners:
                continue
            before = selected_owners[key]
            affected.append(target_id)
            if (target["action_name"] != before["action"].name
                    or target["slot_identifier"] != before["slot"].identifier
                    or target["slot_type"] != before["slot"].target_id_type):
                identity_mismatch.append(target_id)
            for binding in record["bindings"]:
                if binding["target"] != target_id:
                    continue
                frame = _frame_binding(record, binding)
                if (target_id in selected_targets
                        and selected_times["start"] - EPSILON <= frame
                        <= selected_times["end"] + EPSILON):
                    overlap.append({
                        "target": target_id,
                        "frame": frame,
                        "selected_window": [
                            selected_times["start"], selected_times["end"]],
                    })
        rows.append({
            "event_id": event_id,
            "status": other_status.get("status"),
            "contact_known": bool(other_status.get("contact_known")),
            "contact": (target_contact if event_id == selected_id
                        else _record_event(record)["contact"]),
            "affected_targets": affected,
            "identity_mismatch": identity_mismatch,
            "overlap": overlap,
            "action_refresh_expected": bool(affected and not overlap),
        })
    return rows


def review(context, request):
    """Read-only project review for every action. Never writes the Scene."""
    request = contract.normalize_request(request)
    if request["action"] == "inspect":
        _fail("INVALID_REQUEST", "Use project inspect for read-only inspection")
    scene, layer = _resolve_context(context, request)
    book, raw, migrated = _load_book(scene, request["context"])
    _validate_book(book, request["context"])
    action = request["action"]
    base_statuses = _all_statuses(context, book)
    body = {
        "schema": REVIEW_SCHEMA,
        "action": action,
        "request": request,
        "book": _book_summary(book),
        "book_raw_sha256": _sha(raw) if raw is not None else None,
        "migrated_single_record": bool(migrated),
        "events": [_entry_summary(event_id, entry,
                                   base_statuses.get(event_id))
                   for event_id, entry in book["events"].items()],
        "relations": copy.deepcopy(book["relations"]),
        "legacy_review": None,
        "proposed_event": None,
        "other_events": [],
        "relation_checks": None,
        "geometry": None,
        "proposed_book": None,
        "counts": {
            "events": len(book["events"]),
            "relations": len(book["relations"]),
            "bindings": sum(len(entry["record"]["bindings"])
                            for entry in book["events"].values()),
        },
        "evaluation_boundary": (
            "Read-only project review; native key transactions remain owned by "
            "clip_event/core and no file is saved"),
    }
    if action in {"capture", "adopt"}:
        proposed, legacy, diagnostics, relation_checks = _capture_proposed_book(
            context, scene, layer, book, request)
        event_id = request["event_id"]
        proposed_entry = proposed["events"][event_id]
        body["legacy_review"] = legacy
        body["geometry"] = diagnostics
        body["relation_checks"] = relation_checks
        body["proposed_book"] = _book_summary(proposed)
        body["proposed_event"] = _entry_summary(
            event_id, proposed_entry,
            {"status": "ready", "contact_known": True, "drift_targets": [],
             "identity_changed": False, "diagnostics": diagnostics})
        body["counts"].update({
            "events": len(proposed["events"]),
            "relations": len(proposed["relations"]),
            "channels": legacy["counts"]["channels"],
            "bindings": sum(len(entry["record"]["bindings"])
                            for entry in proposed["events"].values()),
        })
        return _seal_review(body)

    if action == "retime":
        event_id = request["event_id"]
        if event_id not in book["events"]:
            _fail("NOT_FOUND", "Retime requires an existing project event id",
                  {"event_id": event_id})
        entry = book["events"][event_id]
        if entry["locked"]:
            _fail("CONFLICT",
                  "Retime cannot move an explicitly contact-locked project event",
                  {"event_id": event_id})
        target_contact = float(request["clip"]["event"]["target_contact"])
        selected_status = base_statuses[event_id]
        other = _analyze_other_events(
            context, book, event_id, selected_status, target_contact)
        overlap = [row for row in other if row["overlap"]]
        if overlap:
            _fail("CONFLICT",
                  "Retime overlaps keys bound by another project event",
                  {"events": overlap})
        contacts = _contact_map(
            context, book, base_statuses, {event_id: target_contact})
        relation_checks = _require_relations(book["relations"], contacts)
        _diagnostics(
            context, scene, layer,
            _record_event_times(entry["record"]),
            entry["contact_intent"], entry["spatial_locks"], require=True)
        raw_record = clip_event._encode_record(entry["record"])
        scene_check = _scene_of(context)
        with clip_event._selected_record_view(scene_check, raw_record):
            legacy = clip_event.review(context, request["clip"])
        body["legacy_review"] = legacy
        body["other_events"] = other
        body["relation_checks"] = relation_checks
        body["proposed_event"] = {
            "event_id": event_id,
            "start": _record_event_times(entry["record"])["start"],
            "contact": target_contact,
            "end": _record_event_times(entry["record"])["end"],
            "locked": bool(entry["locked"]),
            "record": _record_summary(entry["record"]),
            "status": selected_status.get("status", "ready"),
            "contact_known": True,
            "target_contact": target_contact,
        }
        body["proposed_book"] = {
            **_book_summary(book),
            "revision": int(book["revision"]) + 1,
            "proposed_contact": {event_id: target_contact},
        }
        body["counts"].update({
            "events": len(book["events"]),
            "relations": len(book["relations"]),
            "channels": legacy["counts"]["channels"],
            "operations": legacy["counts"]["operations"],
        })
        return _seal_review(body)

    proposed, relation_checks = _static_proposed_book(context, book, request)
    body["proposed_book"] = _book_summary(proposed)
    body["relation_checks"] = relation_checks
    body["relations"] = copy.deepcopy(proposed["relations"])
    body["counts"].update({
        "events": len(proposed["events"]),
        "relations": len(proposed["relations"]),
        "channels": sum(len(entry["record"]["targets"])
                        for entry in proposed["events"].values()),
        "bindings": sum(len(entry["record"]["bindings"])
                        for entry in proposed["events"].values()),
    })
    if action == "set_lock":
        event_id = request["event_id"]
        body["proposed_event"] = {
            "event_id": event_id,
            "locked": bool(proposed["events"][event_id]["locked"]),
            "start": _record_event_times(proposed["events"][event_id]["record"])["start"],
            "contact": _record_event_times(proposed["events"][event_id]["record"])["contact"],
            "end": _record_event_times(proposed["events"][event_id]["record"])["end"],
        }
    return _seal_review(body)


def _action_bag(action, slot_identifier, slot_type):
    if action is None:
        return None
    for slot in action.slots:
        if (slot.identifier != slot_identifier
                or slot.target_id_type != slot_type):
            continue
        for layer in action.layers:
            for strip in layer.strips:
                bag = strip.channelbag(slot)
                if bag is not None:
                    return bag
    return None


def _action_curve(action, target, slot_identifier=None, slot_type=None):
    if action is None:
        return None
    if slot_identifier is None:
        slot_identifier = target["slot_identifier"]
    if slot_type is None:
        slot_type = target["slot_type"]
    bag = _action_bag(action, slot_identifier, slot_type)
    if bag is None:
        return None
    return bag.fcurves.find(target["data_path"],
                            index=int(target["array_index"]))


def _curve_semantics(curve):
    out = {}
    for key in curve.keyframe_points:
        frame = round(float(key.co.x), 9)
        if frame in out:
            _fail("CONFLICT", "Native curve has coincident key times")
        out[frame] = event_ui_state.semantic_key(native.key_state(key))
    return out


def _semantic_at(semantics, frame):
    matches = [semantic for key, semantic in semantics.items()
               if abs(float(key) - float(frame)) <= EPSILON]
    if len(matches) > 1:
        _fail("CONFLICT", "Multiple native keys fall inside the binding tolerance",
              {"frame": float(frame)})
    return matches[0] if matches else None


def _core_semantic(semantic):
    return {key: value for key, value in semantic.items()
            if key not in {"handle_left", "handle_right"}}


def _outside_keys_preserved(old_keys, new_keys, start, end):
    old_outside = {frame: semantic for frame, semantic in old_keys.items()
                   if not (start - EPSILON <= frame <= end + EPSILON)}
    new_outside = {frame: semantic for frame, semantic in new_keys.items()
                   if not (start - EPSILON <= frame <= end + EPSILON)}
    if set(old_outside) != set(new_outside):
        return False, {
            "only_old": sorted(set(old_outside) - set(new_outside))[:8],
            "only_new": sorted(set(new_outside) - set(old_outside))[:8],
        }
    local_start = start - 2.0
    local_end = end + 2.0
    changed = []
    for frame in sorted(old_outside):
        old = old_outside[frame]
        new = new_outside[frame]
        if local_start - EPSILON <= frame <= local_end + EPSILON:
            if _core_semantic(old) != _core_semantic(new):
                changed.append(frame)
        elif old != new:
            changed.append(frame)
    return not changed, {"changed": changed[:8]}


def _capture_prior(context, book, event_id):
    entry = book["events"][event_id]
    request = _clip_capture_request(entry["record"])
    resolved = clip_event._resolve_participants(context, request)
    clip_event._collect_window(resolved, request)
    clip_event._match_record_targets(entry["record"], resolved)
    owners = {}
    for channel in resolved["channels"]:
        owner = channel["owner"]
        pointer = owner.as_pointer()
        if pointer in owners:
            continue
        owners[pointer] = {
            "owner": owner,
            "binding": native.binding_state(owner),
            "action": channel["action"],
            "slot": channel["slot"],
            "action_name": channel["action"].name,
            "action_state": clip_event._semantic_action_state(channel["action"]),
        }
    prior_action_ids = {action.as_pointer() for action in bpy.data.actions}
    return owners, resolved, prior_action_ids


def _candidate_actions(prior_owners, prior_action_ids):
    candidates = []
    seen = set()
    for entry in prior_owners.values():
        owner = entry["owner"]
        animation_data = owner.animation_data
        action = animation_data.action if animation_data else None
        if (action is not None
                and action.as_pointer() not in prior_action_ids
                and action.as_pointer() not in seen):
            candidates.append(action)
            seen.add(action.as_pointer())
    return candidates


def _rollback_project(scene, prior_raw, prior_owners, prior_action_ids,
                      prior_action_states):
    errors = []
    candidates = _candidate_actions(prior_owners, prior_action_ids)
    for pointer in sorted(prior_owners):
        entry = prior_owners[pointer]
        try:
            native.restore_binding(entry["owner"], entry["binding"])
        except BaseException as exc:
            errors.append({"owner": entry["owner"].name,
                           "step": "restore_binding", "error": str(exc)})
    for action in candidates:
        try:
            if action.as_pointer() in prior_action_ids:
                continue
            if bpy.data.actions.get(action.name) == action:
                bpy.data.actions.remove(action)
        except BaseException as exc:
            errors.append({"action": getattr(action, "name", "?"),
                           "step": "remove_candidate", "error": str(exc)})
    try:
        _restore_property(scene, prior_raw)
        if _read_property(scene) != prior_raw:
            raise WorkflowError("ROLLBACK_FAILED",
                                "Project store did not restore")
    except BaseException as exc:
        errors.append({"step": "restore_store", "error": str(exc)})
    for pointer in sorted(prior_owners):
        entry = prior_owners[pointer]
        try:
            if native.binding_state(entry["owner"]) != entry["binding"]:
                raise WorkflowError(
                    "ROLLBACK_FAILED", "Owner binding did not restore",
                    {"owner": entry["owner"].name})
        except BaseException as exc:
            errors.append({"owner": entry["owner"].name,
                           "step": "verify_binding", "error": str(exc)})
    for name, state in prior_action_states.items():
        try:
            action = bpy.data.actions.get(name)
            if action is None or clip_event._semantic_action_state(action) != state:
                raise WorkflowError(
                    "ROLLBACK_FAILED", "Source Action did not restore",
                    {"action": name})
        except BaseException as exc:
            errors.append({"action": name, "step": "verify_action",
                           "error": str(exc)})
    if errors:
        raise WorkflowError(
            "ROLLBACK_FAILED",
            "Project late-failure rollback could not restore every binding/store",
            {"rollback_errors": errors})


def _refresh_other_records(context, book, selected_id, selected_target_ids,
                           selected_times, before_owners, after_owners):
    transitions = {}
    for key, before in before_owners.items():
        after = after_owners.get(key)
        if after is None:
            _fail("CONFLICT", "Retime owner disappeared from the participant set",
                  {"owner": list(key)})
        if (before["action"].name != after["action"].name
                or before["slot"].identifier != after["slot"].identifier
                or before["slot"].target_id_type != after["slot"].target_id_type):
            transitions[key] = {"before": before, "after": after}
    changes = []
    stale_events = set()
    refreshed_events = set()
    for event_id, entry in book["events"].items():
        if event_id == selected_id:
            continue
        record = entry["record"]
        before_bindings = copy.deepcopy(record["bindings"])
        before_targets = copy.deepcopy(record["targets"])
        event_refreshed = False
        event_stale = False
        for target_id, target in record["targets"].items():
            key = (target["owner"]["type"], target["owner"]["name"])
            transition = transitions.get(key)
            if transition is None:
                continue
            before = transition["before"]
            after = transition["after"]
            if (target["action_name"] != before["action"].name
                    or target["slot_identifier"] != before["slot"].identifier
                    or target["slot_type"] != before["slot"].target_id_type):
                event_stale = True
                changes.append({
                    "event_id": event_id, "target": target_id,
                    "status": "skipped_identity",
                    "reason": "recorded Action/slot identity was already stale",
                })
                continue
            old_curve = _action_curve(
                before["action"], target, before["slot"].identifier,
                before["slot"].target_id_type)
            new_curve = _action_curve(
                after["action"], target, after["slot"].identifier,
                after["slot"].target_id_type)
            if old_curve is None or new_curve is None:
                event_stale = True
                changes.append({
                    "event_id": event_id, "target": target_id,
                    "status": "skipped_curve",
                    "reason": "corresponding native curve is missing",
                })
                continue
            bindings = [binding for binding in record["bindings"]
                        if binding["target"] == target_id]
            overlap = []
            for binding in bindings:
                frame = _frame_binding(record, binding)
                if (target_id in selected_target_ids
                        and selected_times["start"] - EPSILON <= frame
                        <= selected_times["end"] + EPSILON):
                    overlap.append({"target": target_id, "frame": frame})
            if overlap:
                _fail("CONFLICT",
                      "Retime overlaps keys bound by another project event",
                      {"event_id": event_id, "bindings": overlap})
            old_keys = _curve_semantics(old_curve)
            new_keys = _curve_semantics(new_curve)
            if target_id in selected_target_ids:
                preserved, mismatch = _outside_keys_preserved(
                    old_keys, new_keys, selected_times["start"],
                    selected_times["end"])
                if not preserved:
                    _fail("CONFLICT",
                          "Copied Action did not preserve a target curve outside "
                          "the selected retime window",
                          {"event_id": event_id, "target": target_id,
                           **mismatch,
                           "window": [selected_times["start"],
                                      selected_times["end"]]})
            elif old_keys != new_keys:
                only_old = sorted(set(old_keys) - set(new_keys))
                only_new = sorted(set(new_keys) - set(old_keys))
                changed = [
                    frame for frame in sorted(set(old_keys) & set(new_keys))
                    if old_keys[frame] != new_keys[frame]
                ]
                _fail("CONFLICT",
                      "Copied Action did not preserve an unrelated target curve",
                      {"event_id": event_id, "target": target_id,
                       "only_old": only_old[:8], "only_new": only_new[:8],
                       "changed": changed[:8]})
            missing = []
            changed = []
            for binding in bindings:
                frame = _frame_binding(record, binding)
                old_key = _semantic_at(old_keys, frame)
                new_key = _semantic_at(new_keys, frame)
                if old_key is None or new_key is None:
                    missing.append(frame)
                    continue
                if old_key != new_key:
                    changed.append(frame)
                if new_key != event_ui_state.semantic_key(binding["expected_key"]):
                    event_stale = True
            if missing or changed:
                event_stale = True
                changes.append({
                    "event_id": event_id, "target": target_id,
                    "status": "skipped_keys",
                    "missing_frames": missing,
                    "changed_frames": changed,
                })
                continue
            target.update({
                "action_name": after["action"].name,
                "slot_identifier": after["slot"].identifier,
                "slot_type": after["slot"].target_id_type,
            })
            event_refreshed = True
            changes.append({
                "event_id": event_id, "target": target_id,
                "status": "refreshed",
                "before_action": before["action"].name,
                "after_action": after["action"].name,
                "before_slot": before["slot"].identifier,
                "after_slot": after["slot"].identifier,
                "stale": event_stale,
            })
        if record["bindings"] != before_bindings:
            _fail("CONFLICT", "Other event bindings changed during identity refresh",
                  {"event_id": event_id})
        for target_id, target in record["targets"].items():
            old = before_targets[target_id]
            for field in ("id", "owner", "data_path", "array_index", "sharing",
                          "domain"):
                if target[field] != old[field]:
                    _fail("CONFLICT",
                          "Other event target address changed during identity refresh",
                          {"event_id": event_id, "target": target_id,
                           "field": field})
        event_ui_state.check_clip_state(record)
        if event_refreshed:
            refreshed_events.add(event_id)
        if event_stale:
            stale_events.add(event_id)
    return {
        "changes": changes,
        "refreshed_events": sorted(refreshed_events),
        "stale_events": sorted(stale_events),
        "transitions": [
            {"owner": list(key),
             "before_action": value["before"]["action"].name,
             "after_action": value["after"]["action"].name}
            for key, value in sorted(transitions.items())
        ],
    }


def _execute_static(context, request, fresh, checkpoint):
    scene, layer = _resolve_context(context, request)
    prior_raw = _read_property(scene)
    prior_sha = _sha(prior_raw) if prior_raw is not None else None
    if prior_sha != fresh.get("book_raw_sha256"):
        _fail("CONFLICT", "Animation event store changed after review")
    book, _, _ = _load_book(scene, request["context"])
    proposed, relation_checks = _static_proposed_book(context, book, request)
    try:
        _checkpoint(checkpoint)
        _write_property(scene, _encode_book(proposed))
        _checkpoint(checkpoint)
        if _decode_book_text(_read_property(scene), request["context"]) != proposed:
            _fail("VALIDATION_FAILED",
                  "Project store readback differs from the transaction")
        statuses = _all_statuses(context, proposed, diagnostics=True)
        return _project_result(request, proposed, statuses, None, None,
                               relation_checks, False,
                               fresh.get("review_sha256"))
    except BaseException as error:
        rollback_errors = []
        try:
            _restore_property(scene, prior_raw)
            if _read_property(scene) != prior_raw:
                raise WorkflowError("ROLLBACK_FAILED",
                                    "Project store did not restore")
        except BaseException as rollback_error:
            rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise WorkflowError(
                "ROLLBACK_FAILED",
                "Project static rollback could not restore the store",
                {"original_error": str(error),
                 "rollback_errors": rollback_errors}) from error
        raise


def _execute_capture(context, request, fresh, checkpoint):
    scene, layer = _resolve_context(context, request)
    prior_raw = _read_property(scene)
    prior_sha = _sha(prior_raw) if prior_raw is not None else None
    if prior_sha != fresh.get("book_raw_sha256"):
        _fail("CONFLICT", "Animation event store changed after review")
    book, _, _ = _load_book(scene, request["context"])
    _validate_book(book, request["context"])
    event_id = request["event_id"]
    action = request["action"]
    if action == "capture" and event_id in book["events"]:
        _fail("CONFLICT", "Capture conflicts with an existing project event id",
              {"event_id": event_id})
    if action == "adopt" and event_id not in book["events"]:
        _fail("NOT_FOUND", "Adopt requires an existing project event id",
              {"event_id": event_id})
    legacy = None
    try:
        with clip_event._selected_record_view(scene, None) as view:
            legacy = clip_event.execute(
                context, fresh["legacy_review"], checkpoint=checkpoint)
        if _read_property(scene) != prior_raw:
            _fail("CONFLICT",
                  "Canonical Scene store changed during single-event capture")
        if not view["written"] or view["text"] is None:
            _fail("VALIDATION_FAILED", "Single-event capture produced no record")
        new_record = clip_event._decode_record(view["text"])
        if _record_event(new_record)["id"] != event_id:
            _fail("CONFLICT", "Committed record event id differs from the request")
        times = _record_event_times(new_record)
        existing = book["events"].get(event_id)
        if action == "capture":
            locked = bool(request.get("lock_contact", False))
            intent = request.get("contact_intent")
            spatial = request.get("spatial_locks", [])
        else:
            old_times = _record_event_times(existing["record"])
            locked = (bool(request["lock_contact"])
                      if "lock_contact" in request else bool(existing["locked"]))
            if (existing["locked"] and times["contact"] != old_times["contact"]
                    and locked):
                _fail("CONFLICT",
                      "Adopt cannot move the contact of an explicitly locked event",
                      {"event_id": event_id,
                       "contact": old_times["contact"],
                       "target_contact": times["contact"]})
            intent = (request["contact_intent"] if "contact_intent" in request
                      else existing["contact_intent"])
            spatial = (request["spatial_locks"] if "spatial_locks" in request
                       else existing["spatial_locks"])
        diagnostics = _diagnostics(
            context, scene, layer, times, intent, spatial, require=True)
        proposed = copy.deepcopy(book)
        proposed["events"][event_id] = {
            "record": new_record,
            "locked": locked,
            "contact_intent": copy.deepcopy(intent),
            "spatial_locks": copy.deepcopy(spatial),
        }
        proposed["revision"] = int(book["revision"]) + 1
        _validate_book(proposed)
        statuses = _all_statuses(context, proposed)
        contacts = _contact_map(
            context, proposed, statuses, {event_id: times["contact"]})
        relation_checks = _require_relations(proposed["relations"], contacts)
        _checkpoint(checkpoint)
        _write_property(scene, _encode_book(proposed))
        _checkpoint(checkpoint)
        if _decode_book_text(_read_property(scene), request["context"]) != proposed:
            _fail("VALIDATION_FAILED",
                  "Project store readback differs from the transaction")
        statuses = _all_statuses(context, proposed, diagnostics=True)
        return _project_result(request, proposed, statuses, legacy, None,
                               relation_checks, False,
                               fresh.get("review_sha256"))
    except BaseException as error:
        rollback_errors = []
        try:
            _restore_property(scene, prior_raw)
            if _read_property(scene) != prior_raw:
                raise WorkflowError("ROLLBACK_FAILED",
                                    "Project store did not restore")
        except BaseException as rollback_error:
            rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise WorkflowError(
                "ROLLBACK_FAILED",
                "Project capture/adopt rollback could not restore the store",
                {"original_error": str(error),
                 "rollback_errors": rollback_errors}) from error
        raise


def _execute_retime(context, request, fresh, checkpoint):
    scene, layer = _resolve_context(context, request)
    prior_raw = _read_property(scene)
    prior_sha = _sha(prior_raw) if prior_raw is not None else None
    if prior_sha != fresh.get("book_raw_sha256"):
        _fail("CONFLICT", "Animation event store changed after review")
    book, _, _ = _load_book(scene, request["context"])
    _validate_book(book, request["context"])
    event_id = request["event_id"]
    if event_id not in book["events"]:
        _fail("NOT_FOUND", "Retime requires an existing project event id",
              {"event_id": event_id})
    entry = book["events"][event_id]
    if entry["locked"]:
        _fail("CONFLICT",
              "Retime cannot move an explicitly contact-locked project event",
              {"event_id": event_id})
    target_contact = float(request["clip"]["event"]["target_contact"])
    statuses = _all_statuses(context, book)
    contacts = _contact_map(
        context, book, statuses, {event_id: target_contact})
    relation_checks = _require_relations(book["relations"], contacts)
    _diagnostics(
        context, scene, layer, _record_event_times(entry["record"]),
        entry["contact_intent"], entry["spatial_locks"], require=True)
    prior_owners = {}
    prior_action_ids = set()
    prior_action_states = {}
    selected_times = _record_event_times(entry["record"])
    selected_target_ids = set()
    before_owners = {}
    try:
        prior_owners, resolved, prior_action_ids = _capture_prior(
            context, book, event_id)
        prior_action_states = {
            value["action_name"]: value["action_state"]
            for value in prior_owners.values()
        }
        before_owners = _owner_map(resolved)
        selected_target_ids = {
            channel["target"]["id"] for channel in resolved["channels"]}
        raw_record = clip_event._encode_record(entry["record"])
        with clip_event._selected_record_view(scene, raw_record) as view:
            legacy = clip_event.execute(
                context, fresh["legacy_review"], checkpoint=checkpoint)
        if _read_property(scene) != prior_raw:
            _fail("CONFLICT",
                  "Canonical Scene store changed during single-event retime")
        if not view["written"] or view["text"] is None:
            _fail("VALIDATION_FAILED", "Single-event retime produced no record")
        new_record = clip_event._decode_record(view["text"])
        new_times = _record_event_times(new_record)
        if new_times["id"] != event_id:
            _fail("CONFLICT", "Retimed record event id changed")
        if float(new_times["contact"]) != target_contact:
            _fail("CONFLICT", "Retimed record contact differs from target_contact",
                  {"actual": new_times["contact"],
                   "requested": target_contact})
        after_request = _clip_capture_request(new_record)
        after_resolved = clip_event._resolve_participants(context, after_request)
        clip_event._collect_window(after_resolved, after_request)
        clip_event._match_record_targets(new_record, after_resolved)
        after_owners = _owner_map(after_resolved)
        proposed = copy.deepcopy(book)
        proposed["events"][event_id]["record"] = new_record
        if "contact_intent" in request:
            proposed["events"][event_id]["contact_intent"] = copy.deepcopy(
                request["contact_intent"])
        if "spatial_locks" in request:
            proposed["events"][event_id]["spatial_locks"] = copy.deepcopy(
                request["spatial_locks"])
        refresh = _refresh_other_records(
            context, proposed, event_id, selected_target_ids, selected_times,
            before_owners, after_owners)
        proposed["revision"] = int(book["revision"]) + 1
        _validate_book(proposed)
        statuses_after = _all_statuses(context, proposed)
        contacts_after = _contact_map(context, proposed, statuses_after)
        relation_checks_after = _require_relations(
            proposed["relations"], contacts_after)
        _diagnostics(
            context, scene, layer, new_times,
            proposed["events"][event_id]["contact_intent"],
            proposed["events"][event_id]["spatial_locks"], require=True)
        _checkpoint(checkpoint)
        _write_property(scene, _encode_book(proposed))
        _checkpoint(checkpoint)
        if _decode_book_text(_read_property(scene), request["context"]) != proposed:
            _fail("VALIDATION_FAILED",
                  "Project store readback differs from the transaction")
        statuses_after = _all_statuses(context, proposed, diagnostics=True)
        return _project_result(
            request, proposed, statuses_after, legacy, refresh,
            relation_checks_after, True, fresh.get("review_sha256"))
    except BaseException as error:
        _rollback_project(scene, prior_raw, prior_owners, prior_action_ids,
                          prior_action_states)
        raise


def _project_result(request, book, statuses, legacy_result, refresh,
                    relation_checks, native_changed, review_sha256=None):
    event_rows = [
        _entry_summary(event_id, entry, statuses.get(event_id))
        for event_id, entry in book["events"].items()
    ]
    action_changes = copy.deepcopy(
        legacy_result.get("action_changes", [])) if legacy_result else []
    result = {
        "schema": RESULT_SCHEMA,
        "status": "committed",
        "action": request["action"],
        "book": _book_summary(book),
        "events": event_rows,
        "record_summaries": {
            event_id: _record_summary(entry["record"])
            for event_id, entry in book["events"].items()
        },
        "relations": copy.deepcopy(book["relations"]),
        "relation_checks": copy.deepcopy(relation_checks),
        "counts": {
            "events": len(book["events"]),
            "relations": len(book["relations"]),
            "bindings": sum(len(entry["record"]["bindings"])
                            for entry in book["events"].values()),
            "channels": sum(len(entry["record"]["targets"])
                            for entry in book["events"].values()),
        },
        "action_changes": action_changes,
        "identity_refresh": (None if refresh is None else {
            "changes": copy.deepcopy(refresh["changes"]),
            "refreshed_events": list(refresh["refreshed_events"]),
            "stale_events": list(refresh["stale_events"]),
            "transitions": copy.deepcopy(refresh["transitions"]),
        }),
        "native_animation_changed": bool(native_changed),
        "review_sha256": review_sha256,
        "follow_up": {
            "project_saved": False,
            "next_step": "Save a new candidate; execute never saves files",
        },
        "evaluation_boundary": (
            "Native FCurve keys/Action bindings and explicit evaluated geometry "
            "diagnostics only; no render or visual artistic acceptance"),
    }
    if legacy_result is not None:
        result["legacy_result"] = {
            "schema": legacy_result.get("schema"),
            "status": legacy_result.get("status"),
            "record_sha256": legacy_result.get("record_sha256"),
            "counts": copy.deepcopy(legacy_result.get("counts")),
            "protection": copy.deepcopy(legacy_result.get("protection")),
            "key_witness": copy.deepcopy(legacy_result.get("key_witness")),
            "locks": copy.deepcopy(legacy_result.get("locks")),
            "contacts": copy.deepcopy(legacy_result.get("contacts")),
        }
    return result


def execute(context, reviewed, checkpoint=None):
    """Commit a reviewed project action after a fresh review and late rollback."""
    if not isinstance(reviewed, dict) or reviewed.get("schema") != REVIEW_SCHEMA:
        _fail("INVALID_REQUEST", "execute requires a project review")
    request = contract.normalize_request(reviewed.get("request"))
    if request["action"] == "inspect":
        _fail("INVALID_REQUEST", "Project inspect is read-only")
    fresh = review(context, request)
    if fresh.get("review_sha256") != reviewed.get("review_sha256"):
        _fail("CONFLICT", "Project review is stale; run review again",
              {"reviewed": reviewed.get("review_sha256"),
               "current": fresh.get("review_sha256")})
    if request["action"] in {"set_relations", "set_lock"}:
        return _execute_static(context, request, fresh, checkpoint)
    if request["action"] in {"capture", "adopt"}:
        return _execute_capture(context, request, fresh, checkpoint)
    return _execute_retime(context, request, fresh, checkpoint)


def inspect(context, request):
    """Read-only project inspection with ready/stale and geometry diagnostics."""
    request = contract.normalize_request(request)
    if request["action"] != "inspect":
        _fail("INVALID_REQUEST", "Project inspect requires action inspect")
    scene, layer = _resolve_context(context, request)
    book, raw, migrated = _load_book(scene, request["context"])
    _validate_book(book, request["context"])
    event_id = request.get("event_id")
    if event_id is not None and event_id not in book["events"]:
        _fail("NOT_FOUND", "Inspect event id does not exist",
              {"event_id": event_id})
    statuses = _all_statuses(context, book, diagnostics=True)
    contacts = _contact_map(context, book, statuses)
    relation_checks = _relation_checks(book["relations"], contacts)
    events = [
        _entry_summary(row_id, entry, statuses.get(row_id))
        for row_id, entry in book["events"].items()
    ]
    state = ("empty" if not book["events"] else
             "legacy_migrated" if migrated else "project")
    return {
        "schema": INSPECT_SCHEMA,
        "action": "inspect",
        "status": state,
        "selected_event_id": event_id,
        "book": _book_summary(book),
        "book_raw_sha256": _sha(raw) if raw is not None else None,
        "migrated_single_record": bool(migrated),
        "events": events,
        "relations": copy.deepcopy(book["relations"]),
        "relation_checks": relation_checks,
        "counts": {
            "events": len(book["events"]),
            "relations": len(book["relations"]),
            "bindings": sum(len(entry["record"]["bindings"])
                            for entry in book["events"].values()),
        },
        "native_animation_changed": False,
        "evaluation_boundary": (
            "Read-only native Action/key inspection and evaluated geometry "
            "diagnostics; frame/subframe restored; no record is created"),
    }


__all__ = [
    "BOOK_SCHEMA", "REVIEW_SCHEMA", "RESULT_SCHEMA", "INSPECT_SCHEMA",
    "review", "execute", "inspect",
]
