import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from pairs import count_pairs, read_range, result


class FakeS3:
    def __init__(self, data):
        self.data = data

    def get_object(self, **kwargs):
        left, right = map(int, kwargs['Range'][6:].split('-'))
        return {'Body': io.BytesIO(self.data[left:right + 1])}


class PairTests(unittest.TestCase):
    def test_every_boundary_including_unterminated_last_record(self):
        data = b'{"skills":["A","B"]}\n{"skills":["B","C"]}\n{"skills":["A","C"]}'
        s3 = FakeS3(data)
        for split in range(1, len(data)):
            left = read_range(s3, 'b', 'k', 0, split - 1, len(data))
            right = read_range(s3, 'b', 'k', split, len(data) - 1, len(data))
            self.assertEqual(left + right, data)

    def test_long_record_and_ranges_wholly_inside_record(self):
        data = json.dumps({'skills': ['A', 'B'], 'text': 'x' * 150000}).encode() + b'\n'
        s3 = FakeS3(data)
        parts = [read_range(s3, 'b', 'k', i, min(len(data)-1, i+999), len(data))
                 for i in range(0, len(data), 1000)]
        self.assertEqual(b''.join(parts), data)
        self.assertEqual(sum(bool(p) for p in parts), 1)

    def test_unique_skills_and_deterministic_order(self):
        rows, counts = count_pairs(b'{"skills":["B","A","A","C"]}\n{"skills":[]}')
        self.assertEqual(rows, 2)
        self.assertEqual(result(rows, counts)['counts'], [
            {'skills': ['A', 'B'], 'count': 1}, {'skills': ['A', 'C'], 'count': 1},
            {'skills': ['B', 'C'], 'count': 1}])

    def test_invalid_skills_rejected(self):
        for value in ['bad', [1]]:
            with self.assertRaises(ValueError):
                count_pairs(json.dumps({'skills': value}).encode())

    def test_full_lab3_result(self):
        root = Path(__file__).resolve().parents[1]
        rows, counts = count_pairs((root / 'data/vacancies.ndjson').read_bytes())
        expected = json.loads((root / 'reference/lab3_pairs.json').read_text(encoding='utf-8'))
        self.assertEqual(rows, expected['vacancies'])
        self.assertEqual(result(rows, counts)['counts'], expected['counts'])


if __name__ == '__main__':
    unittest.main()
