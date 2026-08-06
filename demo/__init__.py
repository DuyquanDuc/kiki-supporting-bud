"""Local demo rig for the Meeting Support Bot."""

import sys

# The Windows console defaults to cp1252, which cannot encode non-Latin audio
# device names or em-dashes and raises UnicodeEncodeError mid-print. Every entry
# point imports this package, so fixing it here covers all of them.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
