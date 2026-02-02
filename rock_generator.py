# Imports

import base64
import matplotlib.pyplot as plt
import numpy as np
import os
import triangle as tr

from collections import defaultdict, deque
from noise import pnoise2
from pygltflib import Accessor, Buffer, BufferView, GLTF2, Mesh, Node, Primitive, Scene
from pygltflib import ARRAY_BUFFER, FLOAT, VEC2, VEC3, VEC4
from shapely.geometry import LineString, MultiPolygon, Polygon, Point
from shapely.ops import unary_union
from shapely.prepared import prep

# Settings

# Saving

# Number of rocks to generate
ROCK_COUNT = 32
# Output directory
OUTPUT_DIRECTORY = "./"
# The maximum number of chunks or triangles when stored as vertex colors
# index = floor(color * QUANTIZATION_SCALE + 0.5)
QUANTIZATION_SCALE = 4096
# The number of angular visibility segments to save in the rock metadata, useful for occlusion tests
VISIBILITY_SEGMENTS = 16
# Whether to create summary plots
CREATE_SUMMARY_PLOTS = True
# If non-zero, will cut and mark the outer outline as outline triangles
OUTLINE_THICKNESS = 0.015

# Triangulation

# Desired number of triangles per chunk (chunks can have less).
# Increase this number to merge more complex chunks.
CHUNK_TRIANGLE_COUNT = 4
# Chunks or the outer outline may not be sharper than this angle
MIN_ANGLE = np.deg2rad(20)
# The minimum thickness of outline peninsulas (fraction of point spacing)
MIN_PENINSULA_SCALE = 0.3

# Rough rock shape

# The number of points making out the rough rock outline
ROUGH_POINTS_MIN = 6
ROUGH_POINTS_MAX = 16

# Maximum perturbation to rough rock outline
RANDOM_ROUGH_RADIUS = 0.4
RANDOM_ROUGH_ANISOTROPY = 0.5

# Fine rock shape

# The number of points sampled along the rough rock outline
FINE_POINTS = 64

# Low and high frequency perlin noise applied to the fine points
FINE_NOISE_LOW_FREQUENCY = 2.0
FINE_NOISE_HIGH_FREQUENCY = 128.0
# Absolute low-frequency noise spacing (rock radius roughly equals 1.0)
FINE_NOISE_LOW_AMPLITUDE_MIN = 0.0
FINE_NOISE_LOW_AMPLITUDE_MAX = 0.8
# Fraction of point spacing (roughly the distance between two points)
FINE_NOISE_HIGH_SCALE_MIN = 0.0
FINE_NOISE_HIGH_SCALE_MAX = 1.5

# Relaxation parameters

# Number of relaxation iterations
ITERATIONS = 250
# Force scale factor
FORCE_SCALE_FACTOR = 0.01
# Edge margin (fraction of point spacing) to prevent points from getting stuck in the outline
EDGE_MARGIN_SCALE = 0.5
# Neighborhood to repulse other points in (fraction of point spacing)
NEIGHBORHOOD_SCALE = 2.0
# Softening of repulsion (fraction of point spacing)
SOFTENING_RADIUS_SCALE = 0.1
# Randomize the repulsion strength (uniform: 0.0 - X)
RANDOM_REPULSION_STRENGTH = 0.8

# Find the smallest angle in a polygon
def polygon_min_angle(polygon):
    coords = np.asarray(polygon.exterior.coords[:-1])
    min_angle = np.inf

    for i in range(len(coords)):
        a = coords[i - 1]
        b = coords[i]
        c = coords[(i + 1) % len(coords)]

        v1 = a - b
        v2 = c - b

        d = np.linalg.norm(v1) * np.linalg.norm(v2)
        if d < 1e-12:
            return 0.0

        angle = np.arccos(np.clip(np.dot(v1, v2) / d, -1.0, 1.0))
        min_angle = min(min_angle, angle)

    return min_angle

# Find the bisector direction for three points
def bisector_direction(a, b, c):
    v1 = a - b
    v2 = c - b

    v1 /= (np.linalg.norm(v1) + 1e-12)
    v2 /= (np.linalg.norm(v2) + 1e-12)

    s = v1 + v2

    d = np.linalg.norm(s)
    if d < 1e-12:
        return np.array([1.0, 0.0])

    return s / d

