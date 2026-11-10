"""Trusted transport preconditions, not credentials or tab ownership grants.

The flat coordinator must keep this binding outside agent-controlled arguments.
Legacy developer clients may omit it; omission is not delegated confinement.
"""
from dataclasses import dataclass
import re


CONNECTION_HEADER = "X-Kitsune-Connection"
_INSTANCE = re.compile(r"[0-9a-f]{32}")
_GENERATION = re.compile(r"[1-9][0-9]{0,18}")


@dataclass(frozen=True)
class ConnectionBinding:
    bridge_instance: str
    extension_generation: int

    def __post_init__(self):
        if (not isinstance(self.bridge_instance, str) or not _INSTANCE.fullmatch(self.bridge_instance)
                or type(self.extension_generation) is not int
                or not 0 < self.extension_generation < 2**63):
            raise ValueError("Invalid bridge/extension connection binding")

    @classmethod
    def from_header(cls, value: str):
        instance, separator, generation = value.partition(":")
        if not separator or not _GENERATION.fullmatch(generation):
            raise ValueError("Invalid bridge/extension connection binding")
        return cls(instance, int(generation))

    @classmethod
    def from_status(cls, status: dict):
        if status.get("extension_connected") is not True:
            raise ValueError("A connected extension is required for a connection binding")
        return cls(status.get("bridge_instance"), status.get("extension_generation"))

    def header_value(self):
        return f"{self.bridge_instance}:{self.extension_generation}"
