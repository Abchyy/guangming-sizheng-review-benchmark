#!/usr/bin/env python3
"""Collect real adapter outputs; never sends expected labels or scores quality."""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from bench import file_hash, loads, number, output_hash, rows, save, string, validate


def collect(cases_path, sources_path, config_path, out, allow_pending=False):
    cases, sources = rows(cases_path), rows(sources_path)
    validate(cases, sources)
    if not allow_pending and any(c['review_status'] != 'approved' for c in cases):
        raise ValueError('Case labels must be approved; --allow-pending is only for an authorized pilot, not formal benchmarking')
    config = loads(config_path.read_text(encoding='utf-8'))
    cmd = config.get('command')
    if not isinstance(cmd, list) or not cmd or any(not string(x) for x in cmd):
        raise ValueError('command must be argv array')
    if any('REPLACE_WITH' in x for x in cmd):
        raise ValueError('No real adapter configured; placeholder execution refused')
    repeat_count, timeout = config.get('repeats', 1), config.get('timeout_seconds', 30)
    if type(repeat_count) is not int or repeat_count < 1 or not number(timeout, low=.001):
        raise ValueError('Invalid repeats/timeout')
    cwd = Path(config.get('cwd', '.'))
    if not cwd.is_absolute():
        cwd = config_path.resolve().parent / cwd
    cwd = cwd.resolve()
    if not cwd.is_dir():
        raise ValueError('cwd does not exist')
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() or out.with_suffix('.metadata.json').exists():
        raise ValueError('Output already exists; use a new versioned run path')
    identity = dict(cases_sha256=file_hash(cases_path), sources_sha256=file_hash(sources_path),
                    config_sha256=file_hash(config_path), collector_sha256=file_hash(__file__),
                    bench_module_sha256=file_hash(Path(__file__).with_name('bench.py')),
                    adapter_argv=cmd, adapter_local_files={}, split=config.get('split', 'unspecified'),
                    application_version=config.get('application_version'), model_version=config.get('model_version'),
                    knowledge_snapshot=config.get('knowledge_snapshot'), mode='collect_only_no_quality_score',
                    case_count=len(cases), repeats=repeat_count, isolation='ordinary_subprocess_not_sandbox',
                    budget_authorization=config.get('budget_authorization'))
    for arg in cmd:
        p = Path(arg)
        if not p.is_absolute():
            p = cwd / p
        if p.is_file():
            identity['adapter_local_files'][str(p.resolve())] = file_hash(p)
    errors = calls = 0
    with out.open('x', encoding='utf-8') as handle:
        for case in cases:
            for repeat in range(1, repeat_count + 1):
                started = time.monotonic()
                record = dict(id=case['id'], repeat=repeat, output=None, error=None, usage=None,
                              system_metadata=None, provenance='real_adapter_collection')
                try:
                    # Only authorized input, case ID and repetition; no expected/reference/candidate fields.
                    payload = dict(id=case['id'], input=case['input'], repeat=repeat)
                    proc = subprocess.run(cmd, input=json.dumps(payload, ensure_ascii=False, allow_nan=False),
                                          text=True, encoding='utf-8', capture_output=True,
                                          cwd=cwd, timeout=timeout, shell=False)
                    if proc.returncode:
                        raise RuntimeError('nonzero_exit:' + str(proc.returncode))
                    response = loads(proc.stdout)
                    if not isinstance(response, dict) or 'output' not in response:
                        raise ValueError('Adapter must return one object with output')
                    record['output'] = response['output']
                    record['usage'] = response.get('usage')
                    record['system_metadata'] = response.get('system_metadata')
                    usage = record['usage']
                    if usage is not None:
                        if not isinstance(usage, dict):
                            raise ValueError('usage must be object')
                        if usage.get('cost') is not None and (not number(usage['cost']) or not string(usage.get('cost_unit'))):
                            raise ValueError('Invalid cost/unit')
                except subprocess.TimeoutExpired:
                    record['error'] = dict(phase='adapter', type='timeout', cost_status='unknown_unless_billing_reconciled')
                except (ValueError, RuntimeError, OSError):
                    # Do not copy raw stderr/provider responses, which may include confidential content.
                    kind = 'process_io' if sys.exc_info()[0] is OSError else 'protocol_or_process_error'
                    record['error'] = dict(phase='adapter', type=kind, cost_status='unknown_unless_billing_reconciled')
                record['latency_seconds'] = time.monotonic() - started
                record['output_sha256'] = output_hash(record['output'])
                errors += record['error'] is not None
                calls += 1
                handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
                handle.flush()
    identity['calls'] = calls
    identity['errors'] = errors
    identity['predictions_sha256'] = file_hash(out)
    save(out.with_suffix('.metadata.json'), identity)
    return dict(calls=calls, errors=errors, quality_score=None, output=str(out))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--allow-pending', action='store_true')
    args = parser.parse_args()
    try:
        result = collect(args.cases, args.sources, args.config, args.out, args.allow_pending)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