# Find the boundary radius at a point
def radius_at_point(boundary, test_radius, x, y):
    delta = np.array([x, y])
    length = np.linalg.norm(delta)
    if length < 1e-12:
        return test_radius

    ray_end = delta / length * test_radius
    ray = LineString([(0.0, 0.0), (ray_end[0], ray_end[1])])
    hit = ray.intersection(boundary)

    if hit.is_empty:
        return test_radius

    if hit.geom_type == "Point":
        return np.linalg.norm(hit.coords[0])

    return min(np.linalg.norm(p.coords[0]) for p in hit.geoms)

# Helper to add a GLTF buffer accessors
def add_accessor(gltf, data, type_string):
    array = np.asarray(data, dtype=np.float32)
    raw = array.tobytes()

    # Align the GLTF blob to 4-bytes
    while len(gltf._blob) % 4:
        gltf._blob.append(0)

    byte_offset = len(gltf._blob)
    gltf._blob.extend(raw)

    # Create bufferview
    bv_index = len(gltf.bufferViews)
    gltf.bufferViews.append(BufferView(
        buffer=0,
        byteOffset=byte_offset,
        byteLength=len(raw),
        target=ARRAY_BUFFER
    ))

    # Create accessor
    accessor_index = len(gltf.accessors)
    gltf.accessors.append(Accessor(
        bufferView=bv_index,
        componentType=FLOAT,
        count=array.shape[0],
        type=type_string,
        min=array.min(axis=0).tolist(),
        max=array.max(axis=0).tolist()
    ))

    return accessor_index

# Calculate the minimum distance between each point on a polygon to any non-adjecent edge
def min_distance_to_nonadjacent_edge(polygon):
    coords = np.asarray(polygon.exterior.coords[:-1])

    min_distance = np.inf
    n = len(coords)
    for i in range(n):
        p = Point(coords[i])
        for j in range(n):
            if j in {(i - 1) % n, i, (i + 1) % n}:
                continue
            a = coords[j]
            b = coords[(j + 1) % n]
            min_distance = min(min_distance, p.distance(LineString([a, b])))

    return min_distance

