from __future__ import annotations

from src.cad_agent.active_model_feature_skill import ActiveModelFeatureSkill
from src.cad_agent.active_model_through_hole import ActiveModelThroughHoleExecutor
from src.cad_agent.advanced_feature_skill import AdvancedFeatureSkill
from src.cad_agent.agents_orchestrator.skill_router_agent import SkillRouterAgent
from src.cad_agent.design_planner import DesignPlanner
from src.cad_agent.skill_planner import SkillPlanner
from src.cad_agent.stage_planner import apply_stage_plan, infer_stage_plan


def gearbox_design() -> dict:
    return {
        "schema_version": "vibecad.design.v1",
        "source_brief": "Create only a parameterized industrial gearbox housing Part.",
        "part_family": "industrial_gearbox_housing",
        "parameters": {
            "unit": "mm",
            "length": 280.0,
            "width": 210.0,
            "thickness": 18.0,
            "material": "EN-GJL-250 gray cast iron",
        },
        "features": [
            {"name": "MountingBase", "type": "base_plate", "params": {"length": 280, "width": 210, "thickness": 18}, "required": True},
            {"name": "BaseCornerFillets", "type": "fillet", "params": {"radius": 12, "target": "four_outer_corners"}, "required": True},
            {"name": "HousingBody", "type": "boss", "params": {"length": 190, "width": 120, "height": 118, "position": "center_top"}, "required": True},
            {"name": "TopFlange", "type": "boss", "params": {"length": 214, "width": 144, "height": 10, "position": "center_top"}, "required": True},
            {"name": "FrontRibs", "type": "rib", "params": {"axis": "y", "face_offset_mm": 60, "center_xz_mm": [0, 44], "center_positions_mm": [-82, 82], "width_mm": 10, "height_mm": 88, "depth_mm": 10, "base_z_mm": 0}, "required": True},
            {"name": "InputFrontBoss", "type": "side_boss", "params": {"axis": "y", "face_offset_mm": 60, "center_xz_mm": [-52, 62], "diameter_mm": 82, "depth_mm": 18}, "required": True},
            {"name": "InputBore", "type": "side_hole", "params": {"axis": "y", "face_offset_mm": 78, "center_xz_mm": [-52, 62], "diameter_mm": 38, "placement": "center"}, "required": True},
            {"name": "InputFlangeHoles", "type": "side_hole", "params": {"axis": "y", "face_offset_mm": 78, "center_xz_mm": [-52, 62], "diameter_mm": 8.5, "placement": "bolt_circle", "pcd_mm": 64, "count": 6, "start_angle_deg": 0}, "required": True},
            {"name": "MountingHoles", "type": "through_hole", "params": {"diameter": 14, "count": 4, "placement": "corner_offsets", "edge_offsets_mm": {"x": 25, "y": 22}}, "required": True},
            {"name": "HousingCavity", "type": "pocket", "params": {"length": 166, "width": 96, "depth": 114, "corner_radius": 12, "position": "center"}, "required": True},
        ],
        "outputs": ["SLDPRT"],
        "datums": [],
        "tolerances": [],
        "assembly_relations": [],
        "unsupported_features": [],
        "risks": [],
        "needs_confirmation": False,
        "review_plan": {"expected_outputs": ["SLDPRT"], "views": ["isometric", "front", "top", "right"], "checks": ["features_created", "output_exists"]},
    }


def staged_gearbox_design() -> dict:
    return apply_stage_plan(gearbox_design(), infer_stage_plan("Create only the 3D gearbox housing Part.", mode="model_3d"))


