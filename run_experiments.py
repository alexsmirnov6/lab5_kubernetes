"""Run the lab in kind-lab5 and preserve commands, responses and assertions."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / 'logs'
K = [str(ROOT / 'tools/kubectl.exe'), '--context', 'kind-lab5']
NS = 'lab5'


def run(args, name, check=True):
    completed = subprocess.run(args, cwd=ROOT, capture_output=True, encoding='utf-8', errors='replace')
    (LOGS / f'{name}.txt').write_text(
        '$ ' + subprocess.list2cmdline(args) + '\n' + completed.stdout + completed.stderr,
        encoding='utf-8')
    if check and completed.returncode:
        raise RuntimeError(f'{name}: {completed.stderr}')
    return completed


def get(kind, name=None, namespace=NS):
    args = K + ['get', kind] + ([name] if name else []) + ['-n', namespace, '-o', 'json']
    p = subprocess.run(args, capture_output=True, encoding='utf-8')
    if p.returncode:
        raise RuntimeError(p.stderr)
    return json.loads(p.stdout)


def apply(name, obj, check=True):
    path = ROOT / 'manifests' / f'{name}.json'
    path.write_text(json.dumps(obj, indent=2) + '\n', encoding='utf-8')
    return run(K + ['apply', '-f', str(path)], f'apply-{name}', check)


def wait_for(description, predicate, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(1)
    raise TimeoutError(description)


def pod_spec(mode, memory='256Mi', cpu='250m', priority=None):
    spec = {'restartPolicy': 'Never', 'terminationGracePeriodSeconds': 3,
            'nodeSelector': {'lab': 'compute'},
            'containers': [{'name': 'pairs', 'image': 'lab5-pairs:1', 'imagePullPolicy': 'Never',
                            'args': [mode], 'resources': {
                                'requests': {'cpu': cpu, 'memory': min_memory(memory)},
                                'limits': {'cpu': cpu, 'memory': memory}},
                            'env': [{'name': 'S3_ENDPOINT', 'value': 'http://rustfs:9000'},
                                    {'name': 'AWS_ACCESS_KEY_ID', 'value': 'lab5-access'},
                                    {'name': 'AWS_SECRET_ACCESS_KEY', 'value': 'lab5-local-secret'}]}]}
    if priority:
        spec['priorityClassName'] = priority
    return spec


def min_memory(limit):
    return f'{min(int(limit[:-2]), 64)}Mi'


def job(name, mode, memory='256Mi', parts=None):
    spec = {'backoffLimit': 0, 'template': {'spec': pod_spec(mode, memory)}}
    if parts:
        spec.update(completions=parts, parallelism=parts, completionMode='Indexed')
        spec['template']['spec']['containers'][0]['args'] += ['--parts', str(parts)]
    apply(name, {'apiVersion': 'batch/v1', 'kind': 'Job',
                 'metadata': {'name': name, 'namespace': NS}, 'spec': spec})
    wait_for(name, lambda: get('jobs', name).get('status', {}).get('succeeded', 0) == (parts or 1)
             or get('jobs', name).get('status', {}).get('failed', 0))
    pods = [p for p in get('pods')['items'] if p['metadata'].get('labels', {}).get('job-name') == name]
    (LOGS / f'{name}-pods.json').write_text(json.dumps(pods, indent=2), encoding='utf-8')
    events = []
    for p in pods:
        out = run(K + ['logs', p['metadata']['name'], '-n', NS], f'{name}-{p["metadata"]["name"]}', False)
        for line in out.stdout.splitlines():
            if line.startswith('{'):
                events.append(json.loads(line))
    return pods, events


def main():
    LOGS.mkdir(exist_ok=True)
    if subprocess.run(K + ['get', 'namespace', NS], capture_output=True).returncode == 0:
        raise RuntimeError('Namespace lab5 exists. Archive logs and delete only this lab namespace before rerunning.')
    run(K + ['version', '-o', 'json'], 'versions')
    run(K + ['get', 'nodes', '-o', 'json'], 'nodes')
    apply('namespace', {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': NS}})
    summary = {'started_utc': datetime.now(timezone.utc).isoformat()}
    # An intentionally insufficient limit must kill the actual calculation process.
    pods, oom_events = job('pairs-oom', 'local', '40Mi')
    terminated = pods[0]['status']['containerStatuses'][0]['state']['terminated']
    assert terminated['reason'] == 'OOMKilled', terminated
    assert terminated['startedAt'] <= terminated['finishedAt'], 'Inconsistent container timestamps'
    assert any(e['event'] == 'started' for e in oom_events), 'OOM occurred before the calculation started'
    summary['oom'] = terminated
    baseline = json.loads((ROOT / 'reference/lab3_pairs.json').read_text(encoding='utf-8'))
    canonical = json.dumps(baseline['counts'], ensure_ascii=False, separators=(',', ':')).encode()
    expected = hashlib.sha256(canonical).hexdigest()
    measurements = []
    for i in range(3):
        _, events = job(f'pairs-profile-{i}', 'local')
        done = next(e for e in events if e['event'] == 'completed')
        assert done['sha256'] == expected
        measurements.append(done)
    peak = max(e['cgroup_peak_bytes'] for e in measurements)
    limit = math.ceil(peak * 1.25 / (16 * 1024**2)) * 16
    _, events = job('pairs-sized', 'local', f'{limit}Mi')
    sized = next(e for e in events if e['event'] == 'completed')
    assert sized['sha256'] == expected and sized['top10'] == baseline['counts'][:10]
    summary.update(profiles=measurements, limit_mib=limit, sized=sized, expected_sha256=expected)
    (LOGS / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')

    apply('rustfs-pvc', {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim',
        'metadata': {'name': 'rustfs-data', 'namespace': NS},
        'spec': {'accessModes': ['ReadWriteOnce'], 'resources': {'requests': {'storage': '1Gi'}}}})
    apply('rustfs', {'apiVersion': 'apps/v1', 'kind': 'Deployment',
        'metadata': {'name': 'rustfs', 'namespace': NS},
        'spec': {'replicas': 1, 'strategy': {'type': 'Recreate'}, 'selector': {'matchLabels': {'app': 'rustfs'}},
         'template': {'metadata': {'labels': {'app': 'rustfs'}}, 'spec': {
          'nodeSelector': {'lab': 'compute'}, 'securityContext': {'fsGroup': 1000},
          'containers': [{'name': 'rustfs', 'image': 'rustfs/rustfs:1.0.0', 'imagePullPolicy': 'Never',
           'args': ['/data'], 'env': [{'name': 'RUSTFS_ACCESS_KEY', 'value': 'lab5-access'},
            {'name': 'RUSTFS_SECRET_KEY', 'value': 'lab5-local-secret'}, {'name': 'RUSTFS_CONSOLE_ENABLE', 'value': 'false'}],
           'resources': {'requests': {'cpu': '250m', 'memory': '256Mi'}, 'limits': {'cpu': '500m', 'memory': '1Gi'}},
           'readinessProbe': {'tcpSocket': {'port': 9000}, 'initialDelaySeconds': 5, 'periodSeconds': 2},
           'volumeMounts': [{'name': 'data', 'mountPath': '/data'}]}],
          'volumes': [{'name': 'data', 'persistentVolumeClaim': {'claimName': 'rustfs-data'}}]}}}})
    apply('rustfs-service', {'apiVersion': 'v1', 'kind': 'Service',
        'metadata': {'name': 'rustfs', 'namespace': NS},
        'spec': {'selector': {'app': 'rustfs'}, 'ports': [{'port': 9000, 'targetPort': 9000}]}})
    run(K + ['rollout', 'status', 'deployment/rustfs', '-n', NS, '--timeout=180s'], 'rustfs-ready')
    _, events = job('upload', 'upload')
    assert any(e['event'] == 'uploaded' for e in events)
    summary['upload'] = next(e for e in events if e['event'] == 'uploaded')
    pods, events = job('pairs-parallel', 'map', '128Mi', parts=4)
    summary['parts'] = [e for e in events if e['event'] == 'completed']
    assert len(summary['parts']) == 4
    _, events = job('pairs-reduce', 'reduce', '128Mi')
    reduced = next(e for e in events if e['event'] == 'completed')
    assert reduced['sha256'] == expected and reduced['top10'] == baseline['counts'][:10]
    assert reduced['vacancies'] == baseline['vacancies']
    summary['parallel'] = reduced
    # Download the published object, rather than trusting only the reducer's log.
    with (LOGS / 'port-forward.txt').open('w', encoding='utf-8') as output:
        forward = subprocess.Popen(K + ['port-forward', '-n', NS, 'service/rustfs', '19100:9000'],
                                   stdout=output, stderr=output)
        try:
            import socket

            def ready():
                try:
                    with socket.create_connection(('127.0.0.1', 19100), timeout=1):
                        return True
                except OSError:
                    return False

            wait_for('S3 port-forward', ready, 30)
            s3 = boto3.client('s3', endpoint_url='http://127.0.0.1:19100',
                              aws_access_key_id='lab5-access', aws_secret_access_key='lab5-local-secret',
                              region_name='us-east-1', config=Config(s3={'addressing_style': 'path'}))
            response = s3.get_object(Bucket='hh-lake', Key='runs/parallel/pairs.json')
            with response['Body'] as stream:
                published = json.load(stream)
            assert published['counts'] == baseline['counts']
            (ROOT / 'results').mkdir(exist_ok=True)
            (ROOT / 'results/pairs.json').write_text(json.dumps(published, ensure_ascii=False, indent=2), encoding='utf-8')
            objects = s3.list_objects_v2(Bucket='hh-lake')['Contents']
            summary['s3_objects'] = [{'key': o['Key'], 'bytes': o['Size']} for o in objects]
            ranges = []
            for i in range(4):
                response = s3.get_object(Bucket='hh-lake', Key=f'runs/parallel/part-{i}.json')
                with response['Body'] as stream:
                    part = json.load(stream)
                ranges.append({k: part[k] for k in ['index', 'start', 'end', 'vacancies', 'unique_pairs']})
            assert ranges[0]['start'] == 0
            assert all(a['end'] + 1 == b['start'] for a, b in zip(ranges, ranges[1:]))
            assert ranges[-1]['end'] + 1 == summary['upload']['bytes']
            summary['ranges'] = ranges
            summary['all_pairs_equal'] = True
        finally:
            forward.terminate()
            forward.wait(timeout=10)

    for name, value in [('lab5-low', 10), ('lab5-high', 100)]:
        apply(name, {'apiVersion': 'scheduling.k8s.io/v1', 'kind': 'PriorityClass',
                     'metadata': {'name': name}, 'value': value, 'globalDefault': False,
                     'preemptionPolicy': 'PreemptLowerPriority'})
    for name in ['low', 'waiting']:
        apply(name, {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': NS},
                     'spec': pod_spec('hold', '64Mi', '1200m', 'lab5-low')})
        if name == 'low':
            wait_for('low running', lambda: get('pod', name)['status']['phase'] == 'Running')
    wait_for('CPU shortage', lambda: any('Insufficient cpu' in c.get('message', '')
             for c in get('pod', 'waiting')['status'].get('conditions', [])))
    run(K + ['describe', 'pod', 'waiting', '-n', NS], 'cpu-waiting')
    summary['waiting'] = get('pod', 'waiting')['status']
    apply('high', {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'high', 'namespace': NS},
                   'spec': pod_spec('hold', '64Mi', '1200m', 'lab5-high')})
    wait_for('high running', lambda: get('pod', 'high')['status']['phase'] == 'Running')
    run(K + ['get', 'events', '-n', NS, '-o', 'json'], 'preemption-events')
    events = json.loads((LOGS / 'preemption-events.txt').read_text().split('\n', 1)[1])
    assert any(e['reason'] == 'Preempted' and e['involvedObject']['name'] == 'low' for e in events['items'])
    summary['preemption'] = [e for e in events['items'] if e['reason'] == 'Preempted']
    run(K + ['get', 'pods', '-n', NS, '-o', 'wide'], 'after-preemption')
    run(K + ['delete', 'pod', 'high', 'waiting', '-n', NS, '--wait=true'], 'release-cpu')

    apply('quota', {'apiVersion': 'v1', 'kind': 'ResourceQuota',
        'metadata': {'name': 'compute-budget', 'namespace': NS},
        'spec': {'hard': {'requests.cpu': '750m', 'requests.memory': '512Mi', 'limits.memory': '2Gi', 'pods': '3'}}})
    wait_for('quota controller', lambda: 'hard' in get('resourcequota', 'compute-budget').get('status', {}))
    apply('quota-ok', {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'quota-ok', 'namespace': NS},
                      'spec': pod_spec('hold', '64Mi', '400m')})
    wait_for('quota usage', lambda: get('resourcequota', 'compute-budget').get('status', {}).get('used', {}).get('requests.cpu') == '650m')
    rejected = apply('quota-rejected', {'apiVersion': 'v1', 'kind': 'Pod',
        'metadata': {'name': 'quota-rejected', 'namespace': NS}, 'spec': pod_spec('hold', '64Mi', '400m')}, check=False)
    assert rejected.returncode and 'exceeded quota' in rejected.stderr
    run(K + ['describe', 'resourcequota', '-n', NS], 'quota-status')
    summary['quota_rejection'] = rejected.stderr
    run(K + ['get', 'all,pvc', '-n', NS, '-o', 'wide'], 'final-resources')
    run(K + ['get', 'events', '-n', NS, '--sort-by=.metadata.creationTimestamp'], 'events')
    summary['finished_utc'] = datetime.now(timezone.utc).isoformat()
    (LOGS / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'verified': True, 'limit_mib': limit, 'sha256': expected}))


if __name__ == '__main__':
    main()