def create_rock(gltf, scene, summary_image_path):
    # Pre-calculate the random parameters as to not bias successfull results
    n_rough_points = np.random.randint(ROUGH_POINTS_MIN, ROUGH_POINTS_MAX + 1)

    random_rough_radius = np.random.uniform(0.0, RANDOM_ROUGH_RADIUS)
    random_rough_angle = np.pi * 2.0 / n_rough_points * 0.25

    anisotropy = np.random.uniform(0.0, RANDOM_ROUGH_ANISOTROPY)

    noise_low_amplitude = np.random.uniform(FINE_NOISE_LOW_AMPLITUDE_MIN, FINE_NOISE_LOW_AMPLITUDE_MAX)
    noise_high_scale = np.random.uniform(FINE_NOISE_HIGH_SCALE_MIN, FINE_NOISE_HIGH_SCALE_MAX)

    random_angle = np.random.uniform(0.0, 2.0 * np.pi)

    print(
        "Creating rock with parameters\n"
        f"  rough points        : {n_rough_points}\n"
        f"  rough radius        : {random_rough_radius:.3f}\n"
        f"  rough angle         : {random_rough_angle:.3f}\n"
        f"  anisotropy          : {anisotropy:.3f}\n"
        f"  low noise amplitude : {noise_low_amplitude:.3f}\n"
        f"  high noise scale    : {noise_high_scale:.3f}"
    )

    attempts = 0
    while True:
        attempts += 1

        # Create the rough rock polygon
        angles = np.linspace(0.0, 2.0 * np.pi, n_rough_points, endpoint=False) + random_angle
        angles += np.random.uniform(-random_rough_angle, random_rough_angle, size=n_rough_points)
        radii = 1.0 + np.random.uniform(-random_rough_radius, random_rough_radius, size=n_rough_points)
        rough_points = np.column_stack([radii * np.cos(angles), radii * np.sin(angles) * (1.0 - anisotropy)])

        # Center polygon
        rough_polygon = Polygon(rough_points).buffer(0, join_style=2)
        cx, cy = rough_polygon.centroid.coords[0]
        rough_points[:, 0] -= cx
        rough_points[:, 1] -= cy

        # Validate polygon
        rough_polygon = Polygon(rough_points).buffer(0, join_style=2)
        if rough_polygon.is_empty or rough_polygon.geom_type != "Polygon":
            print("\r\033[KAttempt %i: Invalid rough polygon. Retrying..." % attempts, end="", flush=True)
            continue

        # Estimate the point spacing parameter from the outer rough polygon
        spacing = rough_polygon.exterior.length / FINE_POINTS
        noise_high_amplitude = noise_high_scale * spacing

        # Create the fine rock polygon
        distances = np.linspace(0, rough_polygon.exterior.length, FINE_POINTS, endpoint=False)
        fine_points = np.array([rough_polygon.exterior.interpolate(d).coords[0] for d in distances], float)

        # Apply random noise
        noise_offset = np.random.uniform(0.0, 256.0, size=2)
        for i, (x, y) in enumerate(fine_points):
            fine_points[i, 0] += (
                noise_low_amplitude * pnoise2(
                    (x + noise_offset[0]) * FINE_NOISE_LOW_FREQUENCY,
                    (y + noise_offset[1] + 15.0) * FINE_NOISE_LOW_FREQUENCY, octaves=2) +
                noise_high_amplitude * pnoise2(
                    (x + noise_offset[0] + 63.0) * FINE_NOISE_HIGH_FREQUENCY,
                    (y + noise_offset[1] + 83.0) * FINE_NOISE_HIGH_FREQUENCY, octaves=1)
            )
            fine_points[i, 1] += (
                noise_low_amplitude * pnoise2(
                    ((x + noise_offset[0]) + 17.0) * FINE_NOISE_LOW_FREQUENCY,
                    ((y + noise_offset[1]) + 41.0) * FINE_NOISE_LOW_FREQUENCY, octaves=2) +
                noise_high_amplitude * pnoise2(
                    ((x + noise_offset[0]) + 91.0) * FINE_NOISE_HIGH_FREQUENCY,
                    ((y + noise_offset[1]) + 73.0) * FINE_NOISE_HIGH_FREQUENCY, octaves=1)
            )

        # Validate polygon
        fine_polygon = Polygon(fine_points).buffer(0, join_style=2)
        if fine_polygon.is_empty or fine_polygon.geom_type != "Polygon":
            print("\r\033[KAttempt %i: Invalid fine polygon. Retrying..." % attempts, end="", flush=True)
            continue

        # Check if any corner is too sharp
        if polygon_min_angle(fine_polygon) < MIN_ANGLE:
            print("\r\033[KAttempt %i: Fine polygon contains sharp corners. Retrying..." % attempts, end="", flush=True)
            continue

        # Check if outline has thin sections (peninsulas)
        if min_distance_to_nonadjacent_edge(fine_polygon) < MIN_PENINSULA_SCALE * spacing:
            print("\r\033[KAttempt %i: Fine polygon contains peninsulas. Retrying..." % attempts, end="", flush=True)
            continue

        # Buffer inner polygon for margin checks
        margin = EDGE_MARGIN_SCALE * spacing
        margin_polygon = fine_polygon.buffer(-margin)

        # Validate polygon
        if margin_polygon.is_empty or margin_polygon.geom_type != "Polygon":
            print("\r\033[KAttempt %i: Invalid inner polygon. Retrying..." % attempts, end="", flush=True)
            continue

        # Prepped geometry
        inner_prepped = prep(margin_polygon)
        inner_boundary = margin_polygon.boundary

        # Estimate the number of inner points based on the spacing parameter
        n_points = max(1, int(margin_polygon.area / (spacing * spacing)))

        # Seed interior points
        min_x, min_y, max_x, max_y = margin_polygon.bounds
        interior_points = []
        while len(interior_points) < n_points:
            p = np.array([np.random.uniform(min_x, max_x), np.random.uniform(min_y, max_y)])
            if inner_prepped.contains(Point(p)):
                interior_points.append(p)
        interior_points = np.array(interior_points, float)
        n_points = len(interior_points)

        # Combine interior + edge points
        edge_points = np.asarray(fine_polygon.exterior.coords[:-1], float)
        points = np.vstack([interior_points, edge_points])
        points0 = points.copy()

        # Generate random repulsion strengths
        strength = 1.0 + RANDOM_REPULSION_STRENGTH * (np.random.random(len(points)) * 2.0 - 1.0)

        # Relaxation loop
        softening2 = (SOFTENING_RADIUS_SCALE * spacing) ** 2
        r_cut = NEIGHBORHOOD_SCALE * spacing

        for _ in range(ITERATIONS):
            # Calculate distances between points
            dvec = points[:n_points, None, :] - points[None, :, :]
            d2 = np.einsum("mni,mni->mn", dvec, dvec)

            # Ignore self
            d2[np.arange(n_points), np.arange(n_points)] = np.inf

            # Calculate repulsion force
            r2_cut = r_cut * r_cut
            inv_dist = 1.0 / (d2 + softening2)
            weight = np.clip((r2_cut - d2) / r2_cut, 0.0, 1.0)
            force = (dvec * (weight * inv_dist * strength)[..., None]).sum(axis=1)

            # Move points
            step = FORCE_SCALE_FACTOR * force
            step_length = np.linalg.norm(step, axis=1, keepdims=True)
            max_step = 0.25 * spacing
            step = np.where(step_length > max_step, step * (max_step / (step_length + 1e-12)), step)
            candidates = points[:n_points] + step

            # Project escapees back into the inner polygon with a random jitter (prevent points getting stuck)
            for i, xy in enumerate(candidates):
                pt = Point(xy)
                if not inner_prepped.contains(pt):
                    proj = inner_boundary.interpolate(inner_boundary.project(pt))
                    jitter = np.random.normal(size=2)
                    jitter /= np.linalg.norm(jitter) + 1e-12
                    candidates[i] = np.asarray(proj.coords[0]) + jitter * spacing * margin * 0.2

            points[:n_points] = candidates

        # Triangulate the resulting points
        segments = [
            (len(interior_points) + i, len(interior_points) + (i + 1) % len(edge_points))
            for i in range(len(edge_points))
        ]
        try:
            triangles = np.array(tr.triangulate({
                "vertices": points,
                "segments": segments,
            }, "p")["triangles"])
        except Exception:
            print("\r\033[KAttempt %i: Triangulation failed. Retrying..." % attempts, end="", flush=True)
            continue

        # Build polygons and precompute triangle areas
        triangle_polygons = [Polygon(points[t]) for t in triangles]
        triangle_areas = np.array([poly.area for poly in triangle_polygons])

        # Build an adjacency map via shared edges
        edge_map = {}
        triangle_neighbors = [set() for _ in range(len(triangles))]

        for ti, (a, b, c) in enumerate(triangles):
            for u, v in ((a, b), (b, c), (c, a)):
                e = (u, v) if u < v else (v, u)
                if e in edge_map:
                    tj = edge_map[e]
                    triangle_neighbors[ti].add(tj)
                    triangle_neighbors[tj].add(ti)
                else:
                    edge_map[e] = ti

        # Initialize one chunk per triangle
        chunks = [set([i]) for i in range(len(triangles))]
        triangle_to_chunk = np.arange(len(triangles))

        def chunk_area(rid):
            return sum(triangle_areas[t] for t in chunks[rid])

        # Merge triangles until they have the desired size
        while True:
            # Find chunks that violate either constraint
            candidates = [i for i in range(len(chunks)) if len(chunks[i]) < CHUNK_TRIANGLE_COUNT]

            # No more candidates
            if not candidates:
                break

            # Pick the smallest area first
            c_i = min(candidates, key=chunk_area)
            chunk_triangles = chunks[c_i]

            # Find neighboring chunks
            neighbor_chunks = set()
            for t_i in chunk_triangles:
                for t_j in triangle_neighbors[t_i]:
                    nr = triangle_to_chunk[t_j]
                    if nr != c_i:
                        neighbor_chunks.add(nr)

            # No neighbors to merge with
            if not neighbor_chunks:
                break

            # Find the best neighbor to merge with
            best_cost = np.inf
            best_index = None

            for n_i in neighbor_chunks:
                # Create the merged candidate
                merged_tris = chunk_triangles | chunks[n_i]
                merged_polygon = unary_union([triangle_polygons[t] for t in merged_tris])

                # The more compact the result is the better
                l = merged_polygon.length
                compactness = (l * l) / (4.0 * np.pi * merged_polygon.area)
                cost = l * compactness

                if cost < best_cost:
                    best_cost = cost
                    best_index = n_i

            # Keep the lower index chunk
            keep, kill = (c_i, best_index) if c_i < best_index else (best_index, c_i)

            chunks[keep] |= chunks[kill]
            for t in chunks[kill]:
                triangle_to_chunk[t] = keep

            chunks.pop(kill)
            triangle_to_chunk[triangle_to_chunk > kill] -= 1

        # Create polygons for all chunks
        chunk_polygons = [unary_union([triangle_polygons[t] for t in r]) for r in chunks]

        # Check if any chunk is too sharp
        too_sharp = [i for i, p in enumerate(chunk_polygons) if polygon_min_angle(p) < MIN_ANGLE]
        if too_sharp:
            print("\r\033[KAttempt %i: Chunk contains sharp corners. Retrying..." % attempts, end="", flush=True)
            continue

        # Cut triangles by outline band
        if OUTLINE_THICKNESS > 0.0:
            outline_polygon = fine_polygon.buffer(-OUTLINE_THICKNESS, join_style=2)
            outline_band = fine_polygon.difference(outline_polygon)

            if outline_band.is_empty or outline_polygon.is_empty or\
                outline_band.geom_type != "Polygon" or outline_polygon.geom_type != "Polygon":
                print("\r\033[KAttempt %i: Invalid outline band. Retrying..." % attempts, end="", flush=True)
                continue

            new_points = points.tolist()
            new_tris = []
            new_chunk_index = {}

            for t_i, tri in enumerate(triangles):
                tri_polygon = Polygon(points[tri])

                if not tri_polygon.intersects(outline_band):
                    old_chunk_index = triangle_to_chunk[t_i]
                    if not old_chunk_index in new_chunk_index:
                        new_chunk_index[old_chunk_index] = len(new_chunk_index)
                    chunk_index = new_chunk_index[old_chunk_index]

                    new_tris.append((tri.tolist(), chunk_index, False))
                    continue

                for polygon, is_outline in [
                    (tri_polygon.intersection(outline_band), True),
                    (tri_polygon.difference(outline_band), False),
                ]:
                    parts = []

                    if polygon.is_empty:
                        continue

                    match polygon.geom_type:
                        case "Polygon":
                            parts = [polygon]
                        case "MultiPolygon":
                            parts = list(polygon.geoms)
                        case "GeometryCollection":
                            parts = [
                                p
                                for g in polygon.geoms
                                if g.geom_type in ("Polygon", "MultiPolygon")
                                for p in (list(g.geoms) if g.geom_type == "MultiPolygon" else [g])
                            ]
                        case _:
                            parts = []

                    for p in parts:
                        exterior_points = np.asarray(p.exterior.coords[:-1], float)
                        n = len(exterior_points)
                        if n < 3:
                            continue
                        segments = [(i, (i + 1) % n) for i in range(n)]

                        new_triangles = np.array(tr.triangulate({
                            "vertices": exterior_points,
                            "segments": segments,
                        }, "p")["triangles"])

                        old_chunk_index = triangle_to_chunk[t_i]
                        if not old_chunk_index in new_chunk_index:
                            new_chunk_index[old_chunk_index] = len(new_chunk_index)
                        chunk_index = new_chunk_index[old_chunk_index]

                        for new_tri in new_triangles:
                            indices = []
                            for t in new_tri:
                                indices.append(len(new_points))
                                new_points.append(exterior_points[t])

                            new_tris.append((indices, chunk_index, is_outline))

            # Update all fields based on the new triangles
            triangles = np.array([t for t, _, _ in new_tris])
            triangle_to_chunk = np.array([c for _, c, _ in new_tris])
            triangle_is_outline = np.array([o for _, _, o in new_tris])
            points = np.array(new_points)
            triangle_polygons = [Polygon(points[t]) for t in triangles]
            chunks = [set() for _ in range(triangle_to_chunk.max() + 1)]
            for i, c in enumerate(triangle_to_chunk):
                chunks[c].add(i)
            chunk_polygons = [unary_union([triangle_polygons[t] for t in r]) for r in chunks]
        else:
            triangle_is_outline = np.array([False] * len(triangles))

        # Ensure that every chunk is a consistent segment
        if not all(not p.is_empty and p.geom_type == "Polygon" for p in chunk_polygons):
            print("\r\033[KAttempt %i: Chunks are complex. Retrying..." % attempts, end="", flush=True)
            continue

        # Merge and validate the final polygon with all triangles
        final_polygon = unary_union(chunk_polygons)
        if final_polygon.is_empty or final_polygon.geom_type != "Polygon":
            print("\r\033[KAttempt %i: Invalid final polygon. Retrying..." % attempts, end="", flush=True)
            continue
        final_boundary = final_polygon.boundary

        break

    print("\nRock successfully created\nCreating GLTF model...")

    # Check if triangle and chunk index fits into the vertex quantization scale
    if len(triangles) >= QUANTIZATION_SCALE:
        raise ValueError("Too many triangles for quantization scale")
    if len(chunks) >= QUANTIZATION_SCALE:
        raise ValueError("Too many chunks for quantization scale")

    # Reorder triangles by chunk
    triangle_order = []
    chunk_span = []

    for c_i, tris in enumerate(chunks):
        tris_sorted = sorted(tris)
        start = len(triangle_order)
        triangle_order.extend(tris_sorted)
        chunk_span.append((c_i, start, len(tris_sorted)))

    triangles = triangles[triangle_order]
    triangle_is_outline = triangle_is_outline[triangle_order]

    # Triangle to chunk lookup
    triangle_to_chunk = np.empty(len(triangles), dtype=int)
    k = 0
    for c_i, _, count in chunk_span:
        triangle_to_chunk[k:k + count] = c_i
        k += count

    # Calculate UV scale and final rock radius
    final_edge_points = np.asarray(final_polygon.exterior.coords)

    min_x, min_y = final_edge_points.min(axis=0)
    max_x, max_y = final_edge_points.max(axis=0)

    uv_scale = max(max_x - min_x, max_y - min_y)
    inv_uv_scale = 1.0 / (uv_scale + 1e-12)

    rock_radius = float(np.max(np.linalg.norm(final_edge_points, axis=1)))

    # Calculate chunk centers
    chunk_centers = [np.array(p.centroid.coords[0]) for p in chunk_polygons]

    # Create the vertex buffers
    buffer_position = []
    buffer_normal = []
    buffer_uv = []
    buffer_color = []
    buffer_triangle_center = []
    buffer_chunk_center = []

    for t_i, tri in enumerate(triangles):
        c_i = triangle_to_chunk[t_i]
        tri_points = points[tri]
        tri_center = tri_points.mean(axis=0)

        # Quantized triangle and chunk index
        color_b = t_i / (QUANTIZATION_SCALE - 1.0)
        color_a = c_i / (QUANTIZATION_SCALE - 1.0)

        for local_i, v_i in enumerate(tri):
            x, y = points[v_i]

            # Save position
            z = 0.1 if triangle_is_outline[t_i] else 0.0
            buffer_position.append([x, y, z])

            # Save 2D edge normal
            p0 = tri_points[(local_i - 1) % 3]
            p1 = tri_points[local_i]
            p2 = tri_points[(local_i + 1) % 3]
            bi = bisector_direction(p0, p1, p2)
            normal_x = -bi[1]
            normal_y = bi[0]

            # Save normalized angle around rock
            angle = np.arctan2(y, x)
            normal_z = (angle + np.pi) / (2.0 * np.pi)

            buffer_normal.append([normal_x, normal_y, normal_z])

            # Save fixed aspect and centered UV
            u = np.clip(x * inv_uv_scale + 0.5, 0.0, 1.0)
            v = np.clip(y * inv_uv_scale + 0.5, 0.0, 1.0)

            buffer_uv.append([u, v])

            # Save absolute radius of rock at point
            r_edge = radius_at_point(final_boundary, rock_radius * 2.0, x, y)
            color_r = r_edge * 0.1
            # Save normalized depth of point
            color_g = max(0.0, r_edge - np.hypot(x, y)) / (r_edge + 1e-12)

            buffer_color.append([color_r, color_g, color_b, color_a])

            # Triangle and chunk centers
            buffer_triangle_center.append([tri_center[0], tri_center[1], local_i])
            buffer_chunk_center.append([
                chunk_centers[c_i][0],
                chunk_centers[c_i][1],
                np.linalg.norm(chunk_centers[c_i]),
            ])

    # Find chunk adjacency by edge
    edge_to_chunk = defaultdict(set)

    for t_i, tri in enumerate(triangles):
        c_i = triangle_to_chunk[t_i]

        for i in range(3):
            p0 = points[tri[i]]
            p1 = points[tri[(i + 1) % 3]]

            e0 = (int(round(p0[0] / 1e-6)), int(round(p0[1] / 1e-6)))
            e1 = (int(round(p1[0] / 1e-6)), int(round(p1[1] / 1e-6)))
            edge_key = tuple(sorted((e0, e1)))

            edge_to_chunk[edge_key].add(c_i)

    # Build chunk neighbor sets
    chunk_neighbors = [set() for _ in chunks]

    for edge_chunks in edge_to_chunk.values():
        if len(edge_chunks) > 1:
            for c_i in edge_chunks:
                chunk_neighbors[c_i].update(edge_chunks - { c_i })

    # Get graph order from rock surface
    surface_chunks = set()

    for i, polygon in enumerate(chunk_polygons):
        if polygon.boundary.intersects(final_boundary):
            surface_chunks.add(i)

    chunk_depth = [-1] * len(chunks)
    chunk_queue = deque()

    for c_i in surface_chunks:
        chunk_depth[c_i] = 0
        chunk_queue.append(c_i)

    while chunk_queue:
        c_i = chunk_queue.popleft()
        for n in chunk_neighbors[c_i]:
            if chunk_depth[n] == -1:
                chunk_depth[n] = chunk_depth[c_i] + 1
                chunk_queue.append(n)

    if any(d < 0 for d in chunk_depth):
        raise RuntimeError("Disconnected chunk graph detected")

    # Create visibility bins
    chunk_visibility = []

    for c_i, center_xy in enumerate(chunk_centers):
        bins = [set() for _ in range(VISIBILITY_SEGMENTS)]

        if VISIBILITY_SEGMENTS > 0:
            for d_i, polygon in enumerate(chunk_polygons):
                if c_i == d_i:
                    continue

                angles = []

                for x, y in polygon.exterior.coords:
                    angles.append(np.arctan2(y - center_xy[1], x - center_xy[0]))

                angles = np.unwrap(np.array(angles))

                b0 = int((angles.min() + np.pi) / (2 * np.pi) * VISIBILITY_SEGMENTS)
                b1 = int((angles.max() + np.pi) / (2 * np.pi) * VISIBILITY_SEGMENTS)

                if b0 <= b1:
                    for b in range(b0, b1 + 1):
                        bins[b % VISIBILITY_SEGMENTS].add(d_i)
                else:
                    for b in list(range(b0, VISIBILITY_SEGMENTS)) + list(range(0, b1 + 1)):
                        bins[b].add(d_i)

        chunk_visibility.append([sorted(s) for s in bins])

    # Create metadata (for GLTF extras)
    chunk_metadata = []
    for c_i, tri_start, tri_count in chunk_span:
        chunk_metadata.append({
            "center": [float(chunk_centers[c_i][0]), float(chunk_centers[c_i][1]), 0.0],
            "connected_chunks": sorted(int(x) for x in chunk_neighbors[c_i]),
            "graph_depth": chunk_depth[c_i],
            "vertex_offset": int(tri_start * 3),
            "vertex_count": int(tri_count * 3),
            "chunk_visibility": chunk_visibility[c_i],
        })

    rock_index = len(scene.nodes)

    # Create GLTF mesh
    primitive = Primitive(
        attributes={
            "POSITION": add_accessor(gltf, buffer_position, VEC3),
            "NORMAL": add_accessor(gltf, buffer_normal, VEC3),
            "TEXCOORD_0": add_accessor(gltf, buffer_uv, VEC2),
            "COLOR_0": add_accessor(gltf, buffer_color, VEC4),
            "_TRIANGLE_CENTER": add_accessor(gltf, buffer_triangle_center, VEC3),
            "_CHUNK_CENTER": add_accessor(gltf, buffer_chunk_center, VEC3),
        },
        mode=4
    )
    mesh = Mesh(primitives=[primitive])
    mesh_index = len(gltf.meshes)
    gltf.meshes.append(mesh)

    # Create GLTF node
    node = Node()
    node.mesh = mesh_index
    node.translation = [(rock_index % 4) * 4.0, int(rock_index / 4) * 4.0, 0.0]
    node.name = "Rock_%i" % rock_index
    node.extras = {
        "chunk_count": len(chunk_metadata),
        "chunks": chunk_metadata,
    }
    node_index = len(gltf.nodes)
    gltf.nodes.append(node)
    scene.nodes.append(node_index)

    # Plot summary
    if not CREATE_SUMMARY_PLOTS:
        return

    fine_xy = np.asarray(fine_polygon.exterior.coords)
    margin_xy = np.asarray(margin_polygon.exterior.coords)

    plt.clf()
    plt.figure(figsize=(7, 7))

    plt.plot(fine_xy[:, 0], fine_xy[:, 1], lw=2, alpha=0.3, label="Rock polygon")
    plt.plot(margin_xy[:, 0], margin_xy[:, 1], lw=1, alpha=0.3, label="Inner margin")
    if OUTLINE_THICKNESS > 0.0:
        outline_xy = np.asarray(outline_polygon.exterior.coords)
        plt.plot(outline_xy[:, 0], outline_xy[:, 1], lw=1, alpha=0.3, label="Outline")

    plt.scatter(points0[n_points:, 0], points0[n_points:, 1], s=6, alpha=0.4, label="Edge points")
    plt.scatter(points[:n_points, 0], points[:n_points, 1], s=6, alpha=0.4, label="Relaxed points")

    plt.triplot(points[:, 0], points[:, 1], triangles, linewidth=0.8, alpha=0.1, color="gray")

    for polygon in chunk_polygons:
        xy = np.asarray(polygon.exterior.coords)
        plt.plot(xy[:, 0], xy[:, 1], lw=0.8)

    plt.axis("equal")
    plt.title("Fractured rock")
    plt.legend(loc='upper right')

    plt.savefig(summary_image_path)

    print('Summary saved at: "%s"' % summary_image_path)

# Create GLTF file
gltf = GLTF2()
scene = Scene()
gltf.scenes.append(scene)
gltf.scene = 0

# Store all binary data in a single buffer
gltf.buffers.append(Buffer(byteLength=0, uri=""))
gltf._blob = bytearray()

# Create all the rocks, add them to the GLTF file and save a summary plot
for i in range(ROCK_COUNT):
    print("Processing rock %i out of %i" % (i + 1, ROCK_COUNT))

    create_rock(gltf, scene, os.path.join(OUTPUT_DIRECTORY, "summary_%i.png" % (i + 1)))

# Finalize GLTF buffer
blob = bytes(gltf._blob)
gltf.buffers[0].byteLength = len(blob)
gltf.buffers[0].uri = None
gltf.set_binary_blob(blob)

# Save GLTF file
gltf_path = os.path.join(OUTPUT_DIRECTORY, "rocks.glb")
gltf.save(gltf_path)

print('Created GLTF file at: "%s"' % gltf_path)

