# Ted Tools for Blender

Install or update `ted-tools.py` through Blender's add-on preferences, then restart
Blender if an older version is already loaded. The add-on appears in the 3D View's
**N panel > Ted** tab. This version is 2.5.0 and supports Blender 4.5+.

Version 2.4.1 fixes material slots collapsing to one material in background exports.
The worker snapshot now uses persistent original material datablocks rather than
evaluated dependency-graph copies, retaining per-face assignments and object-level
material overrides. Re-export affected assets after updating the add-on.

## Export asset FBX files for Unity

In **Object Mode**, click **Ted > Misc > Export Asset FBX Files**, choose an
output folder, and confirm the export.

Choose **Group By** in the export dialog:

- **Parent Objects** (default) retains the existing grouping by top-level object
  parent, described below.
- **Top-level Collections** exports each direct child of **Scene Collection** as
  one FBX named after that collection. All meshes in nested subcollections are
  included in that same file. For example, `Wasteland_Bldg_SM_A_grp` and
  `Wasteland_Bldg_MD_B_grp` produce two FBXs containing their respective parts.

In collection mode, **Selected Assets Only** includes whole top-level collections
containing a selected object. Select a mesh or other object within the collection;
highlighting a collection row in the Outliner alone is not used as the selection.
An object linked into multiple nested collections is included only once per FBX.
If linked into two different top-level collections, it is included in both assets.
Hidden/excluded meshes are included. Empty collections and objects directly in
Scene Collection are skipped in this mode; use Parent Objects for loose objects.

Collections have no object origin, so their **instance offset** supplies the pivot
(zero by default). With **Keep Scene Positions** off, that offset is subtracted
from every part's world position; it never recenters parts individually. With it
on, world positions are preserved. Mesh parenting inside a collection is retained.
Parents outside it do not bring unrelated meshes into the export; their children's
world transforms are preserved under the exported asset root instead.

For **Parent Objects** grouping:

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

Blender's **bottom status bar** shows a progress meter beside the current operation:
asset/part preparation, textures, background FBX writing, publishing and cleanup.
The bar reaches 100% only after temporary scenes have been removed. This is work
progress, not an estimated time; it may hold steady while one large file is written.

The UI operator advances using Blender's modal timer, returning control between
textures, parts and files. The slow FBX writer runs in a **separate, hidden background
Blender process** using a temporary snapshot of the prepared asset. The main window
can keep drawing and handling viewport navigation while that process works. Scene
editing is blocked in the export window to keep the asset consistent.

Press **Esc** to cancel before publishing starts. Cancellation stops only the
background process started by this export and removes the staged files and
temporary scenes. Once publishing begins, the short finalization phase finishes
instead of stopping with only some destination files replaced. The status bar
shows when cancellation is available.

Individual Blender operations such as evaluating a heavy modifier, saving an image
or writing the temporary asset snapshot cannot be subdivided and can still pause
the UI during preparation. FBX serialization itself no longer blocks the main UI.
Temporary snapshots use space in the export folder and are removed automatically.
Your open `.blend` is not saved, switched or reopened to start the worker.

Timestamped `Ted FBX` messages in **Window > Toggle System Console** on Windows
identify the last started operation if a step takes unusually long. Background
scripts using the synchronous helper still run to completion without UI timers.

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
children, recursive collection grouping and pivots, shared collection memberships,
progress completion/cleanup, background-worker round trips, modal steps,
worker cancellation, overwrite protection, and cleanup after missing textures or
an injected export failure. Tested on Blender 4.5 and 5.2.

`tests/test_modal_ui.py` is a separate-process GUI smoke test. It starts an export,
checks real status-bar redraws and UI timer activity while the worker runs, and
quits only its own test Blender. Run it only in a new factory-startup Blender
process, never from a working Blender session.

`tests/benchmark_individual_fbx.py` creates a small synthetic scene with 120 meshes
in six parent groups in a separate Blender process, then times the export. It does
not connect to or change an existing Blender session.
