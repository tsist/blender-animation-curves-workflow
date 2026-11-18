"""Transactional local native channel edits, shared by GUI and CLI."""
import copy

import bpy

from .contract import WorkflowError, normalize_manifest
from . import native


def inspect(manifest):
    manifest = normalize_manifest(manifest)
    scene, layer, rows = native.resolve_targets(manifest, writing=False)
    return native.inspect_rows(manifest, scene, layer, rows)


def _checkpoint(callback):
    if callback is not None and callback() is False:
        native.fail('CANCELLED', 'Animation edit cancelled')


def _key_frame(state):
    return float(state['co'][0])


def _protected_curves(before, after, allowed, derived_handles=None):
    """Allow only the named keys of named channels; every other witness is exact."""
    derived_handles = derived_handles or {}
    derived_changes = []
    left = {(c['data_path'], c['array_index']): c for c in before}
    right = {(c['data_path'], c['array_index']): c for c in after}
    for address in left.keys() | right.keys():
        old, new = left.get(address), right.get(address)
        if address not in allowed:
            if old != new:
                native.fail('CONFLICT', 'Non-target animation channel changed', {'channel': list(address)})
            continue
        frames = allowed[address]
        if old is None:
            if new is None or any(not any(abs(_key_frame(k) - f) <= native.EPSILON for f in frames) for k in new['keys']):
                native.fail('CONFLICT', 'Unexpected keys in newly created channel')
            continue
        if new is None:
            native.fail('CONFLICT', 'Channel was unexpectedly removed')
        old_meta, new_meta = copy.deepcopy(old), copy.deepcopy(new)
        old_keys, new_keys = old_meta.pop('keys'), new_meta.pop('keys')
        if old_meta != new_meta:
            native.fail('CONFLICT', 'Channel metadata or modifiers changed', {'channel': list(address)})
        def untouched(keys):
            return [k for k in keys if not any(abs(_key_frame(k) - f) <= native.EPSILON for f in frames)]
        old_untouched, new_untouched = untouched(old_keys), untouched(new_keys)
        if len(old_untouched) != len(new_untouched):
            native.fail('CONFLICT','Unlisted key inventory changed',{'channel':list(address)})
        for old_key,new_key in zip(old_untouched,new_untouched):
            if old_key == new_key:
                continue
            frames_scope=derived_handles.get(address,set())
            if any(abs(_key_frame(old_key)-frame)<=native.EPSILON for frame in frames_scope):
                from .handle_scope import compare_key
                delta=compare_key(old_key,new_key)
                if delta:derived_changes.append({'channel':list(address),'frame':_key_frame(old_key),'coordinates':delta})
            else:
                native.fail('CONFLICT', 'An unnamed key or its handles changed; native auto handles require explicit wider scope',
                            {'channel': list(address)})
    return derived_changes


def _apply_operation(row, operation):
    spec, bag = row['spec'], row['candidate_bag']
    curve = bag.fcurves.find(spec['data_path'], index=spec['array_index'])
    kind, frame = operation['op'], operation['frame']
    if curve is None:
        if kind != 'set_key':
            native.fail('NOT_FOUND', 'Target channel has no keyframes', {'target': spec['id']})
        curve = bag.fcurves.new(data_path=spec['data_path'], index=spec['array_index'])
    key = native.find_key(curve, frame)
    if kind != 'set_key' and key is None:
        native.fail('NOT_FOUND', 'No key at the requested frame', {'target': spec['id'], 'frame': frame})
    if kind == 'set_key':
        if key is None:
            key = curve.keyframe_points.insert(frame, operation['value'], options={'FAST'})
            # LINEAR/CONSTANT evaluation does not use Bezier handles. New keys
            # use stable FREE handles so a later append does not rewrite this
            # now-untargeted key's automatic handle representation. Existing
            # user keys are never frozen or converted by this policy.
            if operation['interpolation'] in {'LINEAR', 'CONSTANT'}:
                key.handle_left_type = 'FREE'
                key.handle_right_type = 'FREE'
        else:
            delta = operation['value'] - float(key.co.y)
            key.co.y = operation['value']
            key.handle_left.y += delta
            key.handle_right.y += delta
        key.interpolation = operation['interpolation']
    elif kind == 'move_key':
        destination = operation['to_frame']
        other = native.find_key(curve, destination)
        if other is not None and other != key:
            native.fail('CONFLICT', 'Move destination already has a key', {'frame': destination})
        delta = destination - float(key.co.x)
        key.co.x = destination
        if float(key.co.x) != destination:
            native.fail('VALIDATION_FAILED', 'Native key time differs from requested destination',
                        {'requested_frame': destination, 'actual_frame': float(key.co.x)})
        key.handle_left.x += delta
        key.handle_right.x += delta
    else:
        curve.keyframe_points.remove(key, fast=True)
    curve.update()
    row['curve'] = curve


