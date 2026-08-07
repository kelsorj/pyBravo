"""Pure-Python controller for Darwin-generation Bravo instruments.

Submodules:
    topology    — node-tree layout (axis ↔ InstructionAddress)
    axis        — per-axis state machines (commutate/home/init)
    params      — pointer-cached parameter database access
    waxis_params — W-axis per-head-type PID/motion table
    motion      — instruction load + trigger + settle polling
    calibration — hardware ranges + mm↔normalized conversion
    sequences   — composite procedures (grip, open_gripper, jog)
    controller  — DarwinController(BravoController) facade
"""

from pybravo.darwin.controller import DarwinController

__all__ = ["DarwinController"]
