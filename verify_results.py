"""Verify the saved experiment evidence without starting Kubernetes."""

import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(path):
    return json.loads((ROOT / path).read_text(encoding='utf-8'))


def logged_json(path):
    return json.loads((ROOT / path).read_text(encoding='utf-8').split('\n', 1)[1])


def job_events(name):
    pods = read(f'logs/{name}-pods.json')
    manifest = read(f'manifests/{name}.json')
    events = []
    for pod in pods:
        assert pod['metadata']['labels']['job-name'] == name
        actual = pod['spec']['containers'][0]
        expected = manifest['spec']['template']['spec']['containers'][0]
        for key in ('image', 'args', 'resources'):
            assert actual[key] == expected[key], (name, key)
        state = pod['status']['containerStatuses'][0]['state']['terminated']
        assert state['startedAt'] <= state['finishedAt']
        assert state['reason'] == ('OOMKilled' if name == 'pairs-oom' else 'Completed')
        assert state['exitCode'] == (137 if name == 'pairs-oom' else 0)
        path = ROOT / f'logs/{name}-{pod["metadata"]["name"]}.txt'
        events.extend(json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
                      if line.startswith('{'))
    return pods, events


def main():
    summary = read('logs/summary.json')
    reference = read('reference/lab3_pairs.json')
    actual = read('results/pairs.json')
    assert actual['counts'] == reference['counts'], 'Full pair counts differ'
    assert actual['vacancies'] == reference['vacancies'] == 6215
    assert actual['unique_pairs'] == len(actual['counts']) == 25462
    assert actual['pair_occurrences'] == sum(r['count'] for r in actual['counts']) == 173204
    digest = hashlib.sha256(json.dumps(reference['counts'], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    for run in summary['profiles'] + [summary['sized'], summary['parallel']]:
        assert run['sha256'] == digest
        assert run['top10'] == reference['counts'][:10]
    assert summary['oom']['reason'] == 'OOMKilled' and summary['oom']['exitCode'] == 137
    assert summary['oom']['startedAt'] <= summary['oom']['finishedAt']
    assert summary['limit_mib'] * 2**20 >= max(p['cgroup_peak_bytes'] for p in summary['profiles']) * 1.25
    assert summary['sized']['cgroup_peak_bytes'] < summary['limit_mib'] * 2**20
    assert any('Insufficient cpu' in c.get('message', '') for c in summary['waiting']['conditions'])
    assert any(e['reason'] == 'Preempted' and e['involvedObject']['name'] == 'low' for e in summary['preemption'])
    assert 'exceeded quota' in summary['quota_rejection']
    assert len(summary['parts']) == 4
    assert sum(p['vacancies'] for p in summary['parts']) == actual['vacancies']
    assert summary['ranges'][0]['start'] == 0
    assert all(a['end'] + 1 == b['start'] for a, b in zip(summary['ranges'], summary['ranges'][1:]))
    assert summary['ranges'][-1]['end'] + 1 == (ROOT / 'data/vacancies.ndjson').stat().st_size
    assert hashlib.sha256((ROOT / 'data/vacancies.ndjson').read_bytes()).hexdigest() == summary['upload']['sha256']
    assert actual['sha256'] == summary['expected_sha256'] == digest
    assert summary['limit_mib'] == math.ceil(max(p['cgroup_peak_bytes'] for p in summary['profiles']) * 1.25 / 2**24) * 16

    pods, events = job_events('pairs-oom')
    assert pods[0]['status']['containerStatuses'][0]['state']['terminated'] == summary['oom']
    assert any(e['event'] == 'started' for e in events)
    for name, expected in [('pairs-sized', summary['sized']), ('pairs-reduce', summary['parallel'])] + [
            (f'pairs-profile-{i}', p) for i, p in enumerate(summary['profiles'])]:
        _, events = job_events(name)
        assert [e for e in events if e['event'] == 'completed'] == [expected]
    _, events = job_events('upload')
    assert [e for e in events if e['event'] == 'uploaded'] == [summary['upload']]
    pods, events = job_events('pairs-parallel')
    assert len(pods) == 4
    assert [e for e in events if e['event'] == 'completed'] == summary['parts']
    ranges = sorted((e for e in events if e['event'] == 'range'), key=lambda e: e['index'])
    for logged, saved in zip(ranges, summary['ranges']):
        assert all(logged[k] == saved[k] for k in ('index', 'start', 'end'))
    assert len(ranges) == 4
    for event in summary['preemption']:
        assert event in logged_json('logs/preemption-events.txt')['items']
    assert summary['quota_rejection'] in (ROOT / 'logs/apply-quota-rejected.txt').read_text(encoding='utf-8')
    assert summary['waiting']['phase'] == 'Pending'
    assert 'Insufficient cpu' in (ROOT / 'logs/cpu-waiting.txt').read_text(encoding='utf-8')
    versions = logged_json('logs/versions.txt')
    assert versions['clientVersion']['minor'] == versions['serverVersion']['minor']
    provenance = read('reference/provenance.json')
    for path, sha in provenance['files'].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == sha
    print('PASS: OOMKilled, measured memory limit, CPU Pending, preemption, quota, all 25462 pairs and top-10')
    print('SHA256:', digest)


if __name__ == '__main__':
    main()
