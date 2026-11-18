"""Strict Blender 5 system-RNA cue read; custom IDProperties never mask it."""
KEY='acw_experimental_cue'
def read(c):
 scene=c.scene
 if not hasattr(type(scene),KEY):raise ValueError('Cue RNA未注册，请启用插件后再检查或修改')
 value=getattr(scene,KEY)
 if type(value)is not str:raise ValueError('Cue系统数据类型损坏，拒绝默认值掩盖或覆盖')
 if KEY in scene:
  shadow=scene[KEY]
  if type(shadow)is not str or shadow!=value:raise ValueError('Cue用户自定义属性与系统RNA冲突，拒绝掩盖或迁移')
 return value
def require_no_shadow(c):
 if KEY in c.scene:raise ValueError('存在同名Cue用户自定义属性，禁止写入；不会自动删除或迁移')
