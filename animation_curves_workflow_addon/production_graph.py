"""Run native Graph Editor tools on specified channels in an isolated Action."""

from contextlib import contextmanager

import bpy

from . import native
from .production_drivers import require_undriven
from .production_slot import _editable_object, _slot_bags


@contextmanager
def graph_editor(context, owner):
    screen, layer = context.screen, context.view_layer
    if screen is None:
        native.fail('UNSUPPORTED', 'Native graph tools require a Blender screen')
    editor = next((area for area in screen.areas if area.type == 'GRAPH_EDITOR'), None)
    if editor is None:
        editor = next((area for area in screen.areas if area.type == 'DOPESHEET_EDITOR'), None)
    if editor is None:
        editor = next((area for area in screen.areas if area.type == 'VIEW_3D'), None)
    if editor is None:
        native.fail('UNSUPPORTED', 'A Graph Editor or editable area is required in the active screen')
    active = layer.objects.active
    selected = tuple(context.selected_objects)
    old_type, old_ui_type = editor.type, editor.ui_type
    scope = bpy.data.collections.new('Animation Graph Scope')
    try:
        for obj in selected:
            obj.select_set(False, view_layer=layer)
        owner.select_set(True, view_layer=layer)
        layer.objects.active = owner
        scope.objects.link(owner)
        editor.type = 'GRAPH_EDITOR'
        space = editor.spaces.active
        filters = space.dopesheet
        original_mode = space.mode
        filter_values = {
            'show_only_selected': False, 'show_hidden': True,
            'show_only_slot_of_active_object': True, 'use_filter_invert': False,
            'show_only_errors': False, 'filter_text': '', 'filter_fcurve_name': '',
            'filter_collection': scope, 'show_armatures': True, 'show_transforms': True,
        }
        old_filters = {name: getattr(filters, name) for name in filter_values}
        try:
            space.mode = 'FCURVES'
            for name, value in filter_values.items():
                setattr(filters, name, value)
            region = next(region for region in editor.regions if region.type == 'WINDOW')
            with context.temp_override(area=editor, region=region):
                yield
        finally:
            space.mode = original_mode
            for name, value in old_filters.items():
                setattr(filters, name, value)
    finally:
        if editor.type != old_type or editor.ui_type != old_ui_type:
            editor.type = old_type
            editor.ui_type = old_ui_type
        owner.select_set(False, view_layer=layer)
        for obj in selected:
            obj.select_set(True, view_layer=layer)
        layer.objects.active = active
        bpy.data.collections.remove(scope)


def _address(curve):
    return curve.data_path, curve.array_index


def _channels(channels):
    if not isinstance(channels, list) or not channels:
        native.fail('INVALID_REQUEST', 'A graph operation needs explicit target channels')
    valid = set()
    for channel in channels:
        if (not isinstance(channel, dict) or set(channel) != {'path', 'index'}
                or not isinstance(channel['path'], str) or not channel['path']
                or type(channel['index']) is not int or channel['index'] < 0):
            native.fail('INVALID_REQUEST', 'Each target channel needs a path and nonnegative index')
        address = channel['path'], channel['index']
        if address in valid:
            native.fail('INVALID_REQUEST', 'Duplicate graph target', {'channel': address})
        valid.add(address)
    return valid


def _graph_tool(tool, options):
    allowed = {
        'euler_filter': set(),
        'gaussian_smooth': {'factor', 'sigma', 'filter_width'},
        'butterworth_smooth': {'cutoff_frequency', 'filter_order', 'samples_per_frame', 'blend', 'blend_in_out'},
        'decimate': {'mode', 'factor', 'remove_error_margin'},
        'stepped': {'frame_step', 'frame_offset'},
    }
    if not isinstance(tool, str) or tool not in allowed or options.keys() - allowed[tool]:
        native.fail('INVALID_REQUEST', 'Unknown graph tool or options', {'tool': tool})
    if tool == 'stepped':
        step, offset = options.get('frame_step', 2), options.get('frame_offset', 0)
        if (type(step) not in (int, float) or not 1 <= step <= 1000
                or type(offset) not in (int, float) or not -100_000 <= offset <= 100_000):
            native.fail('INVALID_REQUEST', 'Stepped curves need a step of 1..1000 and a finite offset')
        return None
    return getattr(bpy.ops.graph, tool)


