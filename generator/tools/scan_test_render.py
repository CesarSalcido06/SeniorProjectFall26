"""Renders a fake phone photo session of a plastic part whose exact size we know, to test the scan pipeline.

Usage (needs Blender's Python module, so use the UniRig environment):
  .venv-unirig/bin/python tools/scan_test_render.py <out dir> --surface speckled|plain [--photos 36]

Writes <out dir>/photos/*.jpg and <out dir>/truth.npz (the part's real mesh, in meters, z up, table at z=0).
Compare a finished scan against it with scan_test_compare.py.
"""
import argparse
import math
import random
from pathlib import Path

import bpy  # has to come before bmesh, which only exists once Blender is loaded
import bmesh
import numpy as np
from mathutils import Vector

parser = argparse.ArgumentParser()
parser.add_argument("out")
parser.add_argument("--surface", choices=["speckled", "plain"], default="speckled")
parser.add_argument("--photos", type=int, default=36)
parser.add_argument("--samples", type=int, default=48)
args = parser.parse_args()
out = Path(args.out)
(out / "photos").mkdir(parents=True, exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene


def box(name, size, center):
    bpy.ops.mesh.primitive_cube_add(size=1, location=center)
    obj = bpy.context.object
    obj.name, obj.scale = name, size
    bpy.ops.object.transform_apply(scale=True)
    return obj


def cylinder(name, radius, depth, center):
    bpy.ops.mesh.primitive_cylinder_add(vertices=64, radius=radius, depth=depth, location=center)
    obj = bpy.context.object
    obj.name = name
    return obj


def boolean(target, tool, operation):
    mod = target.modifiers.new(operation, "BOOLEAN")
    mod.object, mod.operation = tool, operation
    bpy.context.view_layer.objects.active = target
    bpy.ops.object.modifier_apply(modifier=mod.name)
    bpy.data.objects.remove(tool)


# The part: a 60 x 35 x 20 mm bracket with a raised end, a round peg, and a hole. Asymmetric on purpose.
MM = 0.001
part = box("Part", (60 * MM, 35 * MM, 6 * MM), (0, 0, 3 * MM))
boolean(part, box("Raised", (14 * MM, 35 * MM, 20 * MM), (23 * MM, 0, 10 * MM)), "UNION")
boolean(part, cylinder("Peg", 6 * MM, 14 * MM, (-5 * MM, 6 * MM, 7 * MM)), "UNION")
boolean(part, cylinder("Hole", 4 * MM, 30 * MM, (-20 * MM, -8 * MM, 3 * MM)), "DIFFERENCE")
bevel = part.modifiers.new("Bevel", "BEVEL")
bevel.width, bevel.segments = 0.6 * MM, 3
bpy.context.view_layer.objects.active = part
bpy.ops.object.modifier_apply(modifier=bevel.name)

mesh = bmesh.new()
mesh.from_mesh(part.data)
bmesh.ops.triangulate(mesh, faces=mesh.faces[:])
np.savez(
    out / "truth.npz",
    vertices=np.array([v.co[:] for v in mesh.verts]),
    faces=np.array([[v.index for v in f.verts] for f in mesh.faces]),
)
mesh.free()

# Plastic: "speckled" stands in for a part dusted with powder, "plain" for a shiny part straight out of the bag
plastic = bpy.data.materials.new("Plastic")
plastic.use_nodes = True
nodes, links = plastic.node_tree.nodes, plastic.node_tree.links
shader = nodes["Principled BSDF"]
if args.surface == "plain":
    shader.inputs["Base Color"].default_value = (0.75, 0.22, 0.03, 1)
    shader.inputs["Roughness"].default_value = 0.25
else:
    shader.inputs["Roughness"].default_value = 0.7
    noise = nodes.new("ShaderNodeTexNoise")
    # Texture space spans the part's whole bounding box, so 70 gives flecks about 1 mm wide. Much higher
    # and they shrink below a pixel and average out to a plain color.
    noise.inputs["Scale"].default_value = 70
    noise.inputs["Detail"].default_value = 6
    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position, ramp.color_ramp.elements[1].position = 0.42, 0.58
    ramp.color_ramp.elements[0].color = (0.35, 0.09, 0.02, 1)
    ramp.color_ramp.elements[1].color = (0.9, 0.6, 0.45, 1)
    links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    links.new(ramp.outputs["Color"], shader.inputs["Base Color"])
part.data.materials.append(plastic)

# The table: a busy printed sheet (like newspaper) so the camera can always be located
bpy.ops.mesh.primitive_plane_add(size=0.6, location=(0, 0, 0))
table = bpy.context.object
paper = bpy.data.materials.new("Paper")
paper.use_nodes = True
nodes, links = paper.node_tree.nodes, paper.node_tree.links
shader = nodes["Principled BSDF"]
shader.inputs["Roughness"].default_value = 0.9
noise = nodes.new("ShaderNodeTexNoise")
noise.inputs["Scale"].default_value = 320
noise.inputs["Detail"].default_value = 8
ramp = nodes.new("ShaderNodeValToRGB")
ramp.color_ramp.elements[0].position, ramp.color_ramp.elements[1].position = 0.45, 0.55
ramp.color_ramp.elements[0].color = (0.04, 0.04, 0.05, 1)
ramp.color_ramp.elements[1].color = (0.85, 0.85, 0.8, 1)
links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
links.new(ramp.outputs["Color"], shader.inputs["Base Color"])
table.data.materials.append(paper)

# Soft window-like light plus a dim room
bpy.ops.object.light_add(type="AREA", location=(0.25, -0.2, 0.6), rotation=(0.4, 0.35, 0))
bpy.context.object.data.size, bpy.context.object.data.energy = 0.6, 14
world = bpy.data.worlds.new("Room")
world.use_nodes = True
world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.6
world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.9, 0.9, 0.9, 1)
scene.world = world

