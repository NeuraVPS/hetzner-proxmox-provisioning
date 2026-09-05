import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def selector(source):
    tree = ast.parse(source)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'selected_base_ipv4')
    scope = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), '<metadata-selector>', 'exec'), scope)
    return scope['selected_base_ipv4']


class BaseEgressSelection(unittest.TestCase):
    def test_both_writers_follow_partial_cutover_and_rollback(self):
        backfill = (ROOT/'base/snippets/backfill-publicipv4-base.py').read_text()
        migration = (ROOT/'scripts/migrate_vm.sh').read_text().split('python3 - "$@" <<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0]
        for source in (backfill, migration):
            resolve = selector(source)
            cases = [({}, ('188.40.153.120', '37.27.135.250')),
                     ({'activeBases': {'b0': 'ecc', 'b1': 'legacy'}}, ('116.202.118.221', '37.27.135.250')),
                     ({'activeBases': {'b0': 'legacy', 'b1': 'ecc'}}, ('188.40.153.120', '95.216.102.179')),
                     ({'activeBases': {'b0': 'ecc', 'b1': 'ecc'}}, ('116.202.118.221', '95.216.102.179'))]
            for config, expected in cases:
                with self.subTest(config=config):
                    self.assertEqual(tuple(resolve(config, r) for r in ('Falkenstein', ' Helsinki ')), expected)
            self.assertIsNone(resolve({}, 'unknown'))
            for bad in (None, {}, {'b0': 'ecc'}, {'b0': [], 'b1': 'legacy'}, {'b0': 'typo', 'b1': 'legacy'}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    resolve({'activeBases': bad}, 'Falkenstein')


if __name__ == '__main__':
    unittest.main()
