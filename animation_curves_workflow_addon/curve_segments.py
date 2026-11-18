"""Exact native BEZIER/LINEAR/CONSTANT interval extraction, no baking."""
import copy
import math

def lerp(a, b, t):
    return [a[i] + (b[i] - a[i]) * t for i in range(2)]

def split(points, t):
    a, b, c = [lerp(points[i], points[i+1], t) for i in range(3)]
    d, e = lerp(a, b, t), lerp(b, c, t)
    f = lerp(d, e, t)
    return [points[0], a, d, f], [f, e, c, points[3]]

def insert_boundary(states, frame):
    if any(k['co'][0] == frame for k in states):
        return states
    index = next((i for i in range(len(states)-1) if states[i]['co'][0] < frame < states[i+1]['co'][0]), None)
    if index is None:
        # Constant endpoint extrapolation is supported by the current clip's
        # explicit full-span contract; arbitrary extrapolation is not inferred.
        raise ValueError('Boundary lies outside the native curve span')
    left, right = states[index], states[index+1]
    kind = left['interpolation']
    new = copy.deepcopy(left)
    if kind == 'BEZIER':
        p = [left['co'], left['handle_right'], right['handle_left'], right['co']]
        if not p[0][0] <= p[1][0] <= p[2][0] <= p[3][0]:
            raise ValueError('Bezier boundary needs Blender handle correction; not silently approximated')
        lo, hi = 0., 1.
        for _ in range(56):
            mid = (lo + hi) / 2
            a, b = split(p, mid)
            if a[-1][0] < frame:
                lo = mid
            else:
                hi = mid
        a, b = split(p, (lo+hi)/2)
        new['co'] = [frame, a[-1][1]]
        new['handle_left'], new['handle_right'] = a[-2], b[1]
        left['handle_right'] = a[1]
        right['handle_left'] = b[-2]
        left['handle_right_type'] = 'FREE'
        right['handle_left_type'] = 'FREE'
    elif kind in ('LINEAR', 'CONSTANT'):
        t = (frame-left['co'][0])/(right['co'][0]-left['co'][0])
        y = left['co'][1] if kind == 'CONSTANT' else left['co'][1] + t*(right['co'][1]-left['co'][1])
        new['co'] = [frame, y]
        new['handle_left'] = [frame, y]
        new['handle_right'] = [frame, y]
    else:
        raise ValueError('Unsupported native segment interpolation: '+kind)
    new['handle_left_type'] = new['handle_right_type'] = 'FREE'
    new['co_ui'] = list(new['co'])
    states.insert(index+1, new)
    return states

def trim_states(keys, start, end):
    if not math.isfinite(start) or not math.isfinite(end) or start >= end:
        raise ValueError('Finite ordered interval required')
    states = copy.deepcopy(keys)
    insert_boundary(states, start)
    insert_boundary(states, end)
    result = [k for k in states if start <= k['co'][0] <= end]
    assert result[0]['co'][0] == start and result[-1]['co'][0] == end
    # Boundary derivatives belong to the retained segment. Explicit FREE
    # boundary handles retain its native shape instead of deriving a new end.
    for key in (result[0], result[-1]):
        key['handle_left_type'] = key['handle_right_type'] = 'FREE'
    return result

def write_states(curve, states):
    curve.keyframe_points.clear()
    curve.keyframe_points.add(len(states))
    for key in curve.keyframe_points:
        key.handle_left_type = key.handle_right_type = 'FREE'
    for key,state in zip(curve.keyframe_points,states):
        key.co = state['co']
        key.handle_left = state['handle_left']
        key.handle_right = state['handle_right']
    for key,state in zip(curve.keyframe_points,states):
        for field in ('interpolation','easing','back','amplitude','period','type',
                      'handle_left_type','handle_right_type',
                      'select_left_handle','select_right_handle','select_control_point'):
            if field in state:
                setattr(key,field,state[field])
    for key,state in zip(curve.keyframe_points,states):
        key.co = state['co']
        key.handle_left = state['handle_left']
        key.handle_right = state['handle_right']
