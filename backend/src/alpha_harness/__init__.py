"""Alpha Harness: a local research studio for WorldQuant BRAIN."""

# Before anything imports numpy: its thread pools read these once, at import.
from .resources import limit_math_threads

limit_math_threads()
