"""Full-body localized clip event review/execute/inspect.

Public Blender-side API for the P2 clip flow.  Public capture/retime requests
always go through review() and execute(); inspect() is read-only.  The module is
deliberately usable in factory CLI runs before the add-on is registered: the
record lives in a Scene ID property and no operator/UI is required.
"""
import copy
import contextlib
import hashlib
import json
import math
import struct

import bpy

from . import core, event_ui_state, events, handle_scope, native, pose_gate
from .clip_contract import normalize_request
from .contract import WorkflowError, normalize_manifest

RECORD_PROPERTY = "acw_clip_event_state"
RECORD_MAX_BYTES = 8 * 1024 * 1024
REVIEW_SCHEMA = "animation-clip-event-review-v1"
RESULT_SCHEMA = "animation-clip-event-result-v1"
INSPECT_SCHEMA = "animation-clip-event-inspection-v1"
EPSILON = 1e-6
OBJECT_FIELD = "objects"
OBJECT_TYPES = {"MESH", "EMPTY"}
OBJECT_PATHS = ("location", "rotation_euler", "scale")
LEGACY_WINDOW_INTERPOLATIONS = {"BEZIER"}
OBJECT_WINDOW_INTERPOLATIONS = {"BEZIER", "LINEAR"}
_PERMIT = None
_RECORD_VIEW = None
_SUMMARY_CACHE = {}


def _fail(code, message, details=None):
    raise WorkflowError(code, message, details or {})


def _checkpoint(callback):
    if callback is not None and callback() is False:
        _fail("CANCELLED", "Clip event edit cancelled")


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise WorkflowError("INVALID_REQUEST",
                            "Value is not finite JSON-safe data") from exc


def _sha(value):
    return hashlib.sha256(_canonical(value).encode("utf8")).hexdigest()


def _float32(value):
    try:
        result = struct.unpack("f", struct.pack("f", float(value)))[0]
    except (OverflowError, struct.error) as exc:
        raise WorkflowError("UNSUPPORTED",
                            "Frame is outside native float32 range") from exc
    if not math.isfinite(result):
        _fail("UNSUPPORTED", "Frame is outside native float32 range")
    return result


def _exact_f32(value, label):
    number = float(value)
    if _float32(number) != number:
        _fail("UNSUPPORTED", label + " is not exactly representable as a native float32 frame")
    return number


def _close(a, b, tolerance=EPSILON):
    return abs(float(a) - float(b)) <= tolerance


def _semantic_key(key):
    return event_ui_state.semantic_key(native.key_state(key))


def _semantic_curve_keys(curve):
    return [_semantic_key(key) for key in curve.keyframe_points]


def _semantic_action_state(action):
    if action is None:
        return None
    state = native.action_state(action)
    for curve in state["curves"]:
        curve["keys"] = [event_ui_state.semantic_key(key)
                         for key in curve["keys"]]
    return state


def _action_sha(action):
    return None if action is None else _sha(_semantic_action_state(action))


def _action_identity(action, slot):
    return {"action_name": action.name,
            "slot_identifier": slot.identifier,
            "slot_type": slot.target_id_type}


def _label_for(owner, spec, domain):
    return {"type": owner.bl_rna.identifier, "name": owner.name,
            "domain": domain, "data_path": spec["data_path"],
            "array_index": int(spec["array_index"])}


def active():
    return _PERMIT is not None


def _permit_validate_owner(owner):
    if not isinstance(owner, bpy.types.Object) or owner.type != "ARMATURE":
        _fail("UNSUPPORTED", "Clip pose permission requires an armature object")
    key = owner.as_pointer()
    if key in _PERMIT["validated"]:
        return
    uuid = owner.get("ac_instance_uuid")
    if not isinstance(uuid, str) or not uuid:
        _fail("UNSUPPORTED", "Clip pose permission requires a template instance UUID")
    found = pose_gate.discover(
        bpy, uuid,
        allow_animated_root=True,
        allow_body_colors=True,
        allow_hand_quaternion=True,
        allow_animated_material_trees=True,
        allow_custom_material_color=True,
    )
    if found["rig"] != owner:
        _fail("CONFLICT", "Clip pose permission instance identity changed")
    _PERMIT["validated"].add(key)


def native_target(owner, path, index):
    if not active():
        _fail("UNSUPPORTED", "No active clip-event pose permission")
    address = (owner.as_pointer(), owner.get("ac_instance_uuid"), path, index)
    if address not in _PERMIT["addresses"]:
        _fail("UNSUPPORTED", "Pose channel is outside the exact clip-event permission",
              {"path": path, "array_index": index})
    _permit_validate_owner(owner)
    found = []
    for pose_bone in owner.pose.bones:
        for kind, width in (("rotation_euler", 3), ("rotation_quaternion", 4)):
            if pose_bone.path_from_id(kind) != path:
                continue
            mode = pose_bone.rotation_mode
            if kind == "rotation_euler" and mode == "XYZ" and 0 <= index < width:
                found.append(pose_bone)
            elif (kind == "rotation_quaternion" and mode == "QUATERNION"
                  and pose_bone.name in {"hand.L", "hand.R"} and 0 <= index < width):
                found.append(pose_bone)
    if len(found) != 1:
        _fail("UNSUPPORTED", "Clip permission path is not one canonical rotation channel")
    return found[0]


def validate_manifest(owner, spec, manifest):
    native_target(owner, spec["data_path"], spec["array_index"])
    key = id(manifest)
    if key not in _PERMIT["manifest_ids"]:
        if manifest != _PERMIT["manifest"]:
            _fail("CONFLICT", "Clip event request changed inside the transaction")
        _PERMIT["manifest_ids"].add(key)


def _resolve_context(context, request):
    scene = bpy.data.scenes.get(request["context"]["scene"])
    if scene is None:
        _fail("NOT_FOUND", "Scene does not exist: " + request["context"]["scene"])
    layer = scene.view_layers.get(request["context"]["view_layer"])
    if layer is None:
        _fail("NOT_FOUND", "View layer does not exist: " + request["context"]["view_layer"])
    if context.scene != scene or context.view_layer != layer:
        _fail("CONFLICT", "Clip event requires the explicitly named current scene/view layer")
    native.local(scene)
    return scene, layer


def _object_in_context(scene, layer, name, expected_type, role):
    item = bpy.data.objects.get(name)
    if item is None:
        _fail("NOT_FOUND", role + " object does not exist: " + name)
    if item.type != expected_type:
        _fail("INVALID_REQUEST", role + " must be a " + expected_type + " object: " + name)
    if scene.objects.get(name) != item or layer.objects.get(name) != item:
        _fail("CONFLICT", role + " object is outside the requested scene/view layer: " + name)
    native.local(item)
    return item


def _scene_object_in_context(scene, layer, name, role):
    item = bpy.data.objects.get(name)
    if item is None:
        _fail("NOT_FOUND", role + " object does not exist: " + name)
    if scene.objects.get(name) != item or layer.objects.get(name) != item:
        _fail("CONFLICT", role + " object is outside the requested scene/view layer: "
              + name)
    native.local(item)
    return item


def _window_interpolations(domain):
    return (OBJECT_WINDOW_INTERPOLATIONS if domain == "object"
            else LEGACY_WINDOW_INTERPOLATIONS)


def _exclusive_action(owner, id_type, role):
    try:
        action, slot, bag = native.action_binding(owner, id_type, True)
    except WorkflowError as exc:
        _fail("UNSUPPORTED", role + " Action binding is not admissible",
              {"owner": owner.name, "error": str(exc), "code": exc.code})
    if action is None:
        _fail("NOT_FOUND", role + " requires an existing native Action",
              {"owner": owner.name})
    if native.effective_users(action) != 1:
        _fail("CONFLICT", role + " Action must be exclusive", {"action": action.name})
    return action, slot, bag


def _curve_map(bag):
    out = {}
    for curve in bag.fcurves:
        address = (curve.data_path, int(curve.array_index))
        if address in out:
            _fail("UNSUPPORTED", "Duplicate native FCurve address", {"address": list(address)})
        out[address] = curve
    return out


def _validate_keys(curve, role):
    if curve.lock or curve.mute or len(curve.modifiers) or len(curve.sampled_points):
        _fail("UNSUPPORTED", role + " curve is locked, muted, sampled or modified")
    keys = list(curve.keyframe_points)
    if not keys:
        _fail("UNSUPPORTED", role + " curve has no native keys")
    previous = None
    for key in keys:
        if not all(math.isfinite(float(v)) for v in list(key.co) + list(key.handle_left) + list(key.handle_right)):
            _fail("UNSUPPORTED", role + " key contains non-finite native data")
        if previous is not None and float(key.co.x) - previous <= EPSILON:
            _fail("UNSUPPORTED", role + " key times are not strictly increasing")
        previous = float(key.co.x)
    return keys


def _body_channels(scene, layer, rig, root, request):
    found = pose_gate.discover(
        bpy, rig.get("ac_instance_uuid"),
        allow_animated_root=True,
        allow_body_colors=True,
        allow_hand_quaternion=True,
        allow_animated_material_trees=True,
        allow_custom_material_color=True,
    )
    if found["rig"] != rig or found["root"] != root:
        _fail("CONFLICT", "Rig/root ownership changed inside the clip structure gate")
    action, slot, bag = _exclusive_action(rig, "OBJECT", "Body rig")
    actual = _curve_map(bag)
    expected = set()
    for pose_bone in rig.pose.bones:
        if not pose_bone.bone.use_deform:
            continue
        if pose_bone.name in {"hand.L", "hand.R"}:
            if pose_bone.rotation_mode != "QUATERNION":
                _fail("UNSUPPORTED", "Named hand bones require QUATERNION rotation modes",
                      {"bone": pose_bone.name, "mode": pose_bone.rotation_mode})
            for index in range(4):
                expected.add((pose_bone.path_from_id("rotation_quaternion"), index))
        else:
            if pose_bone.rotation_mode != "XYZ":
                _fail("UNSUPPORTED", "Body bones require XYZ Euler rotation modes",
                      {"bone": pose_bone.name, "mode": pose_bone.rotation_mode})
            for index in range(3):
                expected.add((pose_bone.path_from_id("rotation_euler"), index))
    if set(actual) != expected:
        _fail("UNSUPPORTED", "Body Action channels differ from the point-named Euler/quaternion topology",
              {"owner": rig.name, "missing": sorted(expected - set(actual)),
               "unexpected": sorted(set(actual) - expected)})
    channels = []
    for address in sorted(expected):
        curve = actual[address]
        _validate_keys(curve, "Body " + rig.name + " " + address[0])
        spec = {"id": None, "owner": {"type": "OBJECT", "name": rig.name},
                "data_path": address[0], "array_index": address[1],
                "sharing": "reject"}
        spec["id"] = event_ui_state.target_id(spec)
        channels.append({"target": spec, "owner": rig, "domain": "body",
                         "action": action, "slot": slot, "bag": bag,
                         "curve": curve, "role": "body",
                         "label": _label_for(rig, spec, "body")})
    return channels


def _root_channels(root, request):
    action, slot, bag = _exclusive_action(root, "OBJECT", "Root")
    if root.rotation_mode != "XYZ":
        _fail("UNSUPPORTED", "Clip root policy requires an XYZ Euler root")
    expected = {(path, index) for path in ("location", "rotation_euler")
                for index in range(3)}
    actual = _curve_map(bag)
    if set(actual) != expected:
        _fail("UNSUPPORTED", "Root Action must contain exactly the six location/Euler channels",
              {"owner": root.name, "actual": sorted(actual)})
    channels = []
    for address in sorted(expected):
        curve = actual[address]
        _validate_keys(curve, "Root " + root.name + " " + address[0])
        spec = {"id": None, "owner": {"type": "OBJECT", "name": root.name},
                "data_path": address[0], "array_index": address[1],
                "sharing": "reject"}
        spec["id"] = event_ui_state.target_id(spec)
        channels.append({"target": spec, "owner": root, "domain": "root",
                         "action": action, "slot": slot, "bag": bag,
                         "curve": curve, "role": "root",
                         "label": _label_for(root, spec, "root")})
    return channels


def _default_transform(value, expected):
    return tuple(float(item) for item in value) == tuple(float(item) for item in expected)


