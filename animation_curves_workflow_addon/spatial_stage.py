"""Isolated native spatial preview transaction. Not public commit/authorization.

No file I/O. Caller may save only a successful diagnostic preview, and must
obtain independent cold evidence before forming a reviewable preview package.
"""
import copy
import math
import hashlib
from .spatial_preview_identity import canonical
from .contract import WorkflowError
from .spatial_candidate import polygon
from .spatial_collision import inventory_report, MAX_PROJECTION_WORK
from . import spatial_native_preview,spatial_static_state


def conflict(message):raise WorkflowError('CONFLICT',message)


def collision_gate(report,patch_owner,support_owner,*,after):
    for pair in report['pairs']:
        intended={pair['a'],pair['b']}=={patch_owner,support_owner}
        allowed={'touching_within_tolerance'} if after and intended else {'separated'}
        if pair['relation'] not in allowed:conflict('Unapproved contact or penetration in complete group collision inventory')


def _validate_after(scene,manifest,before,result,tolerance,*,cold_camera_comparison=False):
    from . import core
    current_static=spatial_static_state.capture_for_scope(scene,result.get('preserved_camera_names'))
    if cold_camera_comparison:
        if not result.get('preserved_camera_names'):conflict('Cold camera comparison requires explicit camera scope')
        spatial_static_state.compare_persisted_camera_scope(result['static_scene_witness'],current_static)
    else:spatial_static_state.compare(result['static_scene_witness'],current_static)
    inspection=core.inspect(manifest)
    expected={change['target']:change for change in result['compiled']['expected_changes']}
    oldrows={row['id']:row for row in before['targets']}
    for row in inspection['targets']:
        keys=copy.deepcopy(oldrows[row['id']]['channel']['keys'])
        change=expected.get(row['id'])
        if change:
            indices=[i for i,key in enumerate(keys) if key==change['before']]
            if len(indices)!=1:conflict('Expected original spatial key is ambiguous')
            keys[indices[0]]=change['expected_after']
        if row['channel']['keys']!=keys:conflict('Native spatial full-key readback differs from compiled witness')
    meshes,identities=spatial_native_preview.extract(scene)
    if set(meshes)!=set(result['mesh_witnesses']):conflict('Native mesh inventory changed')
    for witness in result['compiled']['witnesses']:
        identity=witness['identity'];owner=identity['owner']['name'];axis=identity['array_index']
        expected_value=expected.get(identity['id'],{}).get('expected_after',witness['expected_key'])['co'][1]
        if identities[owner]['location'][axis]!=expected_value:conflict('Native participant property readback differs from exact compiled value')
    group=set(result['compiled']['participants']);delta=result['proposal']['translation']
    for name,old in result['mesh_witnesses'].items():
        actual=meshes[name]
        if len(actual['vertices'])!=len(old['vertices']):conflict('Native vertex inventory changed')
        if actual['faces']!=old['faces']:conflict('Native topology changed')
        for a,b in zip(actual['vertices'],old['vertices']):
            desired=[b[i]+(delta[i] if name in group else 0.) for i in range(3)]
            if any(abs(a[i]-desired[i])>(tolerance if name in group else 0.) for i in range(3)):
                conflict('World mesh motion differs from rigid translation')
        old_identity=result['native_identities'][name];new_identity=identities[name]
        for field in ('rotation_mode','rotation_euler','rotation_quaternion','scale','lock_location'):
            if new_identity[field]!=old_identity[field]:conflict('Protected native transform or lock changed')
        if name not in group and new_identity!=old_identity:conflict('Unselected object identity or transform changed')
    patch=result['patch'];support=result['support']
    def face(spec):
        record=meshes[spec['owner']]
        return [record['vertices'][i] for i in record['faces'][spec['face_index']]]
    p=polygon(face(patch));s=polygon(face(support),rectangle=True)
    gap=p[0][2]-s[0][2]
    margin=min(min(v[i] for v in p)-min(v[i] for v in s) for i in (0,1))
    margin=min(margin,min(max(v[i] for v in s)-max(v[i] for v in p) for i in (0,1)))
    if abs(gap)>tolerance or margin+tolerance<result['proposal']['margin']:conflict('Native finite support face gap/margin failed')
    collisions=inventory_report(list(meshes),meshes,result['compiled']['participants'])
    collision_gate(collisions,patch['owner'],support['owner'],after=True)
    return {'inspection':inspection,'mesh_witnesses':meshes,'native_identities':identities,'collision_report':collisions,
            'static_scene_witness':spatial_static_state.capture_for_scope(scene,result.get('preserved_camera_names')),'finite_face':{'gap':gap,'margin':margin,'tolerance':tolerance},'sample_times':[result['frame']]}


