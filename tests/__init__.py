"""Tests for the code in dns/. Run them with `just test`.

Importing this package puts dns/ on the import path, so tests can `import web`
and the like exactly as the container does.
"""

import os
import sys

DNS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dns")
sys.path.insert(0, DNS_DIR)
