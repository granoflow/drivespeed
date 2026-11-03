"""Compare separately recorded peak diagnostics without merging measurements."""

import hashlib
import json
import os
import math
from pathlib import Path
import stat

from system_io import unsafe_path_component
from workloads import (MIB, GIB, CACHE_SUPPLEMENT_METHOD, PEAK_METHOD, PEAK_SUPPLEMENT_METHOD,
                       cache_assessment, cache_observation, case_core_missing)


def load_peak_source(path):
    """Freeze bounded local source bytes; their claims are rechecked on comparison."""
    path = Path(path)
    if unsafe_path_component(path):
        raise ValueError('Peak source must not use symlinks')
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                               getattr(os, 'O_NONBLOCK', 0)), 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16*MIB:
                raise ValueError('Peak source must be bounded regular JSON')
            raw = stream.read(16*MIB + 1)
        def unique(pairs):
            obj = {}
            for key, value in pairs:
                if key in obj:
                    raise ValueError('Duplicate peak source field')
                obj[key] = value
            return obj
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid number')))
        if (len(raw) > 16*MIB or not isinstance(value, dict) or value.get('synthetic')
                or value.get('tool', {}).get('method_version') != PEAK_METHOD
                or value.get('plan', {}).get('method_version') != PEAK_METHOD
                or not isinstance(value.get('run_id'), str) or not value['run_id']
                or not isinstance(value.get('cases'), list)):
            raise ValueError('Expected an original peak run')
        return value, hashlib.sha256(raw).hexdigest()
    except (OSError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError) as error:
        raise ValueError('Invalid or unavailable peak source') from error


def validate_source_target(source, target, file_bytes):
    """Reject a different or insufficiently identified source before writing."""
    from device_identity import validate_device_identity
    try:
        identity = validate_device_identity(source['device_identity'])
        old = source['target']
        if (not isinstance(old, dict) or not isinstance(target, dict)
                or any(old.get(key) is None or old.get(key) != target.get(key)
                       for key in ('path', 'st_dev', 'inode', 'device', 'physical_devices'))
                or source['plan']['file_bytes'] != file_bytes
                or source['plan']['peak_criteria'] != {'minimum_seconds':5, 'tolerance':0.05,
                                                        'minimum_depth_percent':20, 'minimum_half_depth_percent':90}
                or not isinstance(source['environment']['engine'], str)
                or not source['environment']['engine']
                or not isinstance(source['environment']['fio']['version'], str)
                or not source['environment']['fio']['version']):
            raise ValueError('Peak source conditions do not match')
        return identity
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError('Peak source conditions are incomplete') from error