def evaluated_pose_gate(scene,frame,animated_camera_names=None):
    """Reject poses that frame evaluation could reset, without evaluating/resetting.

    Initial staging accepts only mesh-only scenes with transform-only animation.
    The read-only planning adapter remains broader; this is a stricter edit gate.
    """
    from . import native
    spatial_static_state.capture_for_scope(scene,animated_camera_names)
    def no_animation(owner):
        if owner and getattr(owner,'animation_data',None):
            raise WorkflowError('UNSUPPORTED','Non-object animation is outside the initial staging boundary')
    no_animation(scene)
    if hasattr(scene,'compositor_effects'):
        has_compositor = len(scene.compositor_effects) > 0
    elif hasattr(scene,'compositing_node_group'):
        has_compositor = scene.compositing_node_group is not None
    else:
        raise WorkflowError('UNSUPPORTED','Blender compositor API is unavailable')
    if has_compositor or scene.sequence_editor or scene.rigidbody_world:
        raise WorkflowError('UNSUPPORTED','World/compositor/sequence/physics state is outside initial spatial staging')
    for obj in scene.objects:
        if obj.type!='MESH':continue
        no_animation(obj.data)
        action,slot,bag=native.action_binding(obj,'OBJECT',writing=False)
        if bag:
            if obj.animation_data.action_extrapolation!='HOLD':
                raise WorkflowError('UNSUPPORTED','Initial staging requires HOLD Action extrapolation')
            for curve in bag.fcurves:
                value=obj.path_resolve(curve.data_path)[curve.array_index]
                if float(value)!=float(curve.evaluate(frame)):
                    conflict('Native animated transform differs from curve evaluation; manual pose must not be reset')


def stage(scene,manifest,participants,frame,patch_owner,patch_face,support_owner,support_face,*,expected_native_proposal_sha256,margin,max_translation,locked_axes=(),anchor=None,animated_camera_names=None):
    import bpy
    from . import core,native
    scope={} if animated_camera_names is None else {'animated_camera_names':animated_camera_names}
    result=spatial_native_preview.plan(scene,manifest,participants,frame,patch_owner,patch_face,support_owner,support_face,margin=margin,max_translation=max_translation,locked_axes=locked_axes,anchor=anchor,**scope)
    native_plan_sha=hashlib.sha256(canonical(result)).hexdigest()
    if native_plan_sha!=expected_native_proposal_sha256:conflict('Full native spatial proposal changed before staging')
    collision_gate(result['before_collision'],patch_owner,support_owner,after=False)
    collision_gate(result['proposed_collision'],patch_owner,support_owner,after=True)
    evaluated_pose_gate(scene,frame,**scope)
    if not result['compiled']['operations']:conflict('A spatial preview must contain explicit existing-key changes')
    if result['before_collision']['projection_work_upper_bound']*3>MAX_PROJECTION_WORK:
        raise WorkflowError('UNSUPPORTED','Aggregate planning/readback collision budget exceeded')
    before=core.inspect(manifest)
    objects=[bpy.data.objects[name] for name in participants]
    bindings={obj:native.binding_state(obj) for obj in objects}
    originals={obj:native.action_state(bindings[obj]['action']) for obj in objects}
    original_actions=list(bpy.data.actions)
    action_names={a.name for a in original_actions}
    edit=copy.deepcopy(manifest);edit['operation']='edit';edit['operations']=result['compiled']['operations']
    try:
        operation=core.apply_manifest(edit)
        if operation['non_target_protection']['status']!='passed':conflict('Core protection did not pass')
        scene.frame_set(math.floor(frame),subframe=frame-math.floor(frame));bpy.context.view_layer.update()
        checked=_validate_after(scene,manifest,before,result,1e-5)
        for obj in objects:
            if native.action_state(bindings[obj]['action'])!=originals[obj]:conflict('Original Action changed during spatial preview')
        return {'status':'staged_preview_not_accepted','native_proposal_sha256':native_plan_sha,'proposal':result,'operation':operation,'native_validation':checked,
                'pending':['separate_cold_verification','explicit_user_acceptance','public_commit']}
    except BaseException as error:
        failures=[]
        for obj in objects:
            try:native.restore_binding(obj,bindings[obj])
            except BaseException as exc:failures.append(str(exc))
        for action in list(bpy.data.actions):
            if action not in original_actions:
                try:bpy.data.actions.remove(action)
                except BaseException as exc:failures.append(str(exc))
        try:
            scene.frame_set(math.floor(frame),subframe=frame-math.floor(frame));bpy.context.view_layer.update()
            for obj in objects:
                if native.action_state(bindings[obj]['action'])!=originals[obj]:failures.append('Original source Action not restored')
            if core.inspect(manifest)!=before:failures.append('Original key/binding inspection not restored')
            if {a.name for a in bpy.data.actions}!=action_names:failures.append('Action inventory not restored')
            spatial_static_state.compare(result['static_scene_witness'],spatial_static_state.capture_for_scope(scene,result.get('preserved_camera_names')))
            meshes,identities=spatial_native_preview.extract(scene)
            if meshes!=result['mesh_witnesses'] or identities!=result['native_identities']:failures.append('Original native geometry/transform identity not restored')
        except BaseException as exc:failures.append(str(exc))
        if failures:raise WorkflowError('ROLLBACK_FAILED','Spatial preview rollback failed',{'original_error':str(error),'failures':failures}) from error
        raise