def _select(curve, enabled):
    curve.select, curve.hide = enabled, not enabled
    previous = {}
    for key in curve.keyframe_points:
        previous[float(key.co.x)] = key.select_control_point, key.select_left_handle, key.select_right_handle
        key.select_control_point = key.select_left_handle = key.select_right_handle = enabled
    return previous


def _restore_selection(curve, state):
    curve.select, curve.hide, previous = state
    for key in curve.keyframe_points:
        selected = previous.get(float(key.co.x), (False, False, False))
        key.select_control_point, key.select_left_handle, key.select_right_handle = selected


def apply(context, owner, *, tool, channels, options=None):
    _editable_object(owner)
    addresses = _channels(channels)
    require_undriven(owner, addresses)
    options = {} if options is None else options
    if not isinstance(options, dict):
        native.fail('INVALID_REQUEST', 'Graph options must be an object')
    operator = _graph_tool(tool, options)
    animation = owner.animation_data
    if not animation or not animation.action or not animation.action_slot:
        native.fail('NOT_FOUND', 'Graph operations need an active Action Slot')
    if animation.use_tweak_mode or animation.nla_tracks:
        native.fail('CONFLICT', 'Graph operations require an active Action outside NLA')
    source, source_slot = animation.action, animation.action_slot
    native.local(source)
    bags = _slot_bags(source)
    identifier = source_slot.identifier
    _, original_bag = bags[identifier]
    if original_bag is None:
        native.fail('NOT_FOUND', 'Graph target Slot has no channelbag')
    present = {_address(curve): curve for curve in original_bag.fcurves}
    if addresses - present.keys():
        native.fail('NOT_FOUND', 'At least one Graph channel is missing',
                    {'channels': sorted(addresses - present.keys())})
    if any(present[address].lock or present[address].mute or not present[address].keyframe_points
           or present[address].modifiers or present[address].sampled_points for address in addresses):
        native.fail('CONFLICT', 'Graph channels must be unlocked, unmuted, and free of existing modifiers or samples')
    before = {name: native.bag_state(bag) for name, (_, bag) in bags.items()}
    candidate = source.copy()
    try:
        candidate.use_fake_user = False
        copied_bags = _slot_bags(candidate)
        slot, bag = copied_bags[identifier]
        animation.action = candidate
        animation.action_slot = slot
        old_selection = {}
        for curve in bag.fcurves:
            address = _address(curve)
            select, hide = curve.select, curve.hide
            old_selection[address] = select, hide, _select(curve, address in addresses)
        bpy.context.view_layer.update()
        if operator is None:
            for curve in bag.fcurves:
                if _address(curve) in addresses:
                    modifier = curve.modifiers.new(type='STEPPED')
                    modifier.frame_step = options.get('frame_step', 2)
                    modifier.frame_offset = options.get('frame_offset', 0)
        else:
            with graph_editor(context, owner):
                expected = {curve for curve in bag.fcurves if _address(curve) in addresses}
                visible = {curve for curve in context.editable_fcurves if curve.select}
                if visible != expected:
                    native.fail('CONFLICT', 'Graph Editor channel selection differs from the request',
                                {'expected': sorted(addresses), 'observed': sorted(map(_address, visible))})
                if not operator.poll() or 'FINISHED' not in operator('EXEC_DEFAULT', **options):
                    native.fail('VALIDATION_FAILED', 'Blender Graph Editor rejected the requested operation',
                                {'tool': tool})
        for curve in bag.fcurves:
            _restore_selection(curve, old_selection[_address(curve)])
        for name, (_, other_bag) in copied_bags.items():
            unchanged = before[name]
            observed = native.bag_state(other_bag)
            if name == identifier:
                keep = lambda row: (row['data_path'], row['array_index']) not in addresses
                unchanged = list(filter(keep, unchanged))
                observed = list(filter(keep, observed))
            if observed != unchanged:
                native.fail('CONFLICT', 'Graph tool changed a non-target channel or Slot', {'slot': name})
        after = native.bag_state(bag)
        if before[identifier] == after:
            animation.action, animation.action_slot = source, source_slot
            bpy.data.actions.remove(candidate)
            return {'status': 'unchanged', 'tool': tool, 'source_action': source.name}
        source.use_fake_user = True
        return {'status': 'applied', 'tool': tool, 'source_action': source.name,
                'action': candidate.name, 'slot_identifier': identifier,
                'keys_before': sum(len(row['keys']) for row in before[identifier]),
                'keys_after': sum(len(row['keys']) for row in after)}
    except Exception:
        animation.action, animation.action_slot = source, source_slot
        bpy.data.actions.remove(candidate)
        raise