def compare_cache_run(source, supplement, source_sha256):
    """Reassess a bound sustained-write observation without changing either run."""
    from device_identity import validate_device_identity
    incompatible = {'status': 'incompatible'}
    try:
        if (not isinstance(source_sha256, str) or len(source_sha256) != 64
                or any(char not in '0123456789abcdef' for char in source_sha256)):
            return incompatible
        plan, cases = supplement['plan'], supplement['cases']
        if (not isinstance(plan, dict) or not isinstance(cases, list) or len(cases) != 1
                or supplement['tool']['method_version'] != CACHE_SUPPLEMENT_METHOD
                or plan['method_version'] != CACHE_SUPPLEMENT_METHOD
                or source['tool']['method_version'] != PEAK_METHOD
                or source['plan']['method_version'] != PEAK_METHOD
                or plan.get('source_peak_sha256') != source_sha256
                or not isinstance(source.get('run_id'), str) or not source['run_id']
                or plan.get('source_peak_run_id') != source['run_id']):
            return incompatible
        identity = validate_source_target(source, supplement['target'], plan['file_bytes'])
        if (validate_device_identity(supplement['device_identity']) != identity
                or source['environment']['engine'] != supplement['environment']['engine']
                or source['environment']['fio']['version'] != supplement['environment']['fio']['version']):
            return incompatible
        jobs = plan['jobs']
        if not isinstance(jobs, list) or len(jobs) != 1 or not isinstance(jobs[0], dict):
            return incompatible
        job, case = jobs[0], cases[0]
        size, budget, duration = plan['file_bytes'], plan['write_budget'], job['runtime']
        if (not isinstance(case, dict) or type(size) is not int or not GIB <= size <= 2**63 - 1 or size % MIB
                or type(budget) is not int or not size <= budget <= 2**63 - 1 or budget % MIB
                or type(duration) is not int or not 105 <= duration <= 86400
                or plan.get('profile') != 'cache_supplement' or plan.get('max_write_bytes') != budget
                or plan.get('max_runtime_seconds') != duration + 45
                or (job.get('name'), job.get('rw'), job.get('bs'), job.get('qd')) != ('sustained', 'write', MIB, 16)
                or job.get('io_bytes') != budget or job.get('initialize_bytes') != size
                or job.get('cache_test') is not True or case.get('name') != 'sustained'):
            return incompatible
        missing = case_core_missing(case, job, plan)
        assessment = cache_assessment(*cache_observation(case))
        if missing or assessment['status'] != 'observed_drop_stable':
            return {'status': 'unqualified', 'missing_core': missing,
                    'observation_status': assessment['status']}
        return {'status': 'qualified_observation', 'source_run_id': source['run_id'],
                'post_drop_mb_s': assessment['post_cache_mb_s'], 'cause_confirmed': False,
                'note': 'Separate runs; device state is not proven equal and no result is merged.'}
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return incompatible


