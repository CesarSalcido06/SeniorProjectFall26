"""Clean up UniRig's merged output and export it for engines.

Writes a GLB (three.js preview, Unity via glTFast) and an FBX Unity imports natively
(textures embedded, armature kept). Runs with the UniRig environment, which ships Blender
as a Python module (bpy 4.2):
    .venv-unirig/bin/python tools/finalize_rig.py merged.glb rigged.glb rigged.fbx
"""
import os
import sys

import bpy

src, out_glb, out_fbx = sys.argv[1:4]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=src)

armatures = [o for o in bpy.data.objects if o.type == "ARMATURE"]
if not armatures:
    sys.exit("no armature in rigged model")
armature = armatures[0]
armature.name = armature.data.name = "Armature"

# UniRig's merge leaves a stray icosphere next to the skinned mesh; keep only meshes bound to the armature
for obj in [o for o in bpy.data.objects if o.type == "MESH"]:
    if obj.parent is not armature or not obj.vertex_groups:
        bpy.data.objects.remove(obj, do_unlink=True)
meshes = [o for o in bpy.data.objects if o.type == "MESH"]
if not meshes:
    sys.exit("no skinned mesh in rigged model")
for i, obj in enumerate(meshes):
    obj.name = "Model" if i == 0 else f"Model.{i}"

bpy.ops.export_scene.gltf(filepath=out_glb, export_format="GLB", export_skins=True, export_animations=False)
bpy.ops.export_scene.fbx(
    filepath=out_fbx,
    path_mode="COPY",
    embed_textures=True,
    add_leaf_bones=False,  # Blender's extra end bones just clutter Unity's hierarchy
    bake_anim=False,
    apply_scale_options="FBX_SCALE_UNITS",
)
print(f"bones={len(armature.data.bones)} meshes={len(meshes)}")

# bpy can segfault while tearing down at interpreter exit (after the files are written), which would
# look like a failure to the caller. The work is done, so skip the teardown.
sys.stdout.flush()
os._exit(0)
