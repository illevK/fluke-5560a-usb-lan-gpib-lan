"""One-time copy of hardware-free protocol tests. Not needed for app builds."""
import ast
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = root / 'calibration_v1/tests/protocols'
target.mkdir(parents=True, exist_ok=True)
(target / '__init__.py').write_text('', encoding='utf-8')
sources = ('test_units_measurements', 'test_current_groups', 'test_communications', 'test_multimodel',
           'test_dut', 'test_dut_discovery', 'test_visa_ownership')
exclude = {'MeasurementTests', 'GroupGuiTests', 'ModelGuiTests', 'DUTGuiTests', 'DiscoveryUiTests'}
for name in sources:
    text = (root / 'work' / (name + '.py')).read_text(encoding='utf-8-sig')
    tree = ast.parse(text)
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if node.name in exclude:
                continue
            node.body = [child for child in node.body if getattr(child, 'name', '') != 'test_app_close_stops_calibrator_sequence_before_waiting_for_dut']
            node.decorator_list = [d for d in node.decorator_list if 'HAS_TKINTER' not in ast.unparse(d)]
        elif isinstance(node, ast.If):
            continue
        elif isinstance(node, ast.Expr) and not isinstance(node.value, ast.Constant):
            continue
        elif isinstance(node, ast.Import):
            if any(alias.name in ('fluke_5560a_controller', 'test_gui_async') for alias in node.names):
                continue
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('test_'):
            node.module = 'tests.protocols.' + node.module
        nodes.append(node)
    tree.body = nodes
    output = '"""v6.10.38 regression vectors, now exercising the v1 source modules only."""\nfrom tests.api_compat import api as f\n' + ast.unparse(tree) + '\n'
    (target / (name + '.py')).write_text(output, encoding='utf-8')
print('Copied protocol-only regressions')