def test_advanced_parameter_contracts() -> None:
    boss = AdvancedFeatureSkill.normalize_request(
        "side_boss",
        {"axis": "y", "face_offset_mm": 60, "center_xz_mm": [-52, 62], "diameter_mm": 82, "depth_mm": 18},
    )
    assert boss["success"] is True
    assert boss["center_uv_mm"] == [-52.0, 62.0]
    assert boss["diameter_mm"] == 82.0

    holes = AdvancedFeatureSkill.normalize_request(
        "side_hole",
        {"axis": "y", "face_offset_mm": 78, "center_xz_mm": [-52, 62], "diameter_mm": 8.5, "placement": "bolt_circle", "pcd_mm": 64, "count": 6},
    )
    assert holes["success"] is True
    assert holes["count"] == 6
    assert len(holes["centers_uv_mm"]) == 6
    assert holes["end_condition"] == "through_all"

    through_next = AdvancedFeatureSkill.normalize_request(
        "side_hole",
        {
            "axis": "x",
            "face_offset_mm": -91,
            "center_yz_mm": [28, 0],
            "diameter_mm": 14,
            "placement": "center",
            "end_condition": "through_next",
        },
    )
    assert through_next["success"] is True
    assert through_next["end_condition"] == "through_next"
    assert through_next["through_all"] is False
    assert through_next["depth_mm"] == 0.0

    ribs = AdvancedFeatureSkill.normalize_request(
        "rib",
        {"axis": "y", "face_offset_mm": -60, "center_xz_mm": [0, 44], "center_positions_mm": [-82, 82], "width_mm": 10, "height_mm": 88, "depth_mm": 10, "base_z_mm": 0},
    )
    assert ribs["success"] is True
    assert ribs["count"] == 2

    native_rib = AdvancedFeatureSkill.normalize_request(
        "rib",
        {
            "profile": "native_line",
            "sketch_plane_feature": "HousingRibPlane_Xm50",
            "line_points_mm": [[50, 11], [50, 45]],
            "thickness_mm": 6,
            "is_two_sided": True,
            "reverse_material_direction": True,
        },
    )
    assert native_rib["success"] is True
    assert native_rib["profile"] == "native_line"
    assert native_rib["line_points_mm"] == [[50.0, 11.0], [50.0, 45.0]]
    assert native_rib["thickness_mm"] == 6.0
    assert native_rib["is_two_sided"] is True
    assert native_rib["is_drafted_from_wall"] is False

    x_boss = AdvancedFeatureSkill.normalize_request(
        "side_boss",
        {
            "axis": "x",
            "face_offset_mm": -89,
            "center_yz_mm": [28, 0],
            "diameter_mm": 34,
            "depth_mm": 2,
        },
    )
    assert x_boss["success"] is True
    assert x_boss["axis"] == "x"
    assert x_boss["center_uv_mm"] == [28.0, 0.0]
    assert x_boss["merge_result"] is True
    assert AdvancedFeatureSkill._model_point("x", -0.089, [0.028, 0.0]) == (
        -0.089,
        0.028,
        0.0,
    )


def test_invalid_advanced_parameters_are_blocked() -> None:
    assert AdvancedFeatureSkill.normalize_request("side_boss", {"axis": "z"})["success"] is False
    assert AdvancedFeatureSkill.normalize_request(
        "side_hole",
        {"axis": "y", "face_offset_mm": 78, "center_xz_mm": [0, 50], "diameter_mm": 8, "placement": "bolt_circle", "pcd_mm": 60, "count": 1},
    )["success"] is False
    assert AdvancedFeatureSkill.normalize_request(
        "side_hole",
        {"axis": "x", "face_offset_mm": -91, "center_yz_mm": [28, 0], "diameter_mm": 14, "end_condition": "blind"},
    )["success"] is False
    assert AdvancedFeatureSkill.normalize_request(
        "side_hole",
        {"axis": "x", "face_offset_mm": -91, "center_yz_mm": [28, 0], "diameter_mm": 14, "end_condition": "up_to_surface"},
    )["success"] is False


