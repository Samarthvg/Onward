"""Onward's tests. From the repository root: python -m unittest

The whole run gets a data folder of its own, set before any test imports the
app, so no test reads or writes the real one (a developer's jobs.db, settings
and resumes) and a fresh machine has the folder the helper expects.
"""

import atexit
import os
import shutil
import tempfile

_HOME = tempfile.mkdtemp(prefix="onward-tests-")
os.environ["ONWARD_HOME"] = _HOME
os.environ.pop("JOBRADAR_HOME", None)
atexit.register(shutil.rmtree, _HOME, True)