def _object_channels(scene, layer, obj, request):
    """Ordinary MESH/EMPTY XYZ-Euler TRS adapter.

    This is deliberately a separate domain from rig/root policy: it never
    invents a parent relationship, template identity, pose gate, or extra
    channel.  Only explicit native transform F-curves already present on the
    object's exclusive Action are bound.
    """
    if obj.type not in OBJECT_TYPES:
        _fail("INVALID_REQUEST",
              "Object participant must be MESH or EMPTY: " + obj.name,
              {"type": obj.type})
    if obj.parent is not None:
        _fail("UNSUPPORTED", "Ordinary object clip participants must be parentless",
              {"object": obj.name, "parent": obj.parent.name})
    if len(obj.constraints):
        _fail("UNSUPPORTED", "Ordinary object clip participants must be unconstrained",
              {"object": obj.name,
               "constraints": [constraint.name for constraint in obj.constraints]})
    if obj.rotation_mode != "XYZ":
        _fail("UNSUPPORTED", "Ordinary object clip participants require XYZ Euler rotation",
              {"object": obj.name, "mode": obj.rotation_mode})
    if not all(math.isfinite(float(v)) and float(v) > 0.0 for v in obj.scale):
        _fail("UNSUPPORTED",
              "Ordinary object participants require finite positive base scale",
              {"object": obj.name, "scale": [float(v) for v in obj.scale]})
    if (not _default_transform(obj.delta_location, (0.0, 0.0, 0.0))
            or not _default_transform(obj.delta_rotation_euler, (0.0, 0.0, 0.0))
            or not _default_transform(obj.delta_scale, (1.0, 1.0, 1.0))):
        _fail("UNSUPPORTED", "Nondefault delta transforms are outside the object clip policy",
              {"object": obj.name})
    data = obj.data if obj.type == "MESH" else None
    if obj.type == "MESH":
        if data is None:
            _fail("UNSUPPORTED", "MESH participant has no mesh data",
                  {"object": obj.name})
        native.local(data)
    animation_data = obj.animation_data
    if animation_data is not None:
        if len(animation_data.drivers) or len(animation_data.nla_tracks):
            _fail("UNSUPPORTED",
                  "Object clip participants must not use drivers or NLA tracks",
                  {"object": obj.name})
        if animation_data.use_tweak_mode:
            _fail("UNSUPPORTED", "Object clip participants must not be in tweak mode",
                  {"object": obj.name})
    action, slot, bag = _exclusive_action(obj, "OBJECT", "Object")
    actual = _curve_map(bag)
    expected = {(path, index) for path in OBJECT_PATHS for index in range(3)}
    if not actual or not set(actual) <= expected:
        _fail("UNSUPPORTED",
              "Object Action must contain only native location/rotation_euler/scale channels",
              {"owner": obj.name, "actual": sorted(actual),
               "unexpected": sorted(set(actual) - expected)})
    channels = []
    for address in sorted(actual):
        curve = actual[address]
        _validate_keys(curve, "Object " + obj.name + " " + address[0])
        if address[0] == "scale":
            for key in curve.keyframe_points:
                if float(key.co.y) <= 0.0:
                    _fail("UNSUPPORTED",
                          "Object scale keys must remain strictly positive",
                          {"owner": obj.name, "frame": float(key.co.x),
                           "value": float(key.co.y)})
        spec = {"id": None, "owner": {"type": "OBJECT", "name": obj.name},
                "data_path": address[0], "array_index": address[1],
                "sharing": "reject"}
        spec["id"] = event_ui_state.target_id(spec)
        channels.append({"target": spec, "owner": obj, "domain": "object",
                         "action": action, "slot": slot, "bag": bag,
                         "curve": curve, "role": "object",
                         "label": _label_for(obj, spec, "object")})
    return channels


def _light_channels(light_obj, request):
    data = light_obj.data
    if data is None or getattr(data, "id_type", None) != "LIGHT":
        _fail("INVALID_REQUEST", "Light object has no native light data")
    native.local(data)
    if data.users != 1:
        _fail("CONFLICT", "Light data must have exactly one user", {"data": data.name})
    users = [obj for obj in bpy.data.objects if obj.type == "LIGHT" and obj.data == data]
    if users != [light_obj]:
        _fail("CONFLICT", "Light data must be used by exactly the selected light object",
              {"data": data.name})
    try:
        object_action, _, _ = native.action_binding(light_obj, "OBJECT", False)
    except WorkflowError as exc:
        _fail("UNSUPPORTED", "Light object transform animation is outside the clip policy",
              {"error": str(exc)})
    if object_action is not None:
        _fail("UNSUPPORTED", "Clip light policy edits energy only; object transform Action is unsupported",
              {"light": light_obj.name, "action": object_action.name})
    action, slot, bag = _exclusive_action(data, "LIGHT", "Light")
    actual = _curve_map(bag)
    allowed = {("energy", 0)} | {("color", index) for index in range(3)}
    if not actual or not set(actual) <= allowed:
        _fail("UNSUPPORTED",
              "Light Action must animate only the native energy and/or RGB color channels",
              {"light": light_obj.name, "actual": sorted(actual),
               "allowed": sorted(allowed)})
    channels = []
    for address in sorted(actual):
        curve = actual[address]
        _validate_keys(curve, "Light " + light_obj.name + " " + address[0])
        spec = {"id": None, "owner": {"type": "LIGHT", "name": data.name},
                "data_path": address[0], "array_index": address[1],
                "sharing": "reject"}
        spec["id"] = event_ui_state.target_id(spec)
        channels.append({"target": spec, "owner": data, "domain": "light",
                         "action": action, "slot": slot, "bag": bag,
                         "curve": curve, "role": "light",
                         "label": _label_for(data, spec, "light")})
    return channels


def _material_usage(material):
    users = []
    for obj in bpy.data.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        if any(slot.material == material for slot in obj.material_slots):
            users.append(obj)
    return users


def _material_channels(material, rig_names, request):
    native.local(material)
    if material.users != 1:
        _fail("CONFLICT", "Clip material must be exclusive", {"material": material.name})
    if material.animation_data is not None and material.animation_data.action is not None:
        _fail("UNSUPPORTED", "Material datablock Action is outside the point-named emission policy",
              {"material": material.name})
    if not material.use_nodes or material.node_tree is None:
        _fail("UNSUPPORTED", "Clip material requires an existing node tree",
              {"material": material.name})
    tree = material.node_tree
    native.local(tree)
    nodes = [node for node in tree.nodes
             if node.bl_idname == "ShaderNodeBsdfPrincipled"]
    if len(nodes) != 1:
        _fail("UNSUPPORTED", "Clip material requires exactly one Principled node",
              {"material": material.name, "principled_count": len(nodes)})
    principled = nodes[0]
    supported = {
        "Base Color": 4,
        "Emission Color": 4,
        "Emission Strength": 1,
    }
    sockets = {}
    for socket_name, width in supported.items():
        socket = principled.inputs.get(socket_name)
        if socket is None:
            _fail("UNSUPPORTED",
                  "Principled node has no " + socket_name + " input",
                  {"material": material.name})
        try:
            path = socket.path_from_id("default_value")
            if tree.path_resolve(socket.path_from_id()) != socket:
                raise ValueError("socket identity")
            if socket.is_property_readonly("default_value"):
                raise ValueError("readonly")
            expected_type = "VALUE" if width == 1 else "RGBA"
            if socket.type != expected_type:
                raise ValueError("type " + str(socket.type))
            values = list(socket.default_value) if width > 1 else [socket.default_value]
            if len(values) != width or not all(
                    math.isfinite(float(value)) for value in values):
                raise ValueError("nonfinite")
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            _fail("CONFLICT", socket_name + " selection is stale or unsupported",
                  {"material": material.name, "error": str(exc)})
        sockets[path] = {"name": socket_name, "socket": socket,
                         "path": path, "width": width}
    usage = _material_usage(material)
    if len(usage) != 1 or usage[0].parent is None or usage[0].parent.name not in rig_names:
        _fail("CONFLICT", "Clip material must belong to exactly one participant rig mesh",
              {"material": material.name, "users": [obj.name for obj in usage]})
    action, slot, bag = _exclusive_action(tree, "NODETREE", "Material node tree")
    actual = _curve_map(bag)
    allowed = {(path, index) for path, row in sockets.items()
               for index in range(row["width"])}
    if not actual or not set(actual) <= allowed:
        _fail("UNSUPPORTED",
              "Material Action must animate only Principled Base Color/Emission Color/"
              "Emission Strength input channels",
              {"material": material.name, "actual": sorted(actual),
               "allowed": sorted(allowed)})
    channels = []
    for address in sorted(actual):
        curve = actual[address]
        row = sockets.get(address[0])
        if row is None:
            _fail("UNSUPPORTED", "Material Action channel is outside the supported sockets",
                  {"material": material.name, "path": address[0]})
        if row["socket"].is_linked:
            _fail("UNSUPPORTED",
                  row["name"] + " must be an unlinked native input",
                  {"material": material.name})
        _validate_keys(curve, "Material " + material.name + " " + row["name"])
        spec = {"id": None,
                "owner": {"type": "MATERIAL_NODETREE", "name": material.name},
                "data_path": address[0], "array_index": address[1],
                "sharing": "reject"}
        spec["id"] = event_ui_state.target_id(spec)
        channels.append({"target": spec, "owner": tree, "domain": "material",
                         "action": action, "slot": slot, "bag": bag,
                         "curve": curve, "role": "material",
                         "label": _label_for(tree, spec, "material")})
    return channels


def _resolve_participants(context, request):
    scene, layer = _resolve_context(context, request)
    participants = request["participants"]
    objects = []
    if participants.get(OBJECT_FIELD):
        if any(participants[field] for field in
               ("rigs", "roots", "lights", "materials")):
            _fail("INVALID_REQUEST",
                  "Explicit objects mode requires empty rigs/roots/lights/materials")
        objects = [_scene_object_in_context(scene, layer, name, "Object")
                   for name in participants[OBJECT_FIELD]]
        rigs = []
        roots = []
        lights = []
        materials = []
        channels = []
        for obj in objects:
            channels.extend(_object_channels(scene, layer, obj, request))
    else:
        rigs = []
        roots = []
        for name in participants["rigs"]:
            rigs.append(_object_in_context(scene, layer, name, "ARMATURE", "Rig"))
        for name in participants["roots"]:
            roots.append(_object_in_context(scene, layer, name, "EMPTY", "Root"))
        if len(rigs) != len(roots):
            _fail("INVALID_REQUEST", "Clip participants require one root per rig")
        root_by_name = {root.name: root for root in roots}
        paired = {}
        for rig in rigs:
            if rig.parent is None or rig.parent.name not in root_by_name:
                _fail("CONFLICT", "Every clip rig must be parented to a named participant root",
                      {"rig": rig.name})
            if root_by_name[rig.parent.name] != rig.parent:
                _fail("CONFLICT", "Clip rig parent identity changed")
            paired[rig.name] = rig.parent
        for root in roots:
            children = [rig for rig in rigs if rig.parent == root]
            if len(children) != 1:
                _fail("CONFLICT", "Every clip root must own exactly one participant rig",
                      {"root": root.name, "children": [rig.name for rig in children]})
            if root.parent is not None:
                _fail("CONFLICT", "Clip roots must remain parentless", {"root": root.name})
            if len(root.constraints):
                _fail("UNSUPPORTED", "Clip roots must remain unconstrained", {"root": root.name})
        lights = [_object_in_context(scene, layer, name, "LIGHT", "Light")
                  for name in participants["lights"]]
        materials = []
        for name in participants["materials"]:
            material = bpy.data.materials.get(name)
            if material is None:
                _fail("NOT_FOUND", "Participant material does not exist: " + name)
            materials.append(material)

        channels = []
        for rig in rigs:
            channels.extend(_body_channels(scene, layer, rig, paired[rig.name], request))
        for root in roots:
            channels.extend(_root_channels(root, request))
        for light in lights:
            channels.extend(_light_channels(light, request))
        rig_names = {rig.name for rig in rigs}
        for material in materials:
            channels.extend(_material_channels(material, rig_names, request))
    if not 1 <= len(channels) <= 256:
        _fail("UNSUPPORTED", "Clip event channel budget must be 1..256",
              {"channels": len(channels)})
    target_ids = [channel["target"]["id"] for channel in channels]
    if len(target_ids) != len(set(target_ids)):
        _fail("UNSUPPORTED", "Duplicate clip target address")
    action_owners = {}
    owner_actions = {}
    for channel in channels:
        action = channel["action"]
        action_owners.setdefault(id(action), set()).add(channel["owner"].as_pointer())
        owner_actions.setdefault(channel["owner"].as_pointer(), set()).add(id(action))
    if any(len(owners) != 1 for owners in action_owners.values()):
        _fail("CONFLICT", "One Action is shared by unrelated clip owners")
    if any(len(actions) != 1 for actions in owner_actions.values()):
        _fail("CONFLICT", "One clip owner resolves to multiple Actions")
    unique_actions = {id(channel["action"]): channel["action"]
                      for channel in channels}
    if len({action.name for action in unique_actions.values()}) != len(unique_actions):
        _fail("CONFLICT", "Duplicate Action names in participant set")
    return {"scene": scene, "layer": layer, "rigs": rigs, "roots": roots,
            "lights": lights, "materials": materials, "objects": objects,
            "channels": channels, "request": request}


