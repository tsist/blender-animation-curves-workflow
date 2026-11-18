"""Explicit event-to-native-key bridge. Pure planning; no bpy or file writes.

An event owns only its declared key bindings, not every curve of a participant.
This translates time, not spatial contact or motion quality. Full key witnesses
come from native inspection; manual edits require re-reading, never rebuilding.
"""
import copy
import json
import struct
import math
from bisect import bisect_left, bisect_right
from collections import deque
from fractions import Fraction

from .contract import WorkflowError, _object, _array, _text, _number
from .timeline import solve_event_shift, TimelineError


def plan_event_shift(value, target_ids):
    _object(value, {'schema', 'events', 'dependencies', 'event_id', 'target_frame', 'bindings'},
            {'schema', 'events', 'dependencies', 'event_id', 'target_frame', 'bindings'}, 'event_shift')
    if value['schema'] != '1.0':
        raise WorkflowError('INVALID_REQUEST', 'event_shift requires schema 1.0')
    _array(value['events'], 'events', 512, 1)
    _array(value['dependencies'], 'dependencies', 4096)
    for event in value['events']:
        _object(event, {'id', 'frame', 'duration', 'locked', 'min_frame', 'max_frame'},
                {'id', 'frame'}, 'event')
    for dependency in value['dependencies']:
        _object(dependency, {'before', 'after', 'min_gap'}, {'before', 'after'}, 'dependency')
    try:
        schedule = solve_event_shift(value['events'], value['dependencies'], value['event_id'], value['target_frame'])
    except TimelineError as exc:
        raise WorkflowError(exc.code, str(exc), exc.details) from exc
    old = {e['id']: e['frame'] for e in value['events']}
    new = {e['id']: e['frame'] for e in schedule['events']}
    bindings = copy.deepcopy(_array(value['bindings'], 'bindings', 4096, 1))
    seen, bound_events, operations = set(), set(), []
    for binding in bindings:
        _object(binding, {'event', 'target', 'offset', 'expected_key'},
                {'event', 'target', 'offset', 'expected_key'}, 'event binding')
        event = _text(binding['event'], 'binding.event', 128)
        target = _text(binding['target'], 'binding.target', 128)
        if event not in old or target not in target_ids:
            raise WorkflowError('INVALID_REQUEST', 'Unknown event or target in binding')
        offset = _number(binding['offset'], 'binding.offset')
        key = binding['expected_key']
        if not isinstance(key, dict) or not isinstance(key.get('co'), list) or len(key['co']) != 2:
            raise WorkflowError('INVALID_REQUEST', 'expected_key must be a complete native key witness with co')
        try:
            encoded = json.dumps(key, allow_nan=False)
        except (ValueError, TypeError, RecursionError) as exc:
            raise WorkflowError('INVALID_REQUEST', 'expected_key must be finite JSON') from exc
        if len(encoded) > 32768:
            raise WorkflowError('INVALID_REQUEST', 'expected_key is too large')
        frame = _number(key['co'][0], 'expected_key.co[0]')
        _number(key['co'][1], 'expected_key.co[1]')
        expected = _number(old[event] + offset, 'binding source frame')
        destination = _number(new[event] + offset, 'binding destination frame')
        if (Fraction(expected) != Fraction(old[event]) + Fraction(binding['offset'])
                or Fraction(destination) != Fraction(new[event]) + Fraction(binding['offset'])
                or Fraction(frame) != Fraction(key['co'][0])):
            raise WorkflowError('UNSUPPORTED', 'Event binding time arithmetic loses precision; choose exactly representable timing')
        if frame != expected:
            raise WorkflowError('CONFLICT', 'Binding frame no longer matches event and offset', {'event': event, 'target': target})
        address = (target, frame)
        if address in seen:
            raise WorkflowError('CONFLICT', 'A native key cannot belong to multiple bindings', {'target': target, 'frame': frame})
        seen.add(address)
        bound_events.add(event)
        if destination != frame:
            try:
                native_frame = struct.unpack('f', struct.pack('f', destination))[0]
            except (OverflowError, struct.error) as exc:
                raise WorkflowError('UNSUPPORTED', 'Event key destination exceeds native float32 range') from exc
            if not math.isfinite(native_frame):
                raise WorkflowError('UNSUPPORTED', 'Event key destination exceeds native float32 range')
            if native_frame != destination:
                raise WorkflowError('UNSUPPORTED', 'Exact event key destination is not representable by native float32; choose explicit representable timing',
                                    {'target': target, 'requested_frame': destination, 'nearest_native_frame': native_frame})
            operations.append({'op': 'move_key', 'target': target, 'frame': frame, 'to_frame': destination})
    missing = set(old) - bound_events
    if missing:
        raise WorkflowError('INVALID_REQUEST', 'Every declared event requires explicit key bindings', {'events': sorted(missing)})
    return {'schema': 'animation-event-plan-v1', **schedule, 'operations': operations,
            'bindings': bindings,
            'scope': 'Explicit key translations only; spatial/contact/visual quality not evaluated'}


