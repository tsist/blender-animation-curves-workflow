"""Small, deterministic time-only planner; no Blender or external dependencies.

Constraints are ``after.frame >= before.frame + before.duration + min_gap``.
The requested event and already locked events are fixed. DAG passes propagate
their feasible bounds; other events stay at their old times where possible.
For an initially feasible schedule this moves only necessary predecessors or
successors. An inconsistent initial schedule may also require repairs. This is
not a global displacement optimizer, spatial/contact solver, or curve editor.
Frames may be fractional. No rounding or implicit numerical tolerance is used.
"""

from collections import deque
from collections.abc import Mapping
from copy import deepcopy
import math


class TimelineError(ValueError):
    """A rejected request or infeasible set of time constraints."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = {} if details is None else details


def _invalid(message, **details):
    raise TimelineError("INVALID_REQUEST", message, details)


def _number(value, field, **details):
    # JSON numbers only; bool is an int subclass but is never a frame value.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid("Expected a finite number", field=field, **details)
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        _invalid("Expected a finite number", field=field, **details)
    return value


def _arithmetic(value, **details):
    return _number(value, "computed_value", **details)


def _time_arithmetic(operation, calculation):
    try:
        value = calculation()
    except OverflowError:
        _invalid("Time arithmetic exceeds the supported numeric range", operation=operation)
    return _arithmetic(value, operation=operation)


def _identifier(value, field, **details):
    if not isinstance(value, str) or not value:
        _invalid("Expected a nonempty string ID", field=field, **details)
    return value


def _time_parameters(fps, fps_base, start_frame):
    fps = _number(fps, "fps")
    fps_base = _number(fps_base, "fps_base")
    start_frame = _number(start_frame, "start_frame")
    if fps <= 0 or fps_base <= 0:
        _invalid("fps and fps_base must be positive")
    rate = _time_arithmetic("frame_rate", lambda: fps / fps_base)
    if rate <= 0:
        _invalid("Effective frame rate is outside the supported numeric range")
    return rate, start_frame


def seconds_to_frame(seconds, fps, fps_base=1, start_frame=1):
    """Map seconds relative to an explicit scene start to a fractional frame."""
    seconds = _number(seconds, "seconds")
    rate, origin = _time_parameters(fps, fps_base, start_frame)
    return _time_arithmetic("seconds_to_frame", lambda: origin + seconds * rate)


def frame_to_seconds(frame, fps, fps_base=1, start_frame=1):
    """Inverse of seconds_to_frame; never quantize to an integer frame."""
    frame = _number(frame, "frame")
    rate, origin = _time_parameters(fps, fps_base, start_frame)
    return _time_arithmetic("frame_to_seconds", lambda: (frame - origin) / rate)


def solve_event_shift(events, dependencies, event_id, target_frame):
    """Return a copied schedule and changed start frames, or raise TimelineError.

    Events require string ``id`` and finite ``frame``. Optional ``duration`` and
    dependency ``min_gap`` default to zero and must be nonnegative. ``locked``
    defaults to False and must be bool. Optional min/max_frame bound start times,
    not event ends. Extra event metadata and input order are preserved.

    The explicit target is exact and locked peers retain their original frames.
    Unlocked nodes use the closest old value within propagated bounds; a forward
    pass resolves any pre-existing dependency violations. This deterministic
    policy is linear in the number of nodes and edges, not an optimization API.
    Neither successful planning nor rejection mutates caller-owned inputs.
    """
    if not isinstance(events, (list, tuple)):
        _invalid("events must be a list or tuple")
    if not isinstance(dependencies, (list, tuple)):
        _invalid("dependencies must be a list or tuple")
    event_id = _identifier(event_id, "event_id")
    target_frame = _number(target_frame, "target_frame")

    by_id = {}
    order = []
    frames, durations, lower, upper, locked = {}, {}, {}, {}, {}
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            _invalid("Each event must be a mapping", event_index=index)
        name = _identifier(event.get("id"), "id", event_index=index)
        if name in by_id:
            _invalid("Duplicate event ID", event_id=name)
        frame = _number(event.get("frame"), "frame", event_id=name)
        duration = _number(event.get("duration", 0), "duration", event_id=name)
        if duration < 0:
            _invalid("duration must be nonnegative", event_id=name)
        is_locked = event.get("locked", False)
        if not isinstance(is_locked, bool):
            _invalid("locked must be a bool", event_id=name)
        lo = (_number(event["min_frame"], "min_frame", event_id=name)
              if "min_frame" in event else -math.inf)
        hi = (_number(event["max_frame"], "max_frame", event_id=name)
              if "max_frame" in event else math.inf)
        if lo > hi:
            _invalid("min_frame exceeds max_frame", event_id=name)
        by_id[name] = event
        order.append(name)
        frames[name], durations[name], locked[name] = frame, duration, is_locked
        lower[name], upper[name] = lo, hi

    if event_id not in by_id:
        _invalid("Unknown target event ID", event_id=event_id)

    incoming = {name: [] for name in order}
    outgoing = {name: [] for name in order}
    indegree = {name: 0 for name in order}
    for index, dependency in enumerate(dependencies):
        if not isinstance(dependency, Mapping):
            _invalid("Each dependency must be a mapping", dependency_index=index)
        before = _identifier(dependency.get("before"), "before", dependency_index=index)
        after = _identifier(dependency.get("after"), "after", dependency_index=index)
        if before not in by_id or after not in by_id:
            _invalid("Dependency references an unknown event ID", dependency_index=index,
                     before=before, after=after)
        gap = _number(dependency.get("min_gap", 0), "min_gap", dependency_index=index)
        if gap < 0:
            _invalid("min_gap must be nonnegative", dependency_index=index)
        distance = _arithmetic(durations[before] + gap, dependency_index=index)
        outgoing[before].append((after, distance))
        incoming[after].append((before, distance))
        indegree[after] += 1

    ready = deque(name for name in order if indegree[name] == 0)
    topology = []
    while ready:
        name = ready.popleft()
        topology.append(name)
        for after, _ in outgoing[name]:
            indegree[after] -= 1
            if indegree[after] == 0:
                ready.append(after)
    if len(topology) != len(order):
        _invalid("Dependencies must form a DAG",
                 unresolved_ids=[name for name in order if indegree[name] > 0])

    for name in order:
        if name == event_id or locked[name]:
            fixed = target_frame if name == event_id else frames[name]
            if name == event_id and locked[name] and target_frame != frames[name]:
                raise TimelineError("CONFLICT", "The target event is locked",
                                    {"event_id": name, "frame": frames[name],
                                     "target_frame": target_frame})
            if not lower[name] <= fixed <= upper[name]:
                raise TimelineError("CONFLICT", "A fixed event is outside its bounds",
                                    {"event_id": name, "frame": fixed})
            lower[name] = upper[name] = fixed

    def check_bounds(name):
        if lower[name] > upper[name]:
            raise TimelineError("CONFLICT", "Event time constraints are infeasible",
                                {"event_id": name, "min_frame": lower[name],
                                 "max_frame": upper[name]})

    for name in topology:
        check_bounds(name)
        if lower[name] != -math.inf:
            for after, distance in outgoing[name]:
                candidate = _arithmetic(lower[name] + distance, before=name, after=after)
                lower[after] = max(lower[after], candidate)

    for name in reversed(topology):
        check_bounds(name)
        if upper[name] != math.inf:
            for before, distance in incoming[name]:
                candidate = _arithmetic(upper[name] - distance, before=before, after=name)
                upper[before] = min(upper[before], candidate)

    planned = {}
    for name in topology:
        check_bounds(name)
        desired = min(max(frames[name], lower[name]), upper[name])
        for before, distance in incoming[name]:
            required = _arithmetic(planned[before] + distance, before=before, after=name)
            desired = max(desired, required)
        if desired > upper[name]:
            raise TimelineError("CONFLICT", "Event time constraints are infeasible",
                                {"event_id": name, "required_frame": desired,
                                 "max_frame": upper[name]})
        planned[name] = desired

    result = deepcopy([dict(event) for event in events])
    changes = []
    for event in result:
        name = event["id"]
        if planned[name] != frames[name]:
            event["frame"] = planned[name]
            changes.append({"id": name, "before": frames[name], "after": planned[name]})
    return {"events": result, "changes": changes}
