from __future__ import annotations

import json
import importlib.util
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

from .brain_models import ModelProvider
from .model_providers import LocalRuleBasedProvider
from .runtime_config import load_runtime_environment, redact_secrets
from .vibecad_skill import VibeCADSkill


def _design_prompt(prompt: str, schema: dict[str, Any]) -> str:
    return (
        "You are the CADPlanner for a production mechanical CAD agent. Return one valid JSON object only. "
        "Preserve every explicitly requested dimension, material, feature, relationship, manufacturing note, and output. "
        "Do not call CAD software. Do not invent missing dimensions. If information is ambiguous, add an object to risks, "
        "set needs_confirmation=true, and describe the missing parameter in unsupported_features. "
        "Set needs_confirmation=true only when a required value is missing, contradictory, or cannot be mapped to a production contract. "
        "Ordinary geometry review, stacking verification, interference review, or a note that engineers should inspect the result is advisory: "
        "record it with needs_confirmation=false and do not block an otherwise fully specified plan. Explicit feature order in the user request is authoritative. "
        "All numeric CAD dimensions must be plain millimetre numbers, not strings. "
        "Use the base-part center as X=0,Y=0. position_xy must be a signed [x,y] list in that centered coordinate system. "
        "For four corner holes use placement='corner_offsets' and edge_offsets_mm={'x': value, 'y': value}. "
        "For slots, centerline_offsets must be signed scalar offsets from the base center along the axis perpendicular to the slot long axis. "
        "Every feature must contain name, type, params, and required. "
        "For any requested geometry, features must not be empty. Do not store geometry definitions only inside parameters; "
        "parameters is for global body values and every modeled operation must also appear as an item in features. "
        "Use these production parameter contracts when applicable: "
        "base_plate(length,width,thickness); through_hole(diameter,count,position or placement/edge_offsets_mm); "
        "fillet(radius,target); chamfer(size,targets); boss(length,width,height,position or position_xy); "
        "pocket(length,width,depth,corner_radius,position or position_xy); "
        "slot(count,length,width,centerline_offsets,orientation); "
        "side_boss(axis,face_offset_mm,center_xz_mm or center_yz_mm,diameter_mm,depth_mm); "
        "side_hole(axis,face_offset_mm,center_xz_mm or center_yz_mm,diameter_mm,placement='center' or 'bolt_circle',pcd_mm?,count?,start_angle_deg?); "
        "rib(axis,face_offset_mm,center_xz_mm or center_yz_mm,center_positions_mm,width_mm,height_mm,depth_mm,base_z_mm); "
        "For rib, center_positions_mm is a list of signed scalar positions along the in-plane horizontal axis; "
        "center_xz_mm or center_yz_mm is one reference point for the entire rib group, not one center per rib; "
        "each scalar in center_positions_mm creates one rib. base_z_mm and height_mm define its vertical extent, "
        "and depth_mm always extrudes outward from the signed face_offset_mm. This group representation is not ambiguous. "
        "A through side_hole keeps one requested bore diameter across the body; different front/rear side_boss outer diameters do not imply a stepped bore. "
        "Do not add an opposite-side bolt circle unless the user explicitly requests one. These explicit contracts are not ambiguities. "
        "linear_pattern(seed_features,count_1,spacing_1,direction_1,count_2?,spacing_2?,direction_2?); "
        "circular_pattern(seed_features,count,total_angle_deg,axis,axis_feature?); "
        "mirror(seed_features,mirror_plane,scope='features'). "
        "gear(mode='new_model',gear_type='spur',module_mm,teeth,pressure_angle_deg=20,face_width_mm,"
        "bore_diameter_mm=0,backlash_mm=0,keyway=false or {enabled=true,width_mm,depth_mm}). "
        "The gear executor supports only zero-profile-shift full-depth involute straight spur gears. Preserve module, integer tooth count, "
        "pressure angle, face width, bore and keyway explicitly. Mark helical, bevel, worm, rack, internal gears, profile shift, and ambiguous keyways unsupported; do not approximate them. "
        "gear_pair(mode='new_assembly',module_mm,pressure_angle_deg=20,face_width_mm,gear_a={name,teeth,bore_diameter_mm,keyway?},"
        "gear_b={name,teeth,bore_diameter_mm,keyway?},center_distance_mm?,interference_check=true). "
        "Use gear_pair only when the user requests two mating spur gears or a gear transmission assembly. Both gears must have positive bores, the same module and pressure angle; "
        "for zero profile shift the center distance must equal module_mm*(gear_a.teeth+gear_b.teeth)/2. Do not split one gear_pair into unrelated gear features. "
        "revolve(operation='base'|'boss'|'cut',mode='new_model'|'active_model',sketch_plane='front'|'top'|'right',"
        "axis='horizontal'|'vertical',angle_deg,profile or segments). A profile is an ordered closed list of "
        "[axial_mm,radius_mm] points that stays on one side of the axis. A stepped shaft should use segments, "
        "each with diameter_mm and length_mm (or start_mm/end_mm). Never replace a revolved request with stacked cylinders. "
        "For a flanged sleeve or bushing, the closed profile must span the complete total_length and include the inner bore, "
        "the full main-body outer diameter, and the flange outer diameter over exactly the flange thickness. Never return only "
        "the flange cross-section when a longer sleeve body is requested. Preserve total_length, main_outer_diameter, "
        "flange_outer_diameter, flange_thickness, bore_diameter, and flange_side in parameters. "
        "sweep(operation='base'|'boss'|'cut',mode='new_model'|'active_model',profile_type='circle',diameter_mm,"
        "path_plane='front'|'top'|'right',path_points_mm=[[x,y],...],path_type='polyline'); "
        "loft(operation='base'|'boss',mode='new_model'|'active_model',base_plane='front'|'top'|'right',sections=["
        "{offset_mm,shape='circle',diameter_mm,center_mm?} or {offset_mm,shape='rectangle',length_mm,width_mm,center_mm?}]). "
        "Keep loft sections in strictly increasing offset order. Do not invent unsupported sweep paths or open loft profiles. "
        "sheet_metal(mode='new_model',base_plane='top',length_mm,width_mm,thickness_mm,bend_radius_mm,edge_flanges=["
        "{edge='length_positive'|'length_negative'|'width_positive'|'width_negative',height_mm,angle_deg=30..150,flip_direction=false}],"
        "sketched_bends=[{line_orientation='parallel_to_width'|'parallel_to_length',offset_mm,angle_deg=30..150,"
        "bend_radius_mm,fixed_side='negative'|'positive',reverse_direction=false}],"
        "hems=[{edge='length_positive'|'length_negative'|'width_positive'|'width_negative',type='open',"
        "position='inside'|'outside',length_mm,gap_mm,bend_radius_mm?,miter_gap_mm?,reverse_direction=false}],"
        "jogs=[{line_orientation='parallel_to_width'|'parallel_to_length',line_offset_mm,offset_distance_mm,"
        "angle_deg=30..150,bend_radius_mm,fixed_side='negative'|'positive',reverse_direction=false,"
        "fix_projected_length=true,dimension_position='outside_offset',jog_position='bend_centerline'}],"
        "lofted_bend={base_plane='front',plane_offset_mm,profile_1_points_mm=[[x,y],...],"
        "profile_2_points_mm=[[x,y],...],thickness_direction='outside'|'inside',formed=false,"
        "refer_to_endpoint=true,facet_option='bends_per_transition',bends_per_transition=2..20},"
        "forming_tools=[{tool='dimple',position_x_mm,position_y_mm,rotation_deg=0,link_to_library=false,"
        "show_punch=true,show_profile=true,show_center=true}],"
        "flat_pattern={enabled=false|true,final_state='folded'|'flattened',export_dxf={enabled=false|true,"
        "include_bend_lines=true,include_sketches=false,include_library_features=false,"
        "include_forming_tools=true,include_bounding_box=false,file_name?}}). "
        "The production sheet-metal contract supports rectangular base flanges, explicit bounded edge-flange angles, one full-span sketched bend, one open hem, one full-span fixed-projection jog, one two-profile bent lofted bend, one whitelisted dimple forming tool, native FlatPattern state, and native flat-pattern DXF export. "
        "Lofted-bend profiles must be open, have the same point count, and use strictly increasing x coordinates. Do not combine edge_flanges, sketched_bends, hems, jogs, forming_tools, or lofted_bend in one feature. Closed, tear-drop, rolled, and double hems and formed lofted bends are currently unsupported. "
        "weldment(mode='new_model',standard='iso',profile_type='square_tube'|'pipe'|'solid_round',profile_configuration,path_segments_mm=["
        "[[x1,y1,z1],[x2,y2,z2]],...],groups=[[segment_indexes]],corner_treatment='miter'|'butt1'|'butt2'). "
        "Every weldment path segment must be explicit and appear in exactly one group; do not invent an installed profile configuration. "
        "For a curved welded-wire mesh, output exactly one weldment feature and use profile_type='solid_round', "
        "profile_configuration='D4', and wire_mesh={wire_diameter_mm,arc_radius_mm,arc_length_mm,overall_height_mm,"
        "mesh_height_mm,end_pitch_mm,middle_pitch_mm,overall_depth_mm,longitudinal_count,transverse_count,"
        "longitudinal_chord_pitches_mm,outer_surface_chord_mm,transverse_profile_depth_mm,"
        "layering='transverse_arc_wires_on_top',layer_reference='drawing_side_profile',"
        "arc_radius_semantic='inner_surface',arc_length_semantic='outer_surface_arc',"
        "inner_surface_radius_mm=arc_radius_mm,transverse_centerline_radius_mm=arc_radius_mm+wire_diameter_mm/2,"
        "outer_surface_radius_mm=arc_radius_mm+wire_diameter_mm,"
        "longitudinal_center_locus_radius_mm=arc_radius_mm+1.5*wire_diameter_mm}. "
        "For the R269/D4 drawing profile, R269 is the inner surface, R271 is the transverse-wire centerline, "
        "R273 is its outer surface whose arc length is 276.4, and longitudinal-wire centers lie on R275. "
        "The standard Top view must show a sagging U profile with the transverse arc band above the tangent longitudinal circles; "
        "never flip the model or rotate the camera to imitate this relationship. "
        "When the same wire-mesh part explicitly includes flat-bar supports, keep them inside that one wire_mesh object as "
        "flat_bar_frame={width_mm,thickness_mm,orientation='edge_on',support_side='below_wire',contact='tangent',"
        "construction_style='continuous_rails'|'segmented_basket',curved_rail_count,curved_rail_radius_mm,rail_segment_count?,"
        "straight_support_count,straight_support_length_mm,mounting_tabs?={count,width_mm,thickness_mm,height_mm},"
        "replace_longitudinal_wires?=true,wire_seat_notches?={style='shallow_rectangular_edge_notch',"
        "placement='all_support_transverse_intersections',width_mm,depth_mm},"
        "wire_clips?={style='single_hook',placement='all_support_wire_intersections',width_mm,thickness_mm,clearance_mm}}. "
        "A segmented_basket keeps each D4 transverse wire as one continuous arc, but builds each flat-bar edge rail from straight chord members. "
        "When the brief says that rectangular recesses, grooves, slots, or wire seats are cut into the flat bars, use wire_seat_notches, "
        "set replace_longitudinal_wires=true, and do not output wire_clips: the six flat bars replace the coincident longitudinal D4 wires, "
        "and every transverse D4 wire rests partly inside one shallow rectangular notch in each support. "
        "Use an external wire_clips object only when the brief explicitly requests separate added clips or retainers; mounting_tabs are not wire clips. "
        "Do not omit requested flat bars, supports, clamps, or mounting tabs. Do not create a second new-model weldment feature. "
        "Do not split longitudinal and transverse wires into separate weldment features and do not invent path_segments_mm; "
        "the deterministic executor derives all strand coordinates from wire_mesh. The current solid-round contract supports D4 only. "
        "freeform_surface(mode='new_model',surface_type='fill',boundary_curves_mm=[[[x,y,z],...],...],resolution=1|2|3). "
        "Free-form boundary curves must be ordered, closed, contiguous 3D splines with at least three points per curve. "
        "Do not substitute a solid loft for a requested surface or guess missing control points. "
        "shell(thickness_mm,outward,remove_faces=['top'|'bottom'|'x_min'|'x_max'|'y_min'|'y_max']); "
        "draft(angle_deg,neutral_plane='top'|'bottom',target_faces='outer_vertical_faces',flip_direction?); "
        "reference_geometry(reference_type='offset_plane',base_plane,offset_mm) or "
        "reference_geometry(reference_type='axis_two_planes',plane_1,plane_2). Do not guess a shell opening, neutral plane, or reference. "
        "configuration(name,comment?,alternate_name?,activate?,options?); "
        "equation(kind='global_variable'|'dimension',expression or name/value/unit,scope='this'|'all'|'specified',configurations?). "
        "Use native SolidWorks equation syntax such as \"Length\" = 100mm or \"D1@Boss-Extrude1\" = \"Length\". "
        "assembly_mate(mode='new_assembly'|'active_assembly',components=[{id,path?,component_name?,configuration?,position_mm}],"
        "mates=[{name,mate_type='coincident'|'concentric'|'parallel'|'distance'|'gear',entity_a,entity_b,distance_mm?,gear_teeth_a?,gear_teeth_b?}]). "
        "Each mate entity must name a component and use selector_type='reference_plane' with plane front/top/right or selector_type='cylinder' with radius_mm/tolerance_mm. "
        "For gearbox or reducer housings, decompose the requested geometry into base_plate, one or more top bosses, "
        "a top pocket, side_boss bearing seats, side_hole bearing bores or flange bolt circles, ribs, mounting through_holes, "
        "and only explicitly requested fillets/chamfers. Provide signed side-face offsets and all coordinates from the base center. "
        "Never silently omit a requested feature merely because it is not in the production contracts; retain it and mark it unsupported. "
        "The response must include parameters, features, outputs, unsupported_features, risks, needs_confirmation, and review_plan.\n\n"
        f"DESIGN_SCHEMA:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
        f"USER_REQUEST:\n{prompt}"
    )


