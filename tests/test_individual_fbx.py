"""Run with Blender --background --factory-startup --python tests/test_individual_fbx.py."""
import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

import bpy
from mathutils import Vector
from io_scene_fbx import parse_fbx as parse
from io_scene_fbx import export_fbx_bin


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ted_tools', ROOT / 'ted-tools.py')
addon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(addon)
addon.register()


def elements(element, kind):
    found = [element] if element.id == kind else []
    for child in element.elems:
        found.extend(elements(child, kind))
    return found


def read_fbx(path):
    return parse.parse(str(path))[0]


class IndividualFBXTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        self.temp = tempfile.TemporaryDirectory(prefix='ted-fbx-test-')
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'Export'

    def cube(self, name, location=(0, 0, 0)):
        bpy.ops.mesh.primitive_cube_add(location=location)
        obj = bpy.context.object
        obj.name = name
        return obj

    def material(self, name='SharedMaterial', image=None):
        material = bpy.data.materials.new(name)
        material.use_nodes = True
        if image is not None:
            node = material.node_tree.nodes.new('ShaderNodeTexImage')
            node.image = image
            material.node_tree.links.new(node.outputs['Color'],
                                         material.node_tree.nodes.get('Principled BSDF').inputs['Base Color'])
        return material

    def image_file(self, folder, color):
        folder = Path(self.temp.name) / folder
        folder.mkdir()
        image = bpy.data.images.new('Albedo.png', width=2, height=2)
        image.pixels[:] = list(color) * 4
        image.filepath_raw = str(folder / 'Albedo.png')
        image.file_format = 'PNG'
        image.save()
        path = image.filepath_raw
        bpy.data.images.remove(image)
        return bpy.data.images.load(path, check_existing=False)

    def export(self, **kwargs):
        return addon._export_individual_fbx(bpy.context, str(self.output), **kwargs)

    def snapshot(self):
        return {
            'scene': bpy.context.scene,
            'active': bpy.context.view_layer.objects.active,
            'selected': tuple(bpy.context.selected_objects),
            'objects': [(obj.name, obj.data, obj.parent, obj.matrix_world.copy(),
                         obj.hide_get(), obj.hide_viewport) for obj in bpy.context.scene.objects],
            'images': [(im.name, im.filepath_raw, im.source, im.is_dirty,
                        bool(im.packed_file), tuple(im.pixels)) for im in bpy.data.images],
            'counts': (len(bpy.data.scenes), len(bpy.data.objects), len(bpy.data.meshes),
                       len(bpy.data.materials), len(bpy.data.images)),
        }

    def test_shared_texture_material_and_scene_restoration(self):
        image = self.image_file('Source', (0.8, 0.2, 0.1, 1))
        image.pack()
        os.remove(image.filepath_raw)  # A packed texture must not require its old disk file.
        material = self.material(image=image)
        a = self.cube('Building A', (10, 5, 3))
        a.data.materials.append(material)
        b = self.cube('Building B', (-4, 2, 1))
        b.data.materials.append(material)
        before = self.snapshot()
        self.assertEqual(self.export(), (2, 1, []))
        self.assertEqual(before, self.snapshot())
        self.assertEqual(len(list((self.output / 'Textures').iterdir())), 1)
        references = []
        for path in self.output.glob('*.fbx'):
            root = read_fbx(path)
            self.assertEqual(len(elements(root, b'Geometry')), 1)
            self.assertEqual(len(elements(root, b'Material')), 1)
            self.assertIn(b'SharedMaterial', elements(root, b'Material')[0].props[1])
            refs = [node.props[0].decode() for node in elements(root, b'RelativeFilename')]
            self.assertTrue(refs)
            for ref in refs:
                self.assertTrue((self.output / ref.replace('\\', '/')).is_file(), ref)
            references.append(refs)
        self.assertEqual(references[0], references[1])

    def test_generated_dirty_images_and_filename_collisions(self):
        red = self.image_file('Red', (1, 0, 0, 1))
        blue = self.image_file('Blue', (0, 0, 1, 1))
        generated = bpy.data.images.new('Generated', width=2, height=2)
        generated.pixels[:] = [0, 1, 0, 1] * 4
        red.pixels[:] = [1, 1, 0, 1] * 4  # Must export unsaved paint, not disk's red pixels.
        for name, image in zip(('A:B', 'A?B', 'a_b'), (red, blue, generated)):
            self.cube(name).data.materials.append(self.material(name, image))
        before = self.snapshot()
        self.assertEqual(self.export()[:2], (3, 3))
        self.assertEqual(before, self.snapshot())
        self.assertEqual(len({p.name.casefold() for p in self.output.glob('*.fbx')}), 3)
        colors = []
        for path in (self.output / 'Textures').iterdir():
            image = bpy.data.images.load(str(path))
            colors.append(tuple(round(v) for v in image.pixels[:4]))
            bpy.data.images.remove(image)
        self.assertCountEqual(colors, [(1, 1, 0, 1), (0, 0, 1, 1), (0, 1, 0, 1)])

    def test_hidden_excluded_mesh_modifiers_and_material_override(self):
        a = self.cube('Hidden')
        a.hide_set(True)
        a.modifiers.new('Subdivision', 'SUBSURF').levels = 1
        b = self.cube('Excluded')
        collection = bpy.data.collections.new('Excluded Collection')
        bpy.context.scene.collection.children.link(collection)
        for owner in list(b.users_collection):
            owner.objects.unlink(b)
        collection.objects.link(b)
        bpy.context.view_layer.layer_collection.children[collection.name].exclude = True
        b.data.materials.append(self.material('MeshMaterial'))
        b.material_slots[0].link = 'OBJECT'
        b.material_slots[0].material = self.material('ObjectMaterial')
        c = self.cube('Disabled')
        c.hide_viewport = True
        before = self.snapshot()
        self.assertEqual(self.export()[:2], (3, 0))
        self.assertEqual(before, self.snapshot())
        hidden = read_fbx(self.output / 'Hidden.fbx')
        self.assertGreater(len(elements(hidden, b'Vertices')[0].props[0]), 8 * 3)
        excluded = read_fbx(self.output / 'Excluded.fbx')
        self.assertIn(b'ObjectMaterial', elements(excluded, b'Material')[0].props[1])
        self.assertTrue(elements(read_fbx(self.output / 'Disabled.fbx'), b'Geometry'))

    def test_selected_only_and_object_origin_positions_roundtrip(self):
        parent = bpy.data.objects.new('Parent', None)
        bpy.context.scene.collection.objects.link(parent)
        parent.location = (8, 4, 2)
        obj = self.cube('Asset', (2, 3, 4))
        obj.parent = parent
        obj.rotation_euler = (0.2, 0.1, 0.3)
        obj.scale = (2, 3, 1)
        bpy.context.view_layer.update()
        expected = sorted(tuple(round(v, 4) for v in
                                (obj.matrix_world @ vert.co - parent.matrix_world.translation))
                          for vert in obj.data.vertices)
        self.cube('Other')
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        self.assertEqual(self.export(selected_only=True)[:2], (1, 0))
        self.assertEqual(len(list(self.output.glob('*.fbx'))), 1)
        bpy.ops.import_scene.fbx(filepath=str(self.output / 'Parent.fbx'))
        imported = next(obj for obj in bpy.context.selected_objects if obj.type == 'MESH')
        actual = sorted(tuple(round(v, 4) for v in (imported.matrix_world @ vert.co))
                        for vert in imported.data.vertices)
        self.assertEqual(actual, expected)

    def test_parent_groups_export_one_file_with_nested_parts_and_shared_material(self):
        root = bpy.data.objects.new('KB3D_FTW_BldgLgAirTrafficControl_A_grp', None)
        bpy.context.scene.collection.objects.link(root)
        root.location = (40, 12, 5)
        root.rotation_euler = (0.1, 0.2, 0.3)
        root.scale = (2, 2, 2)
        nested = bpy.data.objects.new('RoofGroup', None)
        bpy.context.scene.collection.objects.link(nested)
        nested.parent = root
        nested.location = (0, 0, 4)
        nested.rotation_euler.z = 0.2
        material = self.material(image=self.image_file('Shared', (0.2, 0.5, 0.8, 1)))
        parts = []
        for index in range(5):
            part = self.cube(f'Antenna_{index}', (index * 3, 1, 2))
            part.parent = root if index < 3 else nested
            part.data.materials.append(material)
            parts.append(part)
        self.cube('Unrelated')
        bpy.context.view_layer.update()
        expected = sorted(tuple(part.matrix_world @ vert.co - root.matrix_world.translation)
                          for part in parts for vert in part.data.vertices)
        bpy.ops.object.select_all(action='DESELECT')
        # Selecting both a parent and a descendant must not duplicate files/meshes.
        root.select_set(True)
        parts[3].select_set(True)
        before = self.snapshot()
        save = export_fbx_bin.save
        expected_matrices = {}
        for source in [root, nested, *parts]:
            matrix = source.matrix_world.copy()
            matrix.translation -= root.matrix_world.translation
            expected_matrices[source.name] = matrix

        def verify_export_scene(operator, context, **kwargs):
            context.view_layer.update()
            for obj in context.selected_objects:
                source_name = next(name for name in expected_matrices if obj.name.startswith(name))
                error = sum(abs(a - b) for actual_row, expected_row in
                            zip(obj.matrix_world, expected_matrices[source_name])
                            for a, b in zip(actual_row, expected_row))
                self.assertLess(error, 0.001, f'Export copy transform changed: {obj.name}')
            return save(operator, context, **kwargs)

        with patch.object(export_fbx_bin, 'save', side_effect=verify_export_scene):
            self.assertEqual(self.export(selected_only=True), (1, 1, []))
        self.assertEqual(before, self.snapshot())
        files = list(self.output.glob('*.fbx'))
        self.assertEqual([path.name for path in files], [root.name + '.fbx'])
        parsed = read_fbx(files[0])
        self.assertEqual(len(elements(parsed, b'Geometry')), 5)
        self.assertEqual(len(elements(parsed, b'Material')), 1)
        self.assertEqual(len(list((self.output / 'Textures').iterdir())), 1)
        bpy.ops.import_scene.fbx(filepath=str(files[0]))
        imported = list(bpy.context.selected_objects)
        self.assertEqual(len([obj for obj in imported if obj.type == 'EMPTY']), 2)
        actual = sorted(tuple(obj.matrix_world @ vert.co)
                        for obj in imported if obj.type == 'MESH' for vert in obj.data.vertices)
        self.assertEqual(len(actual), len(expected))
        for actual_point, expected_point in zip(actual, expected):
            self.assertLess((Vector(actual_point) - Vector(expected_point)).length, 0.0001)

    def test_mesh_parent_and_multiple_assets(self):
        root = self.cube('MeshRoot', (5, 0, 0))
        child = self.cube('Child', (2, 0, 0))
        child.parent = root
        self.cube('Standalone')
        bpy.context.view_layer.update()
        self.assertEqual(self.export()[0], 2)
        self.assertEqual(len(elements(read_fbx(self.output / 'MeshRoot.fbx'), b'Geometry')), 2)
        self.assertTrue((self.output / 'Standalone.fbx').is_file())
        self.assertFalse((self.output / 'Child.fbx').exists())

    def test_progress_includes_cleanup_and_ends_only_after_release(self):
        root = bpy.data.objects.new('Group', None)
        bpy.context.scene.collection.objects.link(root)
        self.cube('A').parent = root
        self.cube('B').parent = root
        events = []

        def progress(factor, label):
            events.append((factor, label))
            if factor == 1.0:
                self.assertFalse(any(scene.name.startswith('__TedFBX') for scene in bpy.data.scenes))

        self.export(progress=progress)
        values = [factor for factor, label in events]
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[0], 0.0)
        self.assertEqual(values[-1], 1.0)
        self.assertTrue(any('asset 1/1' in label and 'part 3/3' in label for _, label in events))
        self.assertTrue(any('Publishing FBX' in label for _, label in events))
        self.assertTrue(any('Cleaning evaluation scene' in label for _, label in events))

    def test_progress_indicator_closes_on_error(self):
        wm = Mock()
        context = SimpleNamespace(window=bpy.context.window, scene=bpy.context.scene,
                                  view_layer=bpy.context.view_layer, window_manager=wm, workspace=Mock())
        with self.assertRaisesRegex(RuntimeError, 'Injected progress failure'):
            with addon._FBXExportProgress(context) as progress:
                progress.update(0.5, 'Exporting asset 1/2')
                progress.update(0.4, 'Exporting asset 1/2')
                layout = Mock()
                progress.draw(SimpleNamespace(layout=layout), context)
                self.assertEqual(layout.row.return_value.progress.call_args.kwargs['factor'], 0.5)
                raise RuntimeError('Injected progress failure')
        context.workspace.status_text_set.assert_called_with(None)
        wm.progress_begin.assert_not_called()  # No blocking progress cursor.

    def test_generator_yields_with_original_context_and_paths_and_cancels(self):
        root = bpy.data.objects.new('Group', None)
        bpy.context.scene.collection.objects.link(root)
        material = self.material(image=self.image_file('Source', (0, 1, 0, 1)))
        for name in ('A', 'B'):
            self.cube(name).parent = root
            bpy.context.object.data.materials.append(material)
        before = self.snapshot()
        steps = addon._fbx_export_steps(bpy.context, str(self.output))
        for factor, label in steps:
            self.assertEqual(bpy.context.scene, before['scene'])
            self.assertEqual(tuple(bpy.context.selected_objects), before['selected'])
            self.assertEqual(bpy.context.view_layer.objects.active, before['active'])
            self.assertEqual(self.snapshot()['images'], before['images'])
            if 'part 3/3' in label:
                break
        steps.close()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(list(self.output.iterdir()))

    def test_background_writer_roundtrip_and_portable_textures(self):
        root = bpy.data.objects.new('BackgroundGroup', None)
        bpy.context.scene.collection.objects.link(root)
        root.location = (10, 2, 3)
        generated = bpy.data.images.new('Generated', width=2, height=2)
        generated.pixels[:] = [0, 1, 0, 1] * 4
        packed = self.image_file('Packed', (0, 0, 1, 1))
        packed.pack()
        os.remove(packed.filepath_raw)
        for name, image in [('A', generated), ('B', packed)]:
            obj = self.cube(name, (2, 0, 0))
            obj.parent = root
            obj.data.materials.append(self.material(name, image))
        bpy.context.view_layer.update()
        before = self.snapshot()
        steps = addon._fbx_export_steps(bpy.context, str(self.output), background_fbx=True)
        saw_worker = False
        deadline = time.monotonic() + 45
        try:
            while True:
                self.assertLess(time.monotonic(), deadline, 'Background worker timed out')
                try:
                    factor, label = next(steps)
                except StopIteration as done:
                    self.assertEqual(done.value[:2], (1, 2))
                    break
                self.assertEqual(bpy.context.scene, before['scene'])
                self.assertEqual(self.snapshot()['images'], before['images'])
                if 'writing FBX in background' in label:
                    saw_worker = True
                    time.sleep(0.01)
        finally:
            steps.close()
        self.assertTrue(saw_worker)
        self.assertEqual(self.snapshot(), before)
        path = self.output / 'BackgroundGroup.fbx'
        parsed = read_fbx(path)
        self.assertEqual(len(elements(parsed, b'Geometry')), 2)
        for ref in elements(parsed, b'RelativeFilename'):
            self.assertTrue((self.output / ref.props[0].decode().replace('\\', '/')).is_file())
        bpy.ops.import_scene.fbx(filepath=str(path))
        for obj in bpy.context.selected_objects:
            if obj.type == 'MESH':
                self.assertLess((obj.matrix_world.translation - Vector((2, 0, 0))).length, 0.0001)

    def test_cancel_background_writer_stops_only_owned_child_and_cleans_up(self):
        self.cube('Asset')
        before = self.snapshot()
        workers = []
        start = addon._FBXBackgroundWriter.start

        def remember(writer, *args):
            start(writer, *args)
            workers.append(writer)

        steps = addon._fbx_export_steps(bpy.context, str(self.output), background_fbx=True)
        with patch.object(addon._FBXBackgroundWriter, 'start', remember):
            try:
                for factor, label in steps:
                    if 'writing FBX in background' in label:
                        break
            finally:
                steps.close()
        self.assertEqual(len(workers), 1)
        self.assertIsNotNone(workers[0].process.poll())
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(list(self.output.iterdir()))

    def test_modal_services_one_step_and_cancels_on_escape(self):
        operator = addon.OBJECT_OT_ted_export_individual_fbx
        self.cube('Asset')
        before = self.snapshot()
        state = SimpleNamespace(
            _steps=addon._fbx_export_steps(bpy.context, str(self.output)),
            _timer=object(), _wm=Mock(), _started=time.perf_counter(),
            _last_label=None, _next_tick=0.0, report=Mock(),
            _progress=addon._FBXExportProgress(bpy.context))
        state._stop = lambda: operator._stop(state)
        state._report_complete = lambda result: operator._report_complete(state, result)
        addon._active_fbx_export = state
        try:
            result = operator.modal(state, bpy.context, SimpleNamespace(type='TIMER', value='NOTHING'))
            self.assertEqual(result, {'RUNNING_MODAL'})
            self.assertEqual(state._progress.factor, 0.0)
            self.assertFalse(self.output.exists())  # Only the initial step ran.
            result = operator.modal(state, bpy.context, SimpleNamespace(type='MIDDLEMOUSE', value='PRESS'))
            self.assertEqual(result, {'PASS_THROUGH'})
            result = operator.modal(state, bpy.context, SimpleNamespace(type='ESC', value='PRESS'))
            self.assertEqual(result, {'CANCELLED'})
            self.assertIsNone(addon._active_fbx_export)
            state._wm.event_timer_remove.assert_called_once()
            self.assertEqual(self.snapshot(), before)
        finally:
            state._stop()

    def test_keep_positions_and_units_roundtrip(self):
        obj = self.cube('Positioned', (3, 5, 7))
        bpy.context.scene.unit_settings.system = 'METRIC'
        bpy.context.scene.unit_settings.scale_length = 0.01
        bpy.context.view_layer.update()
        self.export(keep_positions=True)
        bpy.context.scene.unit_settings.scale_length = 1.0
        bpy.ops.import_scene.fbx(filepath=str(self.output / 'Positioned.fbx'))
        imported = bpy.context.selected_objects[0]
        self.assertLess((imported.matrix_world.translation - Vector((0.03, 0.05, 0.07))).length, 0.0001)
        self.assertAlmostEqual(imported.dimensions.x, 0.02, places=4)

    def test_missing_texture_and_unsupported_source_leave_no_outputs(self):
        image = self.image_file('Source', (1, 0, 0, 1))
        image.filepath_raw = str(Path(self.temp.name) / 'missing.png')
        self.cube('Asset').data.materials.append(self.material(image=image))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'Missing texture'):
            self.export()
        self.assertEqual(before, self.snapshot())
        self.assertFalse(list(self.output.iterdir()))
        image.source = 'SEQUENCE'
        with self.assertRaisesRegex(ValueError, 'bake it'):
            self.export()

    def test_export_failure_restores_images_and_removes_staged_outputs(self):
        image = self.image_file('Source', (1, 0, 0, 1))
        material = self.material(image=image)
        self.cube('A').data.materials.append(material)
        self.cube('B').data.materials.append(material)
        before = self.snapshot()
        save = export_fbx_bin.save
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('Injected failure')
            return save(*args, **kwargs)

        with patch.object(export_fbx_bin, 'save', side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, 'Injected failure'):
                self.export()
        self.assertEqual(before, self.snapshot())
        self.assertFalse(list(self.output.iterdir()))

    def test_group_images_are_copied_with_a_material_warning(self):
        image = self.image_file('GroupSource', (1, 0, 0, 1))
        material = self.material()
        group = bpy.data.node_groups.new('TextureGroup', 'ShaderNodeTree')
        group.nodes.new('ShaderNodeTexImage').image = image
        material.node_tree.nodes.new('ShaderNodeGroup').node_tree = group
        self.cube('Asset').data.materials.append(material)
        self.assertEqual(self.export(), (1, 1, ['SharedMaterial']))

    def test_overwrite_guard_empty_selection_and_content_deduplication(self):
        a = self.image_file('One', (0, 1, 0, 1))
        b = self.image_file('Two', (0, 1, 0, 1))
        self.cube('One').data.materials.append(self.material('One', a))
        self.cube('Two').data.materials.append(self.material('Two', b))
        self.assertEqual(self.export()[:2], (2, 1))
        before = {p: p.read_bytes() for p in self.output.rglob('*') if p.is_file()}
        with self.assertRaisesRegex(ValueError, 'already exist'):
            self.export()
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(self.export(overwrite=True)[:2], (2, 1))
        bpy.ops.object.select_all(action='DESELECT')
        with self.assertRaisesRegex(ValueError, 'No mesh objects'):
            self.export(selected_only=True)

    def test_operator_registration_and_execute(self):
        self.cube('Operator')
        self.assertEqual(bpy.ops.object.ted_export_individual_fbx(directory=str(self.output)), {'FINISHED'})
        self.assertTrue((self.output / 'Operator.fbx').is_file())
        addon.unregister()
        addon.register()


result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(IndividualFBXTests))
if not result.wasSuccessful():
    raise SystemExit(1)
