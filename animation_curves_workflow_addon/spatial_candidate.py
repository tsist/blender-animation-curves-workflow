"""Pure proposal geometry only. Not native validation, collision clearance or commit."""
import hashlib
import json
import math
from .contract import WorkflowError


def fail(message):
    raise WorkflowError('INVALID_REQUEST', message)


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail('Expected a finite number')
    try:
        result = float(value)
    except (OverflowError, ValueError):
        fail('Number is outside supported range')
    if not math.isfinite(result) or abs(result) > 1e9:
        fail('Number is outside supported finite geometry range')
    return result


def polygon(value, rectangle=False):
    if not isinstance(value, list) or not 3 <= len(value) <= 64:
        fail('Expected 3 to 64 ordered face vertices')
    if any(not isinstance(p, (list, tuple)) or len(p) != 3 for p in value):
        fail('Each vertex must contain three coordinates')
    points = [tuple(number(x) for x in p) for p in value]
    # Initial policy is deliberately exact-horizontal. Native callers must report
    # unsupported geometry rather than flattening imperfect or tilted surfaces.
    if len(set(points)) != len(points) or any(p[2] != points[0][2] for p in points):
        fail('Face must have distinct vertices in one horizontal plane')
    signs = []
    for a, b in zip(points, points[1:] + points[:1]):
        cross = [(b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0])
                 for p in points if p != a and p != b]
        if not cross or any(c == 0 for c in cross) or not (all(c > 0 for c in cross) or all(c < 0 for c in cross)):
            fail('Face must be strictly convex and ordered without crossings')
        signs.append(cross[0] > 0)
    if len(set(signs)) != 1:
        fail('Face order is inconsistent')
    if rectangle:
        xs, ys = {p[0] for p in points}, {p[1] for p in points}
        if len(points) != 4 or len(xs) != 2 or len(ys) != 2 or {(p[0], p[1]) for p in points} != {(x,y) for x in xs for y in ys}:
            fail('Support must be an axis-aligned rectangle')
    return points


def propose(patch, support, *, margin, max_translation, locked_axes=(), anchor=None):
    """Minimum translation of a convex patch onto a finite horizontal rectangle.

    Anchor is an optional explicit XY vertex-centroid destination. This does not
    check meshes, scene obstacles, key witnesses, native float32 or participant
    identity. Returned status cannot be used as permission to commit.
    """
    p, s = polygon(patch), polygon(support, rectangle=True)
    margin, cap = number(margin), number(max_translation)
    if margin < 0 or cap < 0:
        fail('Margin and maximum translation must be nonnegative')
    if not isinstance(locked_axes, (list, tuple)) or any(x not in ('x','y','z') for x in locked_axes) or len(set(locked_axes)) != len(locked_axes):
        fail('Locked axes must be unique x/y/z names')
    if anchor is not None and (not isinstance(anchor, (list, tuple)) or len(anchor) != 2):
        fail('Anchor must be an explicit XY point')
    anchor = None if anchor is None else [number(x) for x in anchor]
    delta = []
    for i in range(2):
        lo = min(v[i] for v in s) + margin - min(v[i] for v in p)
        hi = max(v[i] for v in s) - margin - max(v[i] for v in p)
        if lo > hi:
            raise WorkflowError('CONFLICT', 'Patch and margin do not fit support')
        d = min(max(0., lo), hi) if anchor is None else anchor[i] - sum(v[i] for v in p)/len(p)
        if not lo <= d <= hi:
            raise WorkflowError('CONFLICT', 'Requested anchor violates finite support margin')
        delta.append(d)
    delta.append(s[0][2] - p[0][2])
    if any(delta['xyz'.index(axis)] != 0 for axis in locked_axes):
        raise WorkflowError('CONFLICT', 'Required translation violates a locked axis')
    if math.hypot(*delta) > cap:
        raise WorkflowError('CONFLICT', 'Required translation exceeds displacement cap')
    translated = [tuple(v[i]+delta[i] for i in range(3)) for v in p]
    # Validate the actual resulting numbers, not only ideal interval algebra.
    # Large coordinates can absorb a small requested margin or translation.
    for i in range(2):
        actual_margin = min(min(v[i] for v in translated)-min(v[i] for v in s),
                            max(v[i] for v in s)-max(v[i] for v in translated))
        if actual_margin < margin:
            raise WorkflowError('UNSUPPORTED', 'Numeric precision loses requested finite-face margin')
    if any(v[2] != s[0][2] for v in translated):
        raise WorkflowError('UNSUPPORTED', 'Numeric precision loses support plane')
    if anchor is not None and any(sum(v[i] for v in translated)/len(translated) != anchor[i] for i in range(2)):
        raise WorkflowError('UNSUPPORTED', 'Numeric precision loses requested anchor')
    result = {'schema':'spatial-geometry-proposal-v1', 'status':'geometry_only_not_accepted',
              'patch':[list(v) for v in p], 'support':[list(v) for v in s],
              'margin':margin, 'max_translation':cap, 'locked_axes':sorted(locked_axes),
              'anchor':anchor, 'translation':delta,
              'pending':['native_identity','float32_readback','scene_collision','key_protection','explicit_acceptance','cold_geometry']}
    result['proposal_sha256'] = hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    return result