def _candidate_prompt(
    prompt: str,
    schema: dict[str, Any],
    repair_feedback: str | None = None,
) -> str:
    repair = f"\nREPAIR_FEEDBACK:\n{repair_feedback}\n" if repair_feedback else ""
    return (
        "You are a candidate CAD planning provider. You never call tools, Skills, Pipelines, "
        "SolidWorks, AutoCAD, file exporters, or save commands. Return exactly one JSON object "
        "matching the supplied candidate schema and no prose. The local deterministic CAD-IR "
        "compiler and Planner Validator make every execution decision.\n"
        "Rules:\n"
        "1. Include candidate_schema_version exactly as supplied, then use only operations listed "
        "in schema.operations. Do not invent operations.\n"
        "2. Every feature must include id, operation, depends_on, target_body, target_reference, "
        "parameters, evidence, assumptions, unresolved, and confidence.\n"
        "3. Preserve only dimensions explicitly stated by the user. Never guess a missing size, "
        "position, count, material, target face, or geometric relationship.\n"
        "4. Normalize linear values to numeric millimetres and angles to numeric degrees. Prefer "
        "canonical *_mm and *_deg parameter names from schema.operation_contracts.\n"
        "5. evidence contains short source phrases from the user request. assumptions contains "
        "only inferences that require confirmation. unresolved contains every missing or ambiguous "
        "fact required for production.\n"
        "6. depends_on contains earlier feature ids only. target_body is a stable logical id such "
        "as body_01. For a modifier, target_reference must use {feature_id, role}. Never use the "
        "vague final roles top, front, right, bottom, or left. If the target cannot be determined, "
        "set target_reference to null and add the ambiguity to unresolved.\n"
        "7. Preserve explicit placement language. If the user says a feature is at the center of a "
        "named base feature (for example, 'at the plate center' or '底板中心'), set position='center', "
        "depend on that base feature, and use target_reference={feature_id:<that id>, "
        "role:'outer_planar_face'}. This is an explicit semantic reference, not a guessed COM face.\n"
        "8. A through hole is through_hole, never threaded_hole unless the user explicitly requests "
        "a thread or tap. Missing hole position must remain unresolved.\n"
        "9. Do not claim that geometry was built, validated, saved, exported, or executed.\n"
        "10. outputs is a flat list of requested format strings such as ['SLDPRT']; never return "
        "objects in outputs, and omit every format the user explicitly says not to generate or export.\n"
        "11. confidence measures extraction certainty only; it cannot authorize execution.\n"
        "12. Do not repeat an explicitly supplied value or an encoded deterministic parameter as an "
        "assumption or unresolved item. Explicit false/zero values are valid information. For a base "
        "profile_extrude, target_reference may be null; body_operation='base', reverse_direction=false, "
        "and merge_result=false are complete values, not missing information.\n"
        "13. The only operation name for every revolved feature is 'revolve'. Put base/boss/cut in "
        "parameters.body_operation and new_model/active_model in parameters.execution_mode. Use "
        "sketch_plane='front'|'top'|'right' and axis='horizontal'|'vertical'. User phrases "
        "revolve_base, revolve_boss, and revolve_cut map directly to those canonical values. "
        "A local horizontal/vertical axis means the corresponding sketch axis through the origin and "
        "is not unresolved. A boss/cut depending on an earlier body uses execution_mode='active_model'.\n"
        f"{repair}\n"
        f"CANDIDATE_SCHEMA:\n{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}\n\n"
        f"USER_REQUEST:\n{prompt}"
    )