def test_side_hole_uses_through_next_and_verifies_removed_volume(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeModel:
        def ForceRebuild3(self, _top_only: bool) -> None:
            return None

    face = object()
    created = object()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        AdvancedFeatureSkill,
        "_find_planar_face",
        staticmethod(lambda *_args, **_kwargs: face),
    )
    monkeypatch.setattr(
        ActiveModelFeatureSkill,
        "_create_face_sketch",
        staticmethod(lambda *_args, **_kwargs: "Sketch1"),
    )
    monkeypatch.setattr(
        AdvancedFeatureSkill,
        "_extrude_cut_end_condition",
        staticmethod(
            lambda _model, _sketch, **kwargs: captured.update(kwargs) or created
        ),
    )
    monkeypatch.setattr(
        ActiveModelFeatureSkill,
        "_name_feature",
        staticmethod(lambda *_args: None),
    )
    monkeypatch.setattr(
        ActiveModelFeatureSkill,
        "_feature_name",
        staticmethod(lambda _feature: "HousingSideBore"),
    )
    monkeypatch.setattr(
        ActiveModelThroughHoleExecutor,
        "_body_info",
        staticmethod(lambda _model: {"success": True, "bodies": [object()], "bbox": {}}),
    )
    volumes = iter([0.001, 0.00099])
    monkeypatch.setattr(
        ActiveModelThroughHoleExecutor,
        "_solid_volume",
        staticmethod(lambda _bodies: next(volumes)),
    )

    request = AdvancedFeatureSkill.normalize_request(
        "side_hole",
        {
            "axis": "x",
            "face_offset_mm": -91,
            "center_yz_mm": [28, 0],
            "diameter_mm": 14,
            "placement": "center",
            "end_condition": "through_next",
        },
    )
    result = AdvancedFeatureSkill(tmp_path)._create_side_hole(
        FakeModel(),
        {"name": "HousingSideBore"},
        request,
        {
            "bodies": [object()],
            "bbox": {"length": 0.23, "width": 0.08},
            "success": True,
        },
    )

    assert result["success"] is True
    assert captured["end_condition_code"] == 2
    assert result["end_condition"] == "through_next"
    assert result["body_count_before"] == result["body_count_after"] == 1
    assert result["volume_delta_m3"] < 0.0


def test_explicit_side_hole_end_condition_maps_to_feature_cut4(monkeypatch) -> None:
    calls: list[object] = []
    created = object()

    class FeatureManager:
        def FeatureCut4(self, *args):
            calls.extend(args)
            return created

    model = type("Model", (), {"FeatureManager": FeatureManager()})()
    monkeypatch.setattr(
        ActiveModelFeatureSkill,
        "_select_sketch",
        classmethod(lambda _cls, _model, _name: True),
    )

    result = AdvancedFeatureSkill._extrude_cut_end_condition(
        model,
        "Sketch1",
        end_condition_code=2,
        depth=0.1,
    )

    assert result is created
    assert calls[:7] == [True, False, False, 2, 0, 0.1, 0.0]