def _collect_window(resolved, request):
    start = float(request["event"]["start"])
    contact = float(request["event"]["contact"])
    end = float(request["event"]["end"])
    total = 0
    by_domain = {"body": 0, "root": 0, "light": 0, "material": 0}
    if any(channel["domain"] == "object" for channel in resolved["channels"]):
        by_domain["object"] = 0
    for channel in resolved["channels"]:
        curve = channel["curve"]
        all_keys = list(curve.keyframe_points)
        window = [key for key in all_keys
                  if start - EPSILON <= float(key.co.x) <= end + EPSILON]
        if not window:
            _fail("UNSUPPORTED", "Every clip channel requires at least one key inside the event window",
                  {"channel": channel["label"]})
        contacts = [key for key in window
                    if abs(float(key.co.x) - contact) <= EPSILON]
        if len(contacts) != 1:
            _fail("UNSUPPORTED", "Every clip channel requires exactly one contact key",
                  {"channel": channel["label"], "contact": contact})
        allowed = _window_interpolations(channel["domain"])
        for key in window:
            if key.interpolation not in allowed:
                _fail("UNSUPPORTED",
                      "Clip retime requires native " + "/".join(sorted(allowed))
                      + " keys in the event window",
                      {"channel": channel["label"], "frame": float(key.co.x),
                       "interpolation": key.interpolation})
        channel["all_keys"] = all_keys
        channel["window_keys"] = sorted(window, key=lambda key: float(key.co.x))
        channel["contact_key"] = contacts[0]
        channel["witness_contact"] = contact
        channel["start_key"] = next(
            (key for key in window if abs(float(key.co.x) - start) <= EPSILON), None)
        channel["end_key"] = next(
            (key for key in window if abs(float(key.co.x) - end) <= EPSILON), None)
        total += len(window)
        by_domain[channel["domain"]] += len(window)
        if total > 4096:
            _fail("UNSUPPORTED", "Clip event binding budget exceeds 4096 keys")
    resolved["window_start"] = start
    resolved["window_contact"] = contact
    resolved["window_end"] = end
    resolved["binding_count"] = total
    resolved["bindings_by_domain"] = by_domain
    return resolved


def _window_semantics(channel):
    return [_semantic_key(key) for key in channel["window_keys"]]


def _channel_summary(channel):
    return {
        "target": channel["target"]["id"],
        "domain": channel["domain"],
        "owner": copy.deepcopy(channel["target"]["owner"]),
        "data_path": channel["target"]["data_path"],
        "array_index": int(channel["target"]["array_index"]),
        "action": channel["action"].name,
        "slot_identifier": channel["slot"].identifier,
        "slot_type": channel["slot"].target_id_type,
        "all_key_count": len(channel["all_keys"]),
        "window_key_count": len(channel["window_keys"]),
        "window_key_frames": [float(key.co.x) for key in channel["window_keys"]],
        "contact_key": abs(float(channel["contact_key"].co.x) - channel["witness_contact"]) <= EPSILON
        if "witness_contact" in channel else True,
        "start_key": channel["start_key"] is not None,
        "end_key": channel["end_key"] is not None,
    }


def _channel_summaries(resolved):
    out = []
    for channel in resolved["channels"]:
        row = _channel_summary(channel)
        out.append(row)
    return out


def _build_capture_record(resolved, request):
    state = event_ui_state.empty_clip()
    state["id"] = "0" * 32
    state["revision"] = 1
    state["context"] = copy.deepcopy(request["context"])
    state["participants"] = copy.deepcopy(request["participants"])
    state["locks"] = copy.deepcopy(request["locks"])
    event = {"id": request["event"]["id"],
             "frame": request["event"]["start"],
             "contact": request["event"]["contact"],
             "end": request["event"]["end"],
             "locked": True}
    state["events"] = {event["id"]: event}
    for channel in resolved["channels"]:
        target = copy.deepcopy(channel["target"])
        ident = event_ui_state.add_clip_target(
            state, target, _action_identity(channel["action"], channel["slot"]),
            channel["domain"])
        for key in channel["window_keys"]:
            event_ui_state.add_clip_binding(
                state, event["id"], ident, float(key.co.x), native.key_state(key))
    event_ui_state.check_clip_state(state)
    state["id"] = _sha(state)[:32]
    event_ui_state.check_clip_state(state)
    return state


@contextlib.contextmanager
def _selected_record_view(scene, text=None):
    """Private in-memory selected-record view used by the project layer.

    The canonical Scene property is never touched by this view.  Legacy
    review/execute validation remains authoritative for the selected record.
    Nested views and cross-scene access are rejected, and both the view and
    any pose permit are cleared on every exit path.
    """
    global _RECORD_VIEW
    if _RECORD_VIEW is not None:
        _fail("UNSUPPORTED", "Nested clip-event selected-record view refused")
    if _PERMIT is not None:
        _fail("UNSUPPORTED",
              "Selected-record view inside an active clip transaction refused")
    if text is not None and not isinstance(text, str):
        _fail("INVALID_REQUEST", "Selected clip record view requires text or None")
    if text is not None and len(text.encode("utf8")) > RECORD_MAX_BYTES:
        _fail("UNSUPPORTED", "Selected clip record exceeds its byte budget")
    view = {"scene_pointer": scene.as_pointer(), "text": text, "written": False}
    _RECORD_VIEW = view
    try:
        yield view
    finally:
        if _RECORD_VIEW is not view:
            _clear_permit()
            _RECORD_VIEW = None
            raise WorkflowError("ROLLBACK_FAILED",
                                "Clip selected-record view ownership changed")
        _RECORD_VIEW = None
        _clear_permit()


def _read_raw(scene):
    if _RECORD_VIEW is not None:
        if _RECORD_VIEW["scene_pointer"] != scene.as_pointer():
            _fail("CONFLICT",
                  "Selected clip record view belongs to another scene")
        return _RECORD_VIEW["text"]
    if RECORD_PROPERTY not in scene:
        return None
    value = scene.get(RECORD_PROPERTY)
    if not isinstance(value, str) or not value:
        _fail("CONFLICT", "Clip event record property is not a nonempty string")
    if len(value) > RECORD_MAX_BYTES:
        _fail("UNSUPPORTED", "Clip event record exceeds its byte budget")
    return value


def _decode_record(text):
    if not isinstance(text, str) or not text:
        _fail("NOT_FOUND", "No clip event record")
    if len(text.encode("utf8")) > RECORD_MAX_BYTES:
        _fail("UNSUPPORTED", "Clip event record exceeds its byte budget")

    def pairs(rows):
        out = {}
        for key, value in rows:
            if key in out:
                _fail("CONFLICT", "Clip event record has duplicate JSON fields")
            out[key] = value
        return out

    try:
        record = json.loads(text, object_pairs_hook=pairs)
    except WorkflowError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise WorkflowError("CONFLICT",
                            "Clip event record is not valid JSON") from exc
    event_ui_state.check_clip_state(record)
    parsed = _decode_record_structure(record)
    return parsed


def _decode_record_structure(record):
    required = {"schema", "policy", "id", "revision", "context", "participants",
                "locks", "targets", "events", "bindings"}
    if not required <= set(record):
        _fail("CONFLICT", "Clip event record is missing required fields",
              {"missing": sorted(required - set(record))})
    participants = record["participants"]
    if not isinstance(participants, dict):
        _fail("CONFLICT", "Clip event participants are malformed")
    fields = set(participants)
    legacy = {"rigs", "roots", "lights", "materials"}
    if fields == legacy:
        for field in legacy:
            names = participants[field]
            if not isinstance(names, list) or not names \
                    or len(names) != len(set(names)):
                _fail("CONFLICT", "Clip event participant names are malformed")
    elif fields == legacy | {OBJECT_FIELD}:
        names = participants[OBJECT_FIELD]
        if (not isinstance(names, list) or not 1 <= len(names) <= 8
                or len(names) != len(set(names))
                or any(not isinstance(name, str) or not name for name in names)):
            _fail("CONFLICT", "Clip event object participant names are malformed")
        for field in legacy:
            names = participants[field]
            if not isinstance(names, list) or names:
                _fail("CONFLICT",
                      "Clip event objects mode requires empty legacy participants")
    else:
        _fail("CONFLICT", "Clip event participants are malformed")
    event = next(iter(record["events"].values()))
    start, end = float(event["frame"]), float(event["end"])
    if not isinstance(record["locks"], list):
        _fail("CONFLICT", "Clip event locks are malformed")
    for frame in record["locks"]:
        if type(frame) not in (int, float) or not math.isfinite(frame):
            _fail("CONFLICT", "Clip event lock frame is not finite")
        if start <= float(frame) <= end:
            _fail("CONFLICT", "Clip event lock frame is inside the window")
    for ident, target in record["targets"].items():
        if not isinstance(target, dict) or target.get("domain") not in {
                "body", "root", "light", "material", "object"}:
            _fail("CONFLICT", "Clip event target domain is malformed")
        if target["domain"] == "object" and OBJECT_FIELD not in participants:
            _fail("CONFLICT",
                  "Legacy clip event record cannot contain object targets")
        for field in ("action_name", "slot_identifier", "slot_type"):
            if not isinstance(target.get(field), str) or not target[field]:
                _fail("CONFLICT", "Clip event target Action identity is malformed")
    return record


def _record_summary(record):
    if record is None:
        return None
    event = next(iter(record["events"].values()))
    return {
        "id": record["id"],
        "revision": record["revision"],
        "event": copy.deepcopy(event),
        "locks": copy.deepcopy(record["locks"]),
        "participants": copy.deepcopy(record["participants"]),
        "target_count": len(record["targets"]),
        "binding_count": len(record["bindings"]),
        "digest": _sha(record),
    }


def _current_event(record):
    """Exact request-shaped current event for cold-reopen CLI inspection."""
    if record is None:
        return None
    event = next(iter(record["events"].values()))
    return {
        "id": event["id"],
        "start": copy.deepcopy(event["frame"]),
        "contact": copy.deepcopy(event["contact"]),
        "end": copy.deepcopy(event["end"]),
    }


def _record_request(request, record):
    """Read-only request shape resolved from the persistent record."""
    out = copy.deepcopy(request)
    event = next(iter(record["events"].values()))
    out["event"] = {"id": event["id"], "start": event["frame"],
                    "contact": event["contact"], "end": event["end"]}
    out["context"] = copy.deepcopy(record["context"])
    out["participants"] = copy.deepcopy(record["participants"])
    out["locks"] = copy.deepcopy(record["locks"])
    return out


def _channel_overview(channel):
    return {
        "target": channel["target"]["id"],
        "domain": channel["domain"],
        "owner": copy.deepcopy(channel["target"]["owner"]),
        "data_path": channel["target"]["data_path"],
        "array_index": int(channel["target"]["array_index"]),
        "action": channel["action"].name,
        "all_key_count": len(channel["curve"].keyframe_points),
    }


def _encode_record(record):
    event_ui_state.check_clip_state(record)
    text = _canonical(record)
    if len(text.encode("utf8")) > RECORD_MAX_BYTES:
        _fail("UNSUPPORTED", "Clip event record exceeds its byte budget")
    return text


