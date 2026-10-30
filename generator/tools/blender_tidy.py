"""Shared clean-up before exporting FBX from Blender, so Autodesk tools (Maya, 3ds Max, FBX Review) and Unity
import our models cleanly. Imported by glb_to_fbx.py and finalize_rig.py."""
import bmesh
import bpy


def tidy(meshes: list) -> None:
    """Name meshes "Model", weld duplicate vertices, and give embedded textures real file names."""
    for i, obj in enumerate(meshes):
        obj.name = "Model" if i == 0 else f"Model.{i}"
        obj.data.name = obj.name
        # glTF round trips can leave every triangle with its own vertices (5x the vertex count in Maya/Unity).
        # UVs and normals live on face corners in Blender, so welding positions keeps texture seams intact.
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
        bm.to_mesh(obj.data)
        bm.free()

    # Textures packed from a GLB have no file name, so the FBX embeds them nameless and Autodesk tools
    # extract them as "<model>.fbm" with no extension. Give each one a .png name.
    for i, image in enumerate(img for img in bpy.data.images if img.source == "FILE"):
        name = "Model_basecolor" if i == 0 else f"Model_texture{i}"
        image.name = name
        image.filepath_raw = f"//{name}.png"
        image.file_format = "PNG"