# A phone's main camera: about 26 mm equivalent, 4:3
camera_data = bpy.data.cameras.new("Phone")
camera_data.lens, camera_data.sensor_width = 26, 36
camera = bpy.data.objects.new("Phone", camera_data)
scene.collection.objects.link(camera)
scene.camera = camera

scene.render.engine = "CYCLES"
scene.cycles.samples = args.samples
scene.cycles.use_denoising = False
scene.render.resolution_x, scene.render.resolution_y = 1600, 1200
scene.render.image_settings.file_format = "JPEG"
scene.render.image_settings.quality = 92
try:
    prefs = bpy.context.preferences.addons["cycles"].preferences
    prefs.compute_device_type = "CUDA"
    prefs.get_devices()
    for device in prefs.devices:
        device.use = device.type == "CUDA"
    scene.cycles.device = "GPU"
except Exception as exc:
    print("Rendering on the CPU:", exc)

# Walk around the part in three rings, hand-held: a little jitter in position and aim
rng = random.Random(7)
rings = (25, 45, 68)
for i in range(args.photos):
    ring = rings[i % len(rings)]
    azimuth = math.radians(i * 360 / args.photos * 3 % 360 + rng.uniform(-5, 5))
    elevation = math.radians(ring + rng.uniform(-4, 4))
    radius = 0.15 + rng.uniform(-0.012, 0.012)
    position = Vector((
        radius * math.cos(elevation) * math.cos(azimuth),
        radius * math.cos(elevation) * math.sin(azimuth),
        radius * math.sin(elevation),
    ))
    aim = Vector((rng.uniform(-0.008, 0.008), rng.uniform(-0.008, 0.008), rng.uniform(0.005, 0.015)))
    camera.location = position
    camera.rotation_euler = (aim - position).to_track_quat("-Z", "Y").to_euler()
    bpy.context.view_layer.update()
    scene.render.filepath = str(out / "photos" / f"shot_{i:03d}.jpg")
    bpy.ops.render.render(write_still=True)
    print(f"rendered {i + 1}/{args.photos}", flush=True)