def _parse_json_output(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("LLM provider did not return a JSON object.")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise RuntimeError("LLM provider returned a non-object JSON value.")
    return data


class OpenAIAgentsProvider:
    name = "openai_agents_sdk"

    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.environ.get("CAD_AGENT_OPENAI_MODEL", "gpt-4.1-mini")

    def generate_design_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        from agents import Agent, Runner  # type: ignore

        agent = Agent(
            name="CADPlannerAgent",
            instructions=(
                "Convert mechanical design requests to normalized Design JSON. "
                "Never execute tools or silently omit unsupported features."
            ),
            model=self.model,
        )
        result = Runner.run_sync(agent, _design_prompt(prompt, schema), max_turns=2)
        return _parse_json_output(result.final_output)


class OpenAICompatibleProvider:
    def __init__(self, name: str, api_key: str, base_url: str, model: str) -> None:
        self.name = name
        self.api_key = api_key
        self.base_url = base_url
        self.model = model

    def generate_design_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        from openai import OpenAI  # type: ignore

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "Return valid mechanical Design JSON only."},
                {"role": "user", "content": _design_prompt(prompt, schema)},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        return _parse_json_output(response.choices[0].message.content)

    def generate_candidate_output(
        self,
        prompt: str,
        schema: dict[str, Any],
        repair_feedback: str | None = None,
    ) -> str:
        from openai import OpenAI  # type: ignore

        client = OpenAI(api_key=self.api_key, base_url=self.base_url, max_retries=0)
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "Return one candidate CAD plan JSON object only. Never execute CAD tools.",
                },
                {"role": "user", "content": _candidate_prompt(prompt, schema, repair_feedback)},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        content = str(response.choices[0].message.content or "").strip()
        if not content:
            raise RuntimeError(f"{self.name} returned empty candidate JSON content.")
        return content


