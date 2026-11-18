"""Apply one frozen native two-person clip asset into the current scene.

This module is the native implementation behind ``apply_clip(context,
request)``.  It appends exactly the four frozen library assets named by the
request, validates their asset IDs, retags the two actor instances, retimes
every owned Action with one affine map, applies the declared world-space
similarity transform, and links the result through a new wrapper Collection.

The function is transactional with respect to its own writes: on failure it
removes only IDs that did not exist before the call.  It never saves, renders,
opens a different file, changes the scene frame range/current frame/camera, or
modifies pre-existing Actions/UUIDs.

The module intentionally imports ``bpy`` lazily so the request contract can be
inspected host-side without Blender.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import struct
import uuid

try:  # Installed as animation_curves_workflow_addon.clip_asset.
    from .contract import WorkflowError
except (ImportError, ValueError):  # Local prototype / host-side import.
    class WorkflowError(RuntimeError):
        def __init__(self, code, message, details=None):
            super().__init__(message)
            self.code = code
            self.message = message
            self.details = details or {}

        def __str__(self):
            return f"{self.code}: {self.message}"


REQUEST_SCHEMA = "animation-native-clip-apply-v1"
CLIP_SCHEMA = "animation-native-clip-v1"
RESULT_SCHEMA = "animation-native-clip-apply-result-v1"
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_KEY_META = (
    "handle_left_type", "handle_right_type", "interpolation",
    "easing", "back", "amplitude", "period",
)


def _fail(code, message, details=None):
    raise WorkflowError(code, message, details or {})


def _f32(value):
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("INVALID_REQUEST", label + " must be a finite number")
    try:
        out = float(value)
    except (OverflowError, ValueError, TypeError):
        _fail("INVALID_REQUEST", label + " exceeds the finite numeric range")
    if not math.isfinite(out):
        _fail("INVALID_REQUEST", label + " must be a finite number")
    return out


def _text(value, label, maximum=512):
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        _fail("INVALID_REQUEST", label + " must be a nonempty bounded string")
    return value


def _canonical_uuid(value, label):
    if not isinstance(value, str):
        _fail("INVALID_REQUEST", label + " must be a canonical UUID string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        _fail("INVALID_REQUEST", label + " must be a canonical UUID string")
    if str(parsed) != value:
        _fail("INVALID_REQUEST", label + " must use canonical lowercase UUID form")
    return value


def _sha256_file(path, label):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        _fail("NOT_FOUND", "Unable to read " + label, {"path": path, "error": str(exc)})
    return digest.hexdigest()


def _normalize_request(request):
    if not isinstance(request, dict):
        _fail("INVALID_REQUEST", "request must be an object")
    required = {"schema", "clip", "instance_name", "start", "end",
                "scale", "translation", "yaw_quarter_turns"}
    missing = sorted(required - request.keys())
    if missing:
        _fail("INVALID_REQUEST", "request is missing required fields", {"missing": missing})
    if request["schema"] != REQUEST_SCHEMA:
        _fail("INVALID_REQUEST", "request.schema must be " + REQUEST_SCHEMA)
    out = copy.deepcopy(request)
    _text(out["instance_name"], "request.instance_name", 200)
    out["start"] = _finite(out["start"], "request.start")
    out["end"] = _finite(out["end"], "request.end")
    if not out["end"] > out["start"]:
        _fail("INVALID_REQUEST", "request.end must be greater than request.start")
    out["scale"] = _finite(out["scale"], "request.scale")
    if out["scale"] <= 0.0:
        _fail("INVALID_REQUEST", "request.scale must be positive")
    if (not isinstance(out["translation"], (list, tuple))
            or len(out["translation"]) != 3):
        _fail("INVALID_REQUEST", "request.translation must contain three numbers")
    out["translation"] = [_finite(v, "request.translation[%d]" % i)
                          for i, v in enumerate(out["translation"])]
    yaw = out["yaw_quarter_turns"]
    if isinstance(yaw, bool) or not isinstance(yaw, int):
        _fail("INVALID_REQUEST", "request.yaw_quarter_turns must be an integer")
    if not 0 <= yaw <= 3:
        _fail("INVALID_REQUEST", "request.yaw_quarter_turns must be 0..3")

    clip = out["clip"]
    if not isinstance(clip, dict):
        _fail("INVALID_REQUEST", "request.clip must be an object")
    clip_required = {"schema", "clip_id", "title", "library", "source",
                     "timing", "meters_per_blender_unit", "actors", "lights"}
    missing = sorted(clip_required - clip.keys())
    if missing:
        _fail("INVALID_REQUEST", "clip is missing required fields", {"missing": missing})
    if clip["schema"] != CLIP_SCHEMA:
        _fail("INVALID_REQUEST", "clip.schema must be " + CLIP_SCHEMA)
    _canonical_uuid(clip["clip_id"], "clip.clip_id")
    _text(clip["title"], "clip.title", 512)
    clip["meters_per_blender_unit"] = _finite(
        clip["meters_per_blender_unit"], "clip.meters_per_blender_unit")
    if clip["meters_per_blender_unit"] <= 0.0:
        _fail("INVALID_REQUEST", "clip.meters_per_blender_unit must be positive")

    library = clip["library"]
    if not isinstance(library, dict):
        _fail("INVALID_REQUEST", "clip.library must be an object")
    _text(library.get("file"), "clip.library.file", 4096)
    if not os.path.isabs(library["file"]):
        _fail("INVALID_REQUEST", "clip.library.file must be absolute")
    if not library["file"].lower().endswith(".blend"):
        _fail("INVALID_REQUEST", "clip.library.file must be a .blend file")
    _text(library.get("expected_sha256"), "clip.library.expected_sha256", 64)
    if not _SHA256.fullmatch(library["expected_sha256"]):
        _fail("INVALID_REQUEST", "clip.library.expected_sha256 must be lowercase SHA256")

    source = clip["source"]
    if not isinstance(source, dict):
        _fail("INVALID_REQUEST", "clip.source must be an object")
    _text(source.get("file"), "clip.source.file", 4096)
    _text(source.get("sha256"), "clip.source.sha256", 64)
    if not _SHA256.fullmatch(source["sha256"]):
        _fail("INVALID_REQUEST", "clip.source.sha256 must be lowercase SHA256")

    timing = clip["timing"]
    if not isinstance(timing, dict):
        _fail("INVALID_REQUEST", "clip.timing must be an object")
    for key in ("start", "end"):
        timing[key] = _finite(timing.get(key), "clip.timing." + key)
    if not timing["end"] > timing["start"]:
        _fail("INVALID_REQUEST", "clip.timing.end must be greater than start")
    timing["fps"] = _finite(timing.get("fps"), "clip.timing.fps")
    timing["fps_base"] = _finite(timing.get("fps_base"), "clip.timing.fps_base")
    if timing["fps"] <= 0.0 or timing["fps_base"] <= 0.0:
        _fail("INVALID_REQUEST", "clip.timing fps/fps_base must be positive")

    actors = clip["actors"]
    if not isinstance(actors, list) or len(actors) != 2:
        _fail("INVALID_REQUEST", "clip.actors must contain exactly two actors")
    roles, collections, asset_ids, template_uuids = set(), set(), set(), set()
    for i, actor in enumerate(actors):
        if not isinstance(actor, dict):
            _fail("INVALID_REQUEST", "clip.actors[%d] must be an object" % i)
        for key in ("role", "collection", "asset_id", "template_uuid"):
            _text(actor.get(key), "clip.actors[%d].%s" % (i, key), 512)
        if actor["role"] not in ("A", "B"):
            _fail("INVALID_REQUEST", "actor role must be A or B")
        _canonical_uuid(actor["asset_id"], "clip.actors[%d].asset_id" % i)
        if not _HEX32.fullmatch(actor["template_uuid"]):
            _fail("INVALID_REQUEST", "actor template_uuid must be 32 lowercase hex")
        if actor["role"] in roles:
            _fail("INVALID_REQUEST", "actor roles must be unique")
        if actor["collection"] in collections:
            _fail("INVALID_REQUEST", "actor collection names must be unique")
        if actor["asset_id"] in asset_ids:
            _fail("INVALID_REQUEST", "actor asset_ids must be unique")
        if actor["template_uuid"] in template_uuids:
            _fail("INVALID_REQUEST", "actor template_uuids must be unique")
        roles.add(actor["role"])
        collections.add(actor["collection"])
        asset_ids.add(actor["asset_id"])
        template_uuids.add(actor["template_uuid"])
    if roles != {"A", "B"}:
        _fail("INVALID_REQUEST", "actors must contain exactly roles A and B")

    lights = clip["lights"]
    if not isinstance(lights, list) or len(lights) != 2:
        _fail("INVALID_REQUEST", "clip.lights must contain exactly two lights")
    light_roles, light_objects = set(), set()
    for i, light in enumerate(lights):
        if not isinstance(light, dict):
            _fail("INVALID_REQUEST", "clip.lights[%d] must be an object" % i)
        for key in ("role", "object", "asset_id"):
            _text(light.get(key), "clip.lights[%d].%s" % (i, key), 512)
        if light["role"] not in ("block", "counter"):
            _fail("INVALID_REQUEST", "light role must be block or counter")
        _canonical_uuid(light["asset_id"], "clip.lights[%d].asset_id" % i)
        if light["role"] in light_roles:
            _fail("INVALID_REQUEST", "light roles must be unique")
        if light["object"] in light_objects:
            _fail("INVALID_REQUEST", "light object names must be unique")
        if light["asset_id"] in asset_ids:
            _fail("INVALID_REQUEST", "all four asset_ids must be unique")
        light_roles.add(light["role"])
        light_objects.add(light["object"])
        asset_ids.add(light["asset_id"])
    if light_roles != {"block", "counter"}:
        _fail("INVALID_REQUEST", "lights must contain exactly roles block and counter")
    if 'source_window' in out:
        window = out['source_window']
        if not isinstance(window, dict) or set(window) != {'start', 'end'}:
            _fail('INVALID_REQUEST', 'source_window requires exactly start and end')
        first = _finite(window['start'], 'source_window.start')
        last = _finite(window['end'], 'source_window.end')
        if not timing['start'] <= first < last <= timing['end']:
            _fail('INVALID_REQUEST', 'source_window must lie within the frozen clip timing')
        native_first, native_last = _f32(first), _f32(last)
        if not timing['start'] <= native_first < native_last <= timing['end']:
            _fail('INVALID_REQUEST', 'source_window collapses or exceeds the native frame range')
        out['source_window'] = {'start': native_first, 'end': native_last,
                                'requested': {'start': first, 'end': last}}
    return out


def _resolve_context(bpy, context):
    if context is None:
        _fail("INVALID_REQUEST", "context is required")
    if isinstance(context, dict):
        scene = context.get("scene")
        layer = context.get("view_layer")
    else:
        scene = getattr(context, "scene", None)
        layer = getattr(context, "view_layer", None)
    if isinstance(scene, str):
        scene = bpy.data.scenes.get(scene)
    if scene is None:
        _fail("INVALID_REQUEST", "context.scene is required")
    if bpy.data.scenes.get(scene.name) is None:
        _fail("NOT_FOUND", "context.scene is not a current scene")
    if scene.library or scene.override_library:
        _fail("UNSUPPORTED", "Linked/override scenes cannot receive a local clip")
    if isinstance(layer, str):
        layer = scene.view_layers.get(layer)
    if layer is None:
        layer = scene.view_layers[0] if len(scene.view_layers) else None
    if layer is None or scene.view_layers.get(layer.name) is None:
        _fail("NOT_FOUND", "context.view_layer does not belong to context.scene")
    return scene, layer


def _scene_state(scene):
    camera = scene.camera
    return {
        "frame_start": int(scene.frame_start),
        "frame_end": int(scene.frame_end),
        "frame_current": int(scene.frame_current),
        "frame_subframe": float(scene.frame_subframe),
        "camera_pointer": camera.as_pointer() if camera else None,
        "camera_name": camera.name if camera else None,
        "fps": int(scene.render.fps),
        "fps_base": float(scene.render.fps_base),
        "unit_scale": float(scene.unit_settings.scale_length),
    }


def _validate_scene(scene, request):
    timing = request["clip"]["timing"]
    if int(scene.render.fps) != int(timing["fps"]):
        _fail("CONFLICT", "Scene fps does not match clip timing",
              {"scene": int(scene.render.fps), "clip": timing["fps"]})
    if abs(float(scene.render.fps_base) - float(timing["fps_base"])) > 1e-9:
        _fail("CONFLICT", "Scene fps_base does not match clip timing",
              {"scene": float(scene.render.fps_base), "clip": timing["fps_base"]})
    if abs(float(scene.unit_settings.scale_length)
           - float(request["clip"]["meters_per_blender_unit"])) > 1e-9:
        _fail("CONFLICT", "Scene unit scale does not match clip meters_per_blender_unit")


def _id_collections(bpy):
    names = ("collections", "objects", "meshes", "armatures", "lights",
             "cameras", "materials", "node_groups", "actions", "images",
             "textures", "worlds", "texts")
    out = {}
    for name in names:
        coll = getattr(bpy.data, name, None)
        if coll is not None:
            out[name] = {item.as_pointer(): item for item in coll}
    return out


def _before_node_trees(bpy):
    pointers = set()
    for material in bpy.data.materials:
        tree = material.node_tree
        if tree is not None:
            pointers.add(tree.as_pointer())
    for tree in bpy.data.node_groups:
        pointers.add(tree.as_pointer())
    return pointers


def _assert_new(item, before_map, label, before_node_trees=None):
    if item is None:
        _fail("CONFLICT", "Missing " + label)
    if item.library is not None or item.override_library is not None:
        _fail("UNSUPPORTED", "Referenced " + label + " is linked/override")
    pointer = item.as_pointer()
    if before_node_trees is not None and item.bl_rna.identifier.endswith("NodeTree"):
        if pointer in before_node_trees:
            _fail("CONFLICT", label + " existed before apply", {"name": item.name})
        return
    bucket = before_map.get(label)
    if bucket is not None and pointer in bucket:
        _fail("CONFLICT", label + " existed before apply", {"name": item.name})


def _data_label(item):
    return {
        "Mesh": "meshes",
        "Armature": "armatures",
        "Light": "lights",
        "Camera": "cameras",
    }.get(item.bl_rna.identifier.split(".")[-1], "")


def _binding(action, owner, expected_type, before_map, before_node_trees):
    ad = owner.animation_data
    if ad is None or ad.action is not action:
        _fail("CONFLICT", "Owner/action binding is not the appended native binding",
              {"owner": owner.name})
    if len(ad.nla_tracks) or len(ad.drivers) or getattr(ad, "use_tweak_mode", False):
        _fail("UNSUPPORTED", "NLA/drivers/tweak mode are outside clip support",
              {"owner": owner.name})
    if ad.action_blend_type != "REPLACE" or abs(float(ad.action_influence) - 1.0) > 1e-6:
        _fail("UNSUPPORTED", "Action must be REPLACE at full influence",
              {"owner": owner.name})
    if len(action.slots) != 1:
        _fail("UNSUPPORTED", "Action must contain exactly one slot",
              {"action": action.name})
    slot = action.slots[0]
    if ad.action_slot != slot or slot.target_id_type != expected_type:
        _fail("UNSUPPORTED", "Action slot type/binding is unsupported",
              {"action": action.name, "expected": expected_type,
               "actual": slot.target_id_type})
    if len(action.layers) != 1 or len(action.layers[0].strips) != 1:
        _fail("UNSUPPORTED", "Action must contain one layer and one strip",
              {"action": action.name})
    strip = action.layers[0].strips[0]
    if strip.type != "KEYFRAME":
        _fail("UNSUPPORTED", "Only KEYFRAME action strips are supported",
              {"action": action.name})
    if hasattr(action.layers[0], "influence") and abs(action.layers[0].influence - 1.0) > 1e-6:
        _fail("UNSUPPORTED", "Action layer influence must be one")
    if getattr(action.layers[0], "mute", False):
        _fail("UNSUPPORTED", "Muted Action layers are unsupported")
    bag = strip.channelbag(slot)
    if bag is None:
        _fail("UNSUPPORTED", "Action slot has no channelbag")
    if hasattr(strip, "channelbags") and len(strip.channelbags) != 1:
        _fail("UNSUPPORTED", "Action strip has unexpected channelbags")
    _assert_new(action, before_map, "actions", before_node_trees)
    if action.users - int(bool(action.use_fake_user)) != 1:
        _fail("CONFLICT", "Action must be exclusively owned by the new instance",
              {"action": action.name, "users": action.users})
    curves = list(bag.fcurves)
    if not curves:
        _fail("UNSUPPORTED", "Action has no native FCurves", {"action": action.name})
    for curve in curves:
        if len(curve.modifiers) or len(curve.sampled_points) or curve.mute or curve.lock:
            _fail("UNSUPPORTED", "Edited channels must be unlocked/muted-free native keys",
                  {"action": action.name, "path": curve.data_path,
                   "index": curve.array_index})
        if not curve.keyframe_points:
            _fail("UNSUPPORTED", "Empty FCurve is unsupported",
                  {"action": action.name, "path": curve.data_path,
                   "index": curve.array_index})
    return slot, bag


def _collect_action(action, owner, expected_type, role, kind, before_map,
                    before_node_trees, action_rows, action_pointers):
    if action is None:
        return None
    _assert_new(action, before_map, "actions", before_node_trees)
    if action.as_pointer() in action_pointers:
        _fail("CONFLICT", "An Action is shared between multiple owners",
              {"action": action.name})
    slot, bag = _binding(action, owner, expected_type, before_map, before_node_trees)
    action_pointers.add(action.as_pointer())
    action_rows.append({
        "action": action,
        "owner": owner,
        "owner_role": role,
        "kind": kind,
        "expected_type": expected_type,
        "slot": slot,
        "bag": bag,
        "source_curves": len(bag.fcurves),
        "source_keys": sum(len(c.keyframe_points) for c in bag.fcurves),
        "source_first_frame": min(float(k.co.x) for c in bag.fcurves
                                  for k in c.keyframe_points),
        "source_last_frame": max(float(k.co.x) for c in bag.fcurves
                                 for k in c.keyframe_points),
    })
    return action


def _actor_structure(collection, before_map, before_node_trees,
                     request_actor, action_rows, action_pointers):
    objects = list(collection.objects)
    if not objects:
        _fail("UNSUPPORTED", "Actor collection has no objects",
              {"collection": collection.name})
    roots = [o for o in objects if o.type == "EMPTY" and o.parent is None]
    if len(roots) != 1:
        _fail("UNSUPPORTED", "Actor requires exactly one parentless root Empty",
              {"collection": collection.name, "count": len(roots)})
    root = roots[0]
    rigs = [o for o in objects if o.type == "ARMATURE" and o.parent == root]
    if len(rigs) != 1:
        _fail("UNSUPPORTED", "Actor requires exactly one Armature child of the root",
              {"collection": collection.name, "count": len(rigs)})
    rig = rigs[0]
    if rig.data is None:
        _fail("UNSUPPORTED", "Actor rig has no Armature data")
    for obj in objects:
        _assert_new(obj, before_map, "objects", before_node_trees)
        if obj.data is not None:
            data_label = _data_label(obj.data)
            if data_label:
                _assert_new(obj.data, before_map, data_label, before_node_trees)
        if obj.animation_data and obj.animation_data.action:
            _collect_action(obj.animation_data.action, obj, "OBJECT",
                            request_actor["role"],
                            "root" if obj == root else ("rig" if obj == rig else "object"),
                            before_map, before_node_trees, action_rows, action_pointers)
        if obj.type == "LIGHT":
            if obj.data is None:
                _fail("UNSUPPORTED", "Light object has no data")
            if obj.data.animation_data and obj.data.animation_data.action:
                _collect_action(obj.data.animation_data.action, obj.data, "LIGHT",
                                request_actor["role"], "light_data",
                                before_map, before_node_trees, action_rows,
                                action_pointers)
        elif obj.data is not None and obj.data.animation_data \
                and obj.data.animation_data.action:
            _fail("UNSUPPORTED", "Only Light data Actions are in this clip scope",
                  {"object": obj.name, "data": obj.data.name})
    if root.animation_data is None or root.animation_data.action is None:
        _fail("UNSUPPORTED", "Actor root requires an exclusive native Action")
    if rig.animation_data is None or rig.animation_data.action is None:
        _fail("UNSUPPORTED", "Actor rig requires an exclusive native Action")
    if root.rotation_mode != "XYZ":
        _fail("UNSUPPORTED", "Root world motion requires XYZ Euler rotation mode")

    materials = []
    material_pointers = set()
    node_tree_pointers = set()
    for obj in objects:
        if obj.type != "MESH":
            continue
        if obj.data is None:
            _fail("UNSUPPORTED", "Mesh object has no mesh data", {"object": obj.name})
        if len(obj.data.materials) != 1:
            _fail("UNSUPPORTED", "Template mesh requires exactly one material",
                  {"object": obj.name, "materials": len(obj.data.materials)})
        material = obj.data.materials[0]
        if material is None:
            _fail("UNSUPPORTED", "Template mesh material is empty",
                  {"object": obj.name})
        if material.as_pointer() not in material_pointers:
            material_pointers.add(material.as_pointer())
            materials.append(material)
    for material in materials:
        _assert_new(material, before_map, "materials", before_node_trees)
        if material.animation_data and material.animation_data.action:
            _fail("UNSUPPORTED", "Material-level Actions are outside clip scope",
                  {"material": material.name})
        tree = material.node_tree
        if tree is None:
            continue
        if tree.as_pointer() not in node_tree_pointers:
            node_tree_pointers.add(tree.as_pointer())
            _assert_new(tree, {}, "node_tree", before_node_trees)
            if tree.animation_data and tree.animation_data.action:
                _collect_action(tree.animation_data.action, tree, "NODETREE",
                                request_actor["role"], "material:" + material.name,
                                before_map, before_node_trees, action_rows,
                                action_pointers)

    tagged = [collection, root, rig, rig.data]
    for obj in objects:
        if obj.type == "MESH":
            tagged.append(obj)
            tagged.append(obj.data)
    tagged.extend(materials)
    seen = set()
    for item in tagged:
        if item is None or item.as_pointer() in seen:
            continue
        seen.add(item.as_pointer())
        if "ac_instance_uuid" not in item.keys():
            _fail("CONFLICT", "Owned template ID lacks ac_instance_uuid",
                  {"type": item.bl_rna.identifier, "name": item.name})
    for item in list(objects) + materials + [collection, rig.data]:
        if item is None or item.as_pointer() in seen:
            continue
        seen.add(item.as_pointer())
        if "ac_instance_uuid" in item.keys():
            _fail("CONFLICT", "Unexpected additionally tagged ID",
                  {"type": item.bl_rna.identifier, "name": item.name})
    for item in tagged:
        if item is not None and item.get("ac_instance_uuid") != request_actor["template_uuid"]:
            _fail("CONFLICT", "Owned template UUID does not match manifest",
                  {"type": item.bl_rna.identifier, "name": item.name,
                   "expected": request_actor["template_uuid"],
                   "actual": item.get("ac_instance_uuid")})
    return {
        "objects": objects,
        "root": root,
        "rig": rig,
        "materials": materials,
        "node_trees": [m.node_tree for m in materials if m.node_tree is not None],
        "tagged": [item for item in tagged if item is not None],
    }


def _validate_full_action_time(action_row, source_start, source_end):
    action = action_row["action"]
    for curve in action_row["bag"].fcurves:
        keys = list(curve.keyframe_points)
        values = [float(k.co.x) for k in keys]
        if any(not math.isfinite(v) for v in values):
            _fail("UNSUPPORTED", "Non-finite native key time", {"action": action.name})
        if values[0] < source_start - 1e-6 or values[-1] > source_end + 1e-6:
            _fail("UNSUPPORTED", "Whole-clip scope requires all keys inside source timing",
                  {"action": action.name, "path": curve.data_path,
                   "index": curve.array_index, "first": values[0], "last": values[-1]})
        if any(b < a for a, b in zip(values, values[1:])):
            _fail("UNSUPPORTED", "Native FCurve key times are not sorted",
                  {"action": action.name, "path": curve.data_path,
                   "index": curve.array_index})
    first = min(float(k.co.x) for c in action_row["bag"].fcurves
                for k in c.keyframe_points)
    last = max(float(k.co.x) for c in action_row["bag"].fcurves
               for k in c.keyframe_points)
    if abs(first - source_start) > 1e-6 or abs(last - source_end) > 1e-6:
        _fail("UNSUPPORTED", "Action must span the complete source clip",
              {"action": action.name, "first": first, "last": last})


def _key_state(key):
    state = {
        "co": (float(key.co.x), float(key.co.y)),
        "left": (float(key.handle_left.x), float(key.handle_left.y)),
        "right": (float(key.handle_right.x), float(key.handle_right.y)),
    }
    for name in _KEY_META:
        state[name] = getattr(key, name)
    return state


def _write_exact_curve(curve, states):
    """Write exact native keys without deriving/changing automatic handles."""
    keys = list(curve.keyframe_points)
    if len(keys) != len(states):
        _fail("UNSUPPORTED", "Native curve key count changed during rewrite")
    for key in keys:
        key.handle_left_type = "FREE"
        key.handle_right_type = "FREE"
    for key, state in zip(keys, states):
        key.co.x = _f32(state["co"][0])
        key.co.y = _f32(state["co"][1])
    for key, state in zip(keys, states):
        key.handle_left.x = _f32(state["left"][0])
        key.handle_left.y = _f32(state["left"][1])
        key.handle_right.x = _f32(state["right"][0])
        key.handle_right.y = _f32(state["right"][1])
    for key, state in zip(keys, states):
        key.interpolation = state["interpolation"]
        key.easing = state["easing"]
        key.back = _f32(state["back"])
        key.amplitude = _f32(state["amplitude"])
        key.period = _f32(state["period"])
        key.handle_left_type = state["handle_left_type"]
        key.handle_right_type = state["handle_right_type"]
    # Restore the exact coordinates after restoring AUTO/AUTO_CLAMPED types so
    # native auto-derivation cannot replace the mapped source handles.
    for key, state in zip(keys, states):
        key.co.x = _f32(state["co"][0])
        key.co.y = _f32(state["co"][1])
        key.handle_left.x = _f32(state["left"][0])
        key.handle_left.y = _f32(state["left"][1])
        key.handle_right.x = _f32(state["right"][0])
        key.handle_right.y = _f32(state["right"][1])


def _retime_curve(curve, map_frame):
    states = []
    for key in curve.keyframe_points:
        state = _key_state(key)
        state["co"] = (_f32(map_frame(float(state["co"][0]))), state["co"][1])
        state["left"] = (_f32(map_frame(float(state["left"][0]))), state["left"][1])
        state["right"] = (_f32(map_frame(float(state["right"][0]))), state["right"][1])
        states.append(state)
    _write_exact_curve(curve, states)


def _curve_map(bag):
    out = {}
    for curve in bag.fcurves:
        out[(curve.data_path, int(curve.array_index))] = curve
    return out


def _root_mapping(root, root_action, action_row, request, mapping):
    bag = action_row["bag"]
    curves = _curve_map(bag)
    allowed = {"location", "rotation_euler"}
    for path, _index in curves:
        if path not in allowed:
            _fail("UNSUPPORTED", "Root Action contains a channel outside world transform policy",
                  {"path": path, "action": root_action.name})
    location = []
    for index in range(3):
        curve = curves.get(("location", index))
        if curve is None:
            _fail("UNSUPPORTED", "Root Action must contain all three location curves")
        location.append(curve)
    if len({len(c.keyframe_points) for c in location}) != 1:
        _fail("UNSUPPORTED", "Root location channels must have identical key counts")
    rotation_z = curves.get(("rotation_euler", 2))
    if rotation_z is None:
        _fail("UNSUPPORTED", "Root Action must contain rotation_euler[2]")

    matrix = mapping["quarter_matrix"]
    scale = mapping["scale"]
    translation = mapping["translation"]
    source = [[_key_state(k) for k in curve.keyframe_points] for curve in location]
    for target_index in range(3):
        if target_index == 2:
            source_index = 2
            sign = 1.0
        else:
            source_index = 0 if matrix[target_index][0] else (
                1 if matrix[target_index][1] else 2)
            sign = matrix[target_index][0] or matrix[target_index][1]
        target_curve = location[target_index]
        if len(target_curve.keyframe_points) != len(source[source_index]):
            _fail("UNSUPPORTED", "Root location channels have inconsistent key counts")
        states = []
        for state in source[source_index]:
            new_state = dict(state)
            new_state["co"] = (state["co"][0],
                               _f32(translation[target_index]
                                    + scale * sign * state["co"][1]))
            new_state["left"] = (state["left"][0],
                                 _f32(translation[target_index]
                                      + scale * sign * state["left"][1]))
            new_state["right"] = (state["right"][0],
                                  _f32(translation[target_index]
                                       + scale * sign * state["right"][1]))
            states.append(new_state)
        _write_exact_curve(target_curve, states)

    yaw = mapping["yaw_radians"]
    if yaw:
        states = []
        for key in rotation_z.keyframe_points:
            state = _key_state(key)
            state["co"] = (state["co"][0], _f32(state["co"][1] + yaw))
            state["left"] = (state["left"][0], _f32(state["left"][1] + yaw))
            state["right"] = (state["right"][0], _f32(state["right"][1] + yaw))
            states.append(state)
        _write_exact_curve(rotation_z, states)

    root_scale = [float(root.scale[i]) for i in range(3)]
    if not all(math.isfinite(v) and v > 0.0 for v in root_scale):
        _fail("UNSUPPORTED", "Root scale must be finite and positive")
    if max(root_scale) - min(root_scale) > 1e-6 * max(root_scale):
        _fail("UNSUPPORTED", "Root must have a uniform positive basis scale")
    root.scale = tuple(_f32(v * scale) for v in root_scale)
    return [{"data_path": c.data_path, "array_index": int(c.array_index)}
            for c in location] + [
        {"data_path": rotation_z.data_path,
         "array_index": int(rotation_z.array_index)}
    ]


def _action_curves(action_row):
    return sorted(action_row["bag"].fcurves,
                  key=lambda c: (c.data_path, int(c.array_index)))


def _curve_witness(curve):
    keys = list(curve.keyframe_points)
    records = []
    for key in keys:
        records.append([
            round(float(key.co.x), 9), round(float(key.co.y), 9),
            round(float(key.handle_left.x), 9), round(float(key.handle_left.y), 9),
            round(float(key.handle_right.x), 9), round(float(key.handle_right.y), 9),
            key.handle_left_type, key.handle_right_type, key.interpolation,
            key.easing, round(float(key.back), 9), round(float(key.amplitude), 9),
            round(float(key.period), 9),
        ])
    digest = hashlib.sha256(
        json.dumps(records, separators=(",", ":"), sort_keys=False).encode("utf-8")
    ).hexdigest()
    return {
        "data_path": curve.data_path,
        "array_index": int(curve.array_index),
        "group": curve.group.name if curve.group else None,
        "keys": len(keys),
        "first_frame": float(keys[0].co.x) if keys else None,
        "last_frame": float(keys[-1].co.x) if keys else None,
        "first_value": float(keys[0].co.y) if keys else None,
        "last_value": float(keys[-1].co.y) if keys else None,
        "handle_types": sorted({key.handle_left_type for key in keys}
                               | {key.handle_right_type for key in keys}),
        "interpolations": sorted({key.interpolation for key in keys}),
        "key_sha256": digest,
    }


def _action_witness(action_row):
    curves = _action_curves(action_row)
    owner = action_row["owner"]
    return {
        "action": action_row["action"].name,
        "slot_identifier": action_row["slot"].identifier,
        "slot_type": action_row["slot"].target_id_type,
        "owner": {"type": owner.bl_rna.identifier, "name": owner.name},
        "kind": action_row["kind"],
        "role": action_row["owner_role"],
        "curves": len(curves),
        "keys": sum(len(c.keyframe_points) for c in curves),
        "first_frame": min(float(k.co.x) for c in curves for k in c.keyframe_points),
        "last_frame": max(float(k.co.x) for c in curves for k in c.keyframe_points),
        "curve_witnesses": [_curve_witness(c) for c in curves],
    }


def _clear_asset_marks(item):
    if getattr(item, "asset_data", None) is not None:
        if hasattr(item, "asset_clear"):
            item.asset_clear()
        else:
            item.asset_data = None
    if "asset_id" in item.keys():
        del item["asset_id"]


def _new_uuid(existing):
    while True:
        value = uuid.uuid4().hex
        if value not in existing:
            existing.add(value)
            return value


def _tagged_actor_ids(collection, structure):
    ids = [collection] + list(structure["tagged"])
    out, seen = [], set()
    for item in ids:
        if item is None or item.as_pointer() in seen:
            continue
        seen.add(item.as_pointer())
        if "ac_instance_uuid" in item.keys():
            out.append(item)
    return out


def _update_metadata(collection, root, old_uuid, new_uuid):
    text = collection.get("ac_template_metadata")
    root_text = root.get("ac_template_metadata")
    if not isinstance(text, str) or not text:
        _fail("CONFLICT", "Actor collection lacks ac_template_metadata")
    if text != root_text:
        _fail("CONFLICT", "Actor collection/root template metadata differ")
    try:
        metadata = json.loads(text)
    except (ValueError, TypeError) as exc:
        _fail("UNSUPPORTED", "Actor template metadata is not valid JSON",
              {"error": str(exc)})
    if not isinstance(metadata, dict) or metadata.get("instance_uuid") != old_uuid:
        _fail("CONFLICT", "Template metadata instance_uuid does not match manifest")
    updated = copy.deepcopy(metadata)
    updated["instance_uuid"] = new_uuid
    check = copy.deepcopy(updated)
    check["instance_uuid"] = old_uuid
    if check != metadata:
        _fail("CONFLICT", "Template metadata retag would change non-identity fields")
    text_new = json.dumps(updated, sort_keys=True, ensure_ascii=False, allow_nan=False)
    collection["ac_template_metadata"] = text_new
    root["ac_template_metadata"] = text_new
    return metadata


def _set_provenance(wrapper, collection, light_objects, request, asset_records,
                    mapping, library_sha):
    clip = request["clip"]
    common = {
        "ac_clip_id": clip["clip_id"],
        "ac_clip_instance_name": request["instance_name"],
        "ac_clip_source_file": clip["source"]["file"],
        "ac_clip_source_sha256": clip["source"]["sha256"],
    }
    wrapper_props = dict(common)
    wrapper_props.update({
        "ac_clip_schema": REQUEST_SCHEMA,
        "ac_clip_library_file": clip["library"]["file"],
        "ac_clip_library_sha256": library_sha,
        "ac_clip_mapping": json.dumps(mapping, sort_keys=True, allow_nan=False),
        "ac_clip_assets": json.dumps(asset_records, sort_keys=True, allow_nan=False),
    })
    for key, value in wrapper_props.items():
        wrapper[key] = value
    for collection_row in collection:
        for key, value in common.items():
            collection_row["collection"][key] = value
        collection_row["collection"]["acw_source_asset_id"] = \
            collection_row["source_asset_id"]
        collection_row["collection"]["ac_clip_source_template_uuid"] = \
            collection_row["template_uuid"]
    for row in light_objects:
        for key, value in common.items():
            row["object"][key] = value
        row["object"]["acw_source_asset_id"] = row["source_asset_id"]
        row["object"]["ac_clip_role"] = row["role"]


def _cleanup_new_ids(bpy, before_map, wrapper):
    errors = []
    if wrapper is not None:
        try:
            if wrapper.name in bpy.data.collections:
                bpy.data.collections.remove(wrapper, do_unlink=True)
        except Exception as exc:
            errors.append("wrapper: " + str(exc))
    order = ("objects", "actions", "materials", "meshes", "armatures",
             "lights", "cameras", "images", "textures", "collections",
             "node_groups", "worlds", "texts")
    for category in order:
        coll = getattr(bpy.data, category, None)
        if coll is None:
            continue
        before = before_map.get(category, {})
        for item in list(coll):
            try:
                if item.as_pointer() in before:
                    continue
                if item.bl_rna.identifier == "Scene":
                    continue
                coll.remove(item, do_unlink=True)
            except Exception as exc:
                errors.append(category + "/" + item.name + ": " + str(exc))
    return errors


def _new_ids(bpy, before_map, before_node_trees):
    rows = []
    for category, before in before_map.items():
        coll = getattr(bpy.data, category, None)
        if coll is None:
            continue
        for item in coll:
            if item.as_pointer() not in before:
                rows.append((category, item))
    for material in bpy.data.materials:
        tree = material.node_tree
        if tree is not None and tree.as_pointer() not in before_node_trees:
            rows.append(("node_tree", tree))
    return rows


def apply_clip(context, request):
    """Apply one frozen native clip; returns a JSON-safe result."""
    import bpy
    from mathutils import Matrix, Vector

    # Keep the existing humanoid branch byte-for-byte in control for its
    # schema.  The rigid branch is a sibling native core with its own strict
    # MESH/Object inventory; it shares this module's before-ID/cleanup helpers.
    if isinstance(request, dict) and isinstance(request.get('clip'), dict) \
            and request['clip'].get('schema') == 'animation-native-rigid-clip-v1':
        from . import rigid_clip_asset
        return rigid_clip_asset.apply_rigid_clip(context, request)
    if isinstance(request, dict) and isinstance(request.get('clip'), dict) \
            and request['clip'].get('schema') == 'animation-native-project-clip-v1':
        from . import project_clip_asset
        return project_clip_asset.apply_project_clip(context, request)

    req = _normalize_request(request)
    clip = req["clip"]
    scene, layer = _resolve_context(bpy, context)
    _validate_scene(scene, req)
    scene_before = _scene_state(scene)
    library_path = clip["library"]["file"]
    library_sha = _sha256_file(library_path, "clip.library.file")
    if library_sha != clip["library"]["expected_sha256"]:
        _fail("CONFLICT", "Frozen library SHA256 does not match request",
              {"expected": clip["library"]["expected_sha256"], "actual": library_sha})

    before_map = _id_collections(bpy)
    before_node_trees = _before_node_trees(bpy)
    existing_uuids = set()
    for category in ("collections", "objects", "meshes", "armatures", "materials"):
        for item in getattr(bpy.data, category):
            if "ac_instance_uuid" in item.keys():
                existing_uuids.add(item.get("ac_instance_uuid"))
    for actor in clip["actors"]:
        existing_uuids.add(actor["template_uuid"])

    wrapper = None
    loaded = {"collections": [], "objects": []}
    action_rows = []
    action_pointers = set()
    asset_records = []
    try:
        with bpy.data.libraries.load(library_path, link=False, relative=False) as (
                data_from, data_to):
            data_to.collections = [a["collection"] for a in clip["actors"]]
            data_to.objects = [light["object"] for light in clip["lights"]]
        loaded["collections"] = list(data_to.collections)
        loaded["objects"] = list(data_to.objects)
        if len(loaded["collections"]) != 2 or any(c is None for c in loaded["collections"]):
            _fail("NOT_FOUND", "Library did not load exactly two actor Collections",
                  {"loaded": len(loaded["collections"])})
        if len(loaded["objects"]) != 2 or any(o is None for o in loaded["objects"]):
            _fail("NOT_FOUND", "Library did not load exactly two Light Objects",
                  {"loaded": len(loaded["objects"])})

        loaded_collections = {}
        for collection in loaded["collections"]:
            if collection.asset_data is None:
                _fail("CONFLICT", "Loaded actor Collection is not marked as an asset",
                      {"collection": collection.name})
            asset_id = collection.get("asset_id")
            _canonical_uuid(asset_id, "loaded actor asset_id")
            if asset_id in loaded_collections:
                _fail("CONFLICT", "Duplicate loaded actor asset_id", {"asset_id": asset_id})
            loaded_collections[asset_id] = collection
        loaded_lights = {}
        for obj in loaded["objects"]:
            if obj.type != "LIGHT":
                _fail("CONFLICT", "Loaded light asset is not a LIGHT object",
                      {"object": obj.name, "type": obj.type})
            if obj.asset_data is None:
                _fail("CONFLICT", "Loaded Light Object is not marked as an asset",
                      {"object": obj.name})
            asset_id = obj.get("asset_id")
            _canonical_uuid(asset_id, "loaded light asset_id")
            if asset_id in loaded_lights:
                _fail("CONFLICT", "Duplicate loaded light asset_id", {"asset_id": asset_id})
            loaded_lights[asset_id] = obj

        actor_specs = {a["asset_id"]: a for a in clip["actors"]}
        light_specs = {l["asset_id"]: l for l in clip["lights"]}
        if set(loaded_collections) != set(actor_specs):
            _fail("CONFLICT", "Loaded actor asset_ids do not match manifest",
                  {"expected": sorted(actor_specs), "actual": sorted(loaded_collections)})
        if set(loaded_lights) != set(light_specs):
            _fail("CONFLICT", "Loaded light asset_ids do not match manifest",
                  {"expected": sorted(light_specs), "actual": sorted(loaded_lights)})

        actor_rows = []
        for actor_spec in clip["actors"]:
            collection = loaded_collections[actor_spec["asset_id"]]
            _assert_new(collection, before_map, "collections", before_node_trees)
            if collection.get("ac_instance_uuid") != actor_spec["template_uuid"]:
                _fail("CONFLICT", "Loaded collection template UUID mismatch",
                      {"collection": collection.name,
                       "expected": actor_spec["template_uuid"],
                       "actual": collection.get("ac_instance_uuid")})
            structure = _actor_structure(
                collection, before_map, before_node_trees, actor_spec,
                action_rows, action_pointers)
            actor_rows.append({
                "spec": actor_spec,
                "collection": collection,
                "structure": structure,
                "source_asset_id": actor_spec["asset_id"],
                "template_uuid": actor_spec["template_uuid"],
                "new_uuid": None,
            })
            asset_records.append({
                "kind": "collection", "role": actor_spec["role"],
                "asset_id": actor_spec["asset_id"],
                "source_name": actor_spec["collection"],
                "loaded_name": collection.name,
                "template_uuid": actor_spec["template_uuid"],
            })

        light_rows = []
        for light_spec in clip["lights"]:
            obj = loaded_lights[light_spec["asset_id"]]
            _assert_new(obj, before_map, "objects", before_node_trees)
            if obj.users_collection:
                _fail("CONFLICT", "Loaded Light Object is already linked",
                      {"object": obj.name})
            if obj.parent is not None or obj.constraints:
                _fail("UNSUPPORTED", "Light object must be unparented and unconstrained",
                      {"object": obj.name})
            if obj.animation_data and obj.animation_data.action:
                _fail("UNSUPPORTED", "Animated light object transform is outside this clip policy",
                      {"object": obj.name})
            if obj.data is None:
                _fail("UNSUPPORTED", "Light object has no data")
            _assert_new(obj.data, before_map, "lights", before_node_trees)
            data_action = None
            if obj.data.animation_data and obj.data.animation_data.action:
                data_action = _collect_action(
                    obj.data.animation_data.action, obj.data, "LIGHT",
                    light_spec["role"], "light_data",
                    before_map, before_node_trees, action_rows, action_pointers)
            light_rows.append({
                "spec": light_spec,
                "role": light_spec["role"],
                "object": obj,
                "data": obj.data,
                "action": data_action,
                "source_asset_id": light_spec["asset_id"],
            })
            asset_records.append({
                "kind": "object", "role": light_spec["role"],
                "asset_id": light_spec["asset_id"],
                "source_name": light_spec["object"],
                "loaded_name": obj.name,
            })

        if len(action_rows) != len({row["action"].as_pointer() for row in action_rows}):
            _fail("CONFLICT", "Action collection contains duplicate entries")
        if not action_rows:
            _fail("UNSUPPORTED", "Clip contains no owned native Actions")
        source_start = clip["timing"]["start"]
        source_end = clip["timing"]["end"]
        for row in action_rows:
            _validate_full_action_time(row, source_start, source_end)

        trim_report = None
        if 'source_window' in req:
            from . import curve_segments, native
            window = req['source_window']
            source_start, source_end = window['start'], window['end']
            library_keys = sum(row['source_keys'] for row in action_rows)
            for row in action_rows:
                for curve in row['bag'].fcurves:
                    try:
                        selected = curve_segments.trim_states(
                            [native.key_state(key) for key in curve.keyframe_points],
                            source_start, source_end)
                        curve_segments.write_states(curve, selected)
                    except ValueError as exc:
                        _fail('UNSUPPORTED', 'Native source-window extraction is unsupported',
                              {'action': row['action'].name, 'path': curve.data_path,
                               'index': curve.array_index, 'reason': str(exc)})
                row['source_keys'] = sum(len(curve.keyframe_points) for curve in row['bag'].fcurves)
                row['source_first_frame'] = source_start
                row['source_last_frame'] = source_end
                _validate_full_action_time(row, source_start, source_end)
            trim_report = {'source_window': {'start': source_start, 'end': source_end},
                           'requested': window['requested'], 'library_keys': library_keys,
                           'selected_keys': sum(row['source_keys'] for row in action_rows),
                           'curve_count': sum(row['source_curves'] for row in action_rows),
                           'boundary_policy': 'Native Bezier subdivision; necessary split/boundary handles FREE',
                           'baked': False, 'outside_keys_retained': False}

        for actor_row in actor_rows:
            collection = actor_row["collection"]
            root = actor_row["structure"]["root"]
            tagged = _tagged_actor_ids(collection, actor_row["structure"])
            expected = actor_row["template_uuid"]
            for item in tagged:
                if item.get("ac_instance_uuid") != expected:
                    _fail("CONFLICT", "Actor ownership tag changed before retag",
                          {"name": item.name})
            actor_row["new_uuid"] = _new_uuid(existing_uuids)

        for actor_row in actor_rows:
            collection = actor_row["collection"]
            structure = actor_row["structure"]
            old_uuid = actor_row["template_uuid"]
            new_uuid = actor_row["new_uuid"]
            tagged = _tagged_actor_ids(collection, structure)
            for item in tagged:
                item["ac_instance_uuid"] = new_uuid
            _update_metadata(collection, structure["root"], old_uuid, new_uuid)

        for category, item in _new_ids(bpy, before_map, before_node_trees):
            if getattr(item, "use_fake_user", False):
                item.use_fake_user = False
            if getattr(item, "asset_data", None) is not None or "asset_id" in item.keys():
                _clear_asset_marks(item)
        for actor_row in actor_rows:
            if actor_row["collection"].users != 0:
                _fail("CONFLICT", "Loaded actor Collection is already shared",
                      {"collection": actor_row["collection"].name,
                       "users": actor_row["collection"].users})
        for light_row in light_rows:
            if light_row["object"].users_collection:
                _fail("CONFLICT", "Loaded Light Object is already linked",
                      {"object": light_row["object"].name})

        frame_span = req["end"] - req["start"]
        source_span = source_end - source_start
        ratio = frame_span / source_span
        quarter = int(req["yaw_quarter_turns"])
        if quarter == 0:
            matrix2 = ((1.0, 0.0), (0.0, 1.0))
        elif quarter == 1:
            matrix2 = ((0.0, -1.0), (1.0, 0.0))
        elif quarter == 2:
            matrix2 = ((-1.0, 0.0), (0.0, -1.0))
        else:
            matrix2 = ((0.0, 1.0), (-1.0, 0.0))
        yaw = quarter * math.pi / 2.0
        translation = list(req["translation"])
        scale = req["scale"]
        mapping = {
            "schema": "animation-native-clip-mapping-v1",
            "source_start": source_start,
            "source_end": source_end,
            "source_window": trim_report['source_window'] if trim_report else None,
            "target_start": req["start"],
            "target_end": req["end"],
            "frame_ratio": ratio,
            "frame_affine": [ratio, req["start"] - source_start * ratio],
            "scale": scale,
            "translation": translation,
            "yaw_quarter_turns": quarter,
            "yaw_radians": yaw,
            "quarter_matrix": [list(row) for row in matrix2],
            "similarity_matrix": [
                [scale * matrix2[0][0], scale * matrix2[0][1], 0.0, translation[0]],
                [scale * matrix2[1][0], scale * matrix2[1][1], 0.0, translation[1]],
                [0.0, 0.0, scale, translation[2]],
                [0.0, 0.0, 0.0, 1.0],
            ],
        }

        def map_frame(frame):
            return req["start"] + (float(frame) - source_start) * ratio

        for row in action_rows:
            for curve in row["bag"].fcurves:
                _retime_curve(curve, map_frame)

        root_rows = {}
        for actor_row in actor_rows:
            root_object = actor_row["structure"]["root"]
            actor_row['source_root_scale'] = [float(value) for value in root_object.scale]
            root_action = root_object.animation_data.action
            matching = [row for row in action_rows
                        if row["action"] == root_action]
            if len(matching) != 1:
                _fail("CONFLICT", "Actor root Action was not collected exactly once")
            actor_row["root_mapping_curves"] = _root_mapping(
                root_object, root_action, matching[0], req, mapping)
            root_rows[root_object.as_pointer()] = matching[0]

        world = Matrix(((scale * matrix2[0][0], scale * matrix2[0][1], 0.0,
                         translation[0]),
                        (scale * matrix2[1][0], scale * matrix2[1][1], 0.0,
                         translation[1]),
                        (0.0, 0.0, scale, translation[2]),
                        (0.0, 0.0, 0.0, 1.0)))

        wrapper = bpy.data.collections.new(req["instance_name"])
        scene.collection.children.link(wrapper)
        for actor_row in actor_rows:
            wrapper.children.link(actor_row["collection"])
        for light_row in light_rows:
            wrapper.objects.link(light_row["object"])
        for light_row in light_rows:
            obj = light_row["object"]
            before_matrix = obj.matrix_basis.copy()
            obj.matrix_world = world @ before_matrix
            light_row["target_matrix"] = [list(row) for row in obj.matrix_world]
        layer.update()

        for actor_row in actor_rows:
            for obj in actor_row["structure"]["objects"]:
                if scene.objects.get(obj.name) is not obj:
                    _fail("CONFLICT", "New actor object is not in the active scene",
                          {"object": obj.name})
        for light_row in light_rows:
            if scene.objects.get(light_row["object"].name) is not light_row["object"]:
                _fail("CONFLICT", "New light object is not in the active scene")

        _set_provenance(
            wrapper,
            [{"collection": row["collection"],
              "source_asset_id": row["source_asset_id"],
              "template_uuid": row["template_uuid"]} for row in actor_rows],
            light_rows, req, asset_records, mapping, library_sha)

        for actor_row in actor_rows:
            root = actor_row["structure"]["root"]
            rig = actor_row["structure"]["rig"]
            expected_scale = _f32(actor_row['source_root_scale'][0] * scale)
            actual_scale = [float(root.scale[i]) for i in range(3)]
            if any(abs(v - max(actual_scale)) > 1e-6 for v in actual_scale):
                _fail("VALIDATION_FAILED", "Root scale is not uniform after apply")
            if abs(actual_scale[0] - expected_scale) > 1e-5:
                _fail("VALIDATION_FAILED", "Root scale does not match request",
                      {"expected": expected_scale, "actual": actual_scale})
            if rig.parent != root:
                _fail("VALIDATION_FAILED", "Rig/root hierarchy changed")

        after_scene = _scene_state(scene)
        for key in ("frame_start", "frame_end", "frame_current",
                    "frame_subframe", "camera_pointer", "camera_name"):
            if after_scene[key] != scene_before[key]:
                _fail("CONFLICT", "Scene state changed during apply",
                      {"field": key, "before": scene_before[key],
                       "after": after_scene[key]})

        for category, item in _new_ids(bpy, before_map, before_node_trees):
            if item.library is not None or item.override_library is not None:
                _fail("CONFLICT", "A newly loaded ID became linked/override",
                      {"name": item.name})
            if getattr(item, "asset_data", None) is not None:
                _fail("CONFLICT", "Asset mark survived on a new instance",
                      {"type": item.bl_rna.identifier, "name": item.name})
            if "asset_id" in item.keys():
                _fail("CONFLICT", "Imported asset_id survived on a new instance",
                      {"type": item.bl_rna.identifier, "name": item.name})

        after_action_rows = []
        for row in action_rows:
            row["target_curves"] = len(row["bag"].fcurves)
            row["target_keys"] = sum(len(c.keyframe_points) for c in row["bag"].fcurves)
            if row["target_curves"] != row["source_curves"] \
                    or row["target_keys"] != row["source_keys"]:
                _fail("VALIDATION_FAILED", "Curve/key count changed during retime",
                      {"action": row["action"].name})
            for curve in row["bag"].fcurves:
                if any(not math.isfinite(float(k.co.x)) or not math.isfinite(float(k.co.y))
                       for k in curve.keyframe_points):
                    _fail("VALIDATION_FAILED", "Non-finite key after retime",
                          {"action": row["action"].name, "path": curve.data_path})
            witness = _action_witness(row)
            if abs(witness["first_frame"] - req["start"]) > 1e-5 \
                    or abs(witness["last_frame"] - req["end"]) > 1e-5:
                _fail("VALIDATION_FAILED", "Action does not span requested target timing",
                      {"action": row["action"].name,
                       "first": witness["first_frame"], "last": witness["last_frame"]})
            after_action_rows.append(witness)

        result = {
            "schema": RESULT_SCHEMA,
            "status": "applied",
            "module": "animation_curves_workflow_addon.clip_asset",
            "clip_id": clip["clip_id"],
            "title": clip["title"],
            "instance_name": req["instance_name"],
            "collection": wrapper.name,
            "actors": [],
            "lights": [],
            "materials": [],
            "mapping": mapping,
            "trim": trim_report,
            "actions": {
                "count": len(after_action_rows),
                "curves": sum(row["curves"] for row in after_action_rows),
                "keys": sum(row["keys"] for row in after_action_rows),
                "by_owner": after_action_rows,
            },
            "assets": {
                "library": clip["library"]["file"],
                "library_sha256": library_sha,
                "requested": asset_records,
            },
            "scene": {
                "name": scene.name,
                "view_layer": layer.name,
                "fps": int(scene.render.fps),
                "fps_base": float(scene.render.fps_base),
                "unit_scale": float(scene.unit_settings.scale_length),
                "frame_start": int(scene.frame_start),
                "frame_end": int(scene.frame_end),
                "frame_current": int(scene.frame_current),
                "camera": scene_before["camera_name"],
                "unchanged": True,
            },
            "limits": [
                "Whole frozen clip or explicit source_window; exact native interval extraction, no bake or linearization.",
                "Root yaw is limited to integer quarter turns; scale is uniform and positive.",
                "Root scale is a static uniform basis scale; animated scale curves are rejected.",
                "Light Object matrices are transformed statically; animated light object transforms are rejected.",
                "NLA, drivers, tweak mode, linked/override data, animated mesh/armature data, and FCurve modifiers/samples are rejected.",
                "Only Object, Light data, and Material node-tree Actions are transformed.",
                "apply_clip neither saves nor renders; the caller persists the candidate.",
                "Pre-existing data/Actions/UUIDs, scene frame range/current frame/camera are never changed.",
                "Imported asset marks/asset_id custom properties are cleared only on new IDs; source asset IDs are retained as acw_source_asset_id on new collections/lights.",
                "The original 1.8 m template rest geometry/proportions are preserved; world scale comes from the root.",
            ],
        }
        for actor_row in actor_rows:
            structure = actor_row["structure"]
            root = structure["root"]
            rig = structure["rig"]
            actor_actions = [row for row in after_action_rows
                             if row["role"] == actor_row["spec"]["role"]
                             and row["slot_type"] in {"OBJECT", "NODETREE"}]
            actor_materials = []
            for material in structure["materials"]:
                tree = material.node_tree
                action = tree.animation_data.action if tree and tree.animation_data \
                    and tree.animation_data.action else None
                kind = "material"
                lowered = material.name.lower()
                if "body" in lowered:
                    kind = "body"
                elif "chest" in lowered:
                    kind = "chest"
                elif "face" in lowered:
                    kind = "face"
                actor_materials.append({
                    "role": actor_row["spec"]["role"],
                    "material": material.name,
                    "kind": kind,
                    "node_tree": tree.name if tree else None,
                    "action": action.name if action else None,
                })
            result["actors"].append({
                "role": actor_row["spec"]["role"],
                "collection": actor_row["collection"].name,
                "root": root.name,
                "rig": rig.name,
                "instance_uuid": actor_row["new_uuid"],
                "template_uuid": actor_row["template_uuid"],
                "source_asset_id": actor_row["source_asset_id"],
                "objects": len(structure["objects"]),
                "meshes": sum(1 for obj in structure["objects"] if obj.type == "MESH"),
                "materials": actor_materials,
                "actions": actor_actions,
                "root_mapping_curves": actor_row["root_mapping_curves"],
            })
            result["materials"].extend(actor_materials)
        for light_row in light_rows:
            result["lights"].append({
                "role": light_row["spec"]["role"],
                "object": light_row["object"].name,
                "data": light_row["data"].name,
                "source_asset_id": light_row["source_asset_id"],
                "action": light_row["action"].name if light_row["action"] else None,
                "matrix_world": light_row["target_matrix"],
            })
        return result
    except BaseException as original:
        cleanup_errors = _cleanup_new_ids(bpy, before_map, wrapper)
        if cleanup_errors:
            raise WorkflowError(
                "CLEANUP_FAILED",
                "Clip apply failed and cleanup was incomplete",
                {"original": str(original), "cleanup": cleanup_errors}) from original
        if isinstance(original, WorkflowError):
            raise
        raise WorkflowError(
            "NATIVE_FAILURE", str(original),
            {"type": type(original).__name__}) from original


__all__ = ["apply_clip", "REQUEST_SCHEMA", "RESULT_SCHEMA"]
