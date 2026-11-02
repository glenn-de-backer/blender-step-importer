"""Blender import options and their presentation. Geometry work lives in core.py."""

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from .core import StepImportEngine


def mesh_tolerances(quality, chord_mm, angle_deg):
    presets = {
        "DRAFT": (2.0, 60.0),
        "VERY_LOW": (1.0, 45.0),
        "LOW": (0.3, 30.0),
        "MEDIUM": (0.1, 15.0),
        "HIGH": (0.02, 10.0),
    }
    return presets.get(quality, (chord_mm, angle_deg))


class IMPORT_OT_step_nvidia(StepImportEngine, bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.step_nvidia"
    bl_label = "SimpleStep importer"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".step"

    filter_glob: StringProperty(
        default="*.step;*.stp;*.STEP;*.STP",
        options={"HIDDEN"},
    )

    quality: EnumProperty(
        name="Mesh quality",
        description="Lower tolerance makes a denser mesh and takes more time",
        items=(
            ("ADAPTIVE", "Adaptive", "Mesh detail follows each part's size; adjust rounding below"),
            ("DRAFT", "Draft", "Very coarse preview: 2.0 mm chord, 60 degree angle"),
            ("VERY_LOW", "Very Low", "Coarse preview: 1.0 mm chord, 45 degree angle"),
            ("LOW", "Low", "Faster preview: 0.3 mm chord, 30 degree angle"),
            ("MEDIUM", "Medium", "Balanced: 0.1 mm chord, 15 degree angle"),
            ("HIGH", "High", "Finer detail: 0.02 mm chord, 10 degree angle"),
            ("CUSTOM", "Custom", "Set chord and angle tolerances manually"),
        ),
        default="MEDIUM",
    )

    chord_mm: FloatProperty(
        name="Chord tolerance (mm)",
        default=0.1,
        min=0.000001,
        precision=4,
    )

    angle_deg: FloatProperty(
        name="Angular tolerance (degrees)",
        default=15.0,
        min=0.1,
        max=180.0,
    )

    adaptive_percent: FloatProperty(
        name="Relative detail (%)",
        description="Chord tolerance as a percentage of each body's diagonal",
        default=0.1,
        min=0.001,
        max=10.0,
        precision=3,
    )

    adaptive_min_chord_mm: FloatProperty(
        name="Minimum chord (mm)",
        description="Prevent extremely dense meshes on very small parts",
        default=0.01,
        min=0.0001,
        max=10.0,
        precision=4,
    )

    adaptive_max_chord_mm: FloatProperty(
        name="Maximum chord (mm)",
        description="Smaller values keep rounded details smoother on large parts",
        default=0.02,
        min=0.0001,
        max=10.0,
        precision=4,
    )

    adaptive_curve_angle_deg: FloatProperty(
        name="Curve smoothness (degrees)",
        description="Smaller angles add triangles to curves and fillets",
        default=10.0,
        min=1.0,
        max=90.0,
        precision=1,
    )

    show_adaptive_advanced: BoolProperty(
        name="Advanced adaptive controls",
        default=False,
        options={"SKIP_SAVE"},
    )

    use_normals: BoolProperty(
        name="Use CAD surface normals",
        default=True,
    )

    origin_mode: EnumProperty(
        name="Object origin",
        description="Changes the object pivot only; the visible shape stays in place",
        items=(
            ("CENTER", "Bounds center", "Put each pivot at its local mesh bounds center"),
            ("CAD", "CAD origin", "Keep each prototype's source CAD pivot"),
        ),
        default="CENTER",
    )

    source_up_axis: EnumProperty(
        name="Source up axis",
        description="Rotate the complete model so the selected STEP axis points up in Blender",
        items=(
            ("Z", "Z up (unchanged)", "Keep the original STEP orientation"),
            ("Y", "Y up", "Rotate the whole model so source +Y points toward Blender +Z"),
            ("X", "X up", "Rotate the whole model so source +X points toward Blender +Z"),
        ),
        default="Z",
    )

    share_meshes: BoolProperty(
        name="Share repeated mesh data",
        description="Repeated parts use the same Blender mesh; turn off to edit each separately",
        default=True,
    )

    use_step_colors: BoolProperty(
        name="Import STEP colors",
        description="Create Blender materials for colors assigned to STEP parts",
        default=True,
    )

    color_uncolored_parts: BoolProperty(
        name="Color parts without STEP colors",
        description=(
            "Assign stable varied colors to uncolored parts; repeated parts "
            "share a color and STEP colors take precedence"
        ),
        default=False,
    )

    repair_failed: BoolProperty(
        name="Repair failed bodies",
        description=(
            "After a tessellation failure, heal a copy and retry. "
            "Repair can change features; full CAD validity is not established. "
            "Repaired objects and an import report are marked"
        ),
        default=True,
    )

    allow_non_manifold_repair: BoolProperty(
        name="Allow non-manifold repaired meshes",
        description=(
            "Allow repaired meshes with non-manifold connectivity for visual "
            "inspection. Check the result before using operations that require "
            "manifold solids. Faces that fail tessellation are still rejected"
        ),
        default=True,
    )

    skip_failed: BoolProperty(
        name="Skip failed parts",
        description=(
            "Continue importing other bodies if a part fails conversion or "
            "tessellation. Skipped geometry is absent; names and errors are "
            "saved in SimpleStep Import Report"
        ),
        default=True,
    )

    parallel_bodies: IntProperty(
        name="Parallel bodies",
        description="Number of isolated conversion workers (more uses more memory)",
        default=4,
        min=1,
        max=8,
    )

    def effective_tolerances(self):
        if self.quality == "ADAPTIVE":
            if self.adaptive_min_chord_mm > self.adaptive_max_chord_mm:
                raise ValueError("Adaptive minimum chord must not exceed maximum chord.")
            return self.adaptive_max_chord_mm, self.adaptive_curve_angle_deg
        return mesh_tolerances(self.quality, self.chord_mm, self.angle_deg)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "quality")

        if self.quality == "ADAPTIVE":
            box = layout.box()
            box.label(text="Rounded details", icon="MOD_TRIANGULATE")
            box.prop(self, "adaptive_max_chord_mm", text="Maximum chord")
            box.prop(self, "adaptive_curve_angle_deg", text="Curve angle")
            box.prop(self, "show_adaptive_advanced", text="Advanced controls")
            if self.show_adaptive_advanced:
                box.prop(self, "adaptive_percent")
                box.prop(self, "adaptive_min_chord_mm")
                if self.adaptive_min_chord_mm > self.adaptive_max_chord_mm:
                    box.label(text="Minimum must be at most maximum", icon="ERROR")
        elif self.quality == "CUSTOM":
            layout.prop(self, "chord_mm")
            layout.prop(self, "angle_deg")

        layout.separator()
        layout.prop(self, "parallel_bodies")
        layout.prop(self, "source_up_axis")
        layout.prop(self, "origin_mode")
        layout.prop(self, "share_meshes")
        layout.prop(self, "use_step_colors")
        if self.use_step_colors:
            layout.prop(self, "color_uncolored_parts")
        layout.prop(self, "use_normals")
        layout.prop(self, "repair_failed")
        if self.repair_failed:
            layout.prop(self, "allow_non_manifold_repair")
        layout.prop(self, "skip_failed")

