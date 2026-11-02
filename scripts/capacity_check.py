"""Plan an optional owned-file integrity pass from completed local measurements."""

import hashlib
import json
import math
import os
from pathlib import Path
import stat

from system_io import unsafe_path_component
from workloads import AUTO_METHODS, GIB, MIB, active_runtime_ms, cache_assessment, collection_status, core_completeness, diagnostic_plan

MAX_SOURCE_BYTES = 16 * MIB
MAX_JOB_SECONDS = 7 * 24 * 60 * 60
METHOD = 'drivespeed-capacity-v1'


def _unique_fields(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate source JSON field')
        value[key] = item
    return value


def _target_identity(target):
    if not isinstance(target, dict) or not isinstance(target.get('path'), str):
        raise ValueError('Target directory identity is required')
    path = Path(target['path'])
    devices = target.get('physical_devices')
    if (not path.is_absolute() or unsafe_path_component(path) or not path.is_dir()
            or not isinstance(devices, list) or not devices
            or any(not isinstance(item, str) or not item for item in devices)
            or len(set(devices)) != len(devices)):
        raise ValueError('Capacity source requires an unchanged identified target directory')
    info = path.stat()
    if (any(type(target.get(key)) is not int for key in ('st_dev', 'inode'))
            or (info.st_dev, info.st_ino) != (target.get('st_dev'), target.get('inode'))):
        raise ValueError('Capacity target directory identity changed')
    return str(path), info.st_dev, info.st_ino, tuple(sorted(devices))


def load_source(path, target):
    """Read bounded regular JSON, requalify the normal run, and bind its target."""
    path = Path(path)
    if unsafe_path_component(path):
        raise ValueError('Source result must not use symlinks or reparse points')
    try:
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        with os.fdopen(os.open(path, flags), 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
                raise ValueError('Source result must be bounded regular JSON')
            raw = stream.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError('Source result exceeds the JSON limit')
        source = json.loads(raw, object_pairs_hook=_unique_fields,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite source JSON')))
        if (not isinstance(source, dict) or source.get('synthetic')
                or source.get('outcome') != 'complete' or not isinstance(source.get('run_id'), str) or not source['run_id']
                or source.get('tool', {}).get('method_version') not in AUTO_METHODS
                or source.get('plan', {}).get('profile') != 'full'
                or not core_completeness(source)['complete']):
            raise ValueError('Source must be a genuinely completed normal full run')
        if _target_identity(source['target']) != _target_identity(target):
            raise ValueError('Capacity source belongs to another target')
    except (OSError, KeyError, TypeError, AttributeError, OverflowError, RecursionError, UnicodeError) as error:
        raise ValueError('Invalid or unavailable capacity source result') from error
    source['_source_sha256'] = hashlib.sha256(raw).hexdigest()
    return source


def _source_rates(source):
    sustained = [case for case in source['cases'] if case['name'] == 'sustained']
    if len(sustained) != 1:
        raise ValueError('Source sustained measurement is ambiguous')
    case = sustained[0]
    assessed = cache_assessment(case.get('series', []), sampling_status=collection_status(case))
    write = assessed.get('post_cache_mb_s')
    if assessed.get('status') != 'observed_drop_stable' or type(write) not in (int, float) or not math.isfinite(write) or write <= 0:
        raise ValueError('A measured stable post-cache write speed is required')
    for name in ('sustained_read', 'seq_read'):
        cases = [item for item in source['cases'] if item['name'] == name]
        if len(cases) != 1 or cases[0].get('status') != 'completed':
            continue
        read = cases[0].get('metrics', {}).get('read', {}).get('mb_s')
        if type(read) in (int, float) and math.isfinite(read) and read > 0:
            return write, read, name
    raise ValueError('A completed measured read speed is required')


def capacity_plan(target, allowance, source):
    """Preview one complete write/read pass; space is not permission to write."""
    if (_target_identity(source['target']) != _target_identity(target)
            or source.get('synthetic') or source.get('outcome') != 'complete'
            or source.get('plan', {}).get('profile') != 'full'
            or not core_completeness(source)['complete']):
        raise ValueError('Capacity source no longer qualifies for this target')
    digest = source.get('_source_sha256')
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in '0123456789abcdef' for char in digest):
        raise ValueError('Load a source result with a recorded file digest first')
    total, free = target.get('total_bytes'), target.get('free_bytes')
    if any(type(value) is not int or not 0 <= value <= 2**64 - 1 for value in (total, free)) or not total or free > total:
        raise ValueError('Known target capacity and free bytes are required')
    reserve = max(10 * GIB, (total + 19) // 20)
    size = max(0, free - reserve - 64 * MIB) // MIB * MIB
    if size < MIB:
        raise ValueError('No complete test-file range fits after the safety reserve')
    if (allowance.get('present') is not True or not isinstance(allowance.get('scope_id'), str) or not allowance['scope_id']
            or type(allowance.get('revision')) is not int or allowance['revision'] < 0 or size > 2**63 - 1 or
            any(type(allowance.get(key)) is not int or not 0 <= allowance[key] <= 2**63 - 1
                for key in ('run_cap', 'remaining'))):
        raise ValueError('An existing finite signed-integer allowance is required')
    additional = 0 if allowance.get('mode') == 'single_run' else max(0, size - allowance['remaining'])
    write, read, read_case = _source_rates(source)
    seconds = dict(write=size / (write * 1_000_000), read=size / (read * 1_000_000))
    if any(not math.isfinite(value) or value * 1.8 > MAX_JOB_SECONDS for value in seconds.values()):
        raise ValueError('Estimated integrity pass exceeds the supported seven-day per-job limit')
    runtimes = {key: max(1, math.ceil(value * 1.8)) for key, value in seconds.items()}
    plan = diagnostic_plan('integrity', target, file_bytes=size, write_budget=size,
                           duration=max(runtimes.values()))
    for job in plan['jobs']:
        job.update(runtime=runtimes[job['rw']], status_interval=10)
    baseline = sum(seconds.values())
    plan.update(method_version=METHOD, reserve_bytes=reserve, metadata_headroom_bytes=64 * MIB,
                source_run_id=source['run_id'], source_sha256=digest,
                capacity_allowance=dict(allowance), additional_authorized_bytes=additional,
                capacity_run_cap_bytes=max(allowance['run_cap'], size),
                authorization_status='proposal_requires_explicit_plan_confirmation',
                max_runtime_seconds=sum(job['runtime'] + 45 for job in plan['jobs']),
                estimate=dict(write_mb_s=write, read_mb_s=read, read_case=read_case,
                              lower_seconds=baseline * 1.2, upper_seconds=baseline * 1.8,
                              verification_overhead='unknown; estimated range is not a completion guarantee',
                              scope='only the owned test file; not existing files or the entire physical device'))
    return plan


def completeness(result):
    """Require exact verified write/read coverage, never a partial-pass success."""
    if not isinstance(result, dict):
        return dict(complete=False, missing_core=['capacity.identity'])
    plan, missing = result.get('plan', {}), []
    if not isinstance(plan, dict):
        return dict(complete=False, missing_core=['capacity.identity'])
    size = plan.get('file_bytes')
    expected = {'integrity_write': 'write', 'integrity_read': 'read'}
    if (result.get('outcome') != 'complete' or result.get('tool', {}).get('method_version') != METHOD
            or plan.get('method_version') != METHOD or plan.get('profile') != 'integrity'
            or type(size) is not int or size < MIB or size % MIB):
        missing.append('capacity.identity')
    for label, cases in (('plan', plan.get('jobs', [])), ('cases', result.get('cases', []))):
        if not isinstance(cases, list) or len(cases) != 2 or {item.get('name') for item in cases if isinstance(item, dict)} != set(expected):
            missing.append('capacity.' + label)
            continue
        for case in cases:
            name = case['name']
            if (case.get('verify') is not True or case.get('rw') != expected[name]
                    or case.get('bs') != MIB or case.get('qd') != 16 or case.get('io_bytes') != size):
                missing.append(name + '.workload')
            if label == 'cases':
                data = case.get('metrics', {})
                if not isinstance(data, dict) or any(not isinstance(data.get(key), dict) for key in ('read', 'write')):
                    missing.append(name + '.coverage')
                    continue
                direction = data.get(expected[name], {})
                other = data.get('write' if expected[name] == 'read' else 'read', {})
                if (case.get('status') != 'completed' or case.get('metrics_complete') is not True
                        or active_runtime_ms(data, expected[name]) <= 0
                        or type(direction.get('bytes')) is not int or direction['bytes'] != size
                        or type(other.get('bytes')) is not int or other['bytes'] != 0):
                    missing.append(name + '.coverage')
    return dict(complete=not missing, missing_core=sorted(set(missing)))
