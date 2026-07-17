"""Network-free descriptions of what each ICON fetcher intends to load."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


def _unique(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for group in groups for item in group))


@dataclass(frozen=True)
class ProductSelection:
    wind: bool = False
    sunshine: bool = True
    rain: bool = True
    sunrain: bool = True
    cloud: bool = False


@dataclass(frozen=True)
class HorizonFetchPlan:
    profile: tuple[str, ...]
    map_fields: tuple[str, ...]
    primary: tuple[str, ...]
    rain: tuple[str, ...]
    cloud: tuple[str, ...]
    radiation: tuple[str, ...]
    batch: tuple[str, ...]

    def operation_trace(self, *, profile_mode: str, products: ProductSelection) -> tuple["PlannedOperation", ...]:
        operations: list[PlannedOperation] = []
        for owner, variables in (
            ("primary", self.primary),
            ("rain", self.rain),
            ("cloud", self.cloud),
            ("radiation", self.radiation),
        ):
            if variables:
                operations.extend(
                    (
                        PlannedOperation("fetch", owner, variables),
                        PlannedOperation("decode", owner, variables),
                    )
                )
        if profile_mode == "direct-chunk":
            operations.append(PlannedOperation("accumulate", "profile", self.profile))
        for enabled, owner, variables in (
            (products.wind, "wind", self.map_fields),
            (products.sunshine, "sunshine", self.radiation),
            (products.rain, "rain", self.rain),
            (products.sunrain, "sunrain", _unique(self.rain, self.radiation)),
            (products.cloud, "cloud", self.cloud),
        ):
            if enabled and variables:
                operations.append(PlannedOperation("accumulate", owner, variables))
        operations.append(PlannedOperation("cleanup", "temporary-downloads", self.batch))
        return tuple(operations)


@dataclass(frozen=True)
class PlannedOperation:
    phase: str
    owner: str
    variables: tuple[str, ...] = ()


def completion_operation_trace(
    *, profile_mode: str, products: ProductSelection
) -> tuple[PlannedOperation, ...]:
    operations: list[PlannedOperation] = []
    if profile_mode == "direct-chunk":
        operations.append(PlannedOperation("finalize", "profile"))
    for enabled, owner in (
        (products.wind, "wind"),
        (products.sunshine, "sunshine"),
        (products.rain, "rain"),
        (products.sunrain, "sunrain"),
        (products.cloud, "cloud"),
    ):
        if enabled:
            operations.append(PlannedOperation("finalize", owner))
            operations.append(PlannedOperation("cleanup", f"old-{owner}-runs"))
    return tuple(operations)


@dataclass(frozen=True)
class ModelFetchPolicy:
    model: str
    collection: str
    static_assets: tuple[str, str]
    profile_variables: tuple[str, ...]
    native_wind_variables: tuple[str, ...]
    radiation_average_variables: tuple[str, ...]
    sunshine_accumulation_variables: tuple[str, ...]
    rain_variables: tuple[str, ...]
    cloud_variables: tuple[str, ...]
    absolute_max_horizon: int
    run_interval_hours: int
    discovery_slots: int
    discovery_limit: int
    processing_candidate_limit: int
    step_digits: int

    @property
    def sunshine_variables(self) -> tuple[str, ...]:
        return self.radiation_average_variables + self.sunshine_accumulation_variables

    def maximum_horizon(self, reference_time: datetime) -> int:
        if self.model == "ch1":
            return 45 if reference_time.hour == 3 else 33
        return self.absolute_max_horizon

    def step_label(self, horizon: int) -> str:
        return f"H{horizon:0{self.step_digits}d}"

    def horizon_plan(
        self,
        horizon: int,
        *,
        profile_mode: str,
        products: ProductSelection,
    ) -> HorizonFetchPlan:
        profile = self.profile_variables if profile_mode == "direct-chunk" else ()
        map_fields = (
            ("U", "V") + self.native_wind_variables
            if products.wind or products.sunshine
            else ()
        )
        primary = _unique(profile, map_fields)
        rain = self.rain_variables if products.rain or products.sunrain else ()
        cloud = self.cloud_variables if products.cloud else ()
        radiation_needed = profile_mode == "direct-chunk" or products.sunshine or products.sunrain
        radiation = self.sunshine_variables if horizon > 0 and radiation_needed else ()
        return HorizonFetchPlan(
            profile=profile,
            map_fields=map_fields,
            primary=primary,
            rain=rain,
            cloud=cloud,
            radiation=radiation,
            batch=_unique(primary, rain, cloud, radiation),
        )


_COMMON = {
    "profile_variables": ("T", "U", "V", "P", "QV"),
    "native_wind_variables": ("U_10M", "V_10M"),
    "radiation_average_variables": ("ASWDIR_S", "ASWDIFD_S"),
    "sunshine_accumulation_variables": ("DURSUN", "DURSUN_M"),
    "rain_variables": ("TOT_PREC",),
    "cloud_variables": ("CLCT", "CLCL", "CLCM", "CLCH"),
}

CH1_POLICY = ModelFetchPolicy(
    model="ch1",
    collection="ogd-forecasting-icon-ch1",
    static_assets=(
        "vertical_constants_icon-ch1-eps.grib2",
        "horizontal_constants_icon-ch1-eps.grib2",
    ),
    absolute_max_horizon=45,
    run_interval_hours=3,
    discovery_slots=16,
    discovery_limit=1,
    processing_candidate_limit=3,
    step_digits=2,
    **_COMMON,
)

CH2_POLICY = ModelFetchPolicy(
    model="ch2",
    collection="ogd-forecasting-icon-ch2",
    static_assets=(
        "vertical_constants_icon-ch2-eps.grib2",
        "horizontal_constants_icon-ch2-eps.grib2",
    ),
    absolute_max_horizon=120,
    run_interval_hours=6,
    discovery_slots=20,
    discovery_limit=2,
    processing_candidate_limit=2,
    step_digits=3,
    **_COMMON,
)

POLICIES = {"ch1": CH1_POLICY, "ch2": CH2_POLICY}
