from .docker import DockerSandbox
from .interface import Sandbox


def create() -> Sandbox:
    return DockerSandbox()