class DeepSeekProvider(OpenAICompatibleProvider):
    name = "deepseek"

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        super().__init__(self.name, api_key, base_url.rstrip("/"), model)

    def generate_design_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        from openai import OpenAI  # type: ignore

        try:
            client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=120.0, max_retries=1)
            content = ""
            for _attempt in range(2):
                response = client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": "You are a mechanical CAD planning engine. Return one complete JSON object and no prose.",
                        },
                        {"role": "user", "content": _design_prompt(prompt, schema)},
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=8192,
                    temperature=0,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                content = str(response.choices[0].message.content or "").strip()
                if content:
                    break
        except Exception as exc:
            raise RuntimeError(redact_secrets(f"DeepSeek planning request failed: {exc}")) from None
        if not content:
            raise RuntimeError("DeepSeek returned empty JSON content after one retry.")
        design = _parse_json_output(content)
        design["llm_provider"] = {
            "id": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "response_format": "json_object",
        }
        return design

    def generate_candidate_output(
        self,
        prompt: str,
        schema: dict[str, Any],
        repair_feedback: str | None = None,
    ) -> str:
        """Return one raw candidate response; local code owns the only repair retry."""

        from openai import OpenAI  # type: ignore

        try:
            client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=120.0, max_retries=0)
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a candidate mechanical CAD planner. Return one JSON object only. "
                            "You cannot call tools or authorize execution."
                        ),
                    },
                    {"role": "user", "content": _candidate_prompt(prompt, schema, repair_feedback)},
                ],
                response_format={"type": "json_object"},
                max_tokens=8192,
                temperature=0,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except Exception as exc:
            raise RuntimeError(redact_secrets(f"DeepSeek candidate planning request failed: {exc}")) from None
        content = str(response.choices[0].message.content or "").strip()
        if not content:
            raise RuntimeError("DeepSeek returned empty candidate JSON content.")
        return content


