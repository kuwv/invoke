"""
Invoke's own 'binary' entrypoint.

Dogfoods the `program` module.
"""

from . import Program, __version__

program = Program(
    name="Invoke",
    binary="inv[oke]",
    binary_names=["invoke", "inv"],
    version=__version__,
)