def compare_peak_runs(source, supplement, source_sha256):
    """Compare qualified samples only against their frozen source bytes."""
    from peak_analysis import assess_peak_pair, assess_peak_scan, select_qd16_case, qd16_criteria
    from device_identity import validate_device_identity
    if not isinstance(source, dict) or not isinstance(supplement, dict):
        return {'status': 'incompatible'}
    a, b = source.get('target', {}), supplement.get('target', {})
    identity_a, identity_b = source.get('device_identity', {}), supplement.get('device_identity', {})
    plan_a, plan_b = source.get('plan', {}), supplement.get('plan', {})
    env_a, env_b = source.get('environment', {}), supplement.get('environment', {})
    tool_a, tool_b = source.get('tool', {}), supplement.get('tool', {})
    fio_a = env_a.get('fio', {}) if isinstance(env_a, dict) else {}
    fio_b = env_b.get('fio', {}) if isinstance(env_b, dict) else {}
    if (not all(isinstance(value, dict) for value in (a, b, identity_a, identity_b, plan_a, plan_b, env_a, env_b, tool_a, tool_b))
            or not isinstance(source_sha256, str) or len(source_sha256) != 64
            or any(char not in '0123456789abcdef' for char in source_sha256)
            or plan_b.get('source_peak_sha256') != source_sha256
            or not isinstance(source.get('run_id'), str) or not source['run_id']
            or plan_b.get('source_peak_run_id') != source['run_id']
            or not isinstance(fio_a, dict) or not isinstance(fio_b, dict)
            or not isinstance(fio_a.get('version'), str) or not fio_a['version']
            or fio_a['version'] != fio_b.get('version')
            or tool_a.get('method_version') != PEAK_METHOD
            or tool_b.get('method_version') != PEAK_SUPPLEMENT_METHOD
            or not identity_a or identity_a != identity_b
            or any(a.get(key) is None or a.get(key) != b.get(key) for key in ('path', 'st_dev', 'inode', 'device', 'physical_devices'))
            or not env_a.get('engine') or env_a.get('engine') != env_b.get('engine')
            or not plan_a.get('file_bytes') or plan_a.get('file_bytes') != plan_b.get('file_bytes')
            or plan_a.get('peak_criteria') != {'minimum_seconds':5, 'tolerance':0.05,
                                               'minimum_depth_percent':20, 'minimum_half_depth_percent':90}
            or plan_a.get('peak_criteria') != plan_b.get('peak_criteria')
            or not isinstance(source.get('cases'), list) or not isinstance(plan_a.get('jobs'), list)
            or not isinstance(supplement.get('cases'), list) or not isinstance(plan_b.get('jobs'), list)
            or not isinstance(supplement.get('peak_supplement'), dict)):
        return {'status': 'incompatible'}
    try:
        validate_device_identity(identity_a)
    except ValueError:
        return {'status':'incompatible'}
    if any(not isinstance(case, dict) or not isinstance(case.get('name'), str)
           for case in source['cases'] + supplement['cases']):
        return {'status':'incompatible'}
    if any(not isinstance(job, dict) or not isinstance(job.get('name'), str) for job in plan_a['jobs']):
        return {'status':'incompatible'}
    jobs = {job['name']:job for job in plan_a['jobs']}
    if len(jobs) != len(plan_a['jobs']):
        return {'status':'incompatible'}
    if any(not isinstance(job, dict) or not isinstance(job.get('name'), str) for job in plan_b['jobs']):
        return {'status':'incompatible'}
    supplement_jobs = {job['name']:job for job in plan_b['jobs']}
    if len(supplement_jobs) != len(plan_b['jobs']):
        return {'status':'incompatible'}
    try:
        old_cases = [case for case in source['cases'] if case['name'] in jobs
                     and not case_core_missing(case, jobs[case['name']], plan_a)]
    except (KeyError, TypeError, ValueError, OverflowError):
        return {'status':'incompatible'}
    names = [case.get('name') for case in supplement['cases']]
    if len(names) != len(set(names)):
        return {'status':'incompatible'}
    by_name = {case.get('name'):case for case in supplement['cases']}
    criteria = plan_a['peak_criteria']
    scan = assess_peak_scan(old_cases, **criteria)
    comparisons = {}
    for name, pair in supplement['peak_supplement'].items():
        if name not in scan['workloads'] or not isinstance(pair, dict) or pair.get('status') != 'plateau':
            continue
        depths = pair.get('depths')
        if depths not in ([16,32], [32,64]):
            continue
        keys = [f'peak_{name}_{depth}_{repeat}' for depth in depths for repeat in (1,2)]
        if any(key not in by_name or key not in supplement_jobs for key in keys):
            continue
        expected_rw = {'seq_read':'read', 'seq_write':'write', 'rand_read':'randread', 'rand_write':'randwrite'}[name]
        expected_bs = MIB if name.startswith('seq_') else 4096
        try:
            if any((by_name[key].get('rw'), by_name[key].get('bs'), by_name[key].get('qd'))
                   != (expected_rw, expected_bs, depths[index // 2])
                   or case_core_missing(by_name[key], supplement_jobs[key], plan_b)
                   for index,key in enumerate(keys)):
                continue
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        actual = assess_peak_pair([by_name[key] for key in keys], **criteria)
        rates = actual.get('rates_mb_s')
        if actual.get('status') != 'plateau' or any(not math.isfinite(v) or v <= 0 for v in rates):
            continue
        candidates = list(scan['workloads'][name]['highest_single_by_qd'])
        baseline = select_qd16_case(old_cases, name, **qd16_criteria(source))
        if baseline:
            direction = 'read' if name.endswith('read') else 'write'
            candidates.append({'name':baseline['name'], 'qd':16, 'mb_s':baseline['metrics'][direction]['mb_s']})
        if not candidates:
            continue
        highest = max(candidates, key=lambda row: row['mb_s'])
        comparisons[name] = {'earlier_highest':highest, 'supplement_highest_mb_s':max(rates),
                             'earlier_higher':highest['mb_s'] > max(rates) * (1 + criteria['tolerance'])}
    return {'status':'compared', 'source_run_id':source['run_id'], 'workloads':comparisons,
            'note':'Separate runs; device state is not proven equal and no result is merged.'}
