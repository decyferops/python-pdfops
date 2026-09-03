"""Container entrypoint: ``python -m pdf_ops``.

The only module that touches the real process environment and exit status;
everything else operates on plain mappings and return values.
"""

import os
import sys

from pdf_ops.config import VAR_OUTPUT_PASSWORD, VAR_PASSWORD
from pdf_ops.main import run


def main() -> None:
    env = dict(os.environ)
    # Scrub secret values from the live environment before any work, so child
    # processes and later os.environ readers see nothing. (This cannot erase
    # the initial environment block - /proc/<pid>/environ and `docker
    # inspect` still show it, which is exactly why the file channel is the
    # preferred one.)
    for var in (VAR_PASSWORD, VAR_OUTPUT_PASSWORD):
        os.environ.pop(var, None)
    sys.exit(run(env))


if __name__ == "__main__":
    main()
