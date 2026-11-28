"""Run one frozen support request or independently reopen its private candidate."""

import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys


sys.dont_write_bytecode = True
root = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('_readonly_worker_package', root / '__init__.py', submodule_search_locations=[str(root)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
h = importlib.import_module(spec.name + '.read_lifecycle')
service = importlib.import_module(spec.name + '.review_core.service')


def load_request(bpy, config):
    h.need(bpy.app.background and not bpy.app.online_access, 'Expected an offline background worker')
    h.need(all(h.checks(config['pins']).values()), 'Exact source/code/runtime pins')
    h.need(h.safe(bpy.app.binary_path) == h.safe(config['binary']), 'Runtime binary path identity')
    windows_runtime = config.get('windows_runtime')
    if windows_runtime:
        windows = importlib.import_module(spec.name + '.windows_owned')
        h.need(windows.runtime_identity(bpy) == windows_runtime, 'Windows worker runtime differs from owner')
    h.need(h.sha(config['request']) == config['request_bytes'], 'Frozen request bytes changed')
    return json.loads(Path(config['request']).read_text(encoding='utf-8'))


def author(bpy, config, request):
    h.need(h.safe(bpy.data.filepath) == h.safe(config.get('worker_source', config['source'])), 'Worker source identity')
    if request['kind'] == 'BILATERAL_CAP_WORKFLOW':
        solver = importlib.import_module(spec.name + '.support_solver')
        return solver.review(bpy, request, config['digest'], Path(config['output']), config['worker_source'],
                             independent_witness=bool(config.get('windows_runtime')))
    if request['kind'] == 'FLAT_CAP_SUPPORT_DIAGNOSTIC':
        diagnostic = importlib.import_module(spec.name + '.support_diagnostic_service')
        return diagnostic.review(bpy, request, config['digest'], Path(config['output']), config['worker_source'])
    return service.review(bpy, request, config['digest'], Path(config['output']))


def cold(bpy, config, request, author_digest):
    h.need(config.get('windows_runtime') and request['kind'] == 'BILATERAL_CAP_WORKFLOW'
           and request['mode'] == 'GENERATE', 'Independent verifier needs a Windows solver candidate')
    output = h.safe(config['output'])
    result = json.loads((output / 'RESULT.json').read_text(encoding='utf-8'))
    h.need(service.digest(result) == author_digest and result['status'] == 'SAMPLED_CANDIDATE_READY', 'Cold author result binding')
    solver = importlib.import_module(spec.name + '.support_solver')
    solver.validate_result(result, request, config['digest'], output)
    return solver.verify_independent(bpy, request, result)


def main(args):
    import bpy

    h.need(len(args) == 2 or (len(args) == 4 and args[2] == 'cold'), 'Exact worker arguments required')
    config_path = h.safe(args[0])
    h.need(h.sha(config_path) == args[1], 'Config integrity')
    config = json.loads(config_path.read_text(encoding='utf-8'))
    independent = len(args) == 4
    receipt = {'success': False, 'errors': [], 'pid': os.getpid(), 'request_digest': config['digest'],
               'runtime': config.get('windows_runtime')}
    try:
        request = load_request(bpy, config)
        if independent:
            receipt['result'] = cold(bpy, config, request, args[3])
            receipt['author_digest'] = args[3]
        else:
            report = author(bpy, config, request)
            receipt['result_digest'] = service.digest(report)
        h.need(all(h.checks(config['pins']).values()), 'Post-review pins')
        receipt['success'] = True
    except BaseException as error:
        receipt['errors'].append(repr(error))
        raise
    finally:
        h.write(config_path.parent / ('COLD-EXIT.json' if independent else 'WORKER-EXIT.json'), receipt)


if __name__ == '__main__':
    main(sys.argv[sys.argv.index('--') + 1:])
