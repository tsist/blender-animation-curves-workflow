"""Canonical preview identity. Hash equality is NOT user authorization.

The future host must establish explicit acceptance separately and revalidate
actual artifact bytes/native evidence. This module never executes a commit.
"""
import copy
import hashlib
import json
import re
from .contract import WorkflowError

FIELDS={'source_sha256','implementation_sha256','proposal','compiled_changes',
        'native_readback','collision_report','inventory','exclusions','sample_times',
        'changed_intervals','tolerances','locks','limitations','artifacts','cold_verification','execution_identity'}
ARTIFACTS={'preview_blend','build_report','cold_report','build_receipt','cold_receipt'}


def sha(value):
    if not isinstance(value,str) or re.fullmatch('[0-9a-f]{64}',value) is None:
        raise WorkflowError('INVALID_REQUEST','Exact lowercase SHA256 required')
    return value


def canonical(payload):
    def validate(value,depth=0):
        if depth>64:raise WorkflowError('INVALID_REQUEST','Preview JSON nesting budget exceeded')
        if value is None or type(value) in (str,bool,int,float):return
        if type(value)is list:
            for item in value:validate(item,depth+1)
            return
        if type(value)is dict and all(type(key)is str for key in value):
            for item in value.values():validate(item,depth+1)
            return
        raise WorkflowError('INVALID_REQUEST','Only JSON containers and string object keys supported')
    validate(payload)
    try:
        return json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()
    except (TypeError,ValueError,OverflowError) as exc:
        raise WorkflowError('INVALID_REQUEST','Preview identity must be finite canonical JSON') from exc


def package(payload):
    if not isinstance(payload,dict) or set(payload)!=FIELDS:
        raise WorkflowError('INVALID_REQUEST','Complete preview identity fields required')
    sha(payload['source_sha256']);sha(payload['implementation_sha256'])
    execution=payload['execution_identity']
    if not isinstance(execution,dict) or set(execution)!={'guard_sha256','runtime_binary_sha256','request_sha256','execution_plan_sha256'}:
        raise WorkflowError('INVALID_REQUEST','Exact guard/runtime/request/execution plan identities required')
    for value in execution.values():sha(value)
    artifacts=payload['artifacts']
    if not isinstance(artifacts,dict) or set(artifacts)!=ARTIFACTS:
        raise WorkflowError('INVALID_REQUEST','Complete build/cold artifact identities required')
    for item in artifacts.values():
        if not isinstance(item,dict) or set(item)!={'sha256','size'} or type(item['size'])is not int or item['size']<=0:
            raise WorkflowError('INVALID_REQUEST','Artifact byte size and SHA256 required')
        sha(item['sha256'])
    # Schema-specific evidence truth is the guarded adapter's responsibility.
    # This utility binds it without claiming that arbitrary JSON is verified.
    canonical_payload=canonical(payload)
    return {'schema':'spatial-preview-identity-v1','status':'identity_only_not_authorization',
            'payload':copy.deepcopy(payload),'preview_sha256':hashlib.sha256(canonical_payload).hexdigest()}


def verify_identity(envelope,requested_preview_sha256,*,current_source_sha256,current_implementation_sha256,current_execution_identity):
    if not isinstance(envelope,dict) or set(envelope)!={'schema','status','payload','preview_sha256'} or envelope['schema']!='spatial-preview-identity-v1' or envelope['status']!='identity_only_not_authorization':
        raise WorkflowError('INVALID_REQUEST','Unexpected preview identity envelope')
    expected=package(envelope['payload'])
    if expected!=envelope or sha(requested_preview_sha256)!=expected['preview_sha256']:
        raise WorkflowError('CONFLICT','Preview package identity changed')
    if sha(current_source_sha256)!=envelope['payload']['source_sha256'] or sha(current_implementation_sha256)!=envelope['payload']['implementation_sha256']:
        raise WorkflowError('CONFLICT','Source or implementation changed since preview')
    if current_execution_identity!=envelope['payload']['execution_identity']:
        raise WorkflowError('CONFLICT','Guard/runtime/request/execution plan changed since preview')
    canonical(current_execution_identity)
    return {'status':'identity_matches_not_authorization','preview_sha256':expected['preview_sha256']}
