"""Pure, bpy-free prototype for calibrated two-segment FK/IK leg contracts.

0 degrees is straight. Positive flexion bends toward the authored front pole.
The 0..150 interval is an authored default, not a medical/anatomical guarantee.
Do not interpret a negative Euler/quaternion component as negative flexion.
"""
import math

FLEXION_MIN_DEG = 0.0
FLEXION_MAX_DEG = 150.0


def finite(x):
    if type(x) not in (float, int) or not math.isfinite(x):
        raise ValueError('Expected finite numeric value')
    return float(x)


def vec(v):
    if len(v) != 3:
        raise ValueError('Expected three-vector')
    return tuple(finite(x) for x in v)


def dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def sub(a, b):
    return tuple(x-y for x, y in zip(a, b))


def unit(a):
    a = vec(a)
    length = math.sqrt(dot(a, a))
    if length < 1e-10:
        raise ValueError('Degenerate direction')
    return tuple(x/length for x in a)


def signed_flexion(hip, knee, end, positive_hinge_axis, hinge_tolerance=1e-5):
    """Measure geometry in a common coordinate space, not channel signs.

    positive_hinge_axis must follow the thigh's actual evaluated orientation;
    for current template both sides use its local +X, not mirrored world axes.
    Out-of-plane rotation is rejected, never hidden by a projected 2D angle.
    """
    h, k, e = map(vec, (hip, knee, end))
    u, v, n = unit(sub(k, h)), unit(sub(e, k)), unit(positive_hinge_axis)
    residual = max(abs(dot(u, n)), abs(dot(v, n)))
    if residual > finite(hinge_tolerance):
        raise ValueError('Knee is outside its calibrated hinge plane')
    return math.degrees(math.atan2(dot(n, cross(u, v)), dot(u, v)))


def flexion_channel(desired_deg, rest_deg, minimum=FLEXION_MIN_DEG,
                    maximum=FLEXION_MAX_DEG, clamp=False):
    """Return local X rotation for this calibrated positive-X hinge.

    This is only valid after rest/basis calibration; arbitrary rigs unsupported.
    Clamping is explicit and observable; generators should inspect `clamped`.
    """
    desired, rest, lo, hi = map(finite, (desired_deg, rest_deg, minimum, maximum))
    if not 0 <= lo < hi < 180 or not 0 <= rest < 180:
        raise ValueError('Invalid authored flexion interval/rest calibration')
    if not clamp and not lo <= desired <= hi:
        raise ValueError('Desired knee flexion outside permitted interval')
    applied = min(hi, max(lo, desired))
    return {'requested_flexion_deg': desired, 'applied_flexion_deg': applied,
            'local_x_deg': applied-rest, 'clamped': applied != desired,
            'local_x_min_deg': lo-rest, 'local_x_max_deg': hi-rest}


def solve_two_segment(hip, end, length_upper, length_lower, front_pole_direction,
                      minimum=FLEXION_MIN_DEG, maximum=FLEXION_MAX_DEG):
    """Strict two-link position solve with explicit positive/front branch.

    No unreachable-target clamp, root movement, scale or singular-pole guess.
    Output does not certify contact, collisions, balance or native interpolation.
    """
    h, e = vec(hip), vec(end)
    l1, l2, lo, hi = map(finite, (length_upper, length_lower, minimum, maximum))
    if not l1 > 0 or not l2 > 0 or not 0 <= lo < hi < 180:
        raise ValueError('Invalid segment lengths/flexion interval')
    offset = sub(e, h)
    distance = math.sqrt(dot(offset, offset))
    if distance < 1e-10 or distance > l1+l2 or distance <= abs(l1-l2):
        raise ValueError('Unreachable or singular end target')
    cosine = (distance*distance-l1*l1-l2*l2)/(2*l1*l2)
    flexion = math.degrees(math.acos(max(-1., min(1., cosine))))
    if not lo-1e-7 <= flexion <= hi+1e-7:
        raise ValueError('Reachable target violates knee flexion limits')
    axis = unit(offset)
    pole = unit(front_pole_direction)
    along = dot(axis, pole)
    bend = unit(tuple(p-a*along for p, a in zip(pole, axis)))
    x = (l1*l1-l2*l2+distance*distance)/(2*distance)
    height = math.sqrt(max(0., l1*l1-x*x))
    knee = tuple(p+a*x+b*height for p, a, b in zip(h, axis, bend))
    positive_axis = unit(cross(bend, axis))
    return {'knee': knee, 'flexion_deg': flexion, 'positive_hinge_axis': positive_axis,
            'bend_direction': bend, 'singular_straight': height < 1e-8}
