"""
Single place that decides which Sandbox implementation to hand out.
evaluator.py calls create() and never imports DockerSandbox (or, later,
KubernetesSandbox) directly -- swapping implementations means changing
this one function, not every caller.
"""

from .docker import DockerSandbox
from .interface import Sandbox


def create() -> Sandbox:
    return DockerSandbox()