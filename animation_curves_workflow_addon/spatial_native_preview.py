"""Read-only single-pose native extraction/proposal adapter; not public registered.

Requires a guarded copied scene. No save, core apply, operator or commit path.
Actual Blender qualification is pending an independently reviewed exact plan.
"""
import copy
from .contract import WorkflowError, normalize_manifest
from .spatial_candidate import propose, number
from .spatial_collision import inventory_report, MAX_PROJECTION_WORK
from .spatial_bindings import compile_existing


def reject(message):raise WorkflowError('UNSUPPORTED',message)


def dependency_check(obj):
    if obj.parent or len(obj.constraints) or len(obj.modifiers):reject('Parents, constraints and modifiers are unsupported')
    if obj.instance_type!='NONE' or obj.rigid_body:reject('Instances and rigid-body dependencies are unsupported')
    for owner in (obj,obj.data):
        ad=getattr(owner,'animation_data',None)
        if ad and (len(ad.drivers) or len(ad.nla_tracks)):reject('Drivers and NLA are unsupported')
    if obj.data.shape_keys:reject('Shape-key dependencies are unsupported')
    ad=obj.animation_data
    if ad and ad.action and ad.action.users!=1:reject('Shared or extra-user Actions are unsupported')


def extract(scene):
    # Inventory includes hidden meshes. No visibility-based omissions.
    if any(o.type not in {'MESH','CAMERA','LIGHT','EMPTY'} for o in scene.objects):reject('Unsupported geometry-producing object type in scene')
    objects=sorted([o for o in scene.objects if o.type=='MESH'],key=lambda o:o.name)
    if not 1<=len(objects)<=64:reject('Complete scene mesh inventory exceeds budget')
    if any(o.instance_type!='NONE' for o in scene.objects):reject('Scene instances require unsupported inventory expansion')
    import bpy
    from . import native
    native.local(scene)
    layer=bpy.context.view_layer
    if bpy.context.scene is not scene:reject('Scene must be current for evaluated inventory')
    layer.update()
    meshes={};identities={}
    for obj in objects:
        dependency_check(obj)
        native.local(obj);native.local(obj.data)
        if obj.data.animation_data is not None:reject('Mesh datablock animation is unsupported')
        if obj.name not in layer.objects or obj.hide_viewport or obj.hide_get(view_layer=layer) or not obj.visible_get(view_layer=layer):
            reject('Excluded or hidden meshes cannot be certified by this initial adapter')
        action,slot,bag=native.action_binding(obj,'OBJECT',writing=False)
        if bag is not None:
            for curve in bag.fcurves:
                if curve.modifiers or curve.mute:reject('Muted or modified obstacle curves are unsupported')
                if curve.data_path not in {'location','rotation_euler','rotation_quaternion','rotation_axis_angle','scale','delta_location','delta_rotation_euler','delta_rotation_quaternion','delta_scale'}:
                    reject('Unknown animated obstacle dependency')
        if not 4<=len(obj.data.vertices)<=64 or not 4<=len(obj.data.polygons)<=128:reject('Native mesh exceeds convex budget')
        vertices=[list(obj.matrix_world @ vertex.co) for vertex in obj.data.vertices]
        faces=[list(face.vertices) for face in obj.data.polygons]
        meshes[obj.name]={'vertices':vertices,'faces':faces}
        ad=obj.animation_data
        identities[obj.name]={'matrix_world':[list(row) for row in obj.matrix_world],
                              'location':list(obj.location),'lock_location':list(obj.lock_location),'rotation_mode':obj.rotation_mode,
                              'rotation_euler':list(obj.rotation_euler),'rotation_quaternion':list(obj.rotation_quaternion),
                              'scale':list(obj.scale),'action_name':ad.action.name if ad and ad.action else None,
                              'slot_identifier':ad.action_slot.identifier if ad and ad.action_slot else None}
    return meshes,identities


