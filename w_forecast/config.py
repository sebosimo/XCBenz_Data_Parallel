"""Small, explicit output contract for the W prototype."""

from dataclasses import asdict, dataclass
import hashlib
import json
import math


@dataclass(frozen=True)
class Model:
    id: str
    cycle_hours: int
    spacing: float

    @property
    def collection(self):
        return "ch.meteoschweiz.ogd-forecasting-icon-" + self.id

    def maximum_horizon(self, reference):
        if self.id == "ch2":
            return 120
        return 45 if reference.hour == 3 else 33


MODELS = {"ch1": Model("ch1", 3, 0.01), "ch2": Model("ch2", 6, 0.02)}
SECTION_ALTITUDES = tuple(range(2000, 4501, 250))


@dataclass(frozen=True)
class Config:
    # Match the existing application's common ICON-CH domain, at CH1 spacing.
    bbox: tuple[float, float, float, float] = (0.8, 42.4, 16.4, 50.0)
    spacing: float = 0.01
    altitudes: tuple[int, ...] = (1000, 1500, 2000, 2500, 3000, 4000, 5000, 6000, 7000, 8000)
    section_altitudes: tuple[int, ...] = ()

    def __post_init__(self):
        if len(self.bbox) != 4 or not all(math.isfinite(v) for v in self.bbox):
            raise ValueError("bbox must contain four finite coordinates")
        west, south, east, north = self.bbox
        if not (-180 <= west < east <= 180 and -90 < south < north < 90):
            raise ValueError("bbox must be west,south,east,north")
        if not math.isfinite(self.spacing) or not 0.005 <= self.spacing <= 0.1:
            raise ValueError("spacing must be between 0.005 and 0.1 degrees")
        if not self.altitudes or tuple(sorted(set(self.altitudes))) != self.altitudes:
            raise ValueError("altitudes must be unique and ascending")
        if not all(isinstance(v, int) and 0 < v <= 15000 for v in self.altitudes):
            raise ValueError("altitudes must be integer metres in (0, 15000]")
        if (tuple(sorted(set(self.section_altitudes))) != self.section_altitudes
                or not all(isinstance(v, int) and 0 < v <= 15000 for v in self.section_altitudes)):
            raise ValueError("section altitudes must be unique ascending integer metres in (0, 15000]")
        if len(self.all_altitudes) > 24 or self.width * self.height > 5_000_000:
            raise ValueError("prototype grid exceeds its bounded working size")
        for extent in (east - west, north - south):
            if abs(extent / self.spacing - round(extent / self.spacing)) > 1e-6:
                raise ValueError("bbox spans must be exact multiples of spacing")

    @property
    def all_altitudes(self):
        return tuple(sorted(set(self.altitudes) | set(self.section_altitudes)))

    @property
    def width(self):
        return round((self.bbox[2] - self.bbox[0]) / self.spacing) + 1

    @property
    def height(self):
        return round((self.bbox[3] - self.bbox[1]) / self.spacing) + 1

    @property
    def key(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:20]

    def grid(self):
        return {
            "crs": "EPSG:4326", "width": self.width, "height": self.height,
            "bbox": list(self.bbox), "longitude_start": self.bbox[0],
            "latitude_start": self.bbox[1], "longitude_step": self.spacing,
            "latitude_step": self.spacing, "registration": "grid_points",
            "layout": "row-major", "row_order": "south_to_north",
            "column_order": "west_to_east",
        }