def test_side_boss_uses_outward_face_normal_and_verifies_merged_volume(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeModel:
        def ClearSelection2(self, _clear: bool) -> None:
            return None

        def ForceRebuild3(self, _top_only: bool) -> None:
            return None

    created_feature = object()
    face = type("Face", (), {"Normal": [-1.0, 0.0, 0.0]})()

    monkeypatch.setattr(
        AdvancedFeatureSkill,
        "_find_planar_face",
        staticmethod(lambda *_args, **_kwargs: face),
    )
    monkeypatch.setattr(
        ActiveModelFeatureSkill,
        "_create_face_sketch",
        staticmethod(lambda *_args, **_kwargs: "Sketch1"),
    )
    monkeypatch.setattr(
        ActiveModelFeatureSkill,
        "_extrude_boss",
        staticmethod(lambda *_args, **_kwargs: created_feature),
    )
    monkeypatch.setattr(
        "src.cad_agent.active_model_feature_skill.ActiveModelFeatureSkill._name_feature",
        staticmethod(lambda *_args: None),
    )
    monkeypatch.setattr(
        "src.cad_agent.active_model_feature_skill.ActiveModelFeatureSkill._feature_name",
        staticmethod(lambda _feature: "SideBoss"),
    )
    monkeypatch.setattr(
        AdvancedFeatureSkill,
        "_feature_axis_extent",
        staticmethod(lambda *_args: [-0.091, -0.089]),
    )
    volumes = iter([0.001, 0.001001])
    monkeypatch.setattr(
        "src.cad_agent.active_model_through_hole.ActiveModelThroughHoleExecutor._solid_volume",
        staticmethod(lambda _bodies: next(volumes)),
    )
    monkeypatch.setattr(
        "src.cad_agent.active_model_through_hole.ActiveModelThroughHoleExecutor._body_info",
        staticmethod(
            lambda _model: {
                "success": True,
                "bodies": [object()],
                "bbox": {},
            }
        ),
    )

    result = AdvancedFeatureSkill(tmp_path)._create_side_boss(
        FakeModel(),
        {"name": "HousingSideBoss"},
        {
            "axis": "x",
            "face_offset_mm": -89.0,
            "center_uv_mm": [28.0, 0.0],
            "diameter_mm": 34.0,
            "depth_mm": 2.0,
            "merge_result": True,
        },
        {"bodies": [object()]},
    )

    assert result["success"] is True
    assert result["reverse_direction"] is False
    assert result["face_normal"] == [-1.0, 0.0, 0.0]
    assert result["merge_result"] is True
    assert result["start_condition"] == "sketch_face"
    assert result["support_face_offset_mm"] == -89.0
    assert result["start_offset_mm"] == 0.0
    assert result["volume_delta_m3"] > 0


def test_side_boss_rejects_feature_node_without_material_change(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeModel:
        def ForceRebuild3(self, _top_only: bool) -> None:
            return None

    face = type("Face", (), {"Normal": [-1.0, 0.0, 0.0]})()
    monkeypatch.setattr(
        AdvancedFeatureSkill,
        "_find_planar_face",
        staticmethod(lambda *_args, **_kwargs: face),
    )
    monkeypatch.setattr(
        ActiveModelFeatureSkill,
        "_create_face_sketch",
        staticmethod(lambda *_args, **_kwargs: "Sketch1"),
    )
    monkeypatch.setattr(
        ActiveModelFeatureSkill,
        "_extrude_boss",
        staticmethod(lambda *_args, **_kwargs: object()),
    )
    monkeypatch.setattr(
        "src.cad_agent.active_model_feature_skill.ActiveModelFeatureSkill._name_feature",
        staticmethod(lambda *_args: None),
    )
    monkeypatch.setattr(
        "src.cad_agent.active_model_through_hole.ActiveModelThroughHoleExecutor._solid_volume",
        staticmethod(lambda _bodies: 0.001),
    )
    monkeypatch.setattr(
        "src.cad_agent.active_model_through_hole.ActiveModelThroughHoleExecutor._body_info",
        staticmethod(lambda _model: {"success": True, "bodies": [object()], "bbox": {}}),
    )

    result = AdvancedFeatureSkill(tmp_path)._create_side_boss(
        FakeModel(),
        {"name": "HousingSideBoss"},
        {
            "axis": "x",
            "face_offset_mm": -89.0,
            "center_uv_mm": [28.0, 0.0],
            "diameter_mm": 34.0,
            "depth_mm": 2.0,
            "merge_result": True,
        },
        {"bodies": [object()]},
    )

    assert result["success"] is False
    assert "did not add material" in result["message"]
    assert result["volume_delta_m3"] == 0.0


def test_find_planar_face_requires_outward_normal_and_trimmed_face_point() -> None:
    class Surface:
        IsPlane = True

    class Face:
        def __init__(self, normal, closest, area):
            self.Normal = normal
            self._closest = closest
            self._area = area

        def GetBox(self):
            return [-0.089, 0.0, -0.05, -0.089, 0.08, 0.05]

        def GetSurface(self):
            return Surface()

        def GetClosestPointOn(self, _x, _y, _z):
            return [*self._closest, 0.0, 0.0]

        def GetArea(self):
            return self._area

    wrong_normal = Face([1.0, 0.0, 0.0], [-0.089, 0.028, 0.0], 1.0)
    outside_trim = Face([-1.0, 0.0, 0.0], [-0.089, 0.04, 0.0], 2.0)
    expected = Face([-1.0, 0.0, 0.0], [-0.089, 0.028, 0.0], 0.5)
    body = type(
        "Body",
        (),
        {"GetFaces": lambda self: [wrong_normal, outside_trim, expected]},
    )()

    selected = AdvancedFeatureSkill._find_planar_face(
        [body],
        "x",
        -0.089,
        (-0.089, 0.028, 0.0),
    )

    assert selected is expected


def test_extrude_boss_maps_merge_and_face_start_to_feature_extrusion3(
    monkeypatch,
) -> None:
    captured: list[object] = []
    created = object()

    class FeatureManager:
        def FeatureExtrusion3(self, *args):
            captured.extend(args)
            return created

    model = type("Model", (), {"FeatureManager": FeatureManager()})()
    monkeypatch.setattr(
        ActiveModelFeatureSkill,
        "_select_sketch",
        classmethod(lambda _cls, _model, _name: True),
    )

    result = ActiveModelFeatureSkill._extrude_boss(
        model,
        "Sketch1",
        0.002,
        direction=False,
        merge=True,
    )

    assert result is created
    assert captured[2] is False
    assert captured[17] is True
    assert captured[18] is False
    assert captured[19] is True
    assert captured[20:] == [0, 0.0, False]


def test_native_line_rib_uses_feature_manager_insert_rib_signature(
    monkeypatch,
    tmp_path,
) -> None:
    captured: list[object] = []

    class Plane:
        @staticmethod
        def Select2(_append, _mark):
            return True

    class Sketch:
        Name = "RibSketch"

    class SketchManager:
        ActiveSketch = Sketch()

        @staticmethod
        def InsertSketch(_update):
            return None

        @staticmethod
        def CreateLine(*_args):
            return object()

    class Created:
        Name = "Rib1"

        @staticmethod
        def GetTypeName2():
            return "Rib"

        @staticmethod
        def GetNextFeature():
            return None

    created = Created()

    class FeatureManager:
        @staticmethod
        def InsertRib(*args):
            captured.extend(args)
            return None

    class Model:
        @staticmethod
        def ClearSelection2(_clear):
            return None

        @staticmethod
        def FeatureByName(_name):
            return Plane()

        @staticmethod
        def ForceRebuild3(_top_only):
            return True

        @staticmethod
        def FirstFeature():
            return created if captured else None

    Model.SketchManager = SketchManager()
    Model.FeatureManager = FeatureManager()

    request = AdvancedFeatureSkill.normalize_request(
        "rib",
        {
            "profile": "native_line",
            "sketch_plane_feature": "HousingRibPlane_Xm50",
            "line_points_mm": [[50, 11], [50, 45]],
            "thickness_mm": 6,
            "is_two_sided": True,
            "reverse_material_direction": True,
            "is_normal_to_sketch": True,
            "is_drafted_from_wall": False,
        },
    )
    monkeypatch.setattr(
        ActiveModelFeatureSkill,
        "_select_sketch",
        staticmethod(lambda _model, _name: True),
    )
    monkeypatch.setattr(
        AdvancedFeatureSkill,
        "_select_body_with_mark",
        staticmethod(lambda _model, _body, mark: mark == 1),
    )
    monkeypatch.setattr(
        AdvancedFeatureSkill,
        "_verify_added_material",
        staticmethod(lambda _before, _model: {"success": True}),
    )
    monkeypatch.setattr(
        AdvancedFeatureSkill,
        "_material_state",
        staticmethod(
            lambda _body_info: {
                "success": True,
                "body_count_before": 1,
                "volume_before_m3": 1.0,
            }
        ),
    )

    result = AdvancedFeatureSkill(tmp_path)._create_native_line_rib(
        Model(),
        {"name": "HousingRib"},
        request,
        {"bodies": [object()]},
    )

    assert result["success"] is True
    assert len(captured) == 10
    assert captured == [
        True,
        False,
        0.006,
        0,
        True,
        False,
        True,
        0.0,
        True,
        False,
    ]


def test_stage_gate_and_skill_planner_keep_gearbox_order() -> None:
    design = staged_gearbox_design()
    expected = ["base_plate", "fillet", "boss", "rib", "side_boss", "through_hole", "side_hole", "pocket", "save_sldprt"]
    assert design["execution_policy"]["allowed_skills"] == expected
    assert design["execution_policy"]["unexecutable_required_features"] == []
    assert [step.skill_key for step in SkillPlanner().plan(design)] == expected


def test_agents_router_preserves_all_advanced_parameters() -> None:
    design = staged_gearbox_design()
    route = SkillRouterAgent().route(design)
    assert [step["skill_key"] for step in route["skill_pipeline"]] == design["execution_policy"]["allowed_skills"]
    routed = {step["skill_key"]: step for step in route["skill_pipeline"]}
    assert routed["side_boss"]["parameters"]["features"][0]["params"]["diameter_mm"] == 82
    assert len(routed["side_hole"]["parameters"]["features"]) == 2
    assert routed["rib"]["parameters"]["features"][0]["params"]["center_positions_mm"] == [-82, 82]


def test_schema_drifted_parameter_groups_expand_without_guessing(tmp_path) -> None:
    raw = {
        "parameters": {
            "material": "EN-GJL-250",
            "base_plate": {"length": 280, "width": 210, "thickness": 18},
            "top_boss": {"length": 190, "width": 120, "height": 118, "position": "center_top"},
            "side_bosses": [
                {"axis": "Y", "face_offset_mm": 60, "center_xz_mm": [-52, 62], "diameter_mm": 82, "depth_mm": 18}
            ],
            "side_holes": [
                {"axis": "Y", "face_offset_mm": 78, "center_xz_mm": [-52, 62], "diameter_mm": 38, "placement": "center"}
            ],
            "ribs": [
                {"axis": "Y", "face_offset_mm": 60, "center_positions_mm": [[-82, 0], [82, 0]], "width_mm": 10, "height_mm": 88, "depth_mm": 10, "base_z_mm": 0}
            ],
        },
        "features": [],
        "outputs": ["SLDPRT"],
        "risks": [],
        "review_plan": {},
    }
    design = DesignPlanner(tmp_path)._normalize_design(raw, "Create only the 3D gearbox housing.", stage_mode="model_3d")
    assert [item["type"] for item in design["features"]] == ["base_plate", "boss", "side_boss", "side_hole", "rib"]
    rib = next(item for item in design["features"] if item["type"] == "rib")
    assert rib["params"]["center_positions_mm"] == [-82.0, 82.0]
    assert rib["params"]["center_xz_mm"] == [0.0, 44.0]
    assert design["execution_policy"]["unexecutable_required_features"] == []


def test_provider_confirmation_risk_blocks_execution(tmp_path) -> None:
    raw = {
        "parameters": {"base_plate": {"length": 100, "width": 60, "thickness": 10}},
        "features": [],
        "outputs": ["SLDPRT"],
        "risks": [{"description": "Bearing position is ambiguous.", "needs_confirmation": True}],
        "review_plan": {},
    }
    design = DesignPlanner(tmp_path)._normalize_design(raw, "Create only the 3D part.", stage_mode="model_3d")
    assert design["needs_confirmation"] is True
    assert design["execution_policy"]["allowed_skills"] == []
