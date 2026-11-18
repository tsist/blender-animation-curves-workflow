"""Bpy-free support policy for a qualified two-segment shin.

Pure target generation only: callers own scene qualification, mesh contact
solve, positive-branch knee validation, exact key whitelist and transaction.
No implicit frame schedule, rig names, file paths or scene mutations.
"""
from .entry_target import build_entry_target, _number, _xy
import math


def build_support_target(*, anchor_xy, incoming_velocity_m_per_frame,
                         initial_min_z, source_end_xy, source_min_z, time,
                         entry_start, plant_start, plant_end, exit_end):
    start, planted, release, end = [_number(v, n) for v, n in zip(
        (entry_start, plant_start, plant_end, exit_end),
        ('entry_start', 'plant_start', 'plant_end', 'exit_end'))]
    if not start < planted <= release < end:
        raise ValueError('expected entry_start < plant_start <= plant_end < exit_end')
    if not all(math.isfinite(b-a) for a,b in [(start,planted),(release,end),(start,end)]):
        raise ValueError('interval duration overflow')
    t = _number(time, 'time')
    if not start <= t <= end:
        raise ValueError('time outside support interval')
    anchor = _xy(anchor_xy, 'anchor_xy')
    velocity = _xy(incoming_velocity_m_per_frame, 'incoming_velocity_m_per_frame')
    height = _number(initial_min_z, 'initial_min_z')
    source = _xy(source_end_xy, 'source_end_xy')
    source_height = _number(source_min_z, 'source_min_z')
    if start < t < planted:
        result = build_entry_target(anchor_xy=anchor,
            incoming_velocity_m_per_frame=velocity, initial_min_z=height,
            time=t, entry_start=start, entry_end=planted)
        return dict(result, phase='entry', preserve_source_key=False)
    if t in (start,end):
        return dict(end_xy_target=list(source), minimum_height_target_m=source_height,
                    weight=0., phase='endpoint', preserve_source_key=True)
    if planted <= t <= release:
        w=1.;phase='plant'
    else:
        u=(end-t)/(end-release);w=u*u*u*(10+u*(-15+6*u));phase='exit'
    xy=[(1-w)*source[i]+w*anchor[i] for i in range(2)]
    z=(1-w)*source_height
    if not all(math.isfinite(v) for v in (*xy,z)):
        raise ValueError('support target overflow')
    return dict(end_xy_target=xy, minimum_height_target_m=z, weight=w,
                phase=phase,preserve_source_key=False)
