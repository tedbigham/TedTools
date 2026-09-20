"""Run in a NEW Blender GUI process with --factory-startup --python this-file.

Exercises real modal TIMER delivery and status-bar draw callbacks, then exits only
that test process. An unrelated, already-running Blender session is never used.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import time

import bpy

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / 'tests' / '_modal-ui-result.json'
spec = importlib.util.spec_from_file_location('ted_tools_ui_test', ROOT / 'ted-tools.py')
addon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(addon)
addon.register()
output = tempfile.TemporaryDirectory(prefix='ted-modal-ui-')
state = {'draws': 0, 'ui_ticks': 0, 'worker_ticks': 0, 'started': False}
deadline = time.monotonic() + 60
original_draw = addon._FBXExportProgress.draw


def draw(self, header, context):
    state['draws'] += 1
    original_draw(self, header, context)


addon._FBXExportProgress.draw = draw


def finish(error=None):
    active = addon._active_fbx_export
    if active is not None:
        active._stop()
    state['error'] = error
    RESULT.write_text(json.dumps(state, indent=2), encoding='utf-8')
    output.cleanup()
    bpy.ops.wm.quit_blender()


def start():
    try:
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete(use_global=False)
        root = bpy.data.objects.new('UITestGroup', None)
        bpy.context.scene.collection.objects.link(root)
        for index in range(3):
            bpy.ops.mesh.primitive_cube_add(location=(index * 3, 0, 0))
            bpy.context.object.parent = root
        result = bpy.ops.object.ted_export_individual_fbx(directory=output.name)
        assert result == {'RUNNING_MODAL'}, result
        state['started'] = True
        bpy.app.timers.register(check, first_interval=0.01)
    except Exception as exc:
        finish(repr(exc))


def check():
    try:
        state['ui_ticks'] += 1
        active = addon._active_fbx_export
        if active is not None and 'writing FBX in background' in active._progress.label:
            state['worker_ticks'] += 1
        if time.monotonic() > deadline:
            raise AssertionError('Modal export did not finish')
        if active is None:
            assert (Path(output.name) / 'UITestGroup.fbx').is_file(), 'No grouped FBX'
            assert state['draws'] > 0, 'Status-bar draw callback was never called'
            assert state['worker_ticks'] > 1, 'UI timers did not run while FBX worker was active'
            assert not any(s.name.startswith('__TedFBX') for s in bpy.data.scenes), 'Leaked scene'
            finish()
            return None
        return 0.01
    except Exception as exc:
        finish(repr(exc))
        return None


bpy.app.timers.register(start, first_interval=0.5)