def apply_manifest(manifest, checkpoint=None, *, candidate_postprocess=None):
    """Apply one manifest; optional postprocess is internal Python-only.

    ``candidate_postprocess(rows)`` runs after all native moves and candidate
    row updates, before lock and protected-channel verification.  It is never
    accepted from JSON/GUI/CLI routing and defaults to the previous behaviour.
    """
    if candidate_postprocess is not None and not callable(candidate_postprocess):
        native.fail('INVALID_REQUEST', 'candidate_postprocess must be callable')
    return _apply_manifest(manifest, checkpoint,
                           candidate_postprocess=candidate_postprocess)


def apply_clip_manifest(manifest, lock_limit, checkpoint=None, *, candidate_postprocess=None):
    """Execute the internally compiled per-channel locks for a reviewed clip."""
    from . import clip_event
    if not clip_event.active():
        native.fail('INVALID_REQUEST', 'Compiled clip locks require an active clip review')
    return _apply_manifest(manifest, checkpoint, candidate_postprocess=candidate_postprocess,
                           compiled_lock_limit=lock_limit)


def apply_linear_value_plan(manifest, plan, checkpoint=None):
    """Internal explicit value-plan path; not exposed by JSON/GUI/CLI routing."""
    if not isinstance(plan, dict):
        native.fail('INVALID_REQUEST', 'Explicit compiled LINEAR value plan required')
    return _apply_manifest(manifest, checkpoint, linear_value_plan=plan)


def apply_coupled_value_profile(manifest, plan, checkpoint=None):
    """Internal strict multi-key profile; no public/GUI registration."""
    if not isinstance(plan, dict):
        native.fail('INVALID_REQUEST', 'Explicit compiled shared profile required')
    return _apply_manifest(manifest, checkpoint, linear_value_plan=plan, coupled_profile=True)


def _coupled_inspection_budget(rows):
    """Bound every serialized inspected row, including unedited owners.

    Counts repeated row curves repeatedly, matching inspect_rows serialization.
    Participant bag protection has a separate complete-bag budget.
    """
    if len(rows) > 64:
        native.fail('UNSUPPORTED', 'Shared-profile inspected row budget exceeded')
    total = 0
    for row in rows:
        curve = row['curve']
        count = len(curve.keyframe_points) if curve is not None else 0
        total += count
        if count > 8192 or total > 50000:
            native.fail('UNSUPPORTED', 'Shared-profile inspected key budget exceeded before inspection')


