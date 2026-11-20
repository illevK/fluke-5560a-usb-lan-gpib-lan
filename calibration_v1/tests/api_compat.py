"""Test-only facade to apply the original protocol test vectors to split modules.

Assignments propagate across imported bindings so unittest.mock patches target
the new modules rather than loading or testing the old executable.
"""
from importlib import import_module

MODULES = [import_module('calibration.' + name) for name in (
    'domain.types', 'domain.units', 'domain.commands', 'domain.clipboard', 'domain.groups',
    'instruments.transport', 'instruments.dut', 'instruments.discovery', 'instruments.fluke')]
OWNERS = {name: [module for module in MODULES if hasattr(module, name)]
          for name in {name for module in MODULES for name in vars(module)}}

class API:
    def __getattr__(self, name):
        for module in MODULES:
            if hasattr(module, name):
                return getattr(module, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        matches = OWNERS.get(name, [])
        if not matches:
            raise AttributeError(name)
        for module in matches:
            setattr(module, name, value)

    def __delattr__(self, name):
        # patch.object treats the facade as a proxy and performs delete + restore.
        for module in OWNERS.get(name, []):
            if hasattr(module, name):
                delattr(module, name)

api = API()
