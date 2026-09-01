"""CT-based ablation-needle path safety checks and entry recommendation.

All public coordinates use VTK I/J/K (X/Y/Z) order.  Volume arrays use the
NumPy Z/Y/X order produced by reshaping VTK point scalars.
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np


BONE_HU_THRESHOLD = 350.0
BONE_WARNING_DISTANCE_MM = 5.0
BONE_COLLISION_MARGIN_MM = 0.5
BODY_TISSUE_HU_THRESHOLD = -350.0
ENTRY_MIN_PATH_MM = 15.0
ENTRY_SKIN_RUN_MM = 5.0
ENTRY_OUTSIDE_RUN_MM = 5.0
ENTRY_RAY_STEP_MM = 1.0


def _xyz(values):
    array = np.asarray(values, dtype=np.float64)
    if array.shape != (3,):
        raise ValueError("coordinates and spacing must contain exactly 3 values")
    return array


def _volume_shape_xyz(volume_zyx):
    if np.ndim(volume_zyx) != 3:
        raise ValueError("volume must be a three-dimensional Z/Y/X array")
    return np.asarray(volume_zyx.shape[::-1], dtype=np.int64)


def _sample_nearest(volume_zyx, points_ijk):
    points = np.asarray(points_ijk, dtype=np.float64)
    dims = _volume_shape_xyz(volume_zyx)
    indices = np.rint(points).astype(np.int64)
    valid = np.all((indices >= 0) & (indices < dims), axis=1)
    values = np.full(len(indices), -1000.0, dtype=np.float32)
    selected = indices[valid]
    if selected.size:
        values[valid] = volume_zyx[
            selected[:, 2], selected[:, 1], selected[:, 0]]
    return values, valid


def _ray_exit_distance_mm(tip_ijk, direction_xyz, dims_xyz, spacing_xyz):
    distances = []
    for axis in range(3):
        direction = float(direction_xyz[axis])
        if abs(direction) < 1e-12:
            continue
        boundary = dims_xyz[axis] - 1 if direction > 0.0 else 0.0
        distance = ((boundary - tip_ijk[axis]) * spacing_xyz[axis]
                    / direction)
        if distance >= 0.0:
            distances.append(float(distance))
    return min(distances) if distances else 0.0


def _true_runs(mask):
    values = np.asarray(mask, dtype=bool)
    padded = np.pad(values.astype(np.int8), (1, 1))
    changes = np.diff(padded)
    starts = np.flatnonzero(changes == 1)
    stops = np.flatnonzero(changes == -1)
    return list(zip(starts.tolist(), stops.tolist()))


def build_body_silhouette(volume_zyx, z_index):
    """Build a filled axial patient outline while excluding a detached table.

    The central threshold-connected soft-tissue component is selected, then its
    row/column spans are filled.  This encloses lung air as part of the patient
    without joining a posterior scanner table across the external-air gap.
    """
    dims = _volume_shape_xyz(volume_zyx)
    z = max(0, min(int(round(z_index)), int(dims[2] - 1)))
    solid = np.asarray(
        volume_zyx[z] > BODY_TISSUE_HU_THRESHOLD, dtype=bool)
    height, width = solid.shape
    cy, cx = (height - 1) * 0.5, (width - 1) * 0.5
    y0, y1 = int(height * 0.25), max(int(height * 0.75), 1)
    x0, x1 = int(width * 0.25), max(int(width * 0.75), 1)
    central = np.argwhere(solid[y0:y1, x0:x1])
    if not central.size:
        return np.zeros_like(solid)
    central[:, 0] += y0
    central[:, 1] += x0
    distances = ((central[:, 0] - cy) ** 2
                 + (central[:, 1] - cx) ** 2)
    seed_y, seed_x = central[int(np.argmin(distances))]

    component = np.zeros_like(solid)
    component[seed_y, seed_x] = True
    queue = deque([(int(seed_y), int(seed_x))])
    while queue:
        y, x = queue.popleft()
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if (0 <= ny < height and 0 <= nx < width
                    and solid[ny, nx] and not component[ny, nx]):
                component[ny, nx] = True
                queue.append((ny, nx))

    ys, xs = np.nonzero(component)
    if not len(ys):
        return component
    row_fill = np.zeros_like(component)
    for y in np.unique(ys):
        row_x = xs[ys == y]
        row_fill[y, int(row_x.min()):int(row_x.max()) + 1] = True
    column_fill = np.zeros_like(component)
    for x in np.unique(xs):
        column_y = ys[xs == x]
        column_fill[int(column_y.min()):int(column_y.max()) + 1, x] = True
    return component | (row_fill & column_fill)


def find_body_entry_on_ray(
        volume_zyx, spacing_xyz, tip_ijk, outward_direction_xyz,
        max_length_mm, step_mm=ENTRY_RAY_STEP_MM, body_silhouette_yx=None):
    """Find the skin-side end of the first substantial body-wall run.

    A lung target can itself be dense (a nodule), so the initial dense run is
    ignored when another substantial tissue run is encountered farther out.
    The selected run must be followed by sustained external air.
    """
    spacing = np.abs(_xyz(spacing_xyz))
    if np.any(spacing <= 0.0):
        raise ValueError("spacing values must be non-zero")
    tip = _xyz(tip_ijk)
    direction = _xyz(outward_direction_xyz)
    norm = float(np.linalg.norm(direction))
    if norm < 1e-9:
        return None
    direction /= norm
    dims = _volume_shape_xyz(volume_zyx)
    exit_distance = _ray_exit_distance_mm(tip, direction, dims, spacing)
    limit = min(float(max_length_mm), exit_distance)
    if limit < ENTRY_MIN_PATH_MM:
        return None

    step = max(0.25, float(step_mm))
    distances = np.arange(0.0, limit + step * 0.5, step,
                          dtype=np.float64)
    points = tip[None, :] + (
        distances[:, None] * direction[None, :] / spacing[None, :])
    values, valid = _sample_nearest(volume_zyx, points)

    if body_silhouette_yx is not None:
        outline = np.asarray(body_silhouette_yx, dtype=bool)
        xy = np.rint(points[:, :2]).astype(np.int64)
        inside = (valid & (xy[:, 0] >= 0) & (xy[:, 0] < outline.shape[1])
                  & (xy[:, 1] >= 0) & (xy[:, 1] < outline.shape[0]))
        selected = np.flatnonzero(inside)
        inside[selected] &= outline[xy[selected, 1], xy[selected, 0]]
        inside_runs = _true_runs(inside)
        containing_tip = next(
            ((start, stop) for start, stop in inside_runs if start == 0), None)
        if containing_tip is None:
            return None
        _, outline_stop = containing_tip
        outline_index = max(0, outline_stop - 1)
        outline_distance = float(distances[outline_index])

        # Refine the 2D-outline estimate against the actual 3D HU transition.
        # The closest substantial tissue-run endpoint is the skin-side voxel.
        tissue = valid & (values > BODY_TISSUE_HU_THRESHOLD)
        min_samples = max(2, int(math.ceil(2.0 / step)))
        endpoints = [
            (stop - 1, float(distances[stop - 1]))
            for start, stop in _true_runs(tissue)
            if stop - start >= min_samples
            and abs(float(distances[stop - 1]) - outline_distance) <= 15.0
        ]
        if endpoints:
            entry_index, distance = min(
                endpoints, key=lambda item: abs(item[1] - outline_distance))
            entry = points[entry_index]
        else:
            distance = outline_distance
            entry = points[outline_index]
        if distance < ENTRY_MIN_PATH_MM:
            return None
        return {
            "entry_ijk": tuple(float(value) for value in entry),
            "path_length_mm": float(distance),
            "direction_xyz": tuple(float(value) for value in direction),
        }

    tissue = valid & (values > BODY_TISSUE_HU_THRESHOLD)
    runs = _true_runs(tissue)
    min_tissue_samples = max(2, int(math.ceil(ENTRY_SKIN_RUN_MM / step)))
    outside_samples = max(2, int(math.ceil(ENTRY_OUTSIDE_RUN_MM / step)))

    qualified = []
    for start, stop in runs:
        if stop - start < min_tissue_samples:
            continue
        outside_stop = min(len(tissue), stop + outside_samples)
        if outside_stop - stop < outside_samples:
            continue
        if np.any(tissue[stop:outside_stop]):
            continue
        distance = distances[max(start, stop - 1)]
        if distance >= ENTRY_MIN_PATH_MM:
            qualified.append((start, stop, float(distance)))
    if not qualified:
        return None

    # Prefer the first body-wall run that does not contain the target.  If the
    # target lies in a solid organ and tissue remains continuous to skin, the
    # initial run is the correct fallback.
    chosen = next((item for item in qualified if item[0] > 0), qualified[0])
    distance = chosen[2]
    entry = tip + distance * direction / spacing
    return {
        "entry_ijk": tuple(float(value) for value in entry),
        "path_length_mm": distance,
        "direction_xyz": tuple(float(value) for value in direction),
    }


def analyze_bone_path(
        volume_zyx, spacing_xyz, entry_ijk, tip_ijk,
        needle_radius_mm=0.8, max_length_mm=None,
        bone_hu_threshold=BONE_HU_THRESHOLD,
        warning_distance_mm=BONE_WARNING_DISTANCE_MM,
        collision_margin_mm=BONE_COLLISION_MARGIN_MM):
    """Measure the shortest physical distance from a needle segment to bone."""
    spacing = np.abs(_xyz(spacing_xyz))
    if np.any(spacing <= 0.0):
        raise ValueError("spacing values must be non-zero")
    dims = _volume_shape_xyz(volume_zyx)
    entry = np.clip(_xyz(entry_ijk), 0.0, dims - 1.0)
    tip = np.clip(_xyz(tip_ijk), 0.0, dims - 1.0)
    p0 = entry * spacing
    p1 = tip * spacing
    vector = p1 - p0
    length = float(np.linalg.norm(vector))
    too_long = (max_length_mm is not None
                and length > float(max_length_mm) + 1e-6)

    search_mm = (float(warning_distance_mm) + float(needle_radius_mm)
                 + float(collision_margin_mm) + float(np.linalg.norm(spacing)))
    radius_ijk = np.ceil(search_mm / spacing).astype(np.int64)
    low = np.maximum(
        0, np.floor(np.minimum(entry, tip)).astype(np.int64) - radius_ijk)
    high = np.minimum(
        dims, np.ceil(np.maximum(entry, tip)).astype(np.int64)
        + radius_ijk + 1)
    sub = volume_zyx[
        low[2]:high[2], low[1]:high[1], low[0]:high[0]]
    bone_zyx = np.argwhere(sub >= float(bone_hu_threshold))

    nearest_ijk = None
    nearest_t = 0.0
    if not bone_zyx.size:
        clearance = float(warning_distance_mm) + 1.0
        entry_clearance = clearance
        tip_clearance = clearance
    else:
        bone_xyz = bone_zyx[:, [2, 1, 0]].astype(np.float64)
        bone_xyz += low[None, :]
        bone_world = bone_xyz * spacing[None, :]
        if length < 1e-9:
            projection = np.zeros(len(bone_world), dtype=np.float64)
            closest = np.repeat(p0[None, :], len(bone_world), axis=0)
        else:
            projection = np.clip(
                ((bone_world - p0[None, :]) @ vector) / (length * length),
                0.0, 1.0)
            closest = p0[None, :] + projection[:, None] * vector[None, :]
        center_distances = np.linalg.norm(bone_world - closest, axis=1)
        nearest_index = int(np.argmin(center_distances))
        voxel_half_diagonal = 0.5 * float(np.linalg.norm(spacing))
        clearance = max(
            0.0, float(center_distances[nearest_index]) - voxel_half_diagonal)
        entry_clearance = max(
            0.0,
            float(np.min(np.linalg.norm(bone_world - p0[None, :], axis=1)))
            - voxel_half_diagonal)
        tip_clearance = max(
            0.0,
            float(np.min(np.linalg.norm(bone_world - p1[None, :], axis=1)))
            - voxel_half_diagonal)
        nearest_ijk = tuple(float(value) for value in bone_xyz[nearest_index])
        nearest_t = float(projection[nearest_index])

    required_clearance = (float(needle_radius_mm)
                          + float(collision_margin_mm))
    collision = clearance <= required_clearance
    return {
        "available": True,
        "collision": bool(collision),
        "near_bone": bool(clearance <= float(warning_distance_mm)),
        "too_long": bool(too_long),
        "path_length_mm": length,
        "bone_clearance_mm": float(clearance),
        "entry_bone_distance_mm": float(entry_clearance),
        "tip_bone_distance_mm": float(tip_clearance),
        "required_clearance_mm": required_clearance,
        "nearest_bone_ijk": nearest_ijk,
        "collision_distance_from_entry_mm": nearest_t * length,
    }


def recommend_entry_point(
        volume_zyx, spacing_xyz, tip_ijk, max_length_mm,
        needle_radius_mm=0.8, azimuth_step_degrees=15,
        elevations_degrees=(-20, 0, 20)):
    """Return the best sampled skin entry that is reachable and avoids bone.

    Paths outside the near-bone warning zone are preferred first; among those,
    the shortest path wins.  If every candidate is near bone, the largest
    remaining clearance wins.  This is a deterministic planning aid, not a
    substitute for clinician review of vessels and other critical structures.
    """
    spacing = np.abs(_xyz(spacing_xyz))
    tip = _xyz(tip_ijk)
    silhouette = build_body_silhouette(volume_zyx, tip[2])
    tip_xy = np.rint(tip[:2]).astype(np.int64)
    if (tip_xy[0] < 0 or tip_xy[0] >= silhouette.shape[1]
            or tip_xy[1] < 0 or tip_xy[1] >= silhouette.shape[0]
            or not silhouette[tip_xy[1], tip_xy[0]]):
        return None
    step = max(5, int(azimuth_step_degrees))
    candidates = []
    for elevation_degrees in elevations_degrees:
        elevation = math.radians(float(elevation_degrees))
        cos_elevation = math.cos(elevation)
        for azimuth_degrees in range(0, 360, step):
            azimuth = math.radians(float(azimuth_degrees))
            direction = (
                cos_elevation * math.cos(azimuth),
                cos_elevation * math.sin(azimuth),
                math.sin(elevation),
            )
            surface = find_body_entry_on_ray(
                volume_zyx, spacing, tip, direction, max_length_mm,
                body_silhouette_yx=silhouette)
            if surface is not None:
                surface["elevation_degrees"] = float(elevation_degrees)
                candidates.append(surface)

    # Short centerline sampling cheaply removes most rib-crossing paths before
    # the exact physical tube-to-bone calculation below.
    viable = []
    for candidate in candidates:
        entry = _xyz(candidate["entry_ijk"])
        vector_world = (tip - entry) * spacing
        length = float(np.linalg.norm(vector_world))
        count = max(2, int(math.ceil(length / max(0.5, float(np.min(spacing))))))
        samples = entry[None, :] + np.linspace(0.0, 1.0, count)[:, None] * (
            tip - entry)[None, :]
        values, _ = _sample_nearest(volume_zyx, samples)
        if np.any(values >= BONE_HU_THRESHOLD):
            continue
        analysis = analyze_bone_path(
            volume_zyx, spacing, entry, tip,
            needle_radius_mm=needle_radius_mm,
            max_length_mm=max_length_mm)
        if analysis["collision"] or analysis["too_long"]:
            continue
        clearance = min(analysis["bone_clearance_mm"],
                        BONE_WARNING_DISTANCE_MM + 1.0)
        if analysis["near_bone"]:
            score = (clearance * 100.0
                     - analysis["path_length_mm"] * 0.1
                     - abs(candidate["elevation_degrees"]) * 0.01)
        else:
            score = (10000.0 - analysis["path_length_mm"]
                     + clearance * 0.1
                     - abs(candidate["elevation_degrees"]) * 0.01)
        item = dict(candidate)
        item["analysis"] = analysis
        item["score"] = float(score)
        viable.append(item)

    if not viable:
        return None
    best = max(viable, key=lambda item: item["score"])
    best["candidate_count"] = len(candidates)
    best["valid_candidate_count"] = len(viable)
    return best