def _coupled_group_gate(scene, layer, rows, plan, *, evaluate_pose=True):
    """Nonmutating current-pose/dependency gate, not full-scene preservation."""
    try:
        if scene != bpy.context.scene or layer != bpy.context.view_layer:
            native.fail('UNSUPPORTED', 'Shared profile requires current explicit scene/view layer')
        frame = native.finite(scene.frame_current + scene.frame_subframe)
        lo, hi = plan['changed_interval']
        if lo < frame < hi:
            native.fail('UNSUPPORTED', 'Initial shared-profile entry requires current frame outside changed interval')
        selected = set(plan['selectors']['targets'])
        prepared = []
        total_keys = 0
        for row in rows:
            if row['spec']['id'] not in selected:
                continue
            obj = row['owner']
            if obj.type != 'MESH' or obj.mode != 'OBJECT' or obj.is_evaluated or obj.original != obj:
                native.fail('UNSUPPORTED', 'Shared-profile participant must be original Object-mode mesh')
            if obj.lock_location[0]:
                native.fail('CONFLICT', 'Shared-profile X axis is locked')
            native.local(obj);native.local(obj.data)
            if obj.data.is_evaluated or obj.data.original != obj.data or obj.data.is_editmode or obj.data.shape_keys is not None or obj.data.animation_data is not None:
                native.fail('UNSUPPORTED', 'Animated/evaluated/edit-mode mesh dependencies unsupported')
            if obj.parent is not None or len(obj.constraints) or len(obj.modifiers) or obj.instance_type != 'NONE' or obj.rigid_body is not None or obj.rigid_body_constraint is not None:
                native.fail('UNSUPPORTED', 'Shared-profile participant has unsupported transform dependencies')
            if obj.field is not None and obj.field.type != 'NONE':
                native.fail('UNSUPPORTED', 'Shared-profile force-field dependency unsupported')
            if obj.hide_render or obj.hide_viewport or obj.hide_get(view_layer=layer) or not obj.visible_get(view_layer=layer):
                native.fail('UNSUPPORTED', 'Initial shared-profile participant must be explicitly visible')
            if tuple(obj.delta_location)!=(0.,0.,0.) or tuple(obj.delta_rotation_euler)!=(0.,0.,0.) or tuple(obj.delta_rotation_quaternion)!=(1.,0.,0.,0.) or tuple(obj.delta_scale)!=(1.,1.,1.):
                native.fail('UNSUPPORTED', 'Nondefault delta transforms unsupported')
            curves = row['bag'].fcurves
            if not 1 <= len(curves) <= 10:
                native.fail('UNSUPPORTED', 'Shared-profile original transform-curve budget exceeded')
            addresses = set()
            for curve in curves:
                width = {'location':3,'rotation_euler':3,'rotation_quaternion':4,'scale':3}.get(curve.data_path)
                address = (curve.data_path, curve.array_index)
                if width is None or not 0 <= curve.array_index < width or address in addresses or len(curve.modifiers) or len(curve.sampled_points) or curve.mute:
                    native.fail('UNSUPPORTED', 'Unsupported/ambiguous participant curve before evaluation')
                count = len(curve.keyframe_points)
                total_keys += count
                if count > 8192 or total_keys > 50000:
                    native.fail('UNSUPPORTED', 'Shared-profile native key budget exceeded before inspection')
                addresses.add(address)
            prepared.append((row,obj,curves))
        if len(prepared) != len(selected):
            native.fail('CONFLICT', 'Shared-profile participant inventory mismatch')
        if not evaluate_pose:
            return None
        witness = {}
        for row,obj,curves in prepared:
            # Every participant and curve is preflighted before any evaluation.
            for curve in curves:
                spec={'owner':{'type':'OBJECT','name':obj.name},'data_path':curve.data_path,'array_index':curve.array_index}
                if native._path_value(obj,spec) != native.finite(curve.evaluate(frame)):
                    native.fail('CONFLICT', 'Unkeyed current participant pose differs from native curve; no reset performed')
            state={'rotation_mode':obj.rotation_mode,'lock_location':list(obj.lock_location)}
            for name in ('location','rotation_euler','rotation_quaternion','rotation_axis_angle','scale','delta_location','delta_rotation_euler','delta_rotation_quaternion','delta_scale'):
                state[name]=[native.finite(v) for v in getattr(obj,name)]
            for name in ('matrix_world','matrix_basis','matrix_local','matrix_parent_inverse'):
                state[name]=[[native.finite(v) for v in line] for line in getattr(obj,name)]
            witness[obj.name]=state
        return {'frame':frame,'objects':witness,'scope':'Current visible parentless mesh transform state only; no full static/geometry/contact proof'}
    except AttributeError as error:
        native.fail('UNSUPPORTED', 'Unsupported fixed-runtime group property', {'error':str(error)})


