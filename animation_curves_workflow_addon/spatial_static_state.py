"""Bounded static-state witnesses. No mutation, evaluation, save or consent.

Only an explicitly supported RNA surface is certified. This is not generic
serialization or full-scene equivalence. Unsupported nested data fails closed.
"""
import math
from .contract import WorkflowError

MAX_FIELDS=8192
MAX_ARRAY=256

def reject(message):raise WorkflowError('UNSUPPORTED',message)


def value(v):
    if v is None or type(v) in (bool,str,int):return v
    if type(v)is float:
        if not math.isfinite(v):reject('Static witness contains nonfinite data')
        return v
    if type(v).__module__=='mathutils':return value(list(v))
    if isinstance(v,set):return sorted(value(x) for x in v)
    if isinstance(v,(list,tuple)):
        if len(v)>MAX_ARRAY:reject('Static witness array exceeds budget')
        return [value(x) for x in v]
    reject('Unsupported static witness value')


def array_value(raw,prop):
    declared=tuple(prop.array_dimensions)
    if any(type(d)is not int or d<0 for d in declared):reject('Invalid native RNA array dimensions')
    zero=declared.index(0) if 0 in declared else len(declared)
    if any(declared[zero:]):reject('Native RNA array zero padding is not trailing')
    dimensions=declared[:zero]
    if not dimensions or len(dimensions)>4 or any(type(d)is not int or d<=0 for d in dimensions):
        reject('Invalid native RNA array dimensions')
    size=math.prod(dimensions)
    if size!=prop.array_length or size>MAX_ARRAY:reject('Native RNA array shape/budget mismatch')
    def read(item,axes):
        if axes:
            if len(item)!=axes[0]:reject('Native RNA array dimension mismatch')
            return [read(item[i],axes[1:]) for i in range(axes[0])]
        if prop.type=='BOOLEAN' and type(item)is bool:return item
        if prop.type=='INT' and type(item)is int:return item
        if prop.type=='FLOAT' and type(item) in (int,float) and math.isfinite(item):return float(item)
        reject('Unsupported native RNA array leaf')
    return read(raw,dimensions)


def scalar_state(owner,budget,exclude=()):
    """All editable primitive RNA fields; nested pointers handled separately.

    Readonly properties are derived/runtime state and not called editable state.
    Required derived matrices and ID identity are captured explicitly elsewhere.
    """
    result={}
    for prop in owner.bl_rna.properties:
        if prop.identifier=='rna_type' or prop.is_readonly or prop.identifier in exclude:continue
        if prop.type not in {'BOOLEAN','INT','FLOAT','STRING','ENUM'}:continue
        budget[0]+=1
        if budget[0]>MAX_FIELDS:reject('Static witness field budget exceeded')
        raw=getattr(owner,prop.identifier)
        try:
            result[prop.identifier]=array_value(raw,prop) if getattr(prop,'is_array',False) else value(raw)
        except (WorkflowError,TypeError,ValueError,OverflowError) as error:
            raise WorkflowError('UNSUPPORTED','Unsupported static RNA field '+owner.bl_rna.identifier+'.'+prop.identifier,{'value_type':type(raw).__name__,'array_dimensions':list(prop.array_dimensions) if getattr(prop,'is_array',False) else [],'reason':str(error)}) from error
    return result


# Explicit pointer/collection registry. Unknown populated nested state rejects.
# preview/asset_data are NOT both cache: asset_data remains unsupported.
RUNTIME_NESTED={'rna_type','id_data','original','preview'}
COMMON_NESTED={'animation_data','library','override_library','library_weak_reference'}
SUPPORTED_NESTED={
    'Object':{'data','parent','instance_collection','constraints','modifiers','rigid_body','rigid_body_constraint','material_slots','users_collection','children','field'},
    'Camera':{'dof','stereo','background_images'},
    'Light':{'node_tree'}, 'PointLight':{'node_tree'}, 'SunLight':{'node_tree'},
    'SpotLight':{'node_tree'}, 'AreaLight':{'node_tree'},
    'World':{'node_tree','mist_settings'}, 'Material':{'node_tree'},
    'Collection':{'objects','children','all_objects'},
    'CameraDOFSettings':{'focus_object'}, 'CameraStereoData':set(),
    'WorldMistSettings':set(), 'FieldSettings':set(),
}

