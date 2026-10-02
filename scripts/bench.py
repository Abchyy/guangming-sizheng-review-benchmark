#!/usr/bin/env python3
"""Offline audit benchmark: validate, source-group split, score and fingerprint."""
import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

VERSION = '0.1.0'
TYPES = ('terminology', 'policy', 'entity', 'quotation', 'context', 'evidence_gap', 'boundary')
STATUSES = ('error', 'suspected', 'needs_context')
RISKS = ('high', 'medium', 'low', None)
DECISIONS = ('clean', 'has_errors', 'manual_review')
REVIEWS = ('pending_review', 'approved', 'rejected')


def ensure(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def output_hash(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def reject_constant(value):
    raise ValueError('Nonfinite JSON number: ' + value)


def loads(text):
    def no_duplicates(pairs):
        obj = {}
        for key, value in pairs:
            ensure(key not in obj, 'Duplicate JSON key: ' + key)
            obj[key] = value
        return obj
    def finite_float(value):
        result = float(value)
        ensure(math.isfinite(result), 'Nonfinite JSON float')
        return result
    return json.loads(text, parse_constant=reject_constant, parse_float=finite_float,
                      object_pairs_hook=no_duplicates)


def rows(path):
    result = []
    for n, line in enumerate(Path(path).read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = loads(line)
            ensure(isinstance(row, dict), 'Expected object')
        except ValueError as exc:
            raise ValueError(f'{path}:{n}: {exc}') from exc
        result.append(row)
    ensure(result, f'Empty JSONL: {path}')
    return result


def unique(items, key='id'):
    seen = set()
    for row in items:
        ensure(isinstance(row, dict), 'Expected object in ' + key + ' collection')
        value = row.get(key)
        ensure(isinstance(value, str) and value, f'Missing {key}')
        ensure(value not in seen, 'Duplicate ' + key + ': ' + value)
        seen.add(value)


def string(value):
    return isinstance(value, str) and bool(value.strip())


def string_list(value, nonempty=False):
    return (isinstance(value, list) and (bool(value) or not nonempty)
            and all(string(v) for v in value) and len(value) == len(set(value)))


def number(value, low=0, high=None):
    try:
        return (type(value) in (float, int) and math.isfinite(value) and value >= low
                and (high is None or value <= high))
    except OverflowError:
        return False


def span_valid(span, text):
    if not isinstance(span, dict):
        return False
    a, b = span.get('start'), span.get('end')
    return (type(a) is int and type(b) is int and 0 <= a < b <= len(text)
            and span.get('quote') == text[a:b])


def validate_sources(sources):
    unique(sources)
    for s in sources:
        ensure(string(s.get('text')) and string(s.get('title')), s['id'] + ': missing source text/title')
        ensure(type(s.get('authority_verified')) is bool, s['id'] + ': authority_verified must be bool')
        ensure(s.get('kind') in {'official_candidate', 'synthetic_fixture', 'unverified_claim', 'verified_original'},
               s['id'] + ': unknown source kind')
        ensure(string(s.get('verification_status')), s['id'] + ': missing verification_status')
        if s['authority_verified']:
            ensure(s['kind'] != 'synthetic_fixture', 'Fixture cannot be authority')
            ensure(s['verification_status'] == 'full_original_checked', 'Authority needs full_original_checked')
            ensure(string(s.get('checked_by')) and string(s.get('checked_at')), 'Authority needs reviewer/date')
            ensure(re.fullmatch('[0-9a-f]{64}', s.get('snapshot_sha256') or ''), 'Authority needs snapshot hash')


def expected_valid(case, source_map):
    expected = case.get('expected')
    ensure(isinstance(expected, dict), case['id'] + ': expected object required')
    ensure(expected.get('decision') in DECISIONS and type(expected.get('needs_human_review')) is bool,
           case['id'] + ': invalid expected decision/human flag')
    issues = expected.get('issues')
    ensure(isinstance(issues, list), 'Expected issues must be list')
    unique(issues)
    for i in issues:
        ensure(i.get('type') in TYPES and i.get('status') in STATUSES and i.get('risk') in RISKS,
               case['id'] + ': invalid expected issue enums')
        ensure(span_valid(i.get('span'), case['input']['text']), case['id'] + ': invalid expected span')
        ensure(type(i.get('evidence_required')) is bool, 'evidence_required must be bool')
        ensure(string_list(i.get('acceptable_source_ids')), 'acceptable_source_ids must be unique strings')
        ensure(set(i['acceptable_source_ids']) <= set(source_map), 'Unknown expected source ID')
        ensure(set(i['acceptable_source_ids']) <= {s['id'] for s in case['input']['evidence']},
               'Expected provided source must be visible to application')
        ensure(not i['evidence_required'] or i['acceptable_source_ids'], 'Required evidence needs source IDs')
        ensure(string(i.get('rationale')) and string(i.get('revision_constraints')), 'Missing rationale/revision conditions')
    errors = any(i['status'] == 'error' for i in issues)
    expected_decision = 'has_errors' if errors else 'manual_review' if issues else 'clean'
    ensure(expected['decision'] == expected_decision, case['id'] + ': inconsistent expected decision')
    if any(i['status'] != 'error' or i['risk'] is None for i in issues):
        ensure(expected['needs_human_review'], 'Uncertain issue needs human review')


def validate(cases, sources):
    unique(cases)
    validate_sources(sources)
    smap = {s['id']: s for s in sources}
    for c in cases:
        ensure(c.get('version') == VERSION and c.get('review_status') in REVIEWS, c['id'] + ': bad version/review status')
        ensure(c.get('origin') in {'synthetic', 'authorized_real', 'expert_written'}, 'Unknown case origin')
        ensure(c.get('evaluation_track') in {'fixture', 'domain_candidate', 'domain'}, 'Unknown evaluation_track')
        ensure(string(c.get('group')), 'Missing group')
        for field in ('lineage_ids', 'near_duplicate_ids', 'holdout_source_ids'):
            ensure(string_list(c.get(field), field == 'lineage_ids'), c['id'] + ': invalid ' + field)
        inp = c.get('input')
        ensure(isinstance(inp, dict) and string(inp.get('text')), c['id'] + ': missing input text')
        ensure(not ({'expected', 'candidate_answer', 'gold'} & set(inp)), 'Labels leaked through input fields')
        ensure(inp.get('evidence_mode') in {'provided', 'retrieval'}, 'Unknown evidence mode')
        ensure(string(inp.get('instructions')) and string(inp.get('genre')) and string(inp.get('as_of')),
               'Input instructions/genre/as_of required')
        ensure(isinstance(inp.get('evidence'), list), 'Input evidence must be list')
        unique(inp['evidence'])
        for s in inp['evidence']:
            ensure(s.get('id') in smap and s == smap[s['id']], 'Input evidence differs from source registry')
        ensure(set(c['holdout_source_ids']) <= set(smap), 'Unknown holdout source')
        if c['evaluation_track'] == 'fixture':
            ensure(all(s['kind'] == 'synthetic_fixture' for s in inp['evidence']), 'Nonfixture source in fixture track')
        expected_valid(c, smap)
        ensure(string(c.get('difficulty_reason')) and string_list(c.get('tags'), True), 'Missing difficulty/tags')
        if c['review_status'] == 'approved':
            ensure(c['evaluation_track'] != 'domain_candidate', 'Approved domain case must be promoted from candidate to domain track')
            a = c.get('annotation', {})
            ensure(a.get('status') == 'approved' and len(a.get('reviewers', [])) >= 2
                   and all(string(x) for x in a['reviewers']) and len(set(a['reviewers'])) >= 2
                   and string(a.get('adjudicator')) and string(a.get('approved_at')), 'Approved case needs two reviewers/adjudication/date')
            if c['evaluation_track'] != 'fixture':
                required = {sid for i in c['expected']['issues'] if i['evidence_required']
                            for sid in i['acceptable_source_ids']}
                if c['expected']['decision'] == 'clean':
                    required |= {s['id'] for s in inp['evidence'] if s['kind'] == 'official_candidate'}
                ensure(all(smap[s]['authority_verified'] for s in required), 'Approved domain case has unverified required authority')
    return smap


def output_problems(output, text):
    problems = []
    if not isinstance(output, dict):
        return ['Output must be object']
    if output.get('decision') not in DECISIONS or type(output.get('needs_human_review')) is not bool:
        problems.append('Invalid decision/human flag')
    issues = output.get('issues')
    if not isinstance(issues, list):
        return problems + ['issues must be list']
    seen = set()
    for i in issues:
        if not isinstance(i, dict):
            problems.append('Issue must be object')
            continue
        iid = i.get('id')
        if not string(iid) or iid in seen:
            problems.append('Invalid/duplicate issue id')
        if string(iid):
            seen.add(iid)
        if i.get('type') not in TYPES or i.get('status') not in STATUSES or i.get('risk') not in RISKS:
            problems.append('Invalid issue type/status/risk')
        if not span_valid(i.get('span'), text):
            problems.append('Span not code point aligned with original quote')
        if not string(i.get('explanation')):
            problems.append('Missing explanation')
        e = i.get('evidence')
        if not isinstance(e, list) or any(not isinstance(x, dict) or not string(x.get('source_id'))
                                         or not string(x.get('quote')) or x.get('use') not in
                                         ('authoritative', 'candidate', 'provided_fixture') for x in e):
            problems.append('Invalid evidence shape')
        suggestion = i.get('suggestion')
        if not isinstance(suggestion, dict) or suggestion.get('action') not in ('replace', 'verify', 'retain') or not string(suggestion.get('text')):
            problems.append('Invalid suggestion')
    if not isinstance(output.get('limitations'), list) or any(not string(x) for x in output.get('limitations', [])):
        problems.append('Invalid limitations')
    if all(isinstance(i, dict) for i in issues):
        errors = any(i.get('status') == 'error' for i in issues)
        decision = 'has_errors' if errors else 'manual_review' if issues else 'clean'
        if output.get('decision') != decision:
            problems.append('Decision inconsistent with issues')
        if any(i.get('status') != 'error' or i.get('risk') is None for i in issues) and output.get('needs_human_review') is not True:
            problems.append('Uncertain issue without human review')
    return problems


def iou(a, b):
    overlap = max(0, min(a['end'], b['end']) - max(a['start'], b['start']))
    union = max(a['end'], b['end']) - min(a['start'], b['start'])
    return overlap / union if overlap else 0.0


def match(expected, predicted):
    edges = {}
    for ei, e in enumerate(expected):
        candidates = [(pi, iou(e['span'], p['span'])) for pi, p in enumerate(predicted)
                      if e['type'] == p['type'] and e['status'] == p['status']]
        edges[ei] = [pi for pi, overlap in sorted(candidates, key=lambda x: (-x[1], x[0])) if overlap >= .5]
    owners = {}
    def visit(ei, seen):
        for pi in edges[ei]:
            if pi in seen:
                continue
            seen.add(pi)
            if pi not in owners or visit(owners[pi], seen):
                owners[pi] = ei
                return True
        return False
    for ei in range(len(expected)):
        visit(ei, set())
    return sorted((ei, pi) for pi, ei in owners.items())


def source_groups(cases):
    parent = list(range(len(cases)))
    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    tokens = {}
    for n, c in enumerate(cases):
        keys = [('group', c['group'])]
        for field in ('lineage_ids', 'near_duplicate_ids', 'holdout_source_ids'):
            keys.extend((field, item) for item in c[field])
        keys.append(('exact_text', output_hash(c['input']['text'])))
        keys.append(('whitespace_normalized', output_hash(''.join(c['input']['text'].split()))))
        for key in keys:
            if key in tokens:
                parent[root(n)] = root(tokens[key])
            else:
                tokens[key] = n
    members = defaultdict(list)
    for n, c in enumerate(cases):
        members[root(n)].append(c['id'])
    result = {}
    for ids in members.values():
        gid = 'cluster-' + output_hash(sorted(ids))[:12]
        for cid in ids:
            result[cid] = gid
    return result


def split_proposal(cases, seed):
    groups = source_groups(cases)
    ordered = sorted(set(groups.values()), key=lambda g: hashlib.sha256((seed + ':' + g).encode()).hexdigest())
    dev_count, validation_count = math.floor(len(ordered)*.6), math.floor(len(ordered)*.2)
    assignments = {group: 'dev' if n < dev_count else 'validation_candidate'
                   if n < dev_count + validation_count else 'test_candidate'
                   for n, group in enumerate(ordered)}
    records = [dict(id=c['id'], source_cluster=groups[c['id']], proposed_split=assignments[groups[c['id']]],
                    current_use='public_dev_seed', tags=c['tags']) for c in cases]
    return dict(version=VERSION, status='proposal_only_not_independent_test', seed=seed,
                allocation_method='hash_order_whole_group_quota_60_20_20_not_stratified',
                limitations=['Small cluster count may leave a candidate set empty; manually stratify whole groups.',
                             'Public synthetic seeds remain development material regardless of proposal.'],
                groups=len(assignments), group_split_counts=dict(Counter(assignments.values())),
                split_counts=dict(Counter(r['proposed_split'] for r in records)), records=records)


def check_split(cases, manifest):
    mapping = source_groups(cases)
    records = manifest.get('records', [])
    unique(records)
    ensure({r['id'] for r in records} == set(mapping), 'Split manifest IDs differ from cases')
    seen = defaultdict(set)
    for r in records:
        ensure(r.get('proposed_split') in {'dev', 'validation_candidate', 'test_candidate'}, 'Invalid proposed split')
        ensure(r.get('source_cluster') == mapping[r['id']], 'Split cluster digest does not match current data')
        seen[mapping[r['id']]].add(r['proposed_split'])
    ensure(all(len(v) == 1 for v in seen.values()), 'Source or duplicate leakage across splits')


def ratio(num, denom):
    return dict(numerator=num, denominator=denom, value=num / denom if denom else None)


def auto_case(case, output, smap):
    exp, pred = case['expected']['issues'], output['issues']
    pairs = match(exp, pred)
    tp = sum(exp[e]['status'] == 'error' for e, _ in pairs)
    ne = sum(e['status'] == 'error' for e in exp)
    np = sum(p['status'] == 'error' for p in pred)
    exact = sum(exp[e]['span'] == pred[p]['span'] for e, p in pairs)
    exact_error = sum(exp[e]['status'] == 'error' and exp[e]['span'] == pred[p]['span'] for e, p in pairs)
    nknown = sum(e['risk'] is not None for e in exp)
    risk_correct = sum(exp[e]['risk'] is not None and exp[e]['risk'] == pred[p]['risk'] for e, p in pairs)
    nhigh = sum(e['risk'] == 'high' and e['status'] == 'error' for e in exp)
    high_detected = sum(exp[e]['risk'] == 'high' and exp[e]['status'] == 'error' for e, _ in pairs)
    high_with_risk = sum(exp[e]['risk'] == 'high' and exp[e]['status'] == 'error' and pred[p]['risk'] == 'high' for e, p in pairs)
    nwarn = sum(e['status'] != 'error' for e in exp)
    mwarn = sum(exp[e]['status'] != 'error' for e, _ in pairs)
    refs = [r for p in pred for r in p['evidence']]
    existing = sum(r['source_id'] in smap for r in refs)
    quote_exists = sum(r['source_id'] in smap and r['quote'] in smap[r['source_id']]['text'] for r in refs)
    bad_authority = sum(r['use'] == 'authoritative' and (r['source_id'] not in smap or not smap[r['source_id']]['authority_verified']) for r in refs)
    bad_fixture = sum(r['use'] == 'provided_fixture' and (r['source_id'] not in smap or smap[r['source_id']]['kind'] != 'synthetic_fixture') for r in refs)
    required = [e for e in exp if e['evidence_required']]
    supplied = 0
    for ei, pi in pairs:
        if exp[ei]['evidence_required'] and any(r['source_id'] in exp[ei]['acceptable_source_ids']
                and r['source_id'] in smap and r['quote'] in smap[r['source_id']]['text'] for r in pred[pi]['evidence']):
            supplied += 1
    clean = int(case['expected']['decision'] == 'clean')
    clean_false_alert = int(bool(clean) and (bool(pred) or output['decision'] != 'clean' or output['needs_human_review']))
    overassert = 0
    for p in pred:
        if p['status'] == 'error' and any(e['status'] != 'error' and iou(e['span'], p['span']) >= .5 for e in exp):
            overassert += 1
    issue_f1 = 2 * len(pairs) / (len(exp) + len(pred)) if exp or pred else 1.0
    loc = sum(iou(exp[e]['span'], pred[p]['span']) for e, p in pairs) / len(exp) if exp else float(not pred)
    risk = risk_correct / nknown if nknown else float(not pred) if not exp else None
    if nknown != len(exp):
        risk = None
    return dict(tp=tp, fp=np-tp, fn=ne-tp, expected_errors=ne, predicted_errors=np,
                matched=len(pairs), expected_issues=len(exp), predicted_issues=len(pred),
                exact_spans=exact, exact_error_spans=exact_error,
                iou_sum=sum(iou(exp[e]['span'], pred[p]['span']) for e, p in pairs),
                known_risks=nknown, matched_known_risks=sum(exp[e]['risk'] is not None for e, _ in pairs),
                risk_correct=risk_correct, high_errors=nhigh, high_detected=high_detected, high_with_risk=high_with_risk,
                uncertain_expected=nwarn, uncertain_matched=mwarn, overassertions=overassert,
                citations=len(refs), citation_ids_exist=existing, citation_quotes_exist=quote_exists,
                unverified_authority_claims=bad_authority, false_fixture_claims=bad_fixture,
                evidence_required=len(required), required_evidence_supplied=supplied,
                clean_cases=clean, clean_false_alerts=clean_false_alert,
                clean_error_alerts=int(bool(clean) and np > 0),
                clean_human_referrals=int(bool(clean) and output['needs_human_review']),
                decision_correct=int(output['decision'] == case['expected']['decision']
                                     and output['needs_human_review'] == case['expected']['needs_human_review']),
                matching=[dict(expected_id=exp[e]['id'], predicted_id=pred[p]['id'],
                               iou=iou(exp[e]['span'], pred[p]['span'])) for e, p in pairs],
                auxiliary_components=dict(identification=issue_f1, localization=loc, risk=risk))


def aggregate(records):
    totals = Counter()
    by_case = defaultdict(list)
    for r in records:
        if r.get('automatic'):
            by_case[r['id']].append(r['automatic'])
    for autos in by_case.values():
        subtotal = Counter()
        for auto in autos:
            subtotal.update({k: v for k, v in auto.items() if type(v) in (int, float)})
        totals.update({k: v / len(autos) for k, v in subtotal.items()})
    t = totals
    return dict(evaluated_successful_calls=len([r for r in records if r.get('automatic')]),
                unique_case_count=len(by_case),
                aggregation='average_counts_within_case_then_sum; partial runs are diagnostic only',
                error_precision=ratio(t['tp'], t['tp']+t['fp']), error_recall=ratio(t['tp'], t['tp']+t['fn']),
                error_f1=ratio(2*t['tp'], 2*t['tp']+t['fp']+t['fn']),
                exact_span_among_matches=ratio(t['exact_spans'], t['matched']),
                exact_error_localization_end_to_end=ratio(t['exact_error_spans'], t['expected_errors']),
                mean_iou_among_matches=ratio(t['iou_sum'], t['matched']),
                risk_accuracy_among_matched_known=ratio(t['risk_correct'], t['matched_known_risks']),
                risk_correct_end_to_end=ratio(t['risk_correct'], t['known_risks']),
                high_risk_error_detection_recall=ratio(t['high_detected'], t['high_errors']),
                high_risk_error_correct_grading_recall=ratio(t['high_with_risk'], t['high_errors']),
                uncertain_status_recall=ratio(t['uncertain_matched'], t['uncertain_expected']),
                clean_false_alert_rate=ratio(t['clean_false_alerts'], t['clean_cases']),
                clean_error_alert_rate=ratio(t['clean_error_alerts'], t['clean_cases']),
                clean_human_referral_rate=ratio(t['clean_human_referrals'], t['clean_cases']),
                citation_id_exists_rate=ratio(t['citation_ids_exist'], t['citations']),
                citation_quote_exists_rate=ratio(t['citation_quotes_exist'], t['citations']),
                required_evidence_present_rate=ratio(t['required_evidence_supplied'], t['evidence_required']),
                decision_and_human_flag_accuracy=ratio(t['decision_correct'], len(by_case)),
                unverified_authority_claims=t['unverified_authority_claims'],
                false_fixture_claims=t['false_fixture_claims'], overassertions_on_uncertain_items=t['overassertions'])


def score(cases, sources, predictions, manual=None, allow_pending=False):
    smap = validate(cases, sources)
    all_approved = all(c['review_status'] == 'approved' for c in cases)
    ensure(all_approved or allow_pending, 'Pending/rejected labels: scoring refused; use --allow-pending ONLY for pipeline checks')
    ensure(not any(c['review_status'] == 'rejected' for c in cases), 'Rejected cases cannot be scored')
    synthetic_predictions = any(p.get('provenance') == 'synthetic_candidate' or p.get('review_status') == 'pending_review' for p in predictions)
    ensure(not synthetic_predictions or allow_pending, 'Pending synthetic candidates cannot be a formal system baseline')
    formal = all_approved and not allow_pending and not synthetic_predictions
    by_id = {c['id']: c for c in cases}
    keys, counts = set(), Counter()
    for p in predictions:
        ensure(p.get('error') is None or isinstance(p.get('error'), dict) and bool(p['error']), 'error must be null or nonempty object')
        ensure(p.get('id') in by_id and type(p.get('repeat')) is int and p['repeat'] > 0, 'Unknown case or invalid repeat')
        key = (p['id'], p['repeat'])
        ensure(key not in keys, 'Duplicate prediction case/repeat')
        keys.add(key)
        counts[p['id']] += 1
    ensure(set(counts) == set(by_id), 'Missing predictions; include explicit error rows rather than dropping cases')
    ensure(len(set(counts.values())) == 1, 'Repeat counts differ by case')
    expected_repeats = set(range(1, next(iter(counts.values())) + 1))
    ensure(all({p['repeat'] for p in predictions if p['id'] == cid} == expected_repeats for cid in by_id), 'Repeats must be contiguous from 1')
    reviews = {}
    for m in manual or []:
        key = (m.get('id'), m.get('repeat'))
        ensure(key in keys and key not in reviews, 'Unknown/duplicate manual review')
        ensure(m.get('review_status') in REVIEWS, 'Invalid manual review status')
        ensure(string(m.get('output_sha256')), 'Manual review must bind output hash')
        if m['review_status'] == 'approved':
            ensure(string(m.get('reviewer')) and string(m.get('reviewed_at')) and string(m.get('reason')), 'Manual approval needs reviewer/date/reason')
            ensure(type(m.get('evidence_score')) is int and m['evidence_score'] in (0, 1, 2)
                   and type(m.get('revision_score')) is int and m['revision_score'] in (0, 1, 2), 'Manual scores must be integer 0/1/2')
            ensure(type(m.get('new_error')) is bool and type(m.get('fabricated_authority')) is bool, 'Manual guardrail booleans required')
        reviews[key] = m
    records = []
    for p in sorted(predictions, key=lambda x: (x['id'], x['repeat'])):
        c = by_id[p['id']]
        out = p.get('output')
        actual_hash = output_hash(out)
        if p.get('output_sha256') is not None:
            ensure(p['output_sha256'] == actual_hash, 'Prediction output hash mismatch')
        m = reviews.get((p['id'], p['repeat']))
        if m:
            ensure(m['output_sha256'] == actual_hash, 'Manual review hash mismatch')
        problems = output_problems(out, c['input']['text']) if p.get('error') is None else []
        error = p.get('error') or (dict(phase='output_protocol', messages=problems) if problems else None)
        auto = auto_case(c, out, smap) if error is None else None
        composite = None
        if auto and m and m['review_status'] == 'approved' and formal:
            a = auto['auxiliary_components']
            if a['risk'] is not None:
                composite = .25*a['identification'] + .15*a['localization'] + .15*a['risk'] + .2*m['evidence_score']/2 + .15*m['revision_score']/2 + .1*auto['decision_correct']
        records.append(dict(id=p['id'], repeat=p['repeat'], group=c['group'], evaluation_track=c['evaluation_track'],
                            genre=c['input']['genre'], evidence_mode=c['input']['evidence_mode'],
                            tags=c['tags'], output=out, output_sha256=actual_hash, error=error, automatic=auto,
                            human_review=m, auxiliary_composite=composite,
                            latency_seconds=p.get('latency_seconds'), usage=p.get('usage')))
    grouped = defaultdict(list)
    for r in records:
        grouped[r['id']].append(r)
    per_case = {cid: sum(r['auxiliary_composite'] for r in rr)/len(rr)
                for cid, rr in grouped.items() if all(r['auxiliary_composite'] is not None for r in rr)}
    errors = sum(r['error'] is not None for r in records)
    complete = not errors and len(per_case) == len(cases) and formal
    sg = source_groups(cases)
    cluster_scores = defaultdict(list)
    for cid, val in per_case.items():
        cluster_scores[sg[cid]].append(val)
    cost_known = all(isinstance(r['usage'], dict) and number(r['usage'].get('cost'))
                     and string(r['usage'].get('cost_unit')) for r in records)
    units = {r['usage']['cost_unit'] for r in records} if cost_known else set()
    cost_known = cost_known and len(units) == 1
    latencies = [r['latency_seconds'] for r in records if number(r['latency_seconds'])]
    slices = {}
    for field in ('evaluation_track', 'evidence_mode', 'genre', 'group', 'tags'):
        vals = sorted({v for r in records for v in (r[field] if field == 'tags' else [r[field]])})
        slices[field] = {v: aggregate([r for r in records if v in r['tags']]) if field == 'tags'
                        else aggregate([r for r in records if r[field] == v]) for v in vals}
    mixed_tracks = len({(r['evaluation_track'], r['evidence_mode']) for r in records}) > 1
    per_track_composite = {}
    for track, mode in sorted({(r['evaluation_track'], r['evidence_mode']) for r in records}):
        subset_ids = {r['id'] for r in records if r['evaluation_track'] == track and r['evidence_mode'] == mode}
        ready = all(cid in per_case for cid in subset_ids) and not any(r['error'] for r in records if r['id'] in subset_ids)
        per_track_composite[track + '/' + mode] = sum(per_case[cid] for cid in subset_ids)/len(subset_ids) if ready else None
    approved_reviews = [m for m in reviews.values() if m['review_status'] == 'approved']
    return dict(version=VERSION, status='approved_data_scoring' if formal else 'pipeline_only',
                scope='Successful-call automatic metrics are diagnostic; no semantic authority or revision judgment implied.',
                summary=dict(cases=len(cases), source_clusters=len(set(sg.values())), repeats=next(iter(counts.values())),
                             calls=len(records), errors=errors, pending_cases=sum(c['review_status'] != 'approved' for c in cases),
                             approved_output_reviews=sum(m['review_status'] == 'approved' for m in reviews.values()),
                             auxiliary_case_macro_score=sum(per_case.values())/len(cases) if complete and not mixed_tracks else None,
                             auxiliary_source_macro_score=sum(sum(v)/len(v) for v in cluster_scores.values())/len(cluster_scores) if complete and not mixed_tracks else None,
                             auxiliary_macro_by_track=per_track_composite,
                             mixed_tracks_require_separate_reporting=mixed_tracks,
                             expert_confirmed_new_errors=sum(m['new_error'] for m in approved_reviews),
                             expert_confirmed_fabricated_authority=sum(m['fabricated_authority'] for m in approved_reviews),
                             total_cost=sum(r['usage']['cost'] for r in records) if cost_known else None,
                             cost_unit=next(iter(units)) if cost_known else None,
                             mean_adapter_wall_seconds=sum(latencies)/len(latencies) if len(latencies) == len(records) else None,
                             acceptance_decision='not_issued_requires_expert_and_sampling_review'),
                automatic_diagnostics=aggregate(records), slices=slices, records=records)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('validate', 'split', 'score'):
        p = sub.add_parser(name)
        p.add_argument('--cases', type=Path, required=True)
        p.add_argument('--sources', type=Path, required=True)
        if name == 'validate':
            p.add_argument('--split-manifest', type=Path)
        if name == 'split':
            p.add_argument('--seed', default='sz-v0.1-proposal')
            p.add_argument('--out', type=Path, required=True)
        if name == 'score':
            p.add_argument('--predictions', type=Path, required=True)
            p.add_argument('--manual', type=Path)
            p.add_argument('--allow-pending', action='store_true')
            p.add_argument('--out', type=Path, required=True)
    p = sub.add_parser('freeze')
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--source-document', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'freeze':
            root = args.root.resolve()
            out = args.out.resolve()
            files = {str(p.relative_to(root)): file_hash(p) for p in sorted(root.rglob('*'))
                     if p.is_file() and p.resolve() != out and '__pycache__' not in p.parts and '.git' not in p.parts
                     and p.suffix != '.pyc' and 'results' not in p.relative_to(root).parts}
            save(out, dict(version=VERSION, status='draft_artifact_fingerprint_not_frozen_gold',
                           files=files, source_document=dict(name=args.source_document.name, sha256=file_hash(args.source_document)),
                           missing=['expert approval', 'verified authority snapshots', 'independent final test', 'application baseline']))
            print(json.dumps(dict(fingerprinted_files=len(files), status='draft_not_frozen_gold'), ensure_ascii=False))
            return 0
        cases, sources = rows(args.cases), rows(args.sources)
        validate(cases, sources)
        if args.command == 'validate':
            if args.split_manifest:
                check_split(cases, loads(args.split_manifest.read_text(encoding='utf-8')))
            result = dict(valid=True, cases=len(cases), sources=len(sources), source_clusters=len(set(source_groups(cases).values())),
                          review_status_counts=dict(Counter(c['review_status'] for c in cases)), performance_evaluation=False)
        elif args.command == 'split':
            result = split_proposal(cases, args.seed)
            check_split(cases, result)
            save(args.out, result)
            result = {k: v for k, v in result.items() if k != 'records'}
        else:
            result = score(cases, sources, rows(args.predictions), rows(args.manual) if args.manual else None, args.allow_pending)
            result['identity'] = dict(cases_sha256=file_hash(args.cases), sources_sha256=file_hash(args.sources),
                                      predictions_sha256=file_hash(args.predictions), scorer_sha256=file_hash(__file__),
                                      manual_sha256=file_hash(args.manual) if args.manual else None)
            save(args.out, result)
            result = dict(status=result['status'], summary=result['summary'], detail_file=str(args.out))
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 2 if args.command == 'score' and result['summary']['errors'] else 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    sys.exit(main())
