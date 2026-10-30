"""Open FBX files with Autodesk's own FBX SDK and report what Maya/3ds Max/Unity will see.

Checks that the file loads, and for each file: meshes, materials, textures (embedded or missing),
skeleton bones, skin weights (every vertex weighted, weights summing to ~1), units and up axis.
Exits non-zero if any file has a problem, so it can run after each generation.

Setup (once): install the FBX Python SDK (Python 3.10 only) into the generator environment. The
installer asks you to accept Autodesk's license:
    curl -LO https://damassets.autodesk.net/content/dam/autodesk/www/files/fbx202037_fbxpythonsdk_linux.tar.gz
    tar xzf fbx202037_fbxpythonsdk_linux.tar.gz && mkdir -p ~/fbxsdk
    ./fbx202037_fbxpythonsdk_linux ~/fbxsdk
    uv pip install --python .venv/bin/python ~/fbxsdk/fbx-2020.3.7-cp310-cp310-manylinux1_x86_64.whl

Usage (from generator/):
    .venv/bin/python tools/check_fbx.py ../backend/data/jobs/<job id>/*.fbx
"""
import sys

import fbx

WEIGHT_TOLERANCE = 0.01


def nodes(root):
    yield root
    for i in range(root.GetChildCount()):
        yield from nodes(root.GetChild(i))


def check(manager, path: str) -> list[str]:
    problems = []
    scene = fbx.FbxScene.Create(manager, "")
    importer = fbx.FbxImporter.Create(manager, "")
    if not importer.Initialize(path, -1, manager.GetIOSettings()):
        return [f"FBX SDK can't open it: {importer.GetStatus().GetErrorString()}"]
    major, minor, rev = importer.GetFileVersion()
    importer.Import(scene)
    importer.Destroy()

    settings = scene.GetGlobalSettings()
    unit = settings.GetSystemUnit().GetScaleFactor()  # centimeters per unit
    up, _ = settings.GetAxisSystem().GetUpVector()
    up_name = {fbx.FbxAxisSystem.EUpVector.eXAxis: "X", fbx.FbxAxisSystem.EUpVector.eYAxis: "Y",
               fbx.FbxAxisSystem.EUpVector.eZAxis: "Z"}.get(up, str(up))
    print(f"  file format FBX {major}.{minor}.{rev}, unit {unit:g} cm, {up_name}-up")

    all_nodes = list(nodes(scene.GetRootNode()))
    meshes = [n for n in all_nodes if isinstance(n.GetNodeAttribute(), fbx.FbxMesh)]
    bones = [n for n in all_nodes if isinstance(n.GetNodeAttribute(), fbx.FbxSkeleton)]
    if not meshes:
        problems.append("no meshes")

    for node in meshes:
        mesh = node.GetNodeAttribute()
        verts, polys = mesh.GetControlPointsCount(), mesh.GetPolygonCount()
        uv_sets = mesh.GetElementUVCount()
        print(f"  mesh {node.GetName()!r}: {verts} vertices, {polys} polygons, {uv_sets} UV set(s)")
        if uv_sets == 0:
            problems.append(f"mesh {node.GetName()!r} has no UVs, so textures can't map onto it")

        for m in range(node.GetMaterialCount()):
            material = node.GetMaterial(m)
            prop = material.FindProperty(fbx.FbxSurfaceMaterial.sDiffuse)
            textures = [prop.GetSrcObject(fbx.FbxCriteria.ObjectType(fbx.FbxFileTexture.ClassId), t)
                        for t in range(prop.GetSrcObjectCount(fbx.FbxCriteria.ObjectType(fbx.FbxFileTexture.ClassId)))]
            names = [t.GetRelativeFileName() or t.GetFileName() for t in textures]
            print(f"    material {material.GetName()!r}: base color texture {', '.join(names) or 'none'}")
            if not textures:
                problems.append(f"material {material.GetName()!r} has no base color texture")
            for name in names:
                if not name.lower().endswith((".png", ".jpg", ".jpeg", ".tga", ".tif", ".tiff")):
                    problems.append(f"texture {name!r} has no image extension, so Maya/Unity may not load it")

        skins = [mesh.GetDeformer(d, fbx.FbxDeformer.EDeformerType.eSkin)
                 for d in range(mesh.GetDeformerCount(fbx.FbxDeformer.EDeformerType.eSkin))]
        if not skins:
            continue
        totals = [0.0] * verts
        clusters = 0
        for skin in skins:
            for c in range(skin.GetClusterCount()):
                cluster = skin.GetCluster(c)
                clusters += 1
                indices, weights = cluster.GetControlPointIndices(), cluster.GetControlPointWeights()
                for i in range(cluster.GetControlPointIndicesCount()):
                    totals[indices[i]] += weights[i]
        unweighted = sum(1 for t in totals if t == 0)
        off = sum(1 for t in totals if t and abs(t - 1) > WEIGHT_TOLERANCE)
        print(f"    skinned to {clusters} bones; unweighted vertices {unweighted}, weights not summing to 1: {off}")
        if unweighted:
            problems.append(f"{unweighted} vertices aren't attached to any bone (they won't move)")
        if off:
            problems.append(f"{off} vertices have weights that don't sum to 1")

    if bones:
        print(f"  skeleton: {len(bones)} bones (root {bones[0].GetName()!r})")

    # Embedded media shows up as a .fbm folder next to the file once Autodesk tools extract it
    for t in range(scene.GetSrcObjectCount(fbx.FbxCriteria.ObjectType(fbx.FbxVideo.ClassId))):
        video = scene.GetSrcObject(fbx.FbxCriteria.ObjectType(fbx.FbxVideo.ClassId), t)
        if not video.ContentIsLoaded() and not video.GetFileName():
            problems.append(f"texture {video.GetName()!r} isn't embedded and has no file path")
    scene.Destroy()
    return problems


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__)
        return 2
    manager = fbx.FbxManager.Create()
    io_settings = fbx.FbxIOSettings.Create(manager, fbx.IOSROOT)
    # Don't write a <name>.fbm folder of extracted textures next to every file we check
    io_settings.SetBoolProp("Import|AdvOptGrp|FileFormat|Fbx|ExtractEmbeddedData", False)  # IMP_FBX_EXTRACT_EMBEDDED_DATA
    manager.SetIOSettings(io_settings)
    failed = 0
    for path in paths:
        print(path)
        problems = check(manager, path)
        for p in problems:
            print(f"  PROBLEM: {p}")
        print("  OK" if not problems else f"  {len(problems)} problem(s)")
        failed += bool(problems)
    manager.Destroy()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