def validate_native_bindings(plan, keys_by_target, epsilon):
    """Validate full witnesses and order moves to vacate occupied destinations.

    Returns copied operations; rejects manual drift, undeclared occupancy,
    sub-epsilon ambiguity and cycles before the transaction creates Actions.
    """
    indexed = {}
    for target, keys in keys_by_target.items():
        ordered_keys = sorted(keys, key=lambda key: key['co'][0])
        indexed[target] = ([key['co'][0] for key in ordered_keys], ordered_keys)

    def matches(target, frame):
        frames, keys = indexed.get(target, ([], []))
        lo = math.nextafter(frame - epsilon, -math.inf)
        hi = math.nextafter(frame + epsilon, math.inf)
        return [key for key in keys[bisect_left(frames, lo):bisect_right(frames, hi)]
                if abs(key['co'][0] - frame) <= epsilon]

    for binding in plan['bindings']:
        target, expected = binding['target'], binding['expected_key']
        found = matches(target, expected['co'][0])
        if len(found) != 1 or found[0] != expected:
            raise WorkflowError('CONFLICT', 'Event key witness changed or is ambiguous; inspect and rebind',
                                {'target': target, 'frame': expected['co'][0]})
    operations = plan['operations']
    destinations = {}
    sources = {(op['target'], op['frame']): i for i, op in enumerate(operations)}
    for operation in operations:
        destinations.setdefault(operation['target'], []).append(operation['to_frame'])
    for target, frames in destinations.items():
        frames.sort()
        if any(right - left <= epsilon for left, right in zip(frames, frames[1:])):
            raise WorkflowError('CONFLICT', 'Event moves collide at a destination', {'target': target})
    dependents = [[] for _ in operations]
    indegree = [0] * len(operations)
    for index, operation in enumerate(operations):
        target, source, destination = operation['target'], operation['frame'], operation['to_frame']
        for key in matches(target, destination):
            occupant = key['co'][0]
            if occupant == source:
                continue
            blocker = sources.get((target, occupant))
            if blocker is None:
                raise WorkflowError('CONFLICT', 'Event destination is occupied by an unmoved key',
                                    {'target': target, 'frame': destination})
            dependents[blocker].append(index)
            indegree[index] += 1
    # A native FCurve.update() may merge a nearby unmoved key even when it
    # lies outside our exact identity epsilon. For a same-direction move set,
    # vacate the far end first, as in native keyframe retiming. This adds order
    # constraints; it never broadens key matching or weakens any witness.
    by_target = {}
    for index, operation in enumerate(operations):
        by_target.setdefault(operation['target'], []).append(index)
    for indices in by_target.values():
        source_order = sorted(indices, key=lambda i: operations[i]['frame'])
        if any(operations[right]['to_frame'] <= operations[left]['to_frame']
               for left, right in zip(source_order, source_order[1:])):
            # Other event plans can intentionally reorder keys. Keep their
            # existing exact-occupancy planning; this rule is for monotone maps.
            continue
        directions = {1 if operations[i]['to_frame'] > operations[i]['frame'] else -1
                      for i in indices}
        if len(directions) != 1:
            continue
        positive = next(iter(directions)) == 1
        far_first = sorted(indices, key=lambda i: operations[i]['frame'], reverse=positive)
        for blocker, dependent in zip(far_first, far_first[1:]):
            if dependent not in dependents[blocker]:
                dependents[blocker].append(dependent)
                indegree[dependent] += 1
    ready = deque(index for index, degree in enumerate(indegree) if degree == 0)
    ordered = []
    while ready:
        index = ready.popleft()
        ordered.append(copy.deepcopy(operations[index]))
        for dependent in dependents[index]:
            indegree[dependent] -= 1
            if not indegree[dependent]:
                ready.append(dependent)
    if len(ordered) != len(operations):
        raise WorkflowError('CONFLICT', 'Event moves form an occupied-destination cycle; choose noncolliding timing')
    return ordered