# Actual Blender5.2 native diagnostic established these persistent structures.
NESTED_FIELDS={
 'Object':('display','lineart','light_linking','animation_visualization','cycles'),
 'Camera':('cycles_custom',), 'AreaLight':('cycles',), 'PointLight':('cycles',),
 'SunLight':('cycles',), 'SpotLight':('cycles',),
 'World':('light_settings','cycles','cycles_visibility'),
 'Material':('lineart','cycles'), 'AnimViz':('motion_path',),
 'CollectionObject':('light_linking',),'CollectionChild':('light_linking',),
}
for name in ('ObjectDisplay','ObjectLineArt','ObjectLightLinking','AnimViz','AnimVizMotionPaths','CyclesObjectSettings','CyclesCustomCameraSettings','CyclesLightSettings','WorldLighting','CyclesWorldSettings','CyclesVisibilitySettings','MaterialLineArt','CyclesMaterialSettings','CollectionObject','CollectionChild','CollectionLightLinking'):
    SUPPORTED_NESTED.setdefault(name,set())
for name,fields in NESTED_FIELDS.items():SUPPORTED_NESTED.setdefault(name,set()).update(fields)
SUPPORTED_NESTED['Object'].add('active_material')
SUPPORTED_NESTED['Collection'].update({'collection_objects','collection_children'})

NO_ID_PROPERTIES={'CollectionObject','CollectionLightLinking','CollectionChild','ObjectDisplay','ObjectLineArt','ObjectLightLinking','AnimViz','AnimVizMotionPaths','CameraStereoData','CameraDOFSettings','WorldLighting','WorldMistSettings','MaterialLineArt','NodeSocketColor','NodeSocketFloat','NodeSocketShader','NodeLink','NodeTreeInterface','NodeSocketFloatFactor','NodeSocketBool','NodeSocketVector','NodeSocketFloatDistance','NodeSocketFloatWavelength','NodePanelState'}

def custom_state(owner):
    from . import native
    try:return native.custom_state(owner)
    except TypeError as error:
        if owner.bl_rna.identifier in NO_ID_PROPERTIES and str(error)=="bpy_struct.keys(): this type doesn't support IDProperties":
            return {'id_properties_supported':False}
        reject('Unsupported custom-property access for '+owner.bl_rna.identifier)


def nested_state(owner,budget,depth=0):
    if depth>4:reject('Static nested state exceeds depth budget')
    nested_gate(owner)
    from . import native
    result={'rna_type':owner.bl_rna.identifier,'scalars':scalar_state(owner,budget),'custom':custom_state(owner)}
    for key in NESTED_FIELDS.get(owner.bl_rna.identifier,()):
        item=getattr(owner,key)
        result[key]=nested_state(item,budget,depth+1) if item is not None else None
    return result


def nested_gate(owner):
    allowed=SUPPORTED_NESTED.get(owner.bl_rna.identifier)
    if allowed is None:reject('Unsupported static RNA type: '+owner.bl_rna.identifier)
    for prop in owner.bl_rna.properties:
        if prop.type not in {'POINTER','COLLECTION'}:continue
        key=prop.identifier
        if key in RUNTIME_NESTED or key in COMMON_NESTED or key in allowed:continue
        raw=getattr(owner,key)
        populated=raw is not None if prop.type=='POINTER' else len(raw)>0
        if populated:reject('Unsupported populated static nested field: '+owner.bl_rna.identifier+'.'+key)


def no_animation(owner):
    if getattr(owner,'animation_data',None) is not None:
        reject('Static nonparticipant datablock has animation data')


def id_state(owner,budget):
    from . import native
    native.local(owner);no_animation(owner);nested_gate(owner)
    return {'name':owner.name,'rna_type':owner.bl_rna.identifier,
            'editable_scalars':scalar_state(owner,budget),'custom':custom_state(owner),
            'nested':{key:nested_state(getattr(owner,key),budget) if getattr(owner,key) is not None else None for key in NESTED_FIELDS.get(owner.bl_rna.identifier,())}}


