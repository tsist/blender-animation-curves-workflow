from . import legacy_spec
from .two_segment_spec import build as build_two, TEMPLATE_ID
BODY=legacy_spec.BODY
CHEST=legacy_spec.CHEST
FACE=legacy_spec.FACE
in_scene_units=legacy_spec.in_scene_units
def build(kind,values=None):
 if kind == TEMPLATE_ID:
  return build_two(legacy_spec,values)
 if kind == 'cylinder_v1':
  spec = legacy_spec.build(kind,values)
  spec['joint_limits'] = {}
  spec['contact_semantics'] = 'Cylinder without legs; no knee, leg-end or foot-contact qualification.'
  return spec
 raise ValueError('Only cylinder_v1 or humanoid_fk_two_segment_v2 supported; no legacy migration')