def _write_raw(scene, text):
    if _RECORD_VIEW is not None:
        if _RECORD_VIEW["scene_pointer"] != scene.as_pointer():
            _fail("CONFLICT",
                  "Selected clip record write targets another scene")
        if not isinstance(text, str) or not text:
            _fail("CONFLICT", "Selected clip record text is empty or malformed")
        if len(text.encode("utf8")) > RECORD_MAX_BYTES:
            _fail("UNSUPPORTED", "Selected clip record exceeds its byte budget")
        _RECORD_VIEW["text"] = text
        _RECORD_VIEW["written"] = True
        if _RECORD_VIEW["text"] != text:
            _fail("VALIDATION_FAILED",
                  "Selected clip record readback differs from the write")
        return
    try:
        scene[RECORD_PROPERTY] = text
    except Exception as exc:
        raise WorkflowError("CONFLICT",
                            "Could not write the clip event record") from exc
    if scene.get(RECORD_PROPERTY) != text:
        _fail("VALIDATION_FAILED", "Clip event record readback differs from the write")


def _restore_raw(scene, prior):
    if _RECORD_VIEW is not None:
        if _RECORD_VIEW["scene_pointer"] != scene.as_pointer():
            _fail("CONFLICT",
                  "Selected clip record restore targets another scene")
        _RECORD_VIEW["text"] = prior
        return
    if prior is None:
        if RECORD_PROPERTY in scene:
            del scene[RECORD_PROPERTY]
    else:
        scene[RECORD_PROPERTY] = prior


def _match_record_request(record, request):
    if record["context"] != request["context"]:
        _fail("CONFLICT", "Clip record context differs from the current request")
    for field in ("rigs", "roots", "lights", "materials", OBJECT_FIELD):
        if sorted(record["participants"].get(field, [])) != sorted(
                request["participants"].get(field, [])):
            _fail("CONFLICT", "Clip record participants differ from the current request",
                  {"field": field})
    if sorted(float(v) for v in record["locks"]) != sorted(float(v) for v in request["locks"]):
        _fail("CONFLICT", "Clip record locks differ from the current request")
    event = next(iter(record["events"].values()))
    if request["event"]["id"] != event["id"]:
        _fail("CONFLICT", "Clip record event id differs from the current request")
    for field, recorded_field in (("start", "frame"), ("contact", "contact"),
                                  ("end", "end")):
        if float(request["event"][field]) != float(event[recorded_field]):
            _fail("CONFLICT", "Clip record event time differs from the current request",
                  {"field": field})
    return event


def _match_record_targets(record, resolved):
    current = {channel["target"]["id"]: channel for channel in resolved["channels"]}
    recorded = record["targets"]
    if set(current) != set(recorded):
        _fail("CONFLICT", "Clip record target inventory differs from the current scene",
              {"missing": sorted(set(recorded) - set(current)),
               "extra": sorted(set(current) - set(recorded))})
    for ident, channel in current.items():
        target = recorded[ident]
        spec = {"id": ident, **{k: target[k] for k in (
            "owner", "data_path", "array_index", "sharing")}}
        if event_ui_state.target_id(spec) != ident:
            _fail("CONFLICT", "Clip record target address hash is invalid", {"target": ident})
        if (target["owner"] != channel["target"]["owner"]
                or target["data_path"] != channel["target"]["data_path"]
                or int(target["array_index"]) != int(channel["target"]["array_index"])
                or target["domain"] != channel["domain"]):
            _fail("CONFLICT", "Clip record target identity changed", {"target": ident})
        identity = _action_identity(channel["action"], channel["slot"])
        if (target["action_name"] != identity["action_name"]
                or target["slot_identifier"] != identity["slot_identifier"]
                or target["slot_type"] != identity["slot_type"]):
            _fail("CONFLICT", "Clip record Action/slot identity changed; inspect and recapture",
                  {"target": ident, "recorded": {
                      "action_name": target["action_name"],
                      "slot_identifier": target["slot_identifier"],
                      "slot_type": target["slot_type"]},
                   "current": identity})
    return current


def _constraint_witness(constraint):
    row = {"name": constraint.name, "type": constraint.type,
           "owner_space": constraint.owner_space,
           "mute": bool(constraint.mute),
           "influence": float(constraint.influence)}
    if constraint.type == "LIMIT_ROTATION":
        row.update({
            "use_transform_limit": bool(constraint.use_transform_limit),
            "use_limit_x": bool(constraint.use_limit_x),
            "use_limit_y": bool(constraint.use_limit_y),
            "use_limit_z": bool(constraint.use_limit_z),
            "min_x": float(constraint.min_x), "max_x": float(constraint.max_x),
            "min_y": float(constraint.min_y), "max_y": float(constraint.max_y),
            "min_z": float(constraint.min_z), "max_z": float(constraint.max_z),
        })
    return row


def _custom_sha(item):
    return _sha(native.custom_state(item))


def _mesh_geometry_sha(obj):
    data = obj.data
    payload = {
        "vertices": [
            {"co": [float(v) for v in vertex.co],
             "groups": [[int(g.group), float(g.weight)] for g in vertex.groups]}
            for vertex in data.vertices
        ],
        "polygons": [list(poly.vertices) for poly in data.polygons],
        "edges": [list(edge.vertices) for edge in data.edges],
    }
    return _sha(payload)


def _bone_rest_sha(armature):
    payload = [{
        "name": bone.name,
        "parent": bone.parent.name if bone.parent else None,
        "head_local": [float(v) for v in bone.head_local],
        "tail_local": [float(v) for v in bone.tail_local],
        "matrix_local": [[float(v) for v in line] for line in bone.matrix_local],
        "use_deform": bool(bone.use_deform),
    } for bone in armature.bones]
    return _sha(payload)


def _structure_witness(context, resolved):
    scene = resolved["scene"]
    witness = {
        "scene": {
            "name": scene.name, "frame_start": int(scene.frame_start),
            "frame_end": int(scene.frame_end),
            "fps": int(scene.render.fps),
            "fps_base": float(scene.render.fps_base),
            "frame_current": int(scene.frame_current),
            "frame_subframe": float(scene.frame_subframe),
            "mode": context.mode,
        },
        "rigs": [],
        "roots": [],
        "lights": [],
        "materials": [],
    }
    for rig in resolved["rigs"]:
        row = {
            "name": rig.name, "data": rig.data.name,
            "uuid": rig.get("ac_instance_uuid"),
            "parent": rig.parent.name if rig.parent else None,
            "users": int(rig.users), "data_users": int(rig.data.users),
            "custom_sha256": _custom_sha(rig),
            "data_custom_sha256": _custom_sha(rig.data),
            "matrix_basis": [[float(v) for v in line] for line in rig.matrix_basis],
            "matrix_parent_inverse": [[float(v) for v in line]
                                      for line in rig.matrix_parent_inverse],
            "bones": [{"name": bone.name,
                       "parent": bone.parent.name if bone.parent else None,
                       "use_deform": bool(bone.use_deform)}
                      for bone in rig.data.bones],
            "bone_rest_sha256": _bone_rest_sha(rig.data),
            "pose_modes": {pose_bone.name: pose_bone.rotation_mode
                           for pose_bone in rig.pose.bones},
            "constraints": {
                pose_bone.name: [_constraint_witness(constraint)
                                 for constraint in pose_bone.constraints]
                for pose_bone in rig.pose.bones if pose_bone.constraints
            },
            "meshes": [],
        }
        for child in sorted((obj for obj in scene.objects
                             if obj.type == "MESH" and obj.parent == rig),
                            key=lambda obj: obj.name):
            row["meshes"].append({
                "name": child.name, "data": child.data.name,
                "vertices": len(child.data.vertices),
                "polygons": len(child.data.polygons),
                "vertex_groups": [group.name for group in child.vertex_groups],
                "materials": [material.name if material else None
                              for material in child.data.materials],
                "modifiers": [{"type": modifier.type,
                               "object": modifier.object.name
                               if getattr(modifier, "object", None) else None}
                              for modifier in child.modifiers],
                "color": [float(v) for v in child.color],
                "custom_sha256": _custom_sha(child),
                "data_users": int(child.data.users),
                "geometry_sha256": _mesh_geometry_sha(child),
            })
        witness["rigs"].append(row)
    for root in resolved["roots"]:
        witness["roots"].append({
            "name": root.name, "type": root.type,
            "parent": root.parent.name if root.parent else None,
            "rotation_mode": root.rotation_mode,
            "users": int(root.users),
            "custom_sha256": _custom_sha(root),
            "constraints": [_constraint_witness(constraint)
                            for constraint in root.constraints],
        })
    for light in resolved["lights"]:
        witness["lights"].append({
            "name": light.name, "data": light.data.name,
            "type": light.data.type, "data_users": int(light.data.users),
            "custom_sha256": _custom_sha(light),
            "data_custom_sha256": _custom_sha(light.data),
        })
    for material in resolved["materials"]:
        tree = material.node_tree
        witness["materials"].append({
            "name": material.name, "users": int(material.users),
            "tree_users": int(tree.users) if tree else None,
            "diffuse_color": [float(v) for v in material.diffuse_color],
            "custom_sha256": _custom_sha(material),
            "tree_custom_sha256": _custom_sha(tree) if tree else None,
        })
    if resolved.get("objects"):
        witness["objects"] = []
        for obj in resolved["objects"]:
            row = {
                "name": obj.name, "type": obj.type,
                "parent": obj.parent.name if obj.parent else None,
                "rotation_mode": obj.rotation_mode,
                "users": int(obj.users),
                "custom_sha256": _custom_sha(obj),
                "constraints": [_constraint_witness(constraint)
                                for constraint in obj.constraints],
                "delta_location": [float(v) for v in obj.delta_location],
                "delta_rotation_euler": [float(v) for v in obj.delta_rotation_euler],
                "delta_scale": [float(v) for v in obj.delta_scale],
                "color": [float(v) for v in obj.color],
                "instance_type": obj.instance_type,
                "hide_render": bool(obj.hide_render),
                "hide_viewport": bool(obj.hide_viewport),
            }
            if obj.type == "MESH":
                data = obj.data
                row.update({
                    "data": data.name,
                    "data_users": int(data.users),
                    "data_custom_sha256": _custom_sha(data),
                    "vertices": len(data.vertices),
                    "polygons": len(data.polygons),
                    "vertex_groups": [group.name for group in obj.vertex_groups],
                    "materials": [material.name if material else None
                                  for material in data.materials],
                    "modifiers": [{"type": modifier.type,
                                   "object": modifier.object.name
                                   if getattr(modifier, "object", None) else None}
                                  for modifier in obj.modifiers],
                    "geometry_sha256": _mesh_geometry_sha(obj),
                })
            else:
                row.update({"data": None, "data_users": None,
                            "data_custom_sha256": None,
                            "geometry_sha256": None})
            witness["objects"].append(row)
    return witness


def _lock_frames(request):
    start = float(request["event"]["start"])
    end = float(request["event"]["end"])
    frames = {start, end}
    frames.update(float(frame) for frame in request["locks"])
    return sorted(frames)


def _lock_witness(resolved, request):
    rows = []
    frames = _lock_frames(request)
    for channel in resolved["channels"]:
        curve = channel["curve"]
        for frame in frames:
            key = native.find_key(curve, frame)
            rows.append({
                "target": channel["target"]["id"],
                "frame": frame,
                "value": native.curve_value(curve, frame, 0.0),
                "key": _semantic_key(key) if key is not None else None,
            })
    return rows


