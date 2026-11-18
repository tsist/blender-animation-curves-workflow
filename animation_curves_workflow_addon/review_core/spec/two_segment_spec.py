"""Candidate specification adapter only; caller must install native constraints.

Legacy generator is injected, not modified. Existing v1 scenes stay unchanged.
The new lower-leg rest length invalidates prior foot-contact/IK qualifications.
"""
from copy import deepcopy
from .leg_contract import signed_flexion, flexion_channel

TEMPLATE_ID = 'humanoid_fk_two_segment_v2'


def build(legacy_spec_module, values=None):
    spec = deepcopy(legacy_spec_module.build('humanoid_fk_v1', values))
    h = spec['height_m']
    feet = {'foot.L','foot.R'}
    spec['bones'] = [b for b in spec['bones'] if b['name'] not in feet]
    spec['parts'] = [p for p in spec['parts'] if p['name'] not in feet]
    spec['references'] = {k:v for k,v in spec['references'].items() if v['bone'] not in feet}
    bones = {b['name']:b for b in spec['bones']}
    parts = {p['name']:p for p in spec['parts']}
    limits = {}
    for side in ('L','R'):
        thigh, shin = bones['thigh.'+side], bones['shin.'+side]
        # Two bones, with the actual shin tail at the new leg end on the floor.
        shin['tail'] = (shin['tail'][0], 0., 0.)
        verts, faces = legacy_spec_module.cylinder(shin['head'],shin['tail'],.035*h)
        # Flatten only the distal ring into a horizontal cap. Avoid the tilted
        # radius otherwise dipping below the ground even in the rest pose.
        verts = [v if i < len(verts)//2 else (v[0],v[1],0.) for i,v in enumerate(verts)]
        parts[shin['name']]['vertices'] = verts
        parts[shin['name']]['faces'] = faces
        spec['references']['leg_end.'+side] = {'bone':shin['name'], 'rest_point':shin['tail']}
        rest = signed_flexion(thigh['head'],shin['head'],shin['tail'],(1,0,0))
        bounds = flexion_channel(rest,rest)
        limits[shin['name']] = {
            'axis':'LOCAL_X', 'positive_hinge_axis_local':(1.,0.,0.),
            'flexion_min_deg':0., 'flexion_max_deg':150., 'rest_flexion_deg':rest,
            'local_x_min_deg':bounds['local_x_min_deg'],
            'local_x_max_deg':bounds['local_x_max_deg'],
            'local_y_min_deg':0.,'local_y_max_deg':0.,
            'local_z_min_deg':0.,'local_z_max_deg':0.,
            'native_requirement':'LIMIT_ROTATION LOCAL; all axes enabled; influence 1; use_transform_limit true; evaluated readback required',
        }
    spec['template_id'] = TEMPLATE_ID
    spec['generator_version'] = 'two-segment-design-0.1.0'
    spec['bone_map'] = {b['name']:b['name'] for b in spec['bones']}
    spec['capabilities']['foot_reference'] = False
    spec['capabilities']['leg_end_reference'] = True
    spec['joint_limits'] = limits
    spec['contact_semantics'] = 'Distal shin endpoint reference only; evaluated distal cap/whole-shin surface required for contact; no sole/heel/toe semantics.'
    return spec