def nonnode_surface(owner,budget):
    state=id_state(owner,budget)
    from . import spatial_static_shader
    state['node_tree']=spatial_static_shader.capture(owner.node_tree,budget,owner) if owner.node_tree is not None else None
    # Legacy texture slots are dependencies, not merely display settings.
    slots=getattr(owner,'texture_slots',None)
    if slots and any(slot is not None for slot in slots):reject('Texture-slot dependency is unsupported')
    if owner.bl_rna.identifier=='World':
        nested_gate(owner.mist_settings)
        state['mist_settings']=scalar_state(owner.mist_settings,budget)
    return state


def object_dependency_gate(obj):
    if obj.parent or obj.constraints or obj.modifiers or obj.instance_type!='NONE' or obj.rigid_body or obj.rigid_body_constraint:
        reject('Static nonparticipant dependency is unsupported')
    if obj.instance_collection is not None:reject('Collection instance dependency is unsupported')


def field_witness(obj,budget):
    field=obj.field
    if field is None:return None
    if field.type!='NONE':reject('Force-field dependency is unsupported')
    return nested_state(field,budget)


def collection_rows(items,direct,budget):
    if len(items)!=len(direct):reject('Collection-row count differs from direct links')
    states=[nested_state(item,budget) for item in items]
    for state_row in states:
        if state_row['scalars'] or state_row['light_linking']['scalars']!={'link_state':'INCLUDE'}:
            reject('Nondefault collection linking requires unsupported named association')
    if states and any(v!=states[0] for v in states):reject('Nonuniform collection link state is unsupported')
    return {'policy':'UNIFORM_DEFAULT_INCLUDE_ONLY','named_links':sorted(x.name for x in direct),'count':len(items),'uniform_state':states[0] if states else None}


def material_witnesses(obj,budget):
    # Effective OBJECT-linked slots do not hide underlying mesh-data bindings.
    refs=[slot.material for slot in obj.material_slots]
    if obj.type=='MESH':refs.extend(obj.data.materials)
    return {material.name:nonnode_surface(material,budget) for material in refs if material is not None}


def capture_for_scope(scene,names=None):
    return capture(scene) if names is None else capture(scene,animated_camera_names=names)