def _origin(context, request, resolved, record_raw=None, record=None):
    # This memo exists only during this single immutable freshness snapshot.
    # Every later review/execute re-reads the complete current Action state.
    action_shas = {}
    def current_action_sha(action):
        if action is None:
            return None
        pointer = action.as_pointer()
        if pointer not in action_shas:
            action_shas[pointer] = _action_sha(action)
        return action_shas[pointer]

    owners = {}
    for channel in resolved["channels"]:
        owner = channel["owner"]
        if owner.as_pointer() in owners:
            continue
        owners[owner.as_pointer()] = {
            "type": owner.bl_rna.identifier,
            "name": owner.name,
            "binding": {
                "had_animation_data": bool(owner.animation_data),
                "action": channel["action"].name if channel["action"] else None,
                "slot_identifier": channel["slot"].identifier if channel["slot"] else None,
                "slot_type": channel["slot"].target_id_type if channel["slot"] else None,
                "settings": dict(native.binding_state(owner)["settings"]),
            },
            "action_sha256": current_action_sha(channel["action"]),
            "effective_users": native.effective_users(channel["action"]),
        }
    targets = []
    for channel in resolved["channels"]:
        targets.append({
            "target": channel["target"]["id"],
            "domain": channel["domain"],
            "owner": copy.deepcopy(channel["target"]["owner"]),
            "data_path": channel["target"]["data_path"],
            "array_index": int(channel["target"]["array_index"]),
            "action": channel["action"].name,
            "slot_identifier": channel["slot"].identifier,
            "slot_type": channel["slot"].target_id_type,
            "action_sha256": current_action_sha(channel["action"]),
            "window_sha256": _sha(_window_semantics(channel)),
            "window_key_count": len(channel["window_keys"]),
            "contact_key_sha256": _sha(_semantic_key(channel["contact_key"])),
            "start_key_sha256": (_sha(_semantic_key(channel["start_key"]))
                                 if channel["start_key"] else None),
            "end_key_sha256": (_sha(_semantic_key(channel["end_key"]))
                               if channel["end_key"] else None),
        })
    structure = _structure_witness(context, resolved)
    return {
        "scene": copy.deepcopy(structure["scene"]),
        "structure_sha256": _sha(structure),
        "structure": structure,
        "owners": [owners[key] for key in sorted(owners)],
        "targets": targets,
        "lock_witness": _lock_witness(resolved, request),
        "record": {
            "raw_sha256": _sha(record_raw) if record_raw is not None else None,
            "summary": _record_summary(record),
        },
    }


def _seal_review(review):
    body = {key: value for key, value in review.items() if key != "review_sha256"}
    review["review_sha256"] = _sha(body)
    return review


def _warp_by_frame(warp):
    return {round(float(row["source"]), 9): float(row["native"])
            for row in warp["rows"]}


def _derived_handle_scope(resolved, start, end):
    targets = []
    local_keys = []
    has_mixed = False
    for channel in resolved["channels"]:
        curve = channel["curve"]
        keys = list(curve.keyframe_points)
        if any(key.interpolation != "BEZIER" for key in keys):
            continue
        mixed = any(key.handle_left_type not in handle_scope.AUTO
                    or key.handle_right_type not in handle_scope.AUTO for key in keys)
        has_mixed = has_mixed or mixed
        targets.append(channel["target"]["id"])
        for key in keys:
            if (start - 2.0 - EPSILON <= float(key.co.x) <= end + 2.0 + EPSILON
                    and key.handle_left_type in handle_scope.AUTO
                    and key.handle_right_type in handle_scope.AUTO):
                local_keys.append({"target": channel["target"]["id"],
                                   "expected_key": native.key_state(key)})
    if not targets:
        return None, []
    if has_mixed:
        return {"schema": "1.0", "policy": handle_scope.LOCAL_POLICY,
                "keys": local_keys}, sorted(set(targets))
    return {"schema": "1.0", "policy": handle_scope.CHANNEL_POLICY,
            "targets": sorted(set(targets))}, sorted(set(targets))


def _retime_plan(resolved, request, record):
    start = float(request["event"]["start"])
    contact = float(request["event"]["contact"])
    target_contact = float(request["event"]["target_contact"])
    end = float(request["event"]["end"])
    if target_contact == contact:
        _fail("INVALID_REQUEST", "retime requires a target_contact that differs from the recorded contact")
    for value, label in ((start, "event.start"), (contact, "event.contact"),
                         (target_contact, "event.target_contact"), (end, "event.end")):
        _exact_f32(value, label)
    for frame in request["locks"]:
        _exact_f32(frame, "lock frame")

    current = _match_record_targets(record, resolved)
    current_semantics = {ident: _window_semantics(channel)
                         for ident, channel in current.items()}
    drift = event_ui_state.compare_clip_state(record, current_semantics)
    stale = [row for row in drift if row["status"] != "unchanged"]
    if stale:
        _fail("CONFLICT",
              "Recorded clip target keys changed; inspect and explicitly recapture before retime",
              {"bindings": stale})

    union_frames = sorted({float(key.co.x) for channel in resolved["channels"]
                           for key in channel["window_keys"]})
    warp = events.plan_local_warp(union_frames, start, contact,
                                  target_contact, end)
    mapping = _warp_by_frame(warp)
    operations = []
    bindings = []
    keys_by_target = {}
    for channel in resolved["channels"]:
        ident = channel["target"]["id"]
        keys_by_target[ident] = [native.key_state(key)
                                 for key in channel["all_keys"]]
        for key in channel["window_keys"]:
            source = float(key.co.x)
            destination = mapping[round(source, 9)]
            bindings.append({"event": request["event"]["id"], "target": ident,
                             "offset": source - start,
                             "expected_key": native.key_state(key)})
            if abs(destination - source) > EPSILON:
                operations.append({"op": "move_key", "target": ident,
                                   "frame": source, "to_frame": destination})
    ordered = events.validate_native_bindings(
        {"bindings": bindings, "operations": operations},
        keys_by_target, EPSILON)
    derived, derived_frames = _derived_handle_scope(resolved, start, end)
    if derived is not None and derived["policy"] == handle_scope.LOCAL_POLICY:
        touched = {}
        for op in ordered:
            touched.setdefault(op["target"], set()).update((op["frame"], op["to_frame"]))
        derived["keys"] = [item for item in derived["keys"]
                           if item["target"] in touched
                           and not any(abs(item["expected_key"]["co"][0] - frame) <= EPSILON
                                       for frame in touched[item["target"]])]
        if not derived["keys"]:
            derived = None
            derived_frames = []

    targets = [copy.deepcopy(channel["target"]) for channel in resolved["channels"]]
    lock_rows = []
    lock_frames = _lock_frames(request)
    for channel in resolved["channels"]:
        for frame in lock_frames:
            lock_rows.append({"target": channel["target"]["id"],
                              "frame": frame, "tolerance": EPSILON})
    manifest = {
        "schema": "1.0", "operation": "edit",
        "context": copy.deepcopy(request["context"]),
        "targets": targets,
        "operations": ordered,
        "samples": sorted({start, contact, target_contact, end}
                          | {float(frame) for frame in request["locks"]}),
        "locks": lock_rows,
    }
    if derived is not None:
        manifest["derived_handle_scope"] = derived
    manifest = normalize_manifest(manifest, lock_limit=len(targets) * len(lock_frames))
    return {
        "manifest": manifest,
        "warp": warp,
        "mapping": mapping,
        "operations": len(ordered),
        "bindings": len(bindings),
        "derived_targets": derived_frames,
        "handle_scope_items": len(derived_frames),
        "current": current,
    }


def _counts(resolved, plan=None):
    by_domain = {domain: 0 for domain in ("body", "root", "light", "material")}
    if any(channel["domain"] == "object" for channel in resolved["channels"]):
        by_domain["object"] = 0
    for channel in resolved["channels"]:
        by_domain[channel["domain"]] += len(channel["window_keys"])
    return {
        "channels": len(resolved["channels"]),
        "bindings": resolved["binding_count"],
        "bindings_by_domain": by_domain,
        "operations": 0 if plan is None else plan["operations"],
    }


def _handle_impact(plan):
    if plan is None:
        return None
    return {
        "policy": plan["manifest"].get("derived_handle_scope", {}).get("policy"),
        "declared_targets": plan["handle_scope_items"],
        "target_ids": copy.deepcopy(plan["derived_targets"]),
        "local_impact_frames": "[start-2, end+2] per channel plus all event-window keys",
        "scope": ("Only AUTO/AUTO_CLAMPED handle coordinates on declared channels may be "
                  "recomputed by Blender; exact non-target handle coordinates outside the "
                  "local impact domain are restored after the native update. Key time/value/"
                  "type/interpolation/easing and all non-handle data remain exact everywhere."),
    }


def review(context, request):
    """Read-only freshness review for capture or retime. Never writes."""
    request = normalize_request(request)
    scene, layer = _resolve_context(context, request)
    resolved = _resolve_participants(context, request)
    _collect_window(resolved, request)
    raw = _read_raw(scene)
    record = _decode_record(raw) if raw is not None else None

    if request["action"] == "capture":
        if record is not None:
            _fail("CONFLICT", "A clip event record already exists; use inspect/retime or clear it explicitly",
                  {"record": _record_summary(record)})
        proposed = _build_capture_record(resolved, request)
        origin = _origin(context, request, resolved)
        body = {
            "schema": REVIEW_SCHEMA,
            "action": "capture",
            "request": request,
            "event": copy.deepcopy(request["event"]),
            "channels": _channel_summaries(resolved),
            "counts": _counts(resolved),
            "record": proposed,
            "record_sha256": _sha(proposed),
            "origin": origin,
            "manifest": None,
            "evaluation_boundary": ("Native FCurve keys and native Action bindings only; "
                                    "no rendered appearance or spatial contact proof"),
        }
        return _seal_review(body)

    if record is None:
        _fail("NOT_FOUND", "No clip event record exists to retime")
    _match_record_request(record, request)
    plan = _retime_plan(resolved, request, record)
    origin = _origin(context, request, resolved, raw, record)
    body = {
        "schema": REVIEW_SCHEMA,
        "action": "retime",
        "request": request,
        "event": copy.deepcopy(request["event"]),
        "channels": _channel_summaries(resolved),
        "counts": _counts(resolved, plan),
        "record": _record_summary(record),
        "record_raw_sha256": _sha(raw),
        "origin": origin,
        "warp": {
            "start": plan["warp"]["start"],
            "contact": plan["warp"]["contact"],
            "target_contact": plan["warp"]["target_contact"],
            "end": plan["warp"]["end"],
            "left_slope": plan["warp"]["left_slope"],
            "right_slope": plan["warp"]["right_slope"],
            "min_gap": plan["warp"]["min_gap"],
            "quantization": copy.deepcopy(plan["warp"]["quantization"]),
            "rows": copy.deepcopy(plan["warp"]["rows"]),
        },
        "handle_impact": _handle_impact(plan),
        "manifest": plan["manifest"],
        "evaluation_boundary": ("Native FCurve keys and native Action bindings only; "
                                "no rendered appearance or spatial contact proof"),
    }
    return _seal_review(body)


def _inspect_capture(context, request):
    scene, layer = _resolve_context(context, request)
    raw = _read_raw(scene)
    record = _decode_record(raw) if raw is not None else None
    inspect_request = _record_request(request, record) if record is not None else request
    resolved = _resolve_participants(context, inspect_request)
    _collect_window(resolved, inspect_request)
    return {
        "schema": INSPECT_SCHEMA,
        "action": "capture",
        "status": "record_exists" if record is not None else "ready",
        "request": request,
        "current_event": _current_event(record),
        "record": _record_summary(record),
        "channels": _channel_summaries(resolved),
        "counts": _counts(resolved),
        "native_animation_changed": False,
        "evaluation_boundary": "Read-only native FCurve inspection; no record is created",
    }


