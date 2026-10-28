"""``python -m imswitch_device_micromanager convert CFG -o OUT.json`` (see :mod:`.convert`)."""
import sys

from .convert import main

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
