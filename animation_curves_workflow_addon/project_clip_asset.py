"""Apply one four-actor project Collection with its native events and locks."""

import copy
import json
import math
import re
from pathlib import Path

from . import clip_asset as base
from . import clip_project, clip_project_contract, curve_segments, native


CLIP_SCHEMA = "animation-native-project-clip-v1"
RESULT_SCHEMA = "animation-native-project-clip-apply-result-v1"
ORIGIN = "acw_project_clip_source_name"
CLIP_FIELDS = {
    "schema", "clip_id", "title", "library", "source", "timing",
    "meters_per_blender_unit", "collection", "recovery_start",
    "actors", "lights", "events", "relations",
}
EVENT_FIELDS = {
    "id", "start", "contact", "end", "locked", "participants",
    "contact_intent", "spatial_locks",
}
ACTOR_FIELDS = {"role", "collection", "root", "rig", "template_uuid"}


def require(condition, message, code="INVALID_REQUEST"):
    if not condition:
        base._fail(code, message)


def rows(value, fields, count, label):
    require(isinstance(value, list) and len(value) == count, f"{label} requires exactly {count} rows")
    require(all(isinstance(row, dict) and set(row) == fields for row in value),
            f"{label} has missing or unknown fields")
    return value


def unique_names(values, label):
    result = [base._text(value, label, 128) for value in values]
    require(len(result) == len(set(result)), f"{label} contains duplicate names")
    return result


def _normalize(request, context):
    fields = {"schema", "clip", "instance_name", "start", "end", "scale", "translation", "yaw_quarter_turns"}
    require(isinstance(request, dict) and set(request) == fields, "Four-actor apply requires the complete clip request")
    require(request["schema"] == base.REQUEST_SCHEMA, "Unsupported clip request schema")
    req = copy.deepcopy(request)
    clip = req["clip"]
    require(isinstance(clip, dict) and set(clip) == CLIP_FIELDS, "Four-actor clip has missing or unknown fields")
    require(clip["schema"] == CLIP_SCHEMA, "Unsupported four-actor schema")
    base._canonical_uuid(clip["clip_id"], "clip_id")
    base._text(clip["title"], "title")
    base._text(clip["collection"], "collection", 128)
    base._text(req["instance_name"], "instance_name", 48)
    require(isinstance(clip["library"], dict) and set(clip["library"]) == {"file", "expected_sha256"},
            "Clip library requires file and expected_sha256")
    require(isinstance(clip["source"], dict) and set(clip["source"]) == {"file", "sha256"},
            "Clip source requires provenance file and sha256")
    timing = clip["timing"]
    require(isinstance(timing, dict) and set(timing) == {"start", "end", "fps", "fps_base"},
            "Clip timing requires start, end, fps and fps_base")
    for field in timing:
        timing[field] = base._finite(timing[field], f"timing.{field}")
    for field in ("start", "end", "scale"):
        req[field] = base._finite(req[field], field)
    pivot = clip["recovery_start"] = base._finite(clip["recovery_start"], "recovery_start")
    require(0 < timing["start"] < pivot < timing["end"], "Invalid source recovery interval")
    require(timing["fps"] > 0 and timing["fps"].is_integer() and timing["fps_base"] > 0,
            "Clip fps must be integral with a positive fps_base")
    require(req["start"] == timing["start"] and req["end"] > pivot,
            "Start must preserve locked event times and end must be after the recovery boundary")
    require(req["scale"] > 0, "Scale must be positive")
    clip["meters_per_blender_unit"] = base._finite(clip["meters_per_blender_unit"], "meters_per_blender_unit")
    require(clip["meters_per_blender_unit"] > 0, "Unit scale must be positive")
    translation = req["translation"]
    require(isinstance(translation, list) and len(translation) == 3, "Translation must be a three-number array")
    req["translation"] = [base._finite(value, "translation") for value in translation]
    require(type(req["yaw_quarter_turns"]) is int and req["yaw_quarter_turns"] in range(4),
            "Yaw must be zero through three quarter turns")
    actors = rows(clip["actors"], ACTOR_FIELDS, 4, "Actors")
    require({row["role"] for row in actors} == set("ABCD"), "Four actors must have roles A, B, C and D")
    for field in ("collection", "root", "rig", "template_uuid"):
        unique_names((actor[field] for actor in actors), f"actor.{field}")
    for actor in actors:
        require(re.fullmatch(r"[0-9a-f]{32}", actor["template_uuid"]),
                "actor.template_uuid must use 32 lowercase hexadecimal characters")
    lights = rows(clip["lights"], {"object"}, 4, "Lights")
    unique_names((row["object"] for row in lights), "light.object")
    events = rows(clip["events"], EVENT_FIELDS, 4, "Events")
    ids = set(unique_names((event["id"] for event in events), "event.id"))
    for event in events:
        require(len(event["id"]) <= 64 and ":" not in event["id"], "Source event ID must fit its instance namespace")
        test = clip_project_contract.normalize_request(_capture_request(context, event))
        require(timing["start"] <= test["clip"]["event"]["start"] < test["clip"]["event"]["contact"]
                < test["clip"]["event"]["end"] < pivot,
                "Four-actor events must lie in the fixed timing prefix")
    relations = clip_project_contract.normalize_relations(clip["relations"])
    for relation in relations:
        names = ("before", "after") if relation["type"] == "after" else ("a", "b")
        require(all(relation[name] in ids for name in names),
                "Project clip conditions must reference events from the same clip")
    clip["relations"] = relations
    return req


