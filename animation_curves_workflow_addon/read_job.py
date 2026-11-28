"""Single owner for the native sampled support workflow and its private workers."""

import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
import uuid

from . import native_slot, read_lifecycle as h
from .review_core import service


ACTIVE = ('RUNNING', 'CLEANUP_PENDING')
RUNTIME = 'c2fd82553c979a7f6ba85202c487aa1173c90db588a67d74d70cc7b0c2bea01c'


def selected(bpy):
    arm = bpy.context.active_object
    h.need(arm is not None and list(bpy.context.selected_objects) == [arm], 'Select exactly one owned template armature')
    return arm


def qualify(bpy):
    binary = h.safe(bpy.app.binary_path)
    if platform.system() == 'Windows':
        from .windows_owned import runtime_identity

        runtime = runtime_identity(bpy)
        return binary, runtime['sha256'], runtime
    h.need(platform.system() == 'Linux' and platform.machine() == 'x86_64', 'Only qualified Linux x86_64 or Windows x86_64 runtime')
    h.need(h.sha(binary) == RUNTIME, 'Unqualified Blender runtime')
    return binary, RUNTIME, None


class Job:
    def __init__(self):
        self.terminal_intent = None
        self.terminal_errors = []
        self.started = None
        self.slot_token = None
        self.state = 'EMPTY'
        self.process = None
        self.log = None
        self.session = None
        self.result = None
        self.error = None
        self.waited = False
        self.cancelled = False
        self.exit_code = None
        self.cleanup_errors = []
        self.cold_receipt = None
        self.phase = None
        self.workers = []

    def other_owner_idle(self):
        from .support_job import JOB as reference, ACTIVE as reference_active

        h.need(reference.state not in reference_active, 'Reference worker owns the native slot')

    def prepare(self, bpy, times, parent, explicit=None, diagnostic=None, solver=None):
        self.other_owner_idle()
        h.need(self.state == 'EMPTY', 'Release previous review')
        if explicit is None:
            selected(bpy)
        binary, sha, windows_runtime = qualify(bpy)
        nonce = uuid.uuid4().hex
        if solver is not None:
            from . import support_solver

            h.need(explicit is None and diagnostic is None, 'No mixed request')
            request = support_solver.freeze(bpy, solver, nonce)
        elif diagnostic is not None:
            from . import support_diagnostic_service

            h.need(explicit is None, 'No mixed request')
            request = support_diagnostic_service.freeze(bpy, diagnostic, nonce)
        else:
            h.need(len(times) == 4, 'Four explicit times required')
            request = (service.freeze(bpy, list(times), nonce) if explicit is None else
                       service.freeze_explicit(bpy, list(times), nonce, **explicit))
        parent = h.safe(parent)
        h.need(parent.is_dir(), 'Existing private output directory required')
        self.session = parent / ('ACW-Readonly-' + uuid.uuid4().hex)
        self.windows_runtime = windows_runtime
        self.state = 'PREPARING'
        try:
            self._mkdir(self.session)
            self.request = request
            self.digest = service.digest(request)
            self.binary = str(binary)
            package = Path(__file__).resolve().parent
            self.pins = {str(p): h.sha(p) for p in package.rglob('*') if p.is_file() and p.suffix in ('.py', '.json')}
            self.pins[self.binary] = sha
            self.pins[request['source']] = request['source_sha256']
            service.write(self.session / 'REQUEST.json', request)
            self.config = {'request': str(self.session / 'REQUEST.json'), 'digest': self.digest,
                           'request_bytes': h.sha(self.session / 'REQUEST.json'), 'pins': self.pins,
                           'source': request['source'], 'source_sha256': request['source_sha256'],
                           'binary': self.binary, 'output': str(self.session / 'review')}
            if windows_runtime:
                self.config['windows_runtime'] = windows_runtime
            if diagnostic is not None or solver is not None:
                copy = self.session / 'SOURCE.blend'
                with h.safe(request['source']).open('rb') as source, copy.open('xb') as target:
                    shutil.copyfileobj(source, target)
                h.need(h.sha(copy) == request['source_sha256'], 'Private copy mismatch')
                self.pins[str(copy)] = request['source_sha256']
                self.config['worker_source'] = str(copy)
            h.write(self.session / 'CONFIG.json', self.config)
            self.config_sha = h.sha(self.session / 'CONFIG.json')
            self.state = 'PREPARED'
        except BaseException as error:
            self.state = 'FAILED'
            self.error = repr(error)
            raise

    def _mkdir(self, path):
        if self.windows_runtime:
            from .windows_owned import private_directory

            return private_directory(path)
        path.mkdir(mode=0o700)
        return path

    def _launch(self, phase, source, tail=()):
        self.phase = phase
        self.started = None
        self.waited = False
        self.exit_code = None
        self.process = None
        env_root = self._mkdir(self.session / ('environment' if phase == 'author' else 'environment-cold'))
        command = [self.binary, '--background', '--factory-startup', '--disable-autoexec', '--offline-mode', '--threads', '2',
                   source, '--python-exit-code', '1', '--python', str(Path(__file__).with_name('read_worker.py')), '--',
                   str(self.session / 'CONFIG.json'), self.config_sha, *tail]
        self.log = (self.session / ('WORKER.log' if phase == 'author' else 'COLD.log')).open('xb')
        if self.windows_runtime:
            from .windows_owned import OwnedProcess, private_env

            env = private_env(env_root, self.binary)
            self.process = OwnedProcess(command, env=env, cwd=self.session, log=self.log)
        else:
            self.process = subprocess.Popen(command, stdout=self.log, stderr=subprocess.STDOUT, env=h.private_env(env_root))
        self.started = time.monotonic()
        self.state = 'RUNNING'

    def start(self):
        self.other_owner_idle()
        h.need(self.state == 'PREPARED', 'Freeze first; no repeated start')
        h.need(all(h.checks(self.pins).values()), 'Frozen pins changed')
        h.need(h.sha(self.session / 'CONFIG.json') == self.config_sha, 'Config changed')
        h.need(h.sha(self.session / 'REQUEST.json') == self.config['request_bytes'], 'Request changed')
        self.slot_token = native_slot.acquire(self)
        try:
            self._launch('author', self.config.get('worker_source', self.request['source']))
        except BaseException as error:
            self.error = repr(error)
            self.cleanup_errors.append('launch:' + repr(error))
            self.state = 'CLEANUP_PENDING'
            self.cancel()
            if self.process is None:
                self.poll()
            raise

    def cancel(self):
        self.cancelled = True
        if self.state == 'PREPARED':
            self.state = 'CANCELLED'
        if self.process is not None and not self.waited:
            try:
                if self.process.poll() is None:
                    self.process.kill()
            except ProcessLookupError:
                pass
            except BaseException as error:
                self.cleanup_errors.append(repr(error))
                self.state = 'CLEANUP_PENDING'

    def _reap(self):
        if self.process is not None and not self.waited:
            if self.started is None or time.monotonic() - self.started > 120:
                self.error = 'Read-only worker timeout'
                self.cancel()
            if self.process.poll() is None:
                return False
            self.exit_code = self.process.wait()
            self.waited = True
            if self.windows_runtime:
                self.workers.append({'phase': self.phase, 'pid': self.process.pid, 'exit_code': self.exit_code,
                                     'waited': True, 'owned_job_drained': True})
        if self.log:
            self.log.close()
            self.log = None
        return True

    def _worker_exit(self, filename):
        receipt = json.loads((self.session / filename).read_text(encoding='utf-8'))
        h.need(receipt['success'] and not receipt['errors'] and receipt['request_digest'] == self.digest,
               'Worker did not complete protection')
        if self.windows_runtime:
            h.need(receipt['runtime'] == self.windows_runtime and receipt['pid'] == self.workers[-1]['pid'],
                   'Windows worker receipt runtime or process differs')
        return receipt

    def _verify_author(self):
        receipt = self._worker_exit('WORKER-EXIT.json')
        output = self.session / 'review'
        result = json.loads((output / 'RESULT.json').read_text(encoding='utf-8'))
        h.need(receipt['result_digest'] == service.digest(result), 'Native result digest mismatch')
        kind = self.request['kind']
        if kind == 'BILATERAL_CAP_WORKFLOW':
            from . import support_solver

            support_solver.validate_result(result, self.request, self.digest, output)
            if self.windows_runtime and result['status'] == 'SAMPLED_CANDIDATE_READY':
                h.need(isinstance(result.get('cold_witness'), dict), 'Independent candidate witness absent')
                self.author_result = result
                self.author_digest = receipt['result_digest']
                self._launch('cold', result['candidate'], ('cold', self.author_digest))
                return False
        elif kind == 'FLAT_CAP_SUPPORT_DIAGNOSTIC':
            from . import support_diagnostic_service

            support_diagnostic_service.validate_result(result, self.request, self.digest)
        else:
            h.need(result['status'] == 'REQUEST_BOUND_SAMPLED_REVIEW_ONLY' and result['request_sha256'] == self.digest
                   and result['request'] == self.request, 'Wrong result binding')
            h.need(result['snapshot_unchanged'] and result['source_unchanged'] and not result['may_apply']
                   and not result['may_write_keys'], 'Wrong result qualification')
            pose = result['pose']
            h.need([sample['time'] for sample in pose['samples']] == self.request['sample_times'], 'Sample schedule')
            h.need(pose['frame_restored'] and pose['view_layer_unchanged'] and all(pose[key] is False for key in
                   ('may_apply', 'may_write_keys', 'continuous_interval_qualified', 'contact_qualified', 'ground_assessed')),
                   'Unsupported pose qualification')
        self.result = result
        return True

    def _verify_cold(self):
        from . import support_solver

        receipt = self._worker_exit('COLD-EXIT.json')
        result = self.author_result
        checked = receipt['result']
        h.need(receipt['author_digest'] == self.author_digest and len(self.workers) == 2
               and self.workers[0]['pid'] != self.workers[1]['pid'], 'Independent worker or author binding differs')
        h.need(checked == {**result['cold_witness'], 'candidate': result['candidate'], 'candidate_sha256': result['candidate_sha256']},
               'Independent candidate witness differs')
        support_solver.validate_result(result, self.request, self.digest, self.session / 'review')
        self.cold_receipt = receipt
        self.result = result

    def poll(self):
        if self.state not in ACTIVE and self.slot_token is None:
            return self.state
        if self.cleanup_errors:
            self.state = 'CLEANUP_PENDING'
        try:
            if not self._reap():
                return self.state
        except BaseException as error:
            self.cleanup_errors.append(repr(error))
            self.error = repr(error)
            self.state = 'CLEANUP_PENDING'
            return self.state
        if self.terminal_intent is not None:
            return native_slot.finish(self, self.terminal_intent)
        try:
            h.need(self.waited and self.exit_code == 0 and not self.cancelled and not self.cleanup_errors,
                   'Worker failed/cancelled or cleanup incomplete')
            h.need(all(h.checks(self.pins).values()), 'Source/code/runtime drift')
            if self.phase == 'cold':
                self._verify_cold()
            elif not self._verify_author():
                return self.state
            self.state = 'SUCCEEDED'
        except BaseException as error:
            self.error = repr(error)
            if self.phase == 'cold' and (self.log is not None or (self.process is not None and not self.waited)):
                self.cleanup_errors.append(repr(error))
                self.state = 'CLEANUP_PENDING'
                self.cancel()
                return self.state
            self.state = 'CANCELLED' if self.cancelled else 'FAILED'
        terminal = {'state': self.state, 'waited': self.waited, 'exit_code': self.exit_code, 'cancelled': self.cancelled,
                    'cleanup_errors': self.cleanup_errors, 'error': self.error, 'request_digest': self.digest}
        if self.windows_runtime:
            terminal['workers'] = self.workers
        return native_slot.finish(self, terminal)

    def close(self):
        if self.slot_token is not None or self.state in ACTIVE:
            self.state = 'CLEANUP_PENDING'
            self.cancel()
            if self.process is not None and not self.waited:
                try:
                    self.process.wait(timeout=10)
                except BaseException as error:
                    self.cleanup_errors.append(repr(error))
                    raise RuntimeError('Retain owner until child reaped') from error
            self.poll()
            h.need(self.slot_token is None and self.state not in ACTIVE, 'Cleanup pending; owner retained')

    def release(self):
        h.need(self.slot_token is None and self.state not in ACTIVE and self.log is None, 'Wait for cleanup')
        self.__init__()


JOB = Job()
