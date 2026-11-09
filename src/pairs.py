"""Count DevOps skill pairs locally or over disjoint S3 byte ranges."""

import argparse
from collections import Counter
import hashlib
from itertools import combinations
import json
import os
from pathlib import Path
import time

import boto3
from botocore.config import Config


def s3_client():
    return boto3.client('s3', endpoint_url=os.environ['S3_ENDPOINT'],
                        region_name='us-east-1',
                        config=Config(s3={'addressing_style': 'path'}))


def read_range(s3, bucket, key, start, end, size):
    """Assign a record to the range containing its first byte (inclusive bounds)."""
    if start >= size or start > end:
        return b''

    def fetch(left, right):
        response = s3.get_object(Bucket=bucket, Key=key, Range=f'bytes={left}-{right}')
        with response['Body'] as stream:
            return stream.read()

    body = bytearray(fetch(max(0, start - 1), end))
    position = end + 1
    while body[-1:] != b'\n' and position < size:
        block = fetch(position, min(size - 1, position + 65535))
        if not block:
            raise IOError('Unexpected empty S3 range response')
        newline = block.find(b'\n')
        body.extend(block if newline < 0 else block[:newline + 1])
        position += len(block)
        if newline >= 0:
            break
    if start:
        newline = body.find(b'\n')
        return bytes(body[newline + 1:]) if newline >= 0 else b''
    return bytes(body)


def count_pairs(data):
    counts = Counter()
    rows = 0
    for line in data.splitlines():
        if not line.strip():
            continue
        skills = json.loads(line)['skills']
        if not isinstance(skills, list) or any(not isinstance(s, str) for s in skills):
            raise ValueError('skills must be a list of strings')
        counts.update(combinations(sorted(set(skills)), 2))
        rows += 1
    return rows, counts


def result(rows, counts):
    records = [{'skills': list(pair), 'count': count}
               for pair, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]
    canonical = json.dumps(records, ensure_ascii=False, separators=(',', ':')).encode()
    return {'vacancies': rows, 'unique_pairs': len(counts), 'pair_occurrences': sum(counts.values()),
            'sha256': hashlib.sha256(canonical).hexdigest(), 'counts': records}


def put_json(s3, key, document):
    s3.put_object(Bucket='hh-lake', Key=key,
                  Body=json.dumps(document, ensure_ascii=False).encode(), ContentType='application/json')


def main():
    import resource

    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['local', 'upload', 'map', 'reduce', 'hold'])
    parser.add_argument('--parts', type=int, default=4)
    parser.add_argument('--prefix', default='runs/parallel')
    args = parser.parse_args()
    if args.parts < 1:
        parser.error('--parts must be positive')
    started = time.monotonic()
    print(json.dumps({'event': 'started', 'mode': args.mode}), flush=True)
    if args.mode == 'hold':
        time.sleep(600)
        return
    if args.mode == 'upload':
        s3 = s3_client()
        s3.create_bucket(Bucket='hh-lake')
        data = Path('/data/vacancies.ndjson').read_bytes()
        s3.put_object(Bucket='hh-lake', Key='input/vacancies.ndjson', Body=data)
        print(json.dumps({'event': 'uploaded', 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}))
        return
    if args.mode == 'local':
        rows, counts = count_pairs(Path('/data/vacancies.ndjson').read_bytes())
    elif args.mode == 'map':
        s3 = s3_client()
        index = int(os.environ['JOB_COMPLETION_INDEX'])
        if not 0 <= index < args.parts:
            raise ValueError('Invalid completion index')
        size = s3.head_object(Bucket='hh-lake', Key='input/vacancies.ndjson')['ContentLength']
        start, end = size * index // args.parts, size * (index + 1) // args.parts - 1
        rows, counts = count_pairs(read_range(s3, 'hh-lake', 'input/vacancies.ndjson', start, end, size))
        partial = result(rows, counts)
        partial.update(index=index, start=start, end=end)
        # Retries replace the same part; the reducer reads each index exactly once.
        put_json(s3, f'{args.prefix}/part-{index}.json', partial)
        print(json.dumps({'event': 'range', 'index': index, 'start': start, 'end': end}))
    else:
        s3 = s3_client()
        rows, counts = 0, Counter()
        for index in range(args.parts):
            response = s3.get_object(Bucket='hh-lake', Key=f'{args.prefix}/part-{index}.json')
            with response['Body'] as stream:
                part = json.load(stream)
            if part['index'] != index:
                raise ValueError('Part index mismatch')
            rows += part['vacancies']
            counts.update({tuple(row['skills']): row['count'] for row in part['counts']})
    document = result(rows, counts)
    if args.mode == 'reduce':
        put_json(s3, f'{args.prefix}/pairs.json', document)
    document.pop('counts')
    document['top10'] = result(rows, counts)['counts'][:10]
    document.update(event='completed', mode=args.mode,
                    elapsed_seconds=round(time.monotonic() - started, 6),
                    peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)
    peak_path = Path('/sys/fs/cgroup/memory.peak')
    if peak_path.exists():
        document['cgroup_peak_bytes'] = int(peak_path.read_text())
    print(json.dumps(document, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
