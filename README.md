# Ted Tools for Blender

Install or update `ted-tools.py` through Blender's add-on preferences, then restart
Blender if an older version is already loaded. The add-on appears in the 3D View's
**N panel > Ted** tab. This version is 2.3.0 and supports Blender 4.5+.

## Export asset FBX files for Unity

In **Object Mode**, click **Ted > Misc > Export Asset FBX Files**, choose an
output folder, and confirm the export.

- Each top-level parent and all its mesh descendants become **one FBX**, named
  after that parent. For example, `KB3D_FTW_BldgLgAirTrafficControl_A_grp` and all
  its antenna/building meshes produce `KB3D_FTW_BldgLgAirTrafficControl_A_grp.fbx`.
  Nested groups stay in that same file. An unparented mesh gets its own FBX.
- Grouping follows **object parenting**, not collections or filename prefixes.
  If several buildings have a common top-level parent, they form one asset.
  Meshes in hidden/excluded collections are included. Assets without meshes are skipped.
- Enable **Selected Assets Only** to export just selected assets. Select the parent
  or any of its children: the entire top-level asset is exported, once, including
  unselected siblings. Shared materials and textures remain shared.
- Each asset's root origin is placed at zero by default, retaining the relative
  placement and hierarchy of its parts, world rotation and scale. Enable **Keep
  Scene Positions** to retain the scene layout instead. The existing root origin
  is used; the pivot is not recalculated from bounds.
- Modifiers are evaluated at the current frame. These are static mesh assets;
  animations, rigs and shape keys are not exported. Meshes remain separate objects
  inside their asset FBX, with parent nodes retained as empties where needed.
  Realize collection/geometry instances before exporting if you need them included
  as mesh geometry.
- Matching FBX files cause an error unless **Overwrite FBX Files** is enabled.
  Names are made safe for Windows, and colliding names receive numeric suffixes.

Blender's **bottom status bar** shows a progress meter, the current asset/part,
texture preparation, file publishing and cleanup. The bar reaches 100% only after
temporary scenes have been removed. This is work progress, not an estimated time:
one large mesh or FBX write may take longer than many small ones. The synchronous
export updates between steps; it cannot update during a single Blender FBX call.
Timestamped `Ted FBX` messages in **Window > Toggle System Console** on Windows
identify the last started operation if a step takes unusually long.

The exporter evaluates the source scene separately from the small asset-writing
scene, avoids unnecessary material-related mesh copies, and removes each asset's
temporary data in bulk. These reduce repeated work on scenes with many parts.

The output looks like this:

```text
Export/
  Building A.fbx
  Building B.fbx
  Textures/
    Albedo_<content hash>.png
    Normal_<content hash>.png
```

All image textures referenced by the exported materials (including images inside
node groups) are copied to `Textures`. Identical image bytes are stored once, even
when multiple objects, materials or image datablocks reuse them. Distinct textures
with the same original filename cannot overwrite each other. FBX files use relative
texture paths, so move the **entire output folder** together.

Packed textures are supported even when their original disk files are missing.
Generated images and unsaved texture paint are exported from their current pixels
as PNG, or EXR for float images. Missing external files and unsupported image types
(UDIM, sequences, movies and multiview) stop the export with an error; bake those to
single images first. The exporter stages the batch before publishing files, and
restores temporary image paths and removes its temporary scene even on failure.
It preserves source meshes, material sharing, transforms, selection and visibility.
Re-exporting does not delete old texture files or the earlier per-part FBXs. Choose
a **new output folder** when switching from the old per-mesh exporter to avoid
mixing its files with the grouped assets. Update/reload the add-on only after any
currently running export has ended; editing this source does not update that run.

### Unity materials and textures

1. Copy the complete export folder into your Unity project's `Assets` directory.
2. Select the FBXs and use their **Materials** import tab. Material names and slot
   assignments are retained, and supported image connections are recorded in FBX.
3. To share materials across models, extract each unique material once into a common
   `Materials` folder. On the remaining FBXs, use **On Demand Remap > Search and
   Remap**, matching **From Model's Material** with a search scope covering that
   folder, or assign the shared materials explicitly in the remapping list. Unity's
   default embedded material subassets are separate per FBX, even when names match.
4. Check the resulting shaders and texture import settings for your render pipeline,
   especially normal maps and metallic/roughness versus smoothness channels.

FBX carries a limited material model, not Blender's full shader graph. Direct image
textures feeding Principled BSDF inputs work best. The exporter copies images used
inside complex graphs, but this does not reproduce the graph in Unity. It reports
materials with unsupported image connections or procedural textures in Blender's
console; bake or rebuild those materials as needed. Texture baking and creation of
Unity `.mat` assets are not performed by this button.

References: [Blender FBX export](https://docs.blender.org/manual/en/5.0/addons/import_export/scene_fbx.html)
and [Unity's Materials import tab](https://docs.unity3d.com/6000.0/Documentation/Manual/FBXImporter-Materials.html).

The separate `ted-tools-unity-export.py` add-on is independent and is not required
for this button.

## Verification

Run the integration tests in a background Blender process:

```powershell
& 'C:\Program Files\Blender Foundation\Blender 4.5\blender.exe' --background --factory-startup --python-exit-code 1 --python tests/test_individual_fbx.py
```

The suite checks real FBX contents and import round trips, shared/packed/generated
textures, unsaved paint, material overrides, name collisions, hidden/excluded
objects, units and parent transforms, nested asset grouping, selected parents and
children, progress completion/cleanup, overwrite protection, and cleanup after
missing textures or an injected export failure. Tested on Blender 4.5 and 5.2.

`tests/benchmark_individual_fbx.py` creates a small synthetic scene with 120 meshes
in six parent groups in a separate Blender process, then times the export. It does
not connect to or change an existing Blender session.