def _capture_request(context, event):
    clip_event = {key: event[key] for key in ("id", "start", "contact", "end")}
    return {
        "schema": clip_project_contract.SCHEMA,
        "action": "capture", "context": context, "event_id": event["id"],
        "lock_contact": event["locked"], "contact_intent": event["contact_intent"],
        "spatial_locks": event["spatial_locks"],
        "clip": {"schema": clip_project.CLIP_SCHEMA, "action": "capture", "context": context,
                 "event": clip_event, "participants": event["participants"], "locks": []},
    }


def _origin_map(items, label):
    by_name = {}
    for item in items:
        name = base._text(item.get(ORIGIN), label, 128)
        require(name not in by_name, f"Duplicate {label}: {name}", "CONFLICT")
        by_name[name] = item
    return by_name


def _recovery(curve, pivot, old_end, new_end):
    states = [native.key_state(key) for key in curve.keyframe_points]
    if new_end == old_end or states[-1]["co"][0] <= pivot:
        return
    ratio = (new_end - pivot) / (old_end - pivot)
    if states[0]["co"][0] < pivot:
        try:
            curve_segments.insert_boundary(states, pivot)
        except ValueError as exc:
            base._fail("UNSUPPORTED", f"Cannot split recovery curve {curve.data_path}: {exc}")
    for key in states:
        position = key["co"][0]
        if position < pivot:
            continue
        if position == pivot:
            key["handle_right"][0] = pivot + (key["handle_right"][0] - pivot) * ratio
            key["handle_left_type"] = key["handle_right_type"] = "FREE"
        else:
            for field in ("co", "co_ui", "handle_left", "handle_right"):
                key[field][0] = pivot + (key[field][0] - pivot) * ratio
    curve_segments.write_states(curve, states)


def _world_transform(req):
    from mathutils import Matrix

    quarter = req["yaw_quarter_turns"]
    cosine, sine = ((1, 0), (0, 1), (-1, 0), (0, -1))[quarter]
    scale = req["scale"]
    x, y, z = req["translation"]
    world = Matrix(((scale * cosine, -scale * sine, 0, x),
                    (scale * sine, scale * cosine, 0, y),
                    (0, 0, scale, z), (0, 0, 0, 1)))
    mapping = {"scale": scale, "quarter_matrix": ((cosine, -sine), (sine, cosine)),
               "translation": req["translation"], "yaw_radians": quarter * math.pi / 2}
    return world, mapping


def _project_events(context, scene, scoped, req, wrapper, objects, materials, prior_relations):
    clip = req["clip"]
    namespace = wrapper.name
    event_ids = {event["id"]: namespace + ":" + event["id"] for event in clip["events"]}
    entries = []
    for recipe in clip["events"]:
        event = copy.deepcopy(recipe)
        source_id = event["id"]
        event["id"] = event_ids[source_id]
        for kind, names in event["participants"].items():
            available = materials if kind == "materials" else objects
            require(all(name in available for name in names), f"Event participant leaves the appended clip: {source_id}")
            event["participants"][kind] = [available[name].name for name in names]
        refs = [lock["point"] for lock in event["spatial_locks"]]
        if event["contact_intent"] is not None:
            refs += [event["contact_intent"]["a"], event["contact_intent"]["b"]]
        for ref in refs:
            require(ref["object"] in objects, f"Event reference leaves the appended clip: {source_id}")
            ref["object"] = objects[ref["object"]].name
        capture = _capture_request(scoped, event)
        capture["clip"]["locks"] = [req["start"], req["end"]]
        clip_project.execute(context, clip_project.review(context, capture))
        entries.append({"source_id": source_id, "id": event["id"]})
    relations = copy.deepcopy(clip["relations"])
    for relation in relations:
        names = ("before", "after") if relation["type"] == "after" else ("a", "b")
        for key in names:
            relation[key] = event_ids[relation[key]]
    if relations:
        link = {"schema": clip_project_contract.SCHEMA, "action": "set_relations",
                "context": scoped, "relations": prior_relations + relations}
        clip_project.execute(context, clip_project.review(context, link))
    return entries