def _float32(value):
    try:
        native = struct.unpack('f', struct.pack('f', value))[0]
    except (OverflowError, struct.error) as exc:
        raise WorkflowError('UNSUPPORTED',
                            'Event key time exceeds native float32 range') from exc
    if not math.isfinite(native):
        raise WorkflowError('UNSUPPORTED',
                            'Event key time exceeds native float32 range')
    return native


def plan_local_warp(frames, start, contact, target_contact, end):
    """Monotone piecewise-linear local time warp around one contact.

    The window endpoints and non-window values are fixed.  The map is affine
    on [start, contact] and [contact, end], so dense native keys stay ordered.
    Frame values are quantized to Blender's native float32 key time and the
    requested/actual difference is reported rather than hidden.
    """
    for value, label in ((start, 'start'), (contact, 'contact'),
                         (target_contact, 'target_contact'), (end, 'end')):
        _number(value, label)
    if not start < contact < end:
        raise WorkflowError('INVALID_REQUEST',
                            'Local warp requires start < contact < end')
    if not start < target_contact < end:
        raise WorkflowError('INVALID_REQUEST',
                            'Local warp target must stay inside the event window')
    if not isinstance(frames, (list, tuple)):
        raise WorkflowError('INVALID_REQUEST', 'frames must be a list')
    left_slope = (target_contact - start) / (contact - start)
    right_slope = (end - target_contact) / (end - contact)
    if left_slope <= 0 or right_slope <= 0:
        raise WorkflowError('UNSUPPORTED', 'Local warp must be strictly increasing')
    rows = []
    previous = None
    maximum_error = 0.0
    for frame in frames:
        _number(frame, 'warp frame')
        if frame < start - 1e-9 or frame > end + 1e-9:
            raise WorkflowError('INVALID_REQUEST',
                                'Local warp only accepts keys inside its window',
                                {'frame': frame})
        if frame == start:
            requested = start
        elif frame == contact:
            requested = target_contact
        elif frame == end:
            requested = end
        elif frame < contact:
            requested = start + (frame - start) * left_slope
        else:
            requested = target_contact + (frame - contact) * right_slope
        native = _float32(requested)
        if previous is not None and native - previous[1] <= 1e-6:
            raise WorkflowError('UNSUPPORTED',
                                'Local warp collapses adjacent native key times',
                                {'previous': previous, 'frame': frame,
                                 'native': native})
        error = abs(native - requested)
        maximum_error = max(maximum_error, error)
        row = {'source': float(frame), 'requested': float(requested),
               'native': native, 'error': error}
        rows.append(row)
        previous = (float(frame), native)
    gaps = [b['native'] - a['native'] for a, b in zip(rows, rows[1:])]
    return {
        'schema': 'animation-local-warp-v1',
        'start': float(start), 'contact': float(contact),
        'target_contact': float(target_contact), 'end': float(end),
        'left_slope': left_slope, 'right_slope': right_slope,
        'rows': rows,
        'min_gap': min(gaps) if gaps else None,
        'quantization': {
            'type': 'float32_key_time',
            'max_abs_error': maximum_error,
            'nonzero': sum(1 for row in rows if row['error'] != 0.0),
        },
        'scope': ('Keys are mapped once; endpoints are fixed and the contact '
                  'is exact. No key deletion, merge, interpolation change, or '
                  'global clip conversion is performed.'),
    }
