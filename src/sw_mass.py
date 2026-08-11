from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MassInfo:
    mass: str
    volume: str
    surface_area: str
    center_of_mass: str


def get_mass_information(doc: Any) -> MassInfo:
    try:
        mass_property = doc.Extension.CreateMassProperty()
        if mass_property is None:
            raise RuntimeError("CreateMassProperty returned no object.")

        center = mass_property.CenterOfMass
        center_text = ", ".join(str(value) for value in center) if center else "N/A"

        return MassInfo(
            mass=str(mass_property.Mass),
            volume=str(mass_property.Volume),
            surface_area=str(mass_property.SurfaceArea),
            center_of_mass=center_text,
        )
    except Exception as exc:
        raise RuntimeError("Failed to read mass information.") from exc
