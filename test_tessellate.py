import json
from pathlib import Path

import usd_brep as sm


source = Path(__file__).parent / "drone_frame.usdc"
output = source.with_name("drone_frame.mesh.json")

# Keep the source BReps alive while tessellating and extracting meshes.
breps = sm.usd.import_breps(str(source), heal=False)

if not breps:
    raise RuntimeError("The USD file contains no imported BReps.")

print(f"Imported BReps: {len(breps)}")

total_vertices = 0
total_triangles = 0
meshes = []

for index, brep in enumerate(breps, start=1):
    # Coordinates and chord tolerance are in millimeters.
    # Strict tessellation raises an error if faces fail.
    mesh = sm.tessellate(
        brep,
        chord_height_tolerance=0.1,
        curve_angle_tolerance_deg=15.0,
        surface_angle_tolerance_deg=15.0,
    )

    # Per-corner normals preserve shading across CAD surface boundaries.
    arrays = mesh.to_mesh_arrays(face_varying_normals=True)

    vertices = len(arrays["points"])
    triangles = len(arrays["face_normals"])

    if vertices == 0 or triangles == 0:
        raise RuntimeError(f"BRep {index} produced an empty mesh.")

    meshes.append({
        "name": f"DroneFrame_{index:03d}",
        "points": arrays["points"],
        "faces": arrays["faces"],
        "normals": arrays["normals"],
    })

    total_vertices += vertices
    total_triangles += triangles

    print(
        f"BRep {index}: {brep.face_count()} CAD faces, "
        f"{vertices} vertices, {triangles} triangles"
    )

# Serialize first so invalid numeric values cannot leave a partial JSON file.
payload = json.dumps(
    {
        "units": "mm",
        "meshes": meshes,
    },
    allow_nan=False,
)

output.write_text(payload, encoding="utf-8")

print(f"Total: {total_vertices} vertices, {total_triangles} triangles")
print("Tessellation completed successfully.")
print(f"Mesh data saved to: {output}")