import json
import math
from pathlib import Path

import bpy


source = Path(
    r"C:\projects\blender-step-importer\drone_frame.mesh.json"
)

if bpy.context.mode != "OBJECT":
    raise RuntimeError("Switch Blender to Object Mode before running.")

with source.open(encoding="utf-8") as file:
    payload = json.load(file)

if payload.get("units") != "mm":
    raise ValueError("Expected mesh coordinates in millimeters.")

# Respect Blender's existing scene scale.
scene_scale = bpy.context.scene.unit_settings.scale_length
scale = 0.001 / scene_scale

prepared = []

for item in payload["meshes"]:
    points = [
        tuple(component * scale for component in point)
        for point in item["points"]
    ]

    if not points or any(
        len(point) != 3
        or not all(math.isfinite(value) for value in point)
        for point in points
    ):
        raise ValueError("Invalid mesh coordinates.")

    # NVIDIA's polygon stream is:
    # [corner_count, vertex_index, vertex_index, ...]
    stream = item["faces"]
    faces = []
    cursor = 0

    while cursor < len(stream):
        count = stream[cursor]
        end = cursor + 1 + count

        if count < 3 or end > len(stream):
            raise ValueError("Invalid polygon stream.")

        face = stream[cursor + 1:end]

        if any(index < 0 or index >= len(points) for index in face):
            raise ValueError("Polygon index out of range.")

        faces.append(face)
        cursor = end

    normals = item["normals"]

    if len(normals) != sum(len(face) for face in faces):
        raise ValueError("Corner-normal count does not match polygons.")

    prepared.append((item["name"], points, faces, normals))

if not prepared:
    raise ValueError("No meshes found.")

# Create a new collection without removing existing scene objects.
collection = bpy.data.collections.new("STEP Import Test")
bpy.context.scene.collection.children.link(collection)

for obj in bpy.context.selected_objects:
    obj.select_set(False)

created = []

for name, points, faces, normals in prepared:
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(points, [], faces)
    mesh.update()

    # Custom corner normals retain the source CAD surface shading.
    for polygon in mesh.polygons:
        polygon.use_smooth = True

    mesh.normals_split_custom_set(normals)

    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj.select_set(True)
    created.append(obj)

bpy.context.view_layer.objects.active = created[0]

print(f"Imported {len(created)} mesh object(s) from {source.name}")