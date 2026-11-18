"""Private worker entry. Never freeze, save, create, apply or render."""
import sys,json,importlib.util,importlib
from pathlib import Path
sys.dont_write_bytecode=True
root=Path(__file__).resolve().parent
sp=importlib.util.spec_from_file_location('_readonly_worker_package',root/'__init__.py',submodule_search_locations=[str(root)]);m=importlib.util.module_from_spec(sp);sys.modules[sp.name]=m;sp.loader.exec_module(m)
h=importlib.import_module(sp.name+'.read_lifecycle');service=importlib.import_module(sp.name+'.review_core.service')
def execute(c):
 import bpy
 h.need(all(h.checks(c['pins']).values()),'Exact source/code/runtime pins')
 h.need(bpy.app.binary_path==c['binary'] and bpy.data.filepath==c.get('worker_source',c['source']),'Runtime/source identity')
 h.need(h.sha(c['request'])==c['request_bytes'],'Frozen request bytes changed')
 request=json.loads(Path(c['request']).read_text())
 if request['kind']=='BILATERAL_CAP_WORKFLOW':
  ss=importlib.import_module(sp.name+'.support_solver');report=ss.review(bpy,request,c['digest'],Path(c['output']),c['worker_source'])
 elif request['kind']=='FLAT_CAP_SUPPORT_DIAGNOSTIC':
  ds=importlib.import_module(sp.name+'.support_diagnostic_service');report=ds.review(bpy,request,c['digest'],Path(c['output']),c['worker_source'])
 else:report=service.review(bpy,request,c['digest'],Path(c['output']))
 h.need(all(h.checks(c['pins']).values()),'Post-review pins')
 return report
if __name__=='__main__':
 args=sys.argv[sys.argv.index('--')+1:];h.need(len(args)==2,'Exact arguments');cp=h.safe(args[0]);h.need(h.sha(cp)==args[1],'Config integrity');c=json.loads(cp.read_text());receipt={'success':False,'errors':[]}
 try:
  report=execute(c);receipt['result_digest']=service.digest(report);receipt['success']=True
 except BaseException as e:receipt['errors'].append(repr(e));raise
 finally:h.write(cp.parent/'WORKER-EXIT.json',receipt)
