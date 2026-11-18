"""Stable enum numeric identities for this loaded module lifetime; never reuse IDs."""
import json
class Registry:
 def __init__(self):self.ids={};self.next_id=1;self.history=[];self.items=[('NONE','请选择自有角色','必须明确选择',0)]
 def refresh(self,rows):
  values=[('NONE','请选择自有角色','必须明确选择',0)];seen=set()
  for row in rows:
   token=self.encode(row)
   if token in seen:raise ValueError('Duplicate explicit instance option')
   seen.add(token)
   if token not in self.ids:self.ids[token]=self.next_id;self.next_id+=1
   values.append((token,row['rig_name'],'默认姿态/无 Action，执行时完整核验',self.ids[token]))
  self.history.append(self.items);self.items=values;return values
 @staticmethod
 def encode(row):
  if type(row)is not dict or set(row)!={'rig_name','instance_uuid'}or not all(isinstance(x,str)and x for x in row.values()):raise ValueError('Explicit name/UUID required')
  return json.dumps(row,sort_keys=True)
 def freeze(self,token):
  if token=='NONE':return ''
  if token not in {x[0]for x in self.items}:raise ValueError('Selection no longer available')
  row=json.loads(token);return self.encode(row)
 def validate(self,token,frozen):
  if not frozen or token!=frozen:raise ValueError('Explicit target changed; reopen dialog')
  if token not in {x[0]for x in self.items}:raise ValueError('Target renamed/deleted or stale list; reopen dialog')
  row=json.loads(frozen)
  if self.encode(row)!=frozen or token not in self.ids:raise ValueError('Unknown explicit target; reopen dialog')
  return row
REGISTRY=Registry()
