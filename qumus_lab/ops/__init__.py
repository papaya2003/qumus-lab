"""L4 operations: v1's molecule-level sequences composed from the generic drivers and named points.

Each operation is a plain function ``op(ctx, params) -> dict`` usable two ways:
- as a single contract command (L4 test: one operation at a time, ``motion``), and
- as a step inside a whole job (L5 flow, ``qumus_lab.flows``).

Every move goes through the SafetyGate (``check_point``: real mode only drives to ``source: measured``
points) and the middle-board zone lock. The G-code sequences are v1's, cited per line in the code.
"""
