"""Check native driver ownership at the exact F-Curve address."""

from . import native


def require_undriven(owner, addresses):
    animation = owner.animation_data
    if animation is None:
        return
    occupied = {(curve.data_path, curve.array_index) for curve in animation.drivers}
    conflicts = occupied.intersection(addresses)
    if conflicts:
        native.fail('CONFLICT', 'A driver owns a requested animation channel', {
            'owner': owner.name,
            'channels': [{'path': path, 'index': index} for path, index in sorted(conflicts)],
        })
