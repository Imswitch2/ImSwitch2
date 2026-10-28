"""Optional link between ImProcess and Fiji (ImageJ).

Nothing in the core ``imswitch`` packages imports this package, and nothing
here imports ImageJ, jpype or scyjava: the modules that exist so far are pure
Python and numpy, so the axis mapping, metadata-loss reporting and message
contract can be tested without Java. The Java-side code will live only in the
future host module, which runs in its own process.
"""
