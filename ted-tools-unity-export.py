bl_info = {
    "name": "Ted Tools Unity FBX Exporter",
    "author": "ChatGPT/Ted",
    "version": (1, 0, 1),
    "blender": (4, 0, 0),
    "location": "File > Export > Ted Tools Unity FBX",
    "category": "Import-Export",
}

import bpy
import os
import re
import math
import mathutils

from bpy.types import Operator
from bpy.props import BoolProperty, StringProperty
from bpy_extras.io_utils import ExportHelper


def safe_name(name):
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", name)


def image_needs_file_backing(img):
    if not img:
        return False

    if img.source == "GENERATED":
        return True

    if img.source == "FILE":
        if not img.filepath:
            return True

        abs_path = bpy.path.abspath(img.filepath)
        if not abs_path or not os.path.exists(abs_path):
            return True

    return False


def make_image_file_backed_and_packed(img, texture_dir):
    os.makedirs(texture_dir, exist_ok=True)

    if not image_needs_file_backing(img):
        return

    filename = safe_name(img.name) + ".png"
    filepath = os.path.join(texture_dir, filename)

    img.filepath_raw = filepath
    img.file_format = "PNG"
    img.save()
    img.pack()

    print(f"Saved and packed image: {filepath}")


def fix_images_for_object(obj, texture_dir):
    if not obj.data:
        return

    for mat in obj.data.materials:
        if not mat or not mat.use_nodes:
            continue

        for node in mat.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image:
                make_image_file_backed_and_packed(node.image, texture_dir)


def apply_rotation(obj):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)


def unity_fix_object(obj):
    original_matrix = obj.matrix_world.copy()

    obj.matrix_world = mathutils.Matrix.Rotation(math.radians(-90.0), 4, "X")
    bpy.context.view_layer.update()

    apply_rotation(obj)

    obj.matrix_world = original_matrix @ mathutils.Matrix.Rotation(math.radians(90.0), 4, "X")
    bpy.context.view_layer.update()


def export_one_object(obj, export_dir, texture_dir, embed_textures):
    filepath = os.path.join(export_dir, safe_name(obj.name) + ".fbx")

    fix_images_for_object(obj, texture_dir)

    export_obj = obj.copy()
    export_obj.data = obj.data.copy()
    bpy.context.collection.objects.link(export_obj)

    export_obj.data.materials.clear()
    for mat in obj.data.materials:
        export_obj.data.materials.append(mat)

    export_obj.location = (0, 0, 0)
    bpy.context.view_layer.update()

    unity_fix_object(export_obj)

    bpy.ops.object.select_all(action="DESELECT")
    export_obj.select_set(True)
    bpy.context.view_layer.objects.active = export_obj

    bpy.ops.export_scene.fbx(
        filepath=filepath,
        use_selection=True,
        object_types={"MESH"},
        apply_scale_options="FBX_SCALE_UNITS",
        use_mesh_modifiers=True,
        mesh_smooth_type="FACE",
        add_leaf_bones=False,
        path_mode="COPY",
        embed_textures=embed_textures,
    )

    bpy.data.objects.remove(export_obj, do_unlink=True)

    return filepath


class ExportUnityIndividualFbxBatch(Operator, ExportHelper):
    bl_idname = "export_scene.unity_individual_fbx_batch"
    bl_label = "Ted Tools Unity FBX"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ""
    filter_glob: StringProperty(default="", options={"HIDDEN"})

    directory: StringProperty(
        name="Export Folder",
        subtype="DIR_PATH",
    )

    embed_textures: BoolProperty(
        name="Embed Textures",
        default=True,
    )

    def execute(self, context):
        export_dir = self.directory

        if not export_dir:
            self.report({"ERROR"}, "No export folder selected.")
            return {"CANCELLED"}

        os.makedirs(export_dir, exist_ok=True)

        texture_dir = os.path.join(export_dir, "_GeneratedTextures")
        os.makedirs(texture_dir, exist_ok=True)

        selected = [obj for obj in context.selected_objects if obj.type == "MESH"]

        if not selected:
            self.report({"WARNING"}, "No mesh objects selected.")
            return {"CANCELLED"}

        for obj in selected:
            export_one_object(obj, export_dir, texture_dir, self.embed_textures)

        self.report({"INFO"}, f"Exported {len(selected)} FBX files.")
        return {"FINISHED"}


def menu_func_export(self, context):
    self.layout.operator(
        ExportUnityIndividualFbxBatch.bl_idname,
        text="Ted Tools Unity FBX (.fbx)",
    )


def register():
    bpy.utils.register_class(ExportUnityIndividualFbxBatch)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.utils.unregister_class(ExportUnityIndividualFbxBatch)


if __name__ == "__main__":
    register()