def capture(scene,*,animated_camera_names=None):
    """Capture supported static dependencies alongside unchanged mesh inventory."""
    from . import native
    budget=[0];objects={};materials={};data={};mesh_objects={}
    approved=set()
    if animated_camera_names is not None:
        if type(animated_camera_names)is not list or not 1<=len(animated_camera_names)<=8 or any(type(n)is not str for n in animated_camera_names) or len(set(animated_camera_names))!=len(animated_camera_names):reject('Explicit unique animated camera names required')
        approved=set(animated_camera_names)
        actual={o.name for o in scene.objects if o.type=='CAMERA' and o.animation_data is not None}
        if approved!=actual:reject('Explicit camera scope must equal complete animated camera inventory')
    if scene.camera is not None and (scene.camera.type!='CAMERA' or scene.camera not in list(scene.objects)):reject('Scene camera is not a supported in-scene camera')
    collections={}
    def visit(collection):
        if collection.name in collections:return
        if len(collections)>=128:reject('Collection witness budget exceeded')
        row=id_state(collection,budget)
        row['objects']=sorted(o.name for o in collection.objects)
        row['children']=sorted(c.name for c in collection.children)
        for key in ('collection_objects','collection_children'):
            items=getattr(collection,key)
            direct=collection.objects if key=='collection_objects' else collection.children
            row[key]=collection_rows(items,direct,budget)
        collections[collection.name]=row
        for child in collection.children:visit(child)
    visit(scene.collection)
    for obj in sorted(scene.objects,key=lambda x:x.name):
        native.local(obj)
        materials.update(material_witnesses(obj,budget))
        object_dependency_gate(obj)
        field_state=field_witness(obj,budget)
        if obj.type=='MESH':
            nested_gate(obj)
            scalars=scalar_state(obj,budget,exclude=('location','matrix_world','matrix_basis','matrix_local'))
            mesh_objects[obj.name]={'field':field_state,'scalars':scalars,'custom':custom_state(obj),'nested':{key:nested_state(getattr(obj,key),budget) if getattr(obj,key) is not None else None for key in NESTED_FIELDS['Object']}}
            continue
        if obj.type not in {'CAMERA','LIGHT','EMPTY'}:reject('Unsupported nonparticipant object type')
        if obj.name in approved:
            from . import spatial_animated_camera
            frame=float(scene.frame_current)+float(scene.frame_subframe)
            witness=spatial_animated_camera.capture(obj,frame,budget)
            objects[obj.name]={'animated_camera_witness':witness}
            data['CAMERA:'+obj.data.name]=witness['data']
            continue
        row=id_state(obj,budget)
        if obj.children:reject('Static nonparticipant child dependency is unsupported')
        row['field']=field_state
        row['collections']=sorted(c.name for c in obj.users_collection)
        row['matrix_world']=value([list(r) for r in obj.matrix_world])
        row['matrix_basis']=value([list(r) for r in obj.matrix_basis])
        row['data']=obj.data.name if obj.data else None
        if obj.data:
            if obj.type=='EMPTY':reject('Empty display-data dependency is unsupported')
            block=id_state(obj.data,budget)
            if obj.type=='LIGHT':
                from . import spatial_static_shader
                block['node_tree']=spatial_static_shader.capture(obj.data.node_tree,budget,obj.data) if obj.data.node_tree is not None else None
            if obj.type=='CAMERA':
                if obj.data.dof.focus_object is not None:reject('Camera focus-object dependency is unsupported')
                nested_gate(obj.data.dof);nested_gate(obj.data.stereo)
                block['dof']=scalar_state(obj.data.dof,budget)
                block['stereo']=scalar_state(obj.data.stereo,budget)
                if obj.data.background_images:reject('Camera background-image dependency is unsupported')
            data[obj.type+':'+obj.data.name]=block
        objects[obj.name]=row
    result={'schema':'STATIC_SCENE_WITNESS_V1','collections':collections,'mesh_object_static_state':mesh_objects,'objects':objects,'data':data,'materials':materials,
            'world':nonnode_surface(scene.world,budget) if scene.world else None,
            'scene_camera':scene.camera.name if scene.camera else None,
            'scene_object_bindings':{o.name:{'type':o.type,'data':o.data.name if o.data else None,'collections':sorted(c.name for c in o.users_collection)} for o in scene.objects},
            'mesh_data_materials':{o.data.name:[m.name if m else None for m in o.data.materials] for o in scene.objects if o.type=='MESH'},
            'mesh_material_indices':{o.name:[p.material_index for p in o.data.polygons] for o in scene.objects if o.type=='MESH'},
            'material_slots':{o.name:[{'link':s.link,'material':s.material.name if s.material else None} for s in o.material_slots] for o in sorted(scene.objects,key=lambda x:x.name)},
            'scope':'editable primitive RNA fields, explicit camera nested settings, transforms, custom properties and supported ID bindings; not full scene equivalence'}
    if animated_camera_names is not None:
        if len(scene.timeline_markers)>2048:reject('Camera marker budget exceeded')
        if any(m.camera is not None and (m.camera.type!='CAMERA' or m.camera not in list(scene.objects)) for m in scene.timeline_markers):reject('Marker camera outside captured scene')
        result['schema']='SCENE_WITH_ANIMATED_CAMERA_WITNESS_V2'
        result['preserved_animated_cameras']=sorted(approved)
        from . import spatial_animated_camera
        result['camera_markers']=[{'metadata':spatial_animated_camera.metadata(m,budget,handled=('camera',)),'camera':m.camera.name if m.camera else None} for m in scene.timeline_markers]
    return result


def compare(expected,actual):
    if actual!=expected:raise WorkflowError('CONFLICT','Protected static scene witness changed')


def compare_persisted_camera_scope(expected,actual):
    """Cold-only comparison; raw in-process compare remains exact."""
    import copy
    from . import spatial_animated_camera
    def project(w):
        if w.get('schema')!='SCENE_WITH_ANIMATED_CAMERA_WITNESS_V2':reject('Expected explicit animated-camera scope')
        names=w.get('preserved_animated_cameras')
        if type(names)is not list or not names or len(set(names))!=len(names):reject('Missing camera scope identity')
        result=copy.deepcopy(w)
        for name in names:
            try:camera=result['objects'][name]['animated_camera_witness']
            except (KeyError,TypeError):reject('Camera witness missing from protected scope')
            if camera['object']['name']!=name:reject('Camera scope name linkage changed')
            result['objects'][name]['animated_camera_witness']=spatial_animated_camera.persisted_projection(camera)
        return result
    if project(expected)!=project(actual):raise WorkflowError('CONFLICT','Cold scene preservation changed beyond explicit camera Action session UID')