def _inspect_retime(context, request):
    scene, layer = _resolve_context(context, request)
    raw = _read_raw(scene)
    if raw is None:
        resolved = _resolve_participants(context, request)
        return {
            "schema": INSPECT_SCHEMA,
            "action": "retime",
            "status": "no_record",
            "request": request,
            "current_event": None,
            "record": None,
            "channels": [_channel_overview(channel)
                         for channel in resolved["channels"]],
            "counts": {"channels": len(resolved["channels"]),
                       "bindings": 0, "bindings_by_domain": {}, "operations": 0},
            "native_animation_changed": False,
            "evaluation_boundary": "Read-only; capture must be committed before retime",
        }
    record = _decode_record(raw)
    inspect_request = _record_request(request, record)
    resolved = _resolve_participants(context, inspect_request)
    _collect_window(resolved, inspect_request)
    try:
        _match_record_request(record, request)
        if float(request["event"]["target_contact"]) == float(request["event"]["contact"]):
            return {
                "schema": INSPECT_SCHEMA,
                "action": "retime",
                "status": "current",
                "request": request,
                "current_event": _current_event(record),
                "record": _record_summary(record),
                "channels": _channel_summaries(resolved),
                "counts": _counts(resolved),
                "warp": None,
                "handle_impact": None,
                "native_animation_changed": False,
                "evaluation_boundary": ("Read-only current persistent event; "
                                        "target_contact equals the record contact so no retime plan is built"),
            }
        plan = _retime_plan(resolved, request, record)
    except WorkflowError as exc:
        return {
            "schema": INSPECT_SCHEMA,
            "action": "retime",
            "status": "blocked",
            "request": request,
            "current_event": _current_event(record),
            "record": _record_summary(record),
            "channels": _channel_summaries(resolved),
            "counts": _counts(resolved),
            "blocked": {"code": exc.code, "message": str(exc), "details": exc.details},
            "native_animation_changed": False,
            "evaluation_boundary": "Read-only; blocked requests are not applied",
        }
    return {
        "schema": INSPECT_SCHEMA,
        "action": "retime",
        "status": "ready",
        "request": request,
        "current_event": _current_event(record),
        "record": _record_summary(record),
        "channels": _channel_summaries(resolved),
        "counts": _counts(resolved, plan),
        "warp": {
            "start": plan["warp"]["start"],
            "contact": plan["warp"]["contact"],
            "target_contact": plan["warp"]["target_contact"],
            "end": plan["warp"]["end"],
            "left_slope": plan["warp"]["left_slope"],
            "right_slope": plan["warp"]["right_slope"],
            "min_gap": plan["warp"]["min_gap"],
            "quantization": copy.deepcopy(plan["warp"]["quantization"]),
        },
        "handle_impact": _handle_impact(plan),
        "native_animation_changed": False,
        "evaluation_boundary": "Read-only native FCurve inspection; no record is created",
    }


def inspect(context, request):
    """Read-only shared GUI/CLI inspection. Never creates or updates a record."""
    request = normalize_request(request)
    if request["action"] == "capture":
        return _inspect_capture(context, request)
    return _inspect_retime(context, request)


def _install_permit(manifest):
    global _PERMIT
    if active():
        _fail("UNSUPPORTED", "Nested clip-event transaction refused")
    addresses = set()
    for target in manifest["targets"]:
        if target["owner"]["type"] != "OBJECT" or not target["data_path"].startswith("pose."):
            continue
        owner = bpy.data.objects.get(target["owner"]["name"])
        if owner is None:
            _fail("NOT_FOUND", "Pose permit owner does not exist",
                  {"owner": target["owner"]["name"]})
        uuid = owner.get("ac_instance_uuid")
        addresses.add((owner.as_pointer(), uuid, target["data_path"],
                       int(target["array_index"])))
    _PERMIT = {
        "addresses": addresses,
        "manifest": manifest,
        "manifest_ids": set(),
        "validated": set(),
    }


def _clear_permit():
    global _PERMIT
    _PERMIT = None


def _domain_map_from_review(review_dict):
    return {row["target"]: row["domain"] for row in review_dict["channels"]}


def _channels_from_rows(rows, domain_by_target, start, contact, end):
    channels = []
    for row in rows:
        spec = row["spec"]
        ident = spec["id"]
        curve = row["curve"]
        if curve is None:
            _fail("NOT_FOUND", "Clip target channel disappeared", {"target": ident})
        all_keys = list(curve.keyframe_points)
        window = [key for key in all_keys
                  if start - EPSILON <= float(key.co.x) <= end + EPSILON]
        contacts = [key for key in window
                    if abs(float(key.co.x) - contact) <= EPSILON]
        if len(contacts) != 1:
            _fail("CONFLICT", "Clip channel does not have exactly one expected contact key",
                  {"target": ident, "contact": contact})
        domain = domain_by_target[ident]
        allowed = _window_interpolations(domain)
        for key in window:
            if key.interpolation not in allowed:
                _fail("CONFLICT", "Committed clip key interpolation changed",
                      {"target": ident, "frame": float(key.co.x),
                       "interpolation": key.interpolation})
        channels.append({
            "target": spec, "owner": row["owner"], "domain": domain,
            "action": row["action"], "slot": row["slot"], "bag": row["bag"],
            "curve": curve, "all_keys": all_keys,
            "window_keys": sorted(window, key=lambda key: float(key.co.x)),
            "contact_key": contacts[0],
            "start_key": next((key for key in window
                               if abs(float(key.co.x) - start) <= EPSILON), None),
            "end_key": next((key for key in window
                             if abs(float(key.co.x) - end) <= EPSILON), None),
            "witness_contact": contact,
            "label": _label_for(row["owner"], spec, domain),
        })
    return channels


def _unique_owner_rows(rows):
    owners = {}
    for row in rows:
        owner = row["owner"]
        if owner.as_pointer() in owners:
            continue
        owners[owner.as_pointer()] = {
            "owner": owner,
            "binding": native.binding_state(owner),
            "action": row["action"],
            "slot": row["slot"],
        }
    return owners


def _compare_cache_before_after(before_channels, after_channels, mapping,
                                local_frames, start, end):
    if {row["target"]["id"] for row in before_channels} != {
            row["target"]["id"] for row in after_channels}:
        _fail("CONFLICT", "Clip target inventory changed during commit")
    before_by_id = {row["target"]["id"]: row for row in before_channels}
    after_by_id = {row["target"]["id"]: row for row in after_channels}
    handle_changes = []
    total_before = total_after = 0
    for ident, before in before_by_id.items():
        after = after_by_id[ident]
        before_keys = {round(float(key.co.x), 9): _semantic_key(key)
                       for key in before["all_keys"]}
        after_keys = {round(float(key.co.x), 9): _semantic_key(key)
                      for key in after["all_keys"]}
        total_before += len(before_keys)
        total_after += len(after_keys)
        expected = {}
        for frame, semantic in before_keys.items():
            in_window = start - EPSILON <= frame <= end + EPSILON
            destination = mapping[round(frame, 9)] if in_window else frame
            expected[round(float(destination), 9)] = (frame, destination, semantic,
                                                      in_window)
        if set(after_keys) != set(expected):
            _fail("CONFLICT", "Clip key inventory/time map changed unexpectedly",
                  {"target": ident,
                   "missing": sorted(set(expected) - set(after_keys)),
                   "extra": sorted(set(after_keys) - set(expected))})
        declared = local_frames.get(ident, set())
        for destination, (source, wanted, before_key, in_window) in expected.items():
            after_key = after_keys[destination]
            if abs(float(after_key["co"][0]) - float(wanted)) > EPSILON:
                _fail("CONFLICT", "Committed key time differs from the local warp",
                      {"target": ident, "source": source,
                       "expected": wanted, "actual": after_key["co"][0]})
            if float(after_key["co"][1]) != float(before_key["co"][1]):
                _fail("CONFLICT", "Committed key value changed",
                      {"target": ident, "source": source})
            for field in ("interpolation", "easing", "back", "amplitude",
                          "period", "type", "handle_left_type", "handle_right_type"):
                if field in before_key and after_key.get(field) != before_key[field]:
                    _fail("CONFLICT", "Committed key metadata changed",
                          {"target": ident, "source": source, "field": field})
            moved = in_window and abs(float(wanted) - float(source)) > EPSILON
            local = any(abs(float(source) - float(frame)) <= EPSILON
                        for frame in declared)
            for field in ("handle_left", "handle_right"):
                old_handle = before_key.get(field)
                new_handle = after_key.get(field)
                if old_handle == new_handle:
                    continue
                change = {"target": ident, "frame": float(source),
                          "destination": float(wanted), "field": field,
                          "before": old_handle, "after": new_handle,
                          "class": ("moved_key" if moved else
                                    "local_auto" if local else "extra")}
                if moved:
                    handle_type = before_key.get(
                        "handle_left_type" if field == "handle_left"
                        else "handle_right_type")
                    if handle_type not in handle_scope.AUTO:
                        expected_handle = [
                            float(old_handle[0]) + (float(wanted) - float(source)),
                            float(old_handle[1]),
                        ]
                        if not all(_close(a, b) for a, b in
                                   zip(new_handle, expected_handle)):
                            _fail("CONFLICT", "Moved FREE handle did not preserve its explicit local shape",
                                  {"target": ident, "source": source, "field": field})
                elif local:
                    handle_type = before_key.get(
                        "handle_left_type" if field == "handle_left"
                        else "handle_right_type")
                    if handle_type not in handle_scope.AUTO:
                        _fail("CONFLICT", "Non-automatic local handle changed",
                              {"target": ident, "source": source, "field": field})
                else:
                    _fail("CONFLICT", "A protected key handle changed",
                          {"target": ident, "source": source, "field": field})
                handle_changes.append(change)
    return {
        "key_count_before": total_before,
        "key_count_after": total_after,
        "key_count_unchanged": total_before == total_after,
        "handle_changes": handle_changes,
        "handle_change_count": len(handle_changes),
        "auto_local_changes": sum(1 for row in handle_changes
                                  if row["class"] == "local_auto"),
        "moved_key_changes": sum(1 for row in handle_changes
                                 if row["class"] == "moved_key"),
        "scope": ("Exact key time/value/type/interpolation for every key; "
                  "automatic handle coordinates may change only for moved keys "
                  "and declared untouched start/end anchors"),
    }


def _verify_locks(before_channels, after_channels, local_frames, lock_frames,
                  start, end):
    before_by_id = {row["target"]["id"]: row for row in before_channels}
    after_by_id = {row["target"]["id"]: row for row in after_channels}
    rows = []
    for ident, before in before_by_id.items():
        after = after_by_id[ident]
        for frame in lock_frames:
            old_value = native.curve_value(before["curve"], frame, 0.0)
            new_value = native.curve_value(after["curve"], frame, 0.0)
            if abs(old_value - new_value) > EPSILON:
                _fail("CONFLICT", "Locked clip evaluation changed",
                      {"target": ident, "frame": frame,
                       "before": old_value, "after": new_value})
            old_key = native.find_key(before["curve"], frame)
            new_key = native.find_key(after["curve"], frame)
            if (old_key is None) != (new_key is None):
                _fail("CONFLICT", "Locked clip key presence changed",
                      {"target": ident, "frame": frame})
            if old_key is not None:
                old_sem = _semantic_key(old_key)
                new_sem = _semantic_key(new_key)
                for field in ("co", "interpolation", "easing", "back",
                              "amplitude", "period", "type",
                              "handle_left_type", "handle_right_type"):
                    if field in old_sem and old_sem.get(field) != new_sem.get(field):
                        _fail("CONFLICT", "Locked clip key metadata changed",
                              {"target": ident, "frame": frame, "field": field})
                for field in ("handle_left", "handle_right"):
                    if old_sem.get(field) != new_sem.get(field):
                        allowed = any(abs(float(frame) - float(local_frame)) <= EPSILON
                                      for local_frame in local_frames.get(ident, set()))
                        if not allowed:
                            _fail("CONFLICT", "Locked clip key handle changed",
                                  {"target": ident, "frame": frame, "field": field})
            rows.append({"target": ident, "frame": frame,
                         "value": new_value, "key": new_key is not None})
    return rows


def _verify_contacts(before_channels, after_channels, target_contact):
    before_by_id = {row["target"]["id"]: row for row in before_channels}
    rows = []
    for after in after_channels:
        ident = after["target"]["id"]
        before = before_by_id[ident]
        keys = [key for key in after["all_keys"]
                if abs(float(key.co.x) - target_contact) <= EPSILON]
        if len(keys) != 1:
            _fail("CONFLICT", "Contact key is missing after retime",
                  {"target": ident, "frame": target_contact})
        old = _semantic_key(before["contact_key"])
        new = _semantic_key(keys[0])
        if float(old["co"][1]) != float(new["co"][1]):
            _fail("CONFLICT", "Contact key value changed",
                  {"target": ident})
        rows.append({"target": ident, "domain": after["domain"],
                     "frame": target_contact, "value": new["co"][1]})
    by_domain = {}
    for row in rows:
        by_domain.setdefault(row["domain"], []).append(row)
    return {
        "target_contact": target_contact,
        "all_channels_present": len(rows) == len(after_channels),
        "by_domain": {domain: {
            "count": len(values),
            "min": min(row["value"] for row in values),
            "max": max(row["value"] for row in values),
        } for domain, values in sorted(by_domain.items())},
        "rows": rows,
    }