class ClaudeProvider:
    name = "claude"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        self.api_key = api_key
        self.model = model or os.environ.get("CAD_AGENT_CLAUDE_MODEL", "claude-sonnet-4-5")

    def generate_design_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "max_tokens": 8192,
            "temperature": 0,
            "messages": [{"role": "user", "content": _design_prompt(prompt, schema)}],
        }
        request = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        data = _request_json(request)
        blocks = data.get("content", [])
        text = "\n".join(str(item.get("text", "")) for item in blocks if item.get("type") == "text")
        return _parse_json_output(text)


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        self.api_key = api_key
        self.model = model or os.environ.get("CAD_AGENT_GEMINI_MODEL", "gemini-2.5-flash")

    def generate_design_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        model = urllib.parse.quote(self.model, safe="")
        key = urllib.parse.quote(self.api_key, safe="")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        payload = {
            "contents": [{"parts": [{"text": _design_prompt(prompt, schema)}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        data = _request_json(request)
        candidates = data.get("candidates", [])
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        return _parse_json_output("\n".join(str(item.get("text", "")) for item in parts))


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        self.base_url = (base_url or os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")
        self.model = model or os.environ.get("CAD_AGENT_OLLAMA_MODEL", "qwen2.5:14b")

    def generate_design_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "prompt": _design_prompt(prompt, schema),
            "format": "json",
            "stream": False,
            "options": {"temperature": 0},
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        data = _request_json(request)
        return _parse_json_output(data.get("response"))


def _request_json(request: urllib.request.Request, timeout: float = 90.0) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(redact_secrets(f"Provider HTTP {exc.code}: {detail[:500]}")) from exc
    if not isinstance(data, dict):
        raise RuntimeError("Provider returned invalid JSON.")
    return data


@dataclass(frozen=True)
class ProviderStatus:
    id: str
    label: str
    configured: bool
    available: bool
    reason: str
    model: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProviderRegistry:
    """Resolve planner providers without exposing credentials to the UI."""

    ORDER = ("openai_agents_sdk", "deepseek", "claude", "gemini", "qwen", "ollama", "local_fallback")

    def __init__(self, vibecad: VibeCADSkill) -> None:
        self.vibecad = vibecad

    def statuses(self) -> list[ProviderStatus]:
        load_runtime_environment()
        # Importing the full SDK adds several seconds and substantial memory
        # to GUI startup. The selected provider still imports it on first use.
        agents_available = importlib.util.find_spec("agents") is not None
        ollama_available = self._ollama_available()
        openai_available = importlib.util.find_spec("openai") is not None
        return [
            ProviderStatus(
                "openai_agents_sdk",
                "OpenAI Agents SDK",
                bool(os.environ.get("OPENAI_API_KEY")),
                agents_available,
                "ready" if agents_available and os.environ.get("OPENAI_API_KEY") else "OPENAI_API_KEY or SDK missing",
                os.environ.get("CAD_AGENT_OPENAI_MODEL", "gpt-4.1-mini"),
            ),
            ProviderStatus(
                "deepseek",
                "DeepSeek",
                bool(os.environ.get("DEEPSEEK_API_KEY")),
                openai_available,
                "ready" if openai_available and os.environ.get("DEEPSEEK_API_KEY") else "DEEPSEEK_API_KEY or openai package missing",
                os.environ.get("CAD_AGENT_DEEPSEEK_MODEL", "deepseek-v4-pro"),
            ),
            ProviderStatus("claude", "Claude", bool(os.environ.get("ANTHROPIC_API_KEY")), True, "ready" if os.environ.get("ANTHROPIC_API_KEY") else "ANTHROPIC_API_KEY missing", os.environ.get("CAD_AGENT_CLAUDE_MODEL", "claude-sonnet-4-5")),
            ProviderStatus("gemini", "Gemini", bool(os.environ.get("GEMINI_API_KEY")), True, "ready" if os.environ.get("GEMINI_API_KEY") else "GEMINI_API_KEY missing", os.environ.get("CAD_AGENT_GEMINI_MODEL", "gemini-2.5-flash")),
            ProviderStatus("qwen", "Qwen", bool(os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_API_KEY")), True, "ready" if (os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_API_KEY")) else "DASHSCOPE_API_KEY/QWEN_API_KEY missing", os.environ.get("CAD_AGENT_QWEN_MODEL", "qwen-plus")),
            ProviderStatus("ollama", "Local Ollama", ollama_available, ollama_available, "ready" if ollama_available else "Ollama is not reachable", os.environ.get("CAD_AGENT_OLLAMA_MODEL", "qwen2.5:14b")),
            ProviderStatus("local_fallback", "Local deterministic fallback", True, True, "ready", "rule-based"),
        ]

    def resolve(
        self,
        provider_id: str = "auto",
        status_snapshot: list[ProviderStatus] | None = None,
    ) -> tuple[ModelProvider, ProviderStatus]:
        load_runtime_environment()
        status_items = status_snapshot if status_snapshot is not None else self.statuses()
        statuses = {item.id: item for item in status_items}
        selected = provider_id.strip().lower() or "auto"
        if selected == "auto":
            preferred = os.environ.get("CAD_AGENT_DEFAULT_PROVIDER", "auto").strip().lower()
            if preferred in statuses and statuses[preferred].configured and statuses[preferred].available:
                selected = preferred
            else:
                selected = next((key for key in self.ORDER if statuses[key].configured and statuses[key].available), "local_fallback")
        if selected not in statuses:
            raise ValueError(f"Unknown provider: {provider_id}")
        status = statuses[selected]
        if not status.configured or not status.available:
            raise RuntimeError(f"Provider {selected} is not ready: {status.reason}")
        if selected == "openai_agents_sdk":
            provider: ModelProvider = OpenAIAgentsProvider(status.model)
        elif selected == "deepseek":
            provider = DeepSeekProvider(
                os.environ["DEEPSEEK_API_KEY"],
                os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                status.model or "deepseek-v4-pro",
            )
        elif selected == "qwen":
            provider = OpenAICompatibleProvider(
                "qwen",
                os.environ.get("DASHSCOPE_API_KEY") or os.environ["QWEN_API_KEY"],
                os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
                status.model or "qwen-plus",
            )
        elif selected == "claude":
            provider = ClaudeProvider(os.environ["ANTHROPIC_API_KEY"], status.model)
        elif selected == "gemini":
            provider = GeminiProvider(os.environ["GEMINI_API_KEY"], status.model)
        elif selected == "ollama":
            provider = OllamaProvider(model=status.model)
        else:
            provider = LocalRuleBasedProvider(self.vibecad)
        return provider, status

    @staticmethod
    def _ollama_available() -> bool:
        base = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        try:
            with urllib.request.urlopen(f"{base}/api/tags", timeout=0.25) as response:
                return 200 <= int(response.status) < 300
        except Exception:
            return False
