"""Allow running as `python -m mediasync`."""

from mediasync.cli import main
import sys

sys.exit(main())