def _local_handle_frames(channel, start, end):
    """Window keys plus a two-frame adjacency band on each side."""
    frames = set()
    for key in channel["all_keys"]:
        frame = float(key.co.x)
        if start - EPSILON <= frame <= end + EPSILON:
            frames.add(frame)
        elif start - 2.0 - EPSILON <= frame < start - EPSILON:
            frames.add(frame)
        elif end + EPSILON < frame <= end + 2.0 + EPSILON:
            frames.add(frame)
    return frames


def _restore_outside_handles(before_channels, after_channels, mapping, start, end):
    """Restore exact non-target handles after Blender's automatic update.

    AUTO/AUTO_CLAMPED handles are recomputed curve-wide by Blender even when
    only a local time window moved.  Inside the declared local impact domain
    those derived changes are witnessed.  Outside it, the original handle
    coordinates are written back without another curve.update(); key times,
    values, types, interpolation and all other key data remain untouched.
    """
    before_by_id = {row["target"]["id"]: row for row in before_channels}
    after_by_id = {row["target"]["id"]: row for row in after_channels}
    restored = []
    local = {}
    for ident, before in before_by_id.items():
        after = after_by_id[ident]
        allowed = _local_handle_frames(before, start, end)
        local[ident] = allowed
        after_by_frame = {round(float(key.co.x), 9): key
                          for key in after["all_keys"]}
        for old_key in before["all_keys"]:
            source = float(old_key.co.x)
            if any(abs(source - frame) <= EPSILON for frame in allowed):
                continue
            old = native.key_state(old_key)
            destination = source
            new_key = after_by_frame.get(round(destination, 9))
            if new_key is None:
                _fail("CONFLICT", "Outside-domain key disappeared before handle restore",
                      {"target": ident, "frame": source})
            new = native.key_state(new_key)
            if (new["handle_left_type"] != old["handle_left_type"]
                    or new["handle_right_type"] != old["handle_right_type"]):
                _fail("CONFLICT", "Outside-domain handle type changed",
                      {"target": ident, "frame": source})
            if (new["handle_left"] != old["handle_left"]
                    or new["handle_right"] != old["handle_right"]):
                new_key.handle_left = old["handle_left"]
                new_key.handle_right = old["handle_right"]
                restored.append({"target": ident, "frame": source,
                                 "handle_left": old["handle_left"],
                                 "handle_right": old["handle_right"]})
    return {"restored_keys": restored, "restored_count": len(restored),
            "local_frames": local,
            "scope": "Exact non-target handle coordinates restored outside [start-2,end+2]"}


def _native_sources_by_record_offset(channels, start):
    """Bridge persisted six-decimal offsets to witnessed native key times.

    A record offset is a semantic identifier, not an exact native float32
    timestamp. Never reconstruct a raw warp-map key by adding it to start.
    """
    by_target = {}
    for channel in channels:
        ident = channel["target"]["id"]
        if ident in by_target:
            _fail("CONFLICT", "Duplicate native source target", {"target": ident})
        sources = {}
        for key in channel["window_keys"]:
            source = float(key.co.x)
            semantic = _semantic_key(key)
            offset = round(float(semantic["co"][0]) - start, 6)
            if offset in sources:
                _fail("CONFLICT", "Native source keys share recorded offset precision",
                      {"target": ident, "offset": offset})
            sources[offset] = (source, semantic)
        by_target[ident] = sources
    return by_target


def _build_updated_record(record, request, after_channels, plan,
                          before_channels):
    start = float(request["event"]["start"])
    target_contact = float(request["event"]["target_contact"])
    old_event = next(iter(record["events"].values()))
    mapping = plan["mapping"]
    current_by_target = {
        row["target"]["id"]: _window_semantics(row) for row in after_channels
    }
    identity_by_target = {
        row["target"]["id"]: _action_identity(row["action"], row["slot"])
        for row in after_channels
    }
    source_by_target = _native_sources_by_record_offset(before_channels, start)
    # Persisted destination buckets must also remain one-to-one.
    _native_sources_by_record_offset(after_channels, start)
    offset_map = {}
    for binding in record["bindings"]:
        ident = binding["target"]
        offset = round(float(binding["offset"]), 6)
        target_offsets = offset_map.setdefault(ident, {})
        if offset in target_offsets:
            _fail("CONFLICT", "Recorded bindings share source offset precision",
                  {"target": ident, "offset": offset})
        witnessed = source_by_target.get(ident, {}).get(offset)
        if witnessed is None:
            _fail("CONFLICT", "Recorded offset lacks a unique native source key",
                  {"target": ident, "offset": offset})
        source, semantic = witnessed
        if semantic != event_ui_state.semantic_key(binding["expected_key"]):
            _fail("CONFLICT", "Recorded native source key witness changed",
                  {"target": ident, "offset": offset})
        source_id = round(source, 9)
        if source_id not in mapping:
            _fail("CONFLICT", "Witnessed native source time is absent from the warp",
                  {"target": ident, "source": source})
        destination = mapping[source_id]
        target_offsets[offset] = (
            float(destination) - start
        )
    new_event = copy.deepcopy(old_event)
    new_event["contact"] = target_contact
    new_event["previous_contact"] = old_event["contact"]
    new_event["retime"] = {
        "from_contact": old_event["contact"],
        "to_contact": target_contact,
        "policy": "piecewise-linear-local-window-v1",
        "review_sha256": None,
        "operation_count": plan["operations"],
    }
    updated = event_ui_state.refresh_clip_state(
        record, current_by_target, identity_by_target, new_event, offset_map)
    updated["revision"] = int(record["revision"]) + 1
    event_ui_state.check_clip_state(updated)
    return updated


def _rollback_error(error, rollback_errors):
    if rollback_errors:
        raise WorkflowError(
            "ROLLBACK_FAILED",
            "Clip event rollback could not restore all bindings/records",
            {"original_error": str(error), "rollback_errors": rollback_errors},
        ) from error
    raise error


def _capture_result(record, resolved, review_dict, action_changes=None):
    return {
        "schema": RESULT_SCHEMA,
        "status": "committed",
        "action": "capture",
        "event": copy.deepcopy(next(iter(record["events"].values()))),
        "record": _record_summary(record),
        "record_sha256": _sha(record),
        "counts": _counts(resolved),
        "action_changes": action_changes or [],
        "native_animation_changed": False,
        "review_sha256": review_dict["review_sha256"],
        "follow_up": {
            "record_readback": "passed",
            "source_actions_unchanged": True,
            "project_saved": False,
            "next_step": "Save a new candidate before retime; execute never saves files",
        },
    }


def _execute_capture_with_checkpoint(context, request, review_dict, checkpoint):
    scene, layer = _resolve_context(context, request)
    prior = _read_raw(scene)
    if prior is not None:
        _fail("CONFLICT", "A clip event record already exists")
    resolved = _resolve_participants(context, request)
    _collect_window(resolved, request)
    text = _encode_record(review_dict["record"])
    try:
        _checkpoint(checkpoint)
        _write_raw(scene, text)
        _checkpoint(checkpoint)
        if _read_raw(scene) != text:
            _fail("VALIDATION_FAILED", "Clip capture record readback changed")
        fresh_resolved = _resolve_participants(context, request)
        _collect_window(fresh_resolved, request)
        if _origin(context, request, fresh_resolved) != review_dict["origin"]:
            _fail("CONFLICT", "Clip capture origin changed during record commit")
        return _capture_result(review_dict["record"], resolved, review_dict)
    except BaseException as error:
        rollback_errors = []
        try:
            _restore_raw(scene, prior)
            if _read_raw(scene) != prior:
                raise WorkflowError("ROLLBACK_FAILED", "Clip capture record did not restore")
        except BaseException as rollback_error:
            rollback_errors.append(str(rollback_error))
        _rollback_error(error, rollback_errors)


def on_window_edge(frame, start, end):
    return abs(float(frame) - float(start)) <= EPSILON or abs(float(frame) - float(end)) <= EPSILON


def _restore_object_handles(before_channels, after_channels, mapping,
                            start, end):
    """Restore native key handles after LINEAR/BEZIER object moves.

    Blender recomputes AUTO handles on neighbors even for LINEAR curves when a
    key time changes.  LINEAR evaluation does not consume handles, and the
    object adapter deliberately keeps the original handle representation:
    untouched keys are restored exactly, moved keys keep their original shape
    translated by the same local time delta.  No interpolation is converted.
    """
    after_by_id = {channel["target"]["id"]: channel
                   for channel in after_channels}
    restored = 0
    for before in before_channels:
        ident = before["target"]["id"]
        after = after_by_id.get(ident)
        if after is None:
            _fail("CONFLICT", "Object target disappeared during commit",
                  {"target": ident})
        after_keys = {round(float(key.co.x), 9): key
                      for key in after["all_keys"]}
        for old_key in before["all_keys"]:
            source = float(old_key.co.x)
            destination = (mapping[round(source, 9)]
                           if start - EPSILON <= source <= end + EPSILON
                           else source)
            new_key = after_keys.get(round(float(destination), 9))
            if new_key is None:
                _fail("CONFLICT", "Object key disappeared during handle restore",
                      {"target": ident, "frame": source})
            old = native.key_state(old_key)
            if (new_key.handle_left_type != old["handle_left_type"]
                    or new_key.handle_right_type != old["handle_right_type"]):
                new_key.handle_left_type = old["handle_left_type"]
                new_key.handle_right_type = old["handle_right_type"]
            delta = float(destination) - source
            wanted_left = [float(old["handle_left"][0]) + delta,
                           float(old["handle_left"][1])]
            wanted_right = [float(old["handle_right"][0]) + delta,
                            float(old["handle_right"][1])]
            if (list(new_key.handle_left) != wanted_left
                    or list(new_key.handle_right) != wanted_right):
                new_key.handle_left = wanted_left
                new_key.handle_right = wanted_right
            restored += 1
    return restored


def _object_candidate_postprocess(before_channels, domain_by_target, mapping,
                                  start, target_contact, end):
    """Return the internal candidate-handle callback for pure objects mode.

    The callback is consumed inside core.apply_manifest after all native moves
    and candidate row updates, before core's lock and protected-channel checks.
    It only writes native key-handle coordinates on the already-created
    candidate curves; core remains solely responsible for cloning, movement,
    locks, source-Action protection, binding, cancellation and rollback.
    """
    def postprocess(rows):
        for row in rows:
            ident = row["spec"]["id"]
            if (row["spec"]["owner"]["type"] != "OBJECT"
                    or domain_by_target.get(ident) != "object"):
                _fail("CONFLICT",
                      "Object candidate callback received a non-object target",
                      {"target": ident})
        after_channels = _channels_from_rows(
            rows, domain_by_target, start, target_contact, end)
        _restore_object_handles(
            before_channels, after_channels, mapping, start, end)

    return postprocess


def _clip_candidate_postprocess(before_channels, domain_by_target, mapping,
                                start, target_contact, end):
    """Restore protected outside handles before the shared core checks them.

    Mixed native Bezier curves (for example FREE trim boundaries) retain
    their explicit handles. Only fully automatic, witnessed local keys enter
    the derived-coordinate scope; no metadata or key values are relaxed.
    """
    def postprocess(rows):
        after_channels = _channels_from_rows(
            rows, domain_by_target, start, target_contact, end)
        after_by_id = {row["target"]["id"]: row for row in after_channels}
        for channel in before_channels:
            ident = channel["target"]["id"]
            after_keys = {round(float(key.co.x), 9): key
                          for key in after_by_id[ident]["all_keys"]}
            for key in channel["all_keys"]:
                source = float(key.co.x)
                destination = mapping.get(round(source, 9), source)
                candidate = after_keys.get(round(destination, 9))
                if candidate is None:
                    _fail("CONFLICT", "A clip key disappeared before explicit handle restore",
                          {"target": ident, "frame": source})
                fixed_mixed_key = (abs(destination - source) <= EPSILON
                                   and (key.handle_left_type not in handle_scope.AUTO
                                        or key.handle_right_type not in handle_scope.AUTO))
                for side in ("left", "right"):
                    field = "handle_" + side
                    handle_type = getattr(key, field + "_type")
                    if handle_type in handle_scope.AUTO and not fixed_mixed_key:
                        continue
                    if getattr(candidate, field + "_type") != handle_type:
                        _fail("CONFLICT", "Explicit clip handle type changed",
                              {"target": ident, "frame": source})
                    old = getattr(key, field)
                    setattr(candidate, field,
                            (float(old.x) + destination - source, float(old.y)))
        _restore_outside_handles(
            before_channels, after_channels, mapping, start, end)
    return postprocess


