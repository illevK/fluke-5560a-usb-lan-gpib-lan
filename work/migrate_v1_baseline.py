"""One-time extraction of reviewed v6.10.38 protocols; never part of the v1 build.

The resulting .py files are the editable source. No runtime source patching.
"""
import ast
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
source = root / 'outputs/fluke_5560a_controller.py'
text = source.read_text(encoding='utf-8-sig')
lines = text.splitlines(keepends=True)
tree = ast.parse(text)
dest = root / 'calibration_v1'
package = dest / 'calibration'
modules = {}
owners = {}

def group(node):
    n = getattr(node, 'name', '')
    if isinstance(node, (ast.Import, ast.ImportFrom, ast.Expr)):
        return None
    if node.lineno >= 4397:
        return None
    if node.lineno < 45:
        return None
    if node.lineno < 146:
        return 'instruments.transport'
    if node.lineno < 200:
        return 'domain.types'
    if node.lineno < 328:
        return 'domain.clipboard'
    if node.lineno < 436:
        return 'domain.units'
    if node.lineno < 659:
        return 'domain.commands'
    if node.lineno < 1278:
        return 'domain.clipboard'
    if node.lineno < 1467:
        return 'domain.commands'
    if node.lineno < 1525:
        return 'domain.groups'
    if node.lineno < 1919:
        return 'instruments.transport'
    if node.lineno < 2930:
        return 'instruments.dut'
    if node.lineno < 3427:
        return 'instruments.discovery'
    return 'instruments.fluke'

for node in tree.body:
    module = group(node)
    if not module:
        continue
    modules.setdefault(module, []).append(node)
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        owners[node.name] = module
    else:
        for child in ast.walk(node):
            if isinstance(child, (ast.Assign, ast.AnnAssign)):
                for target in child.targets if isinstance(child, ast.Assign) else [child.target]:
                    for name in ast.walk(target):
                        if isinstance(name, ast.Name):
                            owners[name.id] = module
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                for alias in child.names:
                    owners[alias.asname or alias.name.split('.')[0]] = module

imports = '''from __future__ import annotations
import os
import math
import re
import sys
import time
import socket
import tempfile
import ipaddress
import json
import threading
import queue
import subprocess
import datetime
import select
import struct
import base64
from contextlib import ExitStack
from pathlib import Path
from dataclasses import dataclass, replace
from decimal import Decimal, DecimalException
from typing import Optional, List, Tuple, Dict
'''
for module, nodes in modules.items():
    path = package.joinpath(*module.split('.')).with_suffix('.py')
    path.parent.mkdir(parents=True, exist_ok=True)
    deps = {}
    for node in nodes:
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                owner = owners.get(child.id)
                if owner and owner != module:
                    deps.setdefault(owner, set()).add(child.id)
    header = '"""Protocols and parsing migrated from v6.10.38. Maintained as normal Python source."""\n' + imports
    for owner, names in sorted(deps.items()):
        header += f'from calibration.{owner} import ' + ', '.join(sorted(names)) + '\n'
    blocks = []
    for node in nodes:
        start = min([node.lineno] + [d.lineno for d in getattr(node, 'decorator_list', ())])
        blocks.append(''.join(lines[start-1:node.end_lineno]))
    path.write_text(header + '\n\n' + '\n\n'.join(blocks), encoding='utf-8')
for folder in ('', 'domain', 'instruments', 'services', 'ui'):
    target = package / folder
    target.mkdir(parents=True, exist_ok=True)
    (target / '__init__.py').write_text('', encoding='utf-8')
(dest / 'baseline.json').write_text(json.dumps({'version': '6.10.38', 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'modules': list(modules)}, indent=2), encoding='utf-8')
print('Created', len(modules), 'source modules in', dest)
