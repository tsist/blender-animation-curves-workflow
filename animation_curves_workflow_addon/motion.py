"""Bounded rigless layout planners; pure Python, no Blender or external dependencies.

All positions are metres in a caller-owned, translation-only world frame. These
planners create fresh candidate motion; they do not understand or overwrite
existing animation. Returned keys are intended for LINEAR native F-Curves.
Support is exact under that piecewise-linear representation because root and
local foot keys share the same times. Swing smoothness is a sampled smoothstep,
not an assurance of collision-free anatomy or production animation quality.
"""
import math


class MotionError(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise MotionError('INVALID_REQUEST', label + ' must be finite')
    return float(value)


def _vector(value, label):
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise MotionError('INVALID_REQUEST', label + ' must contain three coordinates')
    return tuple(_number(x, label) for x in value)


def _add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def _sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def _mul(a, factor):
    return tuple(x * factor for x in a)


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _length(a):
    return math.sqrt(_dot(a, a))


def _lerp(a, b, t):
    return _add(a, _mul(_sub(b, a), t))


def _smooth(t):
    return t * t * (3.0 - 2.0 * t)


def _samples(value):
    if type(value) is not int or not 4 <= value <= 128:
        raise MotionError('INVALID_REQUEST', 'samples_per_phase must be an integer in 4..128')
    return value


def _positive(value, label):
    value = _number(value, label)
    if value <= 0:
        raise MotionError('INVALID_REQUEST', label + ' must be positive')
    return value


def sample_keys(keys, frame):
    """Evaluate this module's ordered vector keys with native LINEAR semantics."""
    frame = _number(frame, 'frame')
    if not keys:
        raise MotionError('INVALID_REQUEST', 'keys are empty')
    if frame <= keys[0]['frame']:
        return tuple(keys[0]['value'])
    for left, right in zip(keys, keys[1:]):
        if frame <= right['frame']:
            span = right['frame'] - left['frame']
            if span <= 0:
                raise MotionError('INVALID_REQUEST', 'key times must increase')
            return _lerp(left['value'], right['value'], (frame - left['frame']) / span)
    return tuple(keys[-1]['value'])


def plan_two_step_walk(start_frame, end_frame, start_position, displacement,
                       scale=1.0, samples_per_phase=24, locked_landings=None):
    """Plan two alternating steps on a flat plane without bones or constraints.

    Root moves uniformly. Left supports the first half while right swings, then
    right supports while left swings. Each foot advances the entire displacement.
    The bounded distance is at most 1.2*scale, not a universal gait/reach solver.
    ``locked_landings`` may contain left/right {frame, position}; an incompatible
    lock rejects the plan instead of silently changing the lock or root path.
    """
    first, last = _number(start_frame, 'start_frame'), _number(end_frame, 'end_frame')
    if last <= first:
        raise MotionError('INVALID_REQUEST', 'end_frame must follow start_frame')
    origin, delta = _vector(start_position, 'start_position'), _vector(displacement, 'displacement')
    scale, samples = _positive(scale, 'scale'), _samples(samples_per_phase)
    if abs(delta[2]) > 1e-12:
        raise MotionError('UNSUPPORTED', 'This planner supports a flat ground translation only')
    distance = _length(delta)
    if not 0 < distance <= 1.2 * scale:
        raise MotionError('CONFLICT', 'Two-step displacement exceeds the bounded stride or is zero',
                          {'distance': distance, 'maximum': 1.2 * scale})
    forward = _mul(delta, 1.0 / distance)
    lateral = (-forward[1], forward[0], 0.0)
    offsets = {'left': _mul(lateral, 0.18 * scale), 'right': _mul(lateral, -0.18 * scale)}
    foot_half_height = 0.06 * scale
    foot_starts = {side: _add(_add(origin, offset), (0.0, 0.0, foot_half_height))
                   for side, offset in offsets.items()}
    midpoint = (first + last) / 2.0
    landing_frames = {'right': midpoint, 'left': last}
    locks = {} if locked_landings is None else locked_landings
    if not isinstance(locks, dict) or set(locks) - {'left', 'right'}:
        raise MotionError('INVALID_REQUEST', 'locked_landings accepts left and right only')
    for side, lock in locks.items():
        if not isinstance(lock, dict) or set(lock) != {'frame', 'position'}:
            raise MotionError('INVALID_REQUEST', 'Landing lock requires frame and position')
        expected = _add(foot_starts[side], delta)
        actual = _vector(lock['position'], 'landing position')
        if abs(_number(lock['frame'], 'landing frame') - landing_frames[side]) > 1e-9 or _length(_sub(actual, expected)) > 1e-9 * scale:
            raise MotionError('CONFLICT', 'Landing lock conflicts with requested root path or timing',
                              {'side': side, 'required_frame': landing_frames[side], 'required_position': list(expected)})
    root_keys, feet = [], {'left': [], 'right': []}
    # The shared knot times make the fixed world support cancellation exact
    # between knots too, not just at the sampled check frames.
    for index in range(2 * samples + 1):
        progress = index / (2.0 * samples)
        frame = first + (last - first) * progress
        root = _add(origin, _mul(delta, progress))
        root_keys.append({'frame': frame, 'value': list(root)})
        for side in ('left', 'right'):
            swing = max(0.0, min(1.0, 2.0 * progress if side == 'right' else 2.0 * progress - 1.0))
            world = _add(foot_starts[side], _mul(delta, _smooth(swing)))
            world = _add(world, (0.0, 0.0, 0.16 * scale * math.sin(math.pi * swing) ** 2))
            feet[side].append({'frame': frame, 'value': list(_sub(world, root))})
    return {
        'schema': 'rigless-two-step-v1', 'interpolation': 'LINEAR', 'scale': scale,
        'root_world': root_keys, 'left_foot_local': feet['left'], 'right_foot_local': feet['right'],
        'support': [
            {'side': 'left', 'start_frame': first, 'end_frame': midpoint,
             'sole_world': list(_sub(foot_starts['left'], (0.0, 0.0, foot_half_height)))},
            {'side': 'right', 'start_frame': midpoint, 'end_frame': last,
             'sole_world': list(_sub(_add(foot_starts['right'], delta), (0.0, 0.0, foot_half_height)))},
        ],
        'landings': {side: {'frame': landing_frames[side], 'position': list(_add(foot_starts[side], delta))}
                     for side in ('left', 'right')},
        'forward': list(forward), 'foot_half_height': foot_half_height,
        'limitations': ['Flat ground; translation-only parent; two prescribed steps.',
                        'No collision, balance, arbitrary existing-rig or visual-quality certification.'],
    }


def solve_two_link(start, end, upper_length, lower_length, pole=(1.0, 0.0, 0.0)):
    """Return the intermediate joint for two rigid Object segments, not a rig.

    The caller owns transforms. Unreachable endpoints reject instead of stretching
    segment lengths. Degenerate/collinear poles are classified rather than guessed.
    """
    start, end, pole = _vector(start, 'start'), _vector(end, 'end'), _vector(pole, 'pole')
    upper, lower = _positive(upper_length, 'upper_length'), _positive(lower_length, 'lower_length')
    direction, distance = _sub(end, start), _length(_sub(end, start))
    if distance <= 1e-12 or distance > upper + lower + 1e-10 or distance < abs(upper - lower) - 1e-10:
        raise MotionError('CONFLICT', 'Rigid segment endpoints are unreachable', {'distance': distance, 'upper': upper, 'lower': lower})
    axis = _mul(direction, 1.0 / distance)
    bend = _sub(pole, _mul(axis, _dot(pole, axis)))
    norm = _length(bend)
    if norm <= 1e-12:
        raise MotionError('CONFLICT', 'Bend pole is collinear with segment endpoints')
    along = (upper * upper - lower * lower + distance * distance) / (2.0 * distance)
    height = math.sqrt(max(0.0, upper * upper - along * along))
    return _add(_add(start, _mul(axis, along)), _mul(bend, height / norm))


def plan_hand_contact(attacker_root, defender_root, attacker_scale=1.0, defender_scale=1.0,
                      approach_frame=1.0, contact_start=13.0, contact_end=19.0,
                      release_frame=31.0, samples_per_phase=12, anchor=None, locks=None):
    """Adapt an approach/held contact/release hand fixture to two body scales.

    Roots are static translation-only ground positions. Hands meet at a common
    world anchor throughout the declared window. The routine checks simple arm
    reach from shoulder reference points; it does not solve torso/limb collisions
    or infer an attack, block or causal performance from coincident hand points.
    """
    a, b = _vector(attacker_root, 'attacker_root'), _vector(defender_root, 'defender_root')
    sa, sb = _positive(attacker_scale, 'attacker_scale'), _positive(defender_scale, 'defender_scale')
    times = [_number(v, name) for v, name in zip(
        (approach_frame, contact_start, contact_end, release_frame),
        ('approach_frame', 'contact_start', 'contact_end', 'release_frame'))]
    if not times[0] < times[1] < times[2] < times[3]:
        raise MotionError('INVALID_REQUEST', 'Contact requires approach < contact_start < contact_end < release')
    samples = _samples(samples_per_phase)
    horizontal = (b[0] - a[0], b[1] - a[1], 0.0)
    separation = _length(horizontal)
    if separation <= 1e-12:
        raise MotionError('CONFLICT', 'Contact roots require distinct ground positions')
    direction = _mul(horizontal, 1.0 / separation)
    shoulders = (_add(a, (0.0, 0.0, 1.35 * sa)), _add(b, (0.0, 0.0, 1.35 * sb)))
    common = _vector(anchor, 'anchor') if anchor is not None else _lerp(shoulders[0], shoulders[1], sa / (sa + sb))
    locks = {} if locks is None else locks
    if not isinstance(locks, dict) or set(locks) - {'contact_start', 'contact_end', 'anchor'}:
        raise MotionError('INVALID_REQUEST', 'Unknown contact lock')
    for name, value in (('contact_start', times[1]), ('contact_end', times[2])):
        if name in locks and abs(_number(locks[name], name) - value) > 1e-9:
            raise MotionError('CONFLICT', 'Contact timing conflicts with lock', {'field': name})
    if 'anchor' in locks and _length(_sub(_vector(locks['anchor'], 'locked anchor'), common)) > 1e-9 * min(sa, sb):
        raise MotionError('CONFLICT', 'Contact anchor conflicts with lock')
    reaches = [0.95 * sa, 0.95 * sb]
    for role, shoulder, reach in zip(('attacker', 'defender'), shoulders, reaches):
        actual = _length(_sub(common, shoulder))
        if actual > reach:
            raise MotionError('CONFLICT', 'Contact anchor exceeds bounded arm reach', {'role': role, 'distance': actual, 'maximum': reach})
    rests = (_add(_add(a, _mul(direction, 0.25 * sa)), (0.0, 0.0, 1.10 * sa)),
             _add(_add(b, _mul(direction, -0.25 * sb)), (0.0, 0.0, 1.10 * sb)))
    frames = [times[0] + (times[1] - times[0]) * i / samples for i in range(samples + 1)]
    frames += [times[2]]
    frames += [times[2] + (times[3] - times[2]) * i / samples for i in range(1, samples + 1)]
    result_keys = [[], []]
    for frame in frames:
        if frame < times[1]:
            blend = _smooth((frame - times[0]) / (times[1] - times[0]))
        elif frame <= times[2]:
            blend = 1.0
        else:
            blend = 1.0 - _smooth((frame - times[2]) / (times[3] - times[2]))
        for index, root in enumerate((a, b)):
            result_keys[index].append({'frame': frame, 'value': list(_sub(_lerp(rests[index], common, blend), root))})
    return {
        'schema': 'rigless-hand-contact-v1', 'interpolation': 'LINEAR',
        'attacker_root': list(a), 'defender_root': list(b), 'attacker_scale': sa, 'defender_scale': sb,
        'attacker_hand_local': result_keys[0], 'defender_hand_local': result_keys[1],
        'contact': {'start_frame': times[1], 'end_frame': times[2], 'anchor_world': list(common)},
        'phases': {'approach': [times[0], times[1]], 'contact': [times[1], times[2]], 'release': [times[2], times[3]]},
        'shoulders_world': [list(p) for p in shoulders],
        'limitations': ['Prescribed hand reference contact, not whole-body collision or performance validation.',
                        'Static translation-only roots; no force, balance or arbitrary prop geometry solve.'],
    }
