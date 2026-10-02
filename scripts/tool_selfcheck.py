#!/usr/bin/env python3
"""Offline checks of benchmark tools; no application/model performance claims."""
import copy
import json
import sys
import tempfile
from pathlib import Path
import bench
import run_adapter

ROOT = Path(__file__).resolve().parents[1]


def rejected(fn):
    try:
        fn()
    except (ValueError, TypeError):
        return
    raise AssertionError('Unsafe/invalid input was not rejected')


def main():
    cases = bench.rows(ROOT / 'data/seeds.jsonl')
    sources = bench.rows(ROOT / 'data/sources.jsonl')
    candidates = bench.rows(ROOT / 'data/candidate_answers.jsonl')
    bench.validate(cases, sources)
    assert len(cases) == len(candidates) == 16
    assert all(c['review_status'] == 'pending_review' for c in cases + candidates)
    for c, p in zip(cases, candidates):
        assert c['id'] == p['id']
        assert not bench.output_problems(p['output'], c['input']['text'])
    proposed = bench.split_proposal(cases, 'selfcheck')
    bench.check_split(cases, proposed)
    leaking = copy.deepcopy(proposed)
    confidence = [r for r in leaking['records'] if r['id'] in {'sz-001', 'sz-002'}]
    confidence[0]['proposed_split'], confidence[1]['proposed_split'] = 'dev', 'test_candidate'
    rejected(lambda: bench.check_split(cases, leaking))
    rejected(lambda: bench.score(cases, sources, candidates))
    first = bench.score(cases, sources, candidates, allow_pending=True)
    second = bench.score(cases, sources, candidates, allow_pending=True)
    assert first == second
    assert first['status'] == 'pipeline_only'
    assert first['summary']['auxiliary_case_macro_score'] is None
    # Duplicate alarms must be false positives, not rewarded as extra detections.
    sample = copy.deepcopy(candidates[13]['output'])
    extra = copy.deepcopy(sample['issues'][0])
    extra['id'] = 'duplicate'
    sample['issues'].append(extra)
    auto = bench.auto_case(cases[13], sample, {s['id']: s for s in sources})
    assert auto['tp'] == 2 and auto['fp'] == 1
    assert len({m['expected_id'] for m in auto['matching']}) == 2
    # Fabricated authority and invented IDs are semantic failures, not successful evidence.
    forged = copy.deepcopy(candidates[1]['output'])
    forged['issues'][0]['evidence'][0]['use'] = 'authoritative'
    bad = bench.auto_case(cases[1], forged, {s['id']: s for s in sources})
    assert bad['unverified_authority_claims'] == 1
    forged['issues'][0]['evidence'][0]['source_id'] = 'nonexistent'
    bad = bench.auto_case(cases[1], forged, {s['id']: s for s in sources})
    assert bad['citation_ids_exist'] == 0 and bad['required_evidence_supplied'] == 0
    # Missing predictions and edited outputs with old human scores must not silently pass.
    rejected(lambda: bench.score(cases, sources, candidates[:-1], allow_pending=True))
    review = dict(id='sz-001', repeat=1, output_sha256='wrong_hash', review_status='pending_review')
    rejected(lambda: bench.score(cases, sources, candidates, [review], True))
    # A runtime failure does not become a text error or a passing quality score.
    failed = copy.deepcopy(candidates)
    failed[0]['output'] = None
    failed[0]['error'] = {'type': 'timeout'}
    result = bench.score(cases, sources, failed, allow_pending=True)
    assert result['summary']['errors'] == 1 and result['summary']['auxiliary_case_macro_score'] is None
    # Unicode and CRLF offsets are preserved; shifting to a UTF-16 count must fail.
    unicode_case, unicode_answer = cases[-1], copy.deepcopy(candidates[-1]['output'])
    assert '\r\n' in unicode_case['input']['text']
    unicode_answer['issues'][0]['span']['start'] += 1
    unicode_answer['issues'][0]['span']['end'] += 1
    assert bench.output_problems(unicode_answer, unicode_case['input']['text'])
    # Repeat calls are aggregated within cases, never advertised as independent samples.
    repeated = copy.deepcopy(candidates)
    repeated += [dict(p, repeat=2) for p in copy.deepcopy(candidates)]
    multi = bench.score(cases, sources, repeated, allow_pending=True)
    assert multi['summary']['cases'] == 16 and multi['summary']['calls'] == 32
    assert multi['summary']['auxiliary_case_macro_score'] is None
    assert multi['automatic_diagnostics']['clean_false_alert_rate']['denominator'] == first['automatic_diagnostics']['clean_false_alert_rate']['denominator']
    assert multi['automatic_diagnostics']['unique_case_count'] == 16
    assert bench.auto_case(cases[0], candidates[0]['output'], {s['id']: s for s in sources})['clean_false_alerts'] == 0
    false_alarm = copy.deepcopy(candidates[1]['output'])
    false_alarm['issues'][0]['span'] = {'start': 0, 'end': 4, 'quote': cases[0]['input']['text'][:4]}
    clean_auto = bench.auto_case(cases[0], false_alarm, {s['id']: s for s in sources})
    assert clean_auto['fp'] == 1 and clean_auto['clean_false_alerts'] == 1
    # Open-ended phrasing is judged by human rubric, never exact reference strings.
    approved = copy.deepcopy(cases[13])
    approved['review_status'] = 'approved'
    approved['annotation'] = {'status': 'approved', 'reviewers': ['tool-fixture-A', 'tool-fixture-B'],
                              'adjudicator': 'tool-fixture-C', 'approved_at': 'tool-fixture-only'}
    test_prediction = copy.deepcopy(candidates[13])
    test_prediction.pop('review_status')
    test_prediction['provenance'] = 'offline_tool_fixture_not_system_run'
    test_prediction['output']['issues'][0]['suggestion']['text'] = '频次调整为每季度一次。'
    manual = dict(id=approved['id'], repeat=1, output_sha256=bench.output_hash(test_prediction['output']),
                  review_status='approved', reviewer='tool-fixture-reviewer', reviewed_at='tool-fixture-only',
                  evidence_score=2, revision_score=2, new_error=False, fabricated_authority=False,
                  reason='Hardcoded tool fixture only; not actual expert approval.')
    checked = bench.score([approved], sources, [test_prediction], [manual])
    assert checked['summary']['auxiliary_case_macro_score'] is not None
    manual['revision_score'] = 0
    changed = bench.score([approved], sources, [test_prediction], [manual])
    assert abs(checked['summary']['auxiliary_case_macro_score'] - changed['summary']['auxiliary_case_macro_score'] - .15) < 1e-9
    candidate_on_approved = dict(test_prediction, provenance='synthetic_candidate')
    rejected(lambda: bench.score([approved], sources, [candidate_on_approved], [manual]))
    malformed = copy.deepcopy(candidates)
    malformed[0]['output']['decision'] = {'invalid': 'enum'}
    assert bench.score(cases, sources, malformed, allow_pending=True)['summary']['errors'] == 1
    rejected(lambda: bench.loads('{"cost":1e999}'))
    rejected(lambda: bench.loads('{"id":"a","id":"b"}'))
    with tempfile.TemporaryDirectory(prefix='sz-tools-') as folder:
        out = Path(folder) / 'no-run.jsonl'
        rejected(lambda: run_adapter.collect(ROOT / 'data/seeds.jsonl', ROOT / 'data/sources.jsonl',
                                            ROOT / 'config/adapter.example.json', out, True))
        assert not out.exists()
        # Exercise protocol collection against local fixtures, with no model/provider.
        tmp = Path(folder)
        adapter = tmp / 'tool_fixture.py'
        adapter.write_text('import json,sys\np=json.load(sys.stdin)\n'
                           'assert set(p)=={"id","input","repeat"}\n'
                           'assert "expected" not in p["input"]\n'
                           'print(json.dumps({"output":{"decision":"clean","needs_human_review":False,"issues":[],"limitations":[]}}))\n',
                           encoding='utf-8')
        cfg = tmp / 'tool-config.json'
        cfg.write_text(json.dumps({'command': [sys.executable, str(adapter)], 'cwd': str(tmp),
                                   'repeats': 1, 'timeout_seconds': 2,
                                   'application_version': 'offline-tool-fixture-not-system'}), encoding='utf-8')
        collected = tmp / 'tool-only-predictions.jsonl'
        info = run_adapter.collect(ROOT / 'data/seeds.jsonl', ROOT / 'data/sources.jsonl', cfg, collected, True)
        assert info['calls'] == 16 and info['errors'] == 0 and info['quality_score'] is None
        assert all('expected' not in r for r in bench.rows(collected))
        rejected(lambda: run_adapter.collect(ROOT / 'data/seeds.jsonl', ROOT / 'data/sources.jsonl', cfg, collected, True))
        single = tmp / 'one-case.jsonl'
        single.write_text(json.dumps(cases[0], ensure_ascii=False) + '\n', encoding='utf-8')
        adapter.write_text('print("invalid json")\n', encoding='utf-8')
        info = run_adapter.collect(single, ROOT / 'data/sources.jsonl', cfg, tmp / 'bad-json.jsonl', True)
        assert info['errors'] == 1
        adapter.write_text('raise SystemExit(3)\n', encoding='utf-8')
        info = run_adapter.collect(single, ROOT / 'data/sources.jsonl', cfg, tmp / 'bad-exit.jsonl', True)
        assert info['errors'] == 1
        adapter.write_text('import time\ntime.sleep(0.2)\n', encoding='utf-8')
        cfg.write_text(json.dumps({'command': [sys.executable, str(adapter)], 'cwd': str(tmp),
                                   'repeats': 1, 'timeout_seconds': .01}), encoding='utf-8')
        timeout_path = tmp / 'timeout.jsonl'
        info = run_adapter.collect(single, ROOT / 'data/sources.jsonl', cfg, timeout_path, True)
        assert info['errors'] == 1 and bench.rows(timeout_path)[0]['error']['type'] == 'timeout'
    print(json.dumps(dict(tool_checks='passed', pending_seed_cases=16,
                          system_evaluation_performed=False, model_calls=0, hillclimb_rounds=0), ensure_ascii=False))


if __name__ == '__main__':
    main()
