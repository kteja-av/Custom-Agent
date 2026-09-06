"""Single source of the SDK version.

Separate from __init__ so modules like api.py can read it without importing the
package root, which would be circular.
"""

__version__ = "0.1.0.dev0"
