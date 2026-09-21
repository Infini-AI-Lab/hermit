"""Machine-readable check receipt; model transport in native learning is synthetic."""
import json
from pathlib import Path
import sys
import unittest

if __name__ == '__main__':
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern='test_*.py')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    receipt = Path(sys.argv[1])
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({'passed': result.wasSuccessful(), 'tests': result.testsRun,
                                  'skipped': len(result.skipped), 'errors': len(result.errors),
                                  'failures': len(result.failures)}, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
