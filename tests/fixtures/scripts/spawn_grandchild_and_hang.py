"""Fixture for tests/test_pipeline.py's real-process-tree integration test
(docs/agent-runtime-audit.md's "process-group cancellation" finding). Spawns a grandchild
subprocess (a bare `time.sleep(30)`, no `start_new_session` of its own -- it inherits this
script's process group, same as `review_with_lm_studio.py` or any future adapter script that
shells out to something would), writes the grandchild's pid to the file named in argv[1] so the
test can check whether it survives, then hangs itself long enough for the test's short pipeline
stage timeout to fire before either process would exit naturally.
"""

import subprocess
import sys
import time

grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
with open(sys.argv[1], "w", encoding="utf-8") as f:
    f.write(str(grandchild.pid))
time.sleep(30)
