"""Small standalone benchmark; never connects to an existing Blender process."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import time

import bpy

path = Path(sys.argv[sys.argv.index('--') + 1]) if '--' in sys.argv else Path(__file__).resolve().parents[1] / 'ted-tools.py'
spec = importlib.util.spec_from_file_location('ted_benchmark', path)
addon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(addon)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
base = bpy.context.object
for index in range(120):
    if index % 20 == 0:
        parent = bpy.data.objects.new(f'Building_{index // 20}', None)
        bpy.context.scene.collection.objects.link(parent)
    obj = bpy.data.objects.new(f'Asset_{index:03}', base.data)
    bpy.context.scene.collection.objects.link(obj)
    obj.parent = parent
    obj.location = (index * 3, 0, 0)
    obj.modifiers.new('Subdivision', 'SUBSURF').levels = 2
bpy.data.objects.remove(base, do_unlink=True)
bpy.context.view_layer.update()
with tempfile.TemporaryDirectory(prefix='ted-benchmark-') as directory:
    started = time.perf_counter()
    with contextlib.redirect_stdout(io.StringIO()):
        result = addon._export_individual_fbx(bpy.context, directory)
    print(f'BENCHMARK {path.name}: {result[0]} FBX files in {time.perf_counter() - started:.3f}s', flush=True)
