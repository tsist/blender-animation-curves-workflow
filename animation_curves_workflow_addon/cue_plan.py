"""Bounded pure planner: explicit beat grid to exact native key times.
No bpy, audio decoding, IO, quantization or Action mutation.
"""
from fractions import Fraction
import math
import struct

def rational(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(label + ': integer or rational string required')
    if isinstance(value, str) and len(value) > 64:
        raise ValueError(label + ': token too long')
    try:
        result = Fraction(value)
    except (ValueError, ZeroDivisionError):
        raise ValueError(label + ': invalid rational') from None
    if abs(result.numerator) > 10**15 or result.denominator > 10**12:
        raise ValueError(label + ': magnitude exceeds planner limit')
    return result

def plan_cues(*, bpm, fps, start_frame, beats):
    tempo, rate, origin = (rational(v, k) for v, k in
                           [(bpm, 'bpm'), (fps, 'fps'), (start_frame, 'start_frame')])
    if not 1 <= tempo <= 1000 or not 1 <= rate <= 1000:
        raise ValueError('bpm and fps must be in [1, 1000]')
    if not isinstance(beats, list) or not 1 <= len(beats) <= 512:
        raise ValueError('beats must be a list of 1..512 explicit beat indices')
    indices = [rational(x, 'beat') for x in beats]
    if any(x < 0 for x in indices) or any(a >= b for a,b in zip(indices, indices[1:])):
        raise ValueError('beat indices must be nonnegative and strictly increasing')
    rows = []
    for index in indices:
        exact = origin + index * 60 * rate / tempo
        if abs(exact) > 1_000_000:
            raise ValueError('frame exceeds bounded feasibility range')
        native = struct.unpack('f', struct.pack('f', float(exact)))[0]
        assert math.isfinite(native)
        loss = Fraction(native) - exact
        rows.append({'beat': str(index), 'exact_frame': str(exact),
                     'native_candidate': native, 'error_frames': str(loss),
                     'error_seconds': str(loss/rate), 'exact_native': loss == 0})
    return {'schema': 'explicit-music-cue-feasibility-v1', 'bpm': str(tempo),
            'fps': str(rate), 'origin': str(origin), 'cues': rows,
            'all_exact_native': all(r['exact_native'] for r in rows),
            'candidate_collisions': [i for i in range(1, len(rows))
                                     if rows[i]['native_candidate'] == rows[i-1]['native_candidate']],
            'mutation_allowed': False,
            'scope': 'Timing feasibility only; no witness, binding, audio analysis or transaction.'}