def _apply_manifest(manifest, checkpoint=None, *, linear_value_plan=None, coupled_profile=False,
                    candidate_postprocess=None, compiled_lock_limit=None):
    if type(coupled_profile) is not bool or (coupled_profile and linear_value_plan is None):
        native.fail('INVALID_REQUEST', 'Coupled policy requires an explicit pinned plan')
    if candidate_postprocess is not None and not callable(candidate_postprocess):
        native.fail('INVALID_REQUEST', 'candidate_postprocess must be callable')
    manifest = (normalize_manifest(manifest) if compiled_lock_limit is None
                else normalize_manifest(manifest, lock_limit=compiled_lock_limit))
    if manifest['operation'] != 'edit':
        native.fail('INVALID_REQUEST', 'apply_manifest requires operation edit')
    scene, layer, rows = native.resolve_targets(manifest, writing=True)
    _checkpoint(checkpoint)
    by_target = {row['spec']['id']: row for row in rows}
    operations = manifest['operations']
    event_plan = None
    if 'event_shift' in manifest:
        from .events import plan_event_shift, validate_native_bindings
        event_plan = plan_event_shift(manifest['event_shift'], set(by_target))
        keys = {target: [native.key_state(key) for key in row['curve'].keyframe_points]
                if row['curve'] else [] for target, row in by_target.items()}
        operations = validate_native_bindings(event_plan, keys, native.EPSILON)
    derived_scope={}
    linear_before = None
    linear_readback = None
    coupled_before = None
    coupled_after = None
    if linear_value_plan is not None:
        if coupled_profile:
            from .coupled_value_profile import preflight_manifest, verify_readback
        else:
            from .linear_value_adapter import preflight_manifest, verify_readback
        for row in rows:
            if row['action'] is None or native.effective_users(row['action']) != 1:
                native.fail('UNSUPPORTED', 'LINEAR value plan requires exclusive existing Actions')
        if coupled_profile:
            _coupled_inspection_budget(rows)
            _coupled_group_gate(scene, layer, rows, linear_value_plan, evaluate_pose=False)
        linear_before = native.inspect_rows(manifest, scene, layer, rows)
        derived_scope = preflight_manifest(manifest, linear_before, linear_value_plan)
        if coupled_profile:
            coupled_before = _coupled_group_gate(scene, layer, rows, linear_value_plan)
    if 'derived_handle_scope' in manifest:
        from .handle_scope import prepare as prepare_handle_scope
        keys={target:[native.key_state(key) for key in row['curve'].keyframe_points] if row['curve'] else [] for target,row in by_target.items()}
        derived_scope=prepare_handle_scope(manifest['derived_handle_scope'],keys,operations)
    if not operations:
        return {'schema': 'animation-curves-edit-report-v1', 'changes': [], 'event_plan': event_plan,
                'non_target_protection': {'status': 'passed', 'edited_owners': 0},
                'inspection': native.inspect_rows(manifest, scene, layer, rows)}
    owners = {}
    for operation in operations:
        row = by_target[operation['target']]
        owner = row['owner']
        if owner not in owners:
            owners[owner] = {'row': row, 'binding': native.binding_state(owner),
                             'before': native.bag_state(row['bag']),
                             'source_action': native.action_state(row['action']),
                             'custom_properties': native.custom_state(owner), 'allowed': {}}
        address = (row['spec']['data_path'], row['spec']['array_index'])
        touched = owners[owner]['allowed'].setdefault(address, set())
        touched.add(operation['frame'])
        if operation['op'] == 'move_key':
            touched.add(operation['to_frame'])
    for target,frames in derived_scope.items():
        row=by_target[target];address=(row['spec']['data_path'],row['spec']['array_index'])
        owners[row['owner']].setdefault('derived_handles',{})[address]=frames
    untouched = {}
    if linear_value_plan is not None:
        for row in rows:
            owner = row['owner']
            if owner not in owners and owner not in untouched:
                untouched[owner] = {'binding': native.binding_state(owner),
                                    'action': native.action_state(row['action']),
                                    'custom': native.custom_state(owner)}
    def verify_untouched():
        for owner, prior in untouched.items():
            if native.binding_state(owner) != prior['binding'] or native.action_state(prior['binding']['action']) != prior['action'] or native.custom_state(owner) != prior['custom']:
                native.fail('CONFLICT', 'Unedited inspected owner binding/Action/custom state changed')
    derived_handle_changes=[]
    lock_witness = []
    for lock in manifest['locks']:
        row = by_target[lock['target']]
        key = native.find_key(row['curve'], lock['frame']) if row['curve'] else None
        lock_witness.append((lock, native.curve_value(row['curve'], lock['frame'], row['base_value']),
                             float(key.co.x) if key else None))
    created, committed = [], []
    try:
        for owner, state in owners.items():
            _checkpoint(checkpoint)
            action, slot, bag = native.clone_action(state['row'])
            created.append(action)
            state.update(candidate=action, candidate_slot=slot, candidate_bag=bag)
        for row in rows:
            state = owners.get(row['owner'])
            if state:
                row.update(candidate_bag=state['candidate_bag'])
        for operation in operations:
            _checkpoint(checkpoint)
            _apply_operation(by_target[operation['target']], operation)
        for row in rows:
            if row['owner'] in owners:
                state = owners[row['owner']]
                row.update(action=state['candidate'], slot=state['candidate_slot'], bag=state['candidate_bag'])
                row['curve'] = row['bag'].fcurves.find(row['spec']['data_path'], index=row['spec']['array_index'])
        if candidate_postprocess is not None:
            _checkpoint(checkpoint)
            candidate_postprocess(rows)
        for lock, value, key_time in lock_witness:
            row = by_target[lock['target']]
            after = native.curve_value(row['curve'], lock['frame'], row['base_value'])
            key = native.find_key(row['curve'], lock['frame']) if row['curve'] else None
            if abs(after - value) > lock['tolerance'] or key_time is not None and (key is None or float(key.co.x) != key_time):
                native.fail('CONFLICT', 'Locked evaluation or key time changed', {'target': lock['target'], 'frame': lock['frame']})
        for owner, state in owners.items():
            changes=_protected_curves(state['before'], native.bag_state(state['candidate_bag']), state['allowed'],state.get('derived_handles'))
            derived_handle_changes.extend({'owner':owner.name,**change} for change in changes)
            if native.action_state(state['binding']['action']) != state['source_action'] or native.custom_state(owner) != state['custom_properties']:
                native.fail('CONFLICT', 'Source Action or ID properties changed before commit')
        if linear_value_plan is not None:
            linear_readback = verify_readback(linear_before, native.inspect_rows(manifest, scene, layer, rows), linear_value_plan)
            verify_untouched()
        _checkpoint(checkpoint)
        for owner, state in owners.items():
            committed.append(owner)
            ad = owner.animation_data_create()
            ad.action = state['candidate']
            ad.action_slot = state['candidate_slot']
            if ad.action != state['candidate'] or ad.action_slot != state['candidate_slot']:
                native.fail('VALIDATION_FAILED', 'Native Action binding did not persist')
        _checkpoint(checkpoint)
        for owner, state in owners.items():
            if native.action_state(state['binding']['action']) != state['source_action'] or native.custom_state(owner) != state['custom_properties']:
                native.fail('CONFLICT', 'Source Action or ID properties changed during commit')
        if linear_value_plan is not None:
            final_scene, final_layer, final_rows = native.resolve_targets(manifest, writing=True)
            if final_scene != scene or final_layer != layer:
                native.fail('CONFLICT', 'Native target context changed after commit')
            if coupled_profile:
                _coupled_inspection_budget(final_rows)
                coupled_after = _coupled_group_gate(final_scene, final_layer, final_rows, linear_value_plan)
                if coupled_after != coupled_before:
                    native.fail('CONFLICT', 'Current group state changed after profile commit')
            derived_handle_changes = []
            for owner, state in owners.items():
                expected_binding = dict(state['binding'])
                expected_binding.update(action=state['candidate'], slot=state['candidate_slot'])
                if native.binding_state(owner) != expected_binding or native.effective_users(state['candidate']) != 1:
                    native.fail('CONFLICT', 'Expected exclusive candidate Action/slot/settings changed after commit')
                changes = _protected_curves(state['before'], native.bag_state(state['candidate_bag']), state['allowed'], state.get('derived_handles'))
                derived_handle_changes.extend({'owner': owner.name, **change} for change in changes)
            for row in final_rows:
                state = owners.get(row['owner'])
                if state and (row['action'] != state['candidate'] or row['slot'] != state['candidate_slot'] or row['bag'] != state['candidate_bag']):
                    native.fail('CONFLICT', 'Fresh native target does not resolve to expected candidate')
            linear_readback = verify_readback(linear_before, native.inspect_rows(manifest, scene, layer, final_rows), linear_value_plan)
            verify_untouched()
        result = {'schema': 'animation-curves-edit-report-v1', 'changes': copy.deepcopy(operations),
                'event_plan': event_plan,
                'derived_handle_changes':derived_handle_changes,
                'derived_handle_scope':({'policy':linear_value_plan['schema'],'declared_keys':len(linear_value_plan['neighbor_witnesses'])} if linear_value_plan is not None else {'policy':manifest['derived_handle_scope']['policy'],'declared_keys':len(manifest['derived_handle_scope'].get('keys',manifest['derived_handle_scope'].get('targets',[])))}) if derived_scope else None,
                'non_target_protection': {'status': 'passed', 'edited_owners': len(owners),
                                          'source_actions_unchanged': True,
                                          'unnamed_channels_keys_handles_modifiers': 'exact_except_explicit_auto_handle_coordinates' if derived_scope else 'exact'},
                'inspection': native.inspect_rows(manifest, scene, layer, rows)}
        if linear_value_plan is not None:
            result['linear_value_readback'] = linear_readback
        if coupled_profile:
            result['coupled_profile_native_state'] = {'before':coupled_before,'after':coupled_after}
        return result
    except BaseException as error:
        rollback_errors = []
        for owner in reversed(committed):
            try:
                native.restore_binding(owner, owners[owner]['binding'])
            except BaseException as rollback_error:
                rollback_errors.append(str(rollback_error))
        for action in reversed(created):
            try:
                bpy.data.actions.remove(action)
            except BaseException as rollback_error:
                rollback_errors.append(str(rollback_error))
        if linear_value_plan is not None:
            try:
                verify_untouched()
                recovered_scene, recovered_layer, recovered_rows = native.resolve_targets(manifest, writing=True)
                if coupled_profile and _coupled_group_gate(recovered_scene, recovered_layer, recovered_rows, linear_value_plan) != coupled_before:
                    native.fail('CONFLICT', 'Current group state did not restore')
                for owner, state in owners.items():
                    if native.binding_state(owner) != state['binding'] or native.action_state(state['binding']['action']) != state['source_action'] or native.custom_state(owner) != state['custom_properties']:
                        native.fail('CONFLICT', 'Protected original owner state did not restore')
            except BaseException as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise WorkflowError('ROLLBACK_FAILED', 'Animation edit rollback could not restore all bindings',
                                {'original_error': str(error), 'rollback_errors': rollback_errors}) from error
        raise