def apply_project_clip(context, request):
    import bpy
    from mathutils import Vector

    scene, layer = base._resolve_context(bpy, context)
    scoped = {"scene": scene.name, "view_layer": layer.name}
    req = _normalize(request, scoped)
    clip = req["clip"]
    base._validate_scene(scene, req)
    path = Path(base._text(clip["library"]["file"], "library.file", 2048))
    require(path.is_absolute() and path.suffix.lower() == ".blend", "Library must use an absolute .blend path")
    digest = base._sha256_file(path, "library.file")
    require(digest == clip["library"]["expected_sha256"], "Frozen library SHA256 changed", "CONFLICT")
    prior_book = clip_project._read_property(scene)
    existing, _, _ = clip_project._load_book(scene, scoped)
    if prior_book is not None:
        require(json.loads(prior_book).get("schema") == clip_project.BOOK_SCHEMA,
                "Complete the legacy project migration before appending a project clip", "CONFLICT")
    world, mapping = _world_transform(req)
    for event in clip["events"]:
        for lock in event["spatial_locks"]:
            point = Vector(lock["world"])
            require((world @ point - point).length <= min(0.00001, lock["tolerance"] / 2),
                    f"Placement would move the absolute spatial lock for {event['id']}", "CONFLICT")

    frame, subframe = scene.frame_current, scene.frame_subframe
    before = base._id_collections(bpy)
    before_trees = base._before_node_trees(bpy)
    before_ids = set(bpy.data.user_map())
    before_libraries = {item.as_pointer() for item in bpy.data.libraries}
    existing_uuids = {
        item["ac_instance_uuid"] for item in before_ids
        if "ac_instance_uuid" in item.keys()
    }
    existing_uuids.update(actor["template_uuid"] for actor in clip["actors"])
    wrapper = None
    try:
        with bpy.data.libraries.load(str(path), link=False, relative=False) as (source, destination):
            require(clip["collection"] in source.collections, "Frozen project Collection is missing", "NOT_FOUND")
            destination.collections = [clip["collection"]]
        wrapper, = destination.collections
        require(wrapper is not None, "Frozen project Collection was not appended", "NOT_FOUND")
        base._assert_new(wrapper, before, "collections", before_trees)
        require(wrapper.asset_data is not None and wrapper.get(ORIGIN) == clip["collection"],
                "Project Collection is not the marked frozen asset", "CONFLICT")
        wrapper.name = req["instance_name"]
        if any(name.startswith(wrapper.name + ":") for name in existing["events"]):
            wrapper.name = req["instance_name"] + "_" + base._new_uuid(existing_uuids)[:8]

        collections = _origin_map(wrapper.children_recursive, "collection origin")
        objects = _origin_map(wrapper.all_objects, "object origin")
        action_rows, action_pointers = [], set()
        actors, owned, materials, roots = [], set(), set(), []
        for spec in clip["actors"]:
            collection = collections.get(spec["collection"])
            require(collection is not None and not collection.children, "Actor Collection is missing or nested", "CONFLICT")
            base._assert_new(collection, before, "collections", before_trees)
            structure = base._actor_structure(collection, before, before_trees, spec, action_rows, action_pointers)
            root, rig = structure["root"], structure["rig"]
            require(root.get(ORIGIN) == spec["root"] and rig.get(ORIGIN) == spec["rig"],
                    "Actor root or rig does not match the frozen role", "CONFLICT")
            require(not set(structure["objects"]) & owned, "Actor objects are shared", "CONFLICT")
            for obj in structure["objects"]:
                require(not obj.constraints and obj.instance_type == "NONE",
                        "Constraints and instancing are outside project clip support", "UNSUPPORTED")
                require(obj.type in {"MESH", "ARMATURE", "EMPTY"}, "Unexpected actor object type", "UNSUPPORTED")
                if obj.type == "MESH":
                    require(obj.parent == rig and obj.data.shape_keys is None
                            and all(modifier.type == "ARMATURE" and modifier.object == rig for modifier in obj.modifiers),
                            "Mesh depends on geometry outside its actor", "UNSUPPORTED")
            owned.update(structure["objects"])
            materials.update(structure["materials"])
            roots.append(root)
            new_uuid = base._new_uuid(existing_uuids)
            for item in base._tagged_actor_ids(collection, structure):
                item["ac_instance_uuid"] = new_uuid
            base._update_metadata(collection, root, spec["template_uuid"], new_uuid)
            actors.append({"role": spec["role"], "collection": collection.name,
                           "root": root.name, "rig": rig.name, "instance_uuid": new_uuid})
        lights = []
        for spec in clip["lights"]:
            light = objects.get(spec["object"])
            require(light is not None and light.type == "LIGHT" and light.parent is None
                    and not light.constraints and not light.animation_data and light.data is not None,
                    "Frozen clip requires static light transforms and local Light data", "UNSUPPORTED")
            base._assert_new(light, before, "objects", before_trees)
            base._assert_new(light.data, before, "lights", before_trees)
            if light.data.animation_data and light.data.animation_data.action:
                base._collect_action(light.data.animation_data.action, light.data, "LIGHT", "light",
                                     "light_data", before, before_trees, action_rows, action_pointers)
            owned.add(light)
            lights.append(light)
        require(owned == set(objects.values()), "Unexpected extra objects in appended Collection", "CONFLICT")
        require({row["action"] for row in action_rows} ==
                {item for item in set(bpy.data.user_map()) - before_ids if isinstance(item, bpy.types.Action)},
                "Appended Collection has unowned Actions", "CONFLICT")
        for category, item in base._new_ids(bpy, before, before_trees):
            require(category not in {"images", "textures", "cameras", "worlds", "texts"}
                    and item.library is None and item.override_library is None,
                    "Clip contains external or unsupported datablocks", "UNSUPPORTED")
            if getattr(item, "use_fake_user", False):
                item.use_fake_user = False
            if getattr(item, "asset_data", None) is not None or "asset_id" in item.keys():
                base._clear_asset_marks(item)

        for row in action_rows:
            require(clip["timing"]["start"] <= row["source_first_frame"]
                    <= clip["recovery_start"] < row["source_last_frame"] == clip["timing"]["end"],
                    "Frozen Action span does not cover the recovery boundary", "CONFLICT")
            for curve in row["bag"].fcurves:
                _recovery(curve, clip["recovery_start"], clip["timing"]["end"], req["end"])
        for root in roots:
            action = root.animation_data.action
            row = next(row for row in action_rows if row["action"] == action)
            base._root_mapping(root, action, row, req, mapping)
        for light in lights:
            light.matrix_world = world @ light.matrix_basis
        scene.collection.children.link(wrapper)
        wrapper["acw_project_clip_id"] = clip["clip_id"]
        wrapper["acw_project_clip_library_sha256"] = digest
        layer.update()
        entries = _project_events(context, scene, scoped, req, wrapper, objects,
                                  _origin_map(materials, "material origin"), existing["relations"])
        scene.frame_set(frame, subframe=subframe)
        require(base._sha256_file(path, "library.file") == digest, "Library changed during apply", "CONFLICT")
        return {
            "schema": RESULT_SCHEMA, "collection": wrapper.name,
            "actors": actors, "lights": [{"object": item.name} for item in lights], "events": entries,
            "actions": {"count": len(action_rows),
                        "curves": sum(len(row["bag"].fcurves) for row in action_rows),
                        "keys": sum(len(curve.keyframe_points) for row in action_rows for curve in row["bag"].fcurves),
                        "names": sorted(row["action"].name for row in action_rows)},
        }
    except BaseException:
        clip_project._restore_property(scene, prior_book)
        new_ids = set(bpy.data.user_map()) - before_ids
        new_ids.update(item for item in bpy.data.libraries if item.as_pointer() not in before_libraries)
        if new_ids:
            bpy.data.batch_remove(ids=new_ids)
        scene.frame_set(frame, subframe=subframe)
        raise