def plan(scene,inspection_manifest,participants,frame,patch_owner,patch_face,support_owner,support_face,*,margin,max_translation,locked_axes=(),anchor=None,animated_camera_names=None):
    # Caller must set/evaluate exact frame before requesting inspection. Do not
    # silently advance after inspecting keys or reuse a different pose witness.
    frame=number(frame)
    if float(scene.frame_current)+float(scene.frame_subframe)!=frame:reject('Scene pose must match explicit planning frame')
    if not isinstance(participants,list) or patch_owner not in participants or support_owner in participants:
        raise WorkflowError('INVALID_REQUEST','Patch must belong to explicit group and support must be outside it')
    if type(patch_face)is not int or type(support_face)is not int or patch_face<0 or support_face<0:
        raise WorkflowError('INVALID_REQUEST','Explicit native polygon indices required')
    import bpy
    from . import core, native
    if scene is not bpy.context.scene:reject('Planning requires the current bound scene')
    if inspection_manifest.get('context')!={'scene':scene.name,'view_layer':bpy.context.view_layer.name}:
        reject('Manifest context must equal current scene and view layer')
    if animated_camera_names is not None:
        from . import spatial_static_state
        before_update=spatial_static_state.capture_for_scope(scene,animated_camera_names)
    bpy.context.view_layer.update()
    if animated_camera_names is not None:spatial_static_state.compare(before_update,spatial_static_state.capture_for_scope(scene,animated_camera_names))
    if inspection_manifest.get('operation')!='inspect':
        raise WorkflowError('INVALID_REQUEST','Read-only native inspection manifest required')
    inspection_manifest=normalize_manifest(inspection_manifest)
    native.resolve_targets(inspection_manifest,writing=True)
    inspection=core.inspect(inspection_manifest)
    from . import spatial_static_state
    static_witness=spatial_static_state.capture_for_scope(scene,animated_camera_names)
    meshes,identities=extract(scene)
    if patch_owner not in meshes or support_owner not in meshes:reject('Patch/support absent from full mesh inventory')
    def face(owner,index):
        record=meshes[owner]
        if index>=len(record['faces']):reject('Native polygon index missing')
        return [record['vertices'][i] for i in record['faces'][index]]
    proposal=propose(face(patch_owner,patch_face),face(support_owner,support_face),margin=margin,max_translation=max_translation,locked_axes=locked_axes,anchor=anchor)
    for owner in participants:
        if owner not in identities:reject('Participant absent from complete inventory')
        if any(identities[owner]['lock_location'][i] and proposal['translation'][i]!=0 for i in range(3)):
            raise WorkflowError('CONFLICT','Native participant location lock conflicts with translation')
    compiled=compile_existing(inspection,participants,frame,proposal['translation'])
    for witness in compiled['witnesses']:
        identity=witness['identity'];owner=identity['owner']['name'];axis=identity['array_index']
        if identities[owner]['location'][axis]!=witness['expected_key']['co'][1]:
            raise WorkflowError('CONFLICT','Current native location differs from exact bound key; manual pose must not be reset')
    before=inventory_report(list(meshes),meshes,participants)
    moved=copy.deepcopy(meshes)
    for owner in participants:
        moved[owner]['vertices']=[[point[i]+proposal['translation'][i] for i in range(3)] for point in meshes[owner]['vertices']]
    # Shared two-pose conservative bound, not a fresh budget for each pose.
    if before['projection_work_upper_bound']*2>MAX_PROJECTION_WORK:reject('Aggregate before/after SAT work exceeds budget')
    after=inventory_report(list(moved),moved,participants)
    result={'status':'native_extraction_proposal_not_accepted','frame':frame,
            'proposal':proposal,'compiled':compiled,'mesh_inventory':list(meshes),'mesh_witnesses':meshes,
            'static_scene_witness':static_witness,'native_identities':identities,'patch':{'owner':patch_owner,'face_index':patch_face},
            'support':{'owner':support_owner,'face_index':support_face},
            'before_collision':before,'proposed_collision':after,
            'sample_times':[frame], 'pending':['guarded_native_qualification','native_staged_readback','support_touch_classification','preview_cold','explicit_user_acceptance','commit']}

    if animated_camera_names is not None:result['preserved_camera_names']=list(animated_camera_names)
    return result
