"""Pure entry-target kernel. No scene access, file IO, rig solver or key writes.

Coordinates/heights are metres; velocity is metres per frame (not per second).
Caller must independently qualify the footless two-segment owned template,
actual LOCAL knee constraints and evaluated physical flexion in [0, 150].
This target is not a contact-point lock or a continuity/artistic guarantee.
"""
import math


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(name + ' must be a finite real number')
    try:
        value = float(value)
    except (OverflowError, ValueError):
        raise ValueError(name + ' must be a finite real number') from None
    if not math.isfinite(value):
        raise ValueError(name + ' must be finite')
    return value


def _xy(value, name):
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError(name + ' must have exactly two components')
    return tuple(_number(x, name) for x in value)


def build_entry_target(*, anchor_xy, incoming_velocity_m_per_frame,
                       initial_min_z, time, entry_start, entry_end):
    """Return explicit targets within the closed entry interval.

    Outside this interval is rejected, never clamped or used to alter later
    planted/exit motion. Preserve the caller's existing endpoint key policy.
    Negative initial_min_z is retained, not silently hidden; contact tolerance
    is a separate evaluated-mesh qualification owned by the native workflow.
    """
    anchor = _xy(anchor_xy, 'anchor_xy')
    velocity = _xy(incoming_velocity_m_per_frame, 'incoming_velocity_m_per_frame')
    height = _number(initial_min_z, 'initial_min_z')
    t = _number(time, 'time')
    start = _number(entry_start, 'entry_start')
    end = _number(entry_end, 'entry_end')
    duration = end - start
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('entry interval must have finite positive duration')
    if not start <= t <= end:
        raise ValueError('time outside entry interval')
    u = (t - start) / duration
    # Match the qualified compiler operation ordering, not algebraic rewrites.
    weight = u*u*u*(10+u*(-15+6*u))
    factor = duration*(u*u*u-2*u*u+u)
    xy = tuple(anchor[i] + factor*velocity[i] for i in range(2))
    minimum = (1-weight)*height
    if not all(math.isfinite(x) for x in (*xy, minimum)):
        raise ValueError('entry target overflow')
    return {'end_xy_target': list(xy), 'minimum_height_target_m': minimum,
            'weight': weight}
