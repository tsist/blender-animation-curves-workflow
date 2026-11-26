"""Animation Curves Workflow with general events and two-segment native-FK template candidate."""
bl_info = {'name': 'Animation Curves Workflow', 'author': 'Blender AI',
           'version': (0, 5, 0), 'blender': (5, 3, 0),
           'location': 'View3D > Sidebar > Animation Curves',
           'category': 'Animation', 'description': 'Three-domain event editing, clip-local retime and footless two-segment FK templates (development)'}
VERSION = '0.5.0-animation-production'
_REGISTERED=[]

def register():
    from . import ui, templates_ui, support_ui, practice_ui, knee_ui, pose_ui, support_diagnostic_ui, support_solver_ui, pose_event_ui, pose_event_store_ui, cross_event_ui, cue_ui, clip_event_ui, clip_asset_ui, clip_project_ui, recording_ui, production_ui
    if _REGISTERED:raise RuntimeError('Animation Curves Workflow already registered')
    # Reject prior ownership before acquiring anything. A child's already-
    # registered error is not evidence that this invocation owns its classes.
    modules=(ui,templates_ui,support_ui,practice_ui,knee_ui,pose_ui,support_diagnostic_ui,support_solver_ui,pose_event_ui,pose_event_store_ui,cross_event_ui,cue_ui,clip_event_ui,clip_asset_ui,clip_project_ui,recording_ui,production_ui)
    for module in modules:
        if (getattr(module, '_REGISTERED_CLASSES', ())
                or getattr(module, '_REGISTERED', ())):
            raise RuntimeError('A workflow component is already registered; refusing to change it')
    try:
        for module in modules:
            try:
                module.register()
            except Exception:
                # Retain exact module ownership if its own rollback failed.
                if getattr(module, '_REGISTERED_CLASSES', ()) or getattr(module, '_REGISTERED', ()):
                    _REGISTERED.append(module)
                raise
            _REGISTERED.append(module)
    except Exception:
        unregister();raise

def unregister():
    while _REGISTERED:
        _REGISTERED[-1].unregister();_REGISTERED.pop()