def _execute_retime(context, request, review_dict, checkpoint):
    scene, layer = _resolve_context(context, request)
    raw = _read_raw(scene)
    if raw is None:
        _fail("NOT_FOUND", "No clip event record exists to retime")
    record = _decode_record(raw)
    if _sha(raw) != review_dict.get("record_raw_sha256"):
        _fail("CONFLICT", "Clip event record changed after review")
    start = float(request["event"]["start"])
    contact = float(request["event"]["contact"])
    target_contact = float(request["event"]["target_contact"])
    end = float(request["event"]["end"])
    lock_frames = _lock_frames(request)
    reviewed_manifest = review_dict["manifest"]
    lock_limit = len(reviewed_manifest["targets"]) * len(lock_frames)
    manifest = normalize_manifest(reviewed_manifest, lock_limit=lock_limit)
    if manifest != reviewed_manifest:
        _fail("CONFLICT", "Clip retime manifest changed after review")
    domain_by_target = _domain_map_from_review(review_dict)
    object_mode = bool(request["participants"].get(OBJECT_FIELD))
    mapping = _warp_by_frame(review_dict["warp"])
    prior_actions = set(bpy.data.actions)
    prior_raw = raw
    prior_bindings = {}
    prior_action_states = {}
    before_channels = []
    rollback_ready = False
    _install_permit(manifest)
    try:
        before_scene, before_layer, before_rows = native.resolve_targets(
            manifest, writing=True)
        if before_scene != scene or before_layer != layer:
            _fail("CONFLICT", "Clip retime context changed inside the transaction")
        owners = _unique_owner_rows(before_rows)
        prior_bindings = {entry["owner"]: entry["binding"]
                          for entry in owners.values()}
        for entry in owners.values():
            action = entry["action"]
            if id(action) not in prior_action_states:
                prior_action_states[id(action)] = (action, _semantic_action_state(action))
        before_channels = _channels_from_rows(
            before_rows, domain_by_target, start, contact, end)
        rollback_ready = True
        postprocess = (_object_candidate_postprocess(
            before_channels, domain_by_target, mapping, start,
            target_contact, end) if object_mode else _clip_candidate_postprocess(
            before_channels, domain_by_target, mapping, start,
            target_contact, end))
        result = core.apply_clip_manifest(
            manifest, lock_limit, checkpoint=checkpoint,
            candidate_postprocess=postprocess)
        _checkpoint(checkpoint)
        after_scene, after_layer, after_rows_raw = native.resolve_targets(
            manifest, writing=True)
        if after_scene != scene or after_layer != layer:
            _fail("CONFLICT", "Clip retime context changed after commit")
        after_channels_raw = _channels_from_rows(
            after_rows_raw, domain_by_target, start, target_contact, end)
        restored = _restore_outside_handles(
            before_channels, after_channels_raw, mapping, start, end)
        after_scene, after_layer, after_rows = native.resolve_targets(
            manifest, writing=True)
        if after_scene != scene or after_layer != layer:
            _fail("CONFLICT", "Clip retime context changed after handle restore")
        after_channels = _channels_from_rows(
            after_rows, domain_by_target, start, target_contact, end)
        key_witness = _compare_cache_before_after(
            before_channels, after_channels, mapping, restored["local_frames"],
            start, end)
        lock_witness = _verify_locks(
            before_channels, after_channels, restored["local_frames"], lock_frames,
            start, end)
        contact_witness = _verify_contacts(
            before_channels, after_channels, target_contact)
        updated = _build_updated_record(
            record, request, after_channels, {
                "mapping": mapping,
                "operations": review_dict["counts"]["operations"],
            }, before_channels)
        next(iter(updated["events"].values()))["retime"][
            "review_sha256"] = review_dict["review_sha256"]
        fresh_resolved = _resolve_participants(context, request)
        structure = _structure_witness(context, fresh_resolved)
        if _sha(structure) != review_dict["origin"]["structure_sha256"]:
            _fail("CONFLICT", "Clip structure/topology/constraint witness changed during retime")
        for action, state in prior_action_states.values():
            if action.name not in bpy.data.actions:
                _fail("CONFLICT", "A source Action disappeared during retime",
                      {"action": action.name})
            if _semantic_action_state(action) != state:
                _fail("CONFLICT", "A source Action changed during retime",
                      {"action": action.name})
        text = _encode_record(updated)
        _write_raw(scene, text)
        _checkpoint(checkpoint)
        readback = _decode_record(_read_raw(scene))
        if readback != updated:
            _fail("VALIDATION_FAILED", "Clip retime record readback differs from the transaction")
        new_actions = [action for action in bpy.data.actions
                       if action not in prior_actions]
        after_action_ids = {
            id(entry["owner"].animation_data.action)
            for entry in owners.values()
            if entry["owner"].animation_data is not None
            and entry["owner"].animation_data.action is not None
        }
        expected_new = after_action_ids
        if {id(action) for action in new_actions} != expected_new:
            _fail("CONFLICT", "Retime candidate Action set is incomplete")
        action_changes = []
        for entry in owners.values():
            after_action = entry["owner"].animation_data.action
            action_changes.append({
                "owner": entry["owner"].name,
                "owner_type": entry["owner"].bl_rna.identifier,
                "before_action": entry["action"].name,
                "after_action": after_action.name if after_action else None,
                "created_action": after_action.name in {
                    action.name for action in new_actions} if after_action else False,
            })
        after_by_domain = {domain: 0 for domain in (
            "body", "root", "light", "material")}
        if any(channel["domain"] == "object" for channel in after_channels):
            after_by_domain["object"] = 0
        for channel in after_channels:
            after_by_domain[channel["domain"]] += len(channel["window_keys"])
        result_counts = {
            "channels": len(after_channels),
            "bindings": sum(len(channel["window_keys"])
                            for channel in after_channels),
            "bindings_by_domain": after_by_domain,
            "operations": review_dict["counts"]["operations"],
        }
        return {
            "schema": RESULT_SCHEMA,
            "status": "committed",
            "action": "retime",
            "event": copy.deepcopy(next(iter(updated["events"].values()))),
            "record": _record_summary(updated),
            "record_sha256": _sha(updated),
            "counts": result_counts,
            "action_changes": action_changes,
            "native_animation_changed": True,
            "review_sha256": review_dict["review_sha256"],
            "warp": copy.deepcopy(review_dict["warp"]),
            "protection": {
                "non_target_protection": result.get("non_target_protection"),
                "derived_handle_changes": result.get("derived_handle_changes"),
                "outside_handle_restore": {
                    "restored_count": restored["restored_count"],
                    "scope": restored["scope"],
                },
                "locked_targets": len(lock_witness),
                "outside_window_keys": ("key time/value/type/interpolation exact; "
                                        "non-target handles restored exactly outside "
                                        "the local automatic-handle impact domain"),
            },
            "key_witness": key_witness,
            "locks": {
                "frames": lock_frames,
                "checked_targets": len(lock_witness),
                "status": "passed",
            },
            "contacts": contact_witness,
            "follow_up": {
                "all_domains_at_target_contact": contact_witness["all_channels_present"],
                "key_count_unchanged": key_witness["key_count_unchanged"],
                "interpolation_unchanged": (
                    "passed (native LINEAR/BEZIER keys retained)"
                    if object_mode else
                    "passed (native BEZIER keys retained)"),
                "record_readback": "passed",
                "source_actions_unchanged": "passed",
                "project_saved": False,
                "next_step": "Save a new candidate; execute never saves files",
            },
        }
    except BaseException as error:
        rollback_errors = []
        if rollback_ready:
            candidates = set()
            for entry in owners.values():
                animation_data = entry["owner"].animation_data
                action = animation_data.action if animation_data else None
                if action is not None and action not in prior_actions:
                    candidates.add(action)
            for owner, binding in prior_bindings.items():
                try:
                    native.restore_binding(owner, binding)
                except BaseException as rollback_error:
                    rollback_errors.append(str(rollback_error))
            for action in candidates:
                try:
                    if action.name in bpy.data.actions:
                        bpy.data.actions.remove(action)
                except BaseException as rollback_error:
                    rollback_errors.append(str(rollback_error))
            try:
                _restore_raw(scene, prior_raw)
                if _read_raw(scene) != prior_raw:
                    raise WorkflowError("ROLLBACK_FAILED", "Clip record did not restore")
            except BaseException as rollback_error:
                rollback_errors.append(str(rollback_error))
            for owner, binding in prior_bindings.items():
                try:
                    if native.binding_state(owner) != binding:
                        raise WorkflowError(
                            "ROLLBACK_FAILED",
                            "Clip owner binding did not restore",
                            {"owner": owner.name})
                except BaseException as rollback_error:
                    rollback_errors.append(str(rollback_error))
            for action, state in prior_action_states.values():
                try:
                    if action.name not in bpy.data.actions or _semantic_action_state(action) != state:
                        raise WorkflowError(
                            "ROLLBACK_FAILED",
                            "Clip source Action did not restore",
                            {"action": action.name})
                except BaseException as rollback_error:
                    rollback_errors.append(str(rollback_error))
        _rollback_error(error, rollback_errors)
    finally:
        _clear_permit()


def execute(context, review_dict, checkpoint=None):
    """Commit a previously reviewed capture/retime after freshness recheck."""
    if not isinstance(review_dict, dict) or review_dict.get("schema") != REVIEW_SCHEMA:
        _fail("INVALID_REQUEST", "execute requires a clip-event review")
    request = normalize_request(review_dict.get("request"))
    fresh = review(context, request)
    if fresh.get("review_sha256") != review_dict.get("review_sha256"):
        _fail("CONFLICT", "Clip review is stale; run review again",
              {"reviewed": review_dict.get("review_sha256"),
               "current": fresh.get("review_sha256")})
    if fresh["action"] == "capture":
        return _execute_capture_with_checkpoint(context, request, fresh, checkpoint)
    return _execute_retime(context, request, fresh, checkpoint)


def has_record(scene):
    try:
        return _read_raw(scene) is not None
    except WorkflowError:
        return True


def record_summary(scene):
    raw = _read_raw(scene)
    if raw is None:
        return None
    key = (scene.as_pointer(), len(raw),
           hashlib.sha256(raw.encode("utf8")).hexdigest()[:24])
    if key in _SUMMARY_CACHE:
        return copy.deepcopy(_SUMMARY_CACHE[key])
    summary = _record_summary(_decode_record(raw))
    _SUMMARY_CACHE.clear()
    _SUMMARY_CACHE[key] = summary
    return copy.deepcopy(summary)


def clear_record(context, checkpoint=None):
    """Explicit maintenance reset for recapture; never changes animation."""
    scene = context.scene
    prior = _read_raw(scene)
    if prior is None:
        return {"schema": RESULT_SCHEMA, "status": "empty",
                "native_animation_changed": False, "cleared": False}
    record = _decode_record(prior)
    try:
        _checkpoint(checkpoint)
        _restore_raw(scene, None)
        _checkpoint(checkpoint)
        if _read_raw(scene) is not None:
            _fail("VALIDATION_FAILED", "Clip record did not clear")
        return {"schema": RESULT_SCHEMA, "status": "cleared",
                "native_animation_changed": False, "cleared": True,
                "record": _record_summary(record)}
    except BaseException as error:
        rollback_errors = []
        try:
            _restore_raw(scene, prior)
            if _read_raw(scene) != prior:
                raise WorkflowError("ROLLBACK_FAILED", "Clip record did not restore")
        except BaseException as rollback_error:
            rollback_errors.append(str(rollback_error))
        _rollback_error(error, rollback_errors)


__all__ = ["review", "execute", "inspect", "clear_record", "has_record",
           "record_summary", "active", "native_target", "validate_manifest",
           "RECORD_PROPERTY"]
