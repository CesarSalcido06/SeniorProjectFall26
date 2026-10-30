"""Convert a GLB to an FBX Unity can import directly (textures embedded, armature kept).

Runs with the UniRig environment, which ships Blender as a Python module (bpy 4.2):
    .venv-unirig/bin/python tools/glb_to_fbx.py in.glb out.fbx
"""
import os
import sys

import bpy

src, dst = sys.argv[1], sys.argv[2]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=src)
bpy.ops.export_scene.fbx(
    filepath=dst,
    path_mode="COPY",
    embed_textures=True,
    add_leaf_bones=False,  # Blender's extra end bones just clutter Unity's hierarchy
    bake_anim=False,
    apply_scale_options="FBX_SCALE_UNITS",
)

# bpy can segfault while tearing down at interpreter exit (after the files are written), which would
# look like a failure to the caller. The work is done, so skip the teardown.
sys.stdout.flush()
os._exit(0)
