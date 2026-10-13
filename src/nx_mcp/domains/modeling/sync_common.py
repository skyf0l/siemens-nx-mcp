"""Small pieces the synchronous-modeling tools (``sync_*``) share: the body consistency
read-back, the CSYS-to-CSYS motion of a ``ModlMotion`` and plane offsets of faces."""

from __future__ import annotations

from nx_mcp.kit import builders, geom, readback, select


def consistent(tx, body, what="The edit") -> None:
    """Fail (and roll back) when NX reports the body inconsistent after an edit."""
    check = getattr(tx.uf.Modeling, "AskBodyConsistency", None)
    if check is not None:
        faults = check(body.Tag)[0]
        readback.expect(not faults, "consistency", f"{what} left an invalid body", faults=faults)


def csys_motion(tx, motion, translation, rotation) -> None:
    """Configure a ``ModlMotion`` as CSYS to CSYS: p -> R p + t (``bodies._pose``)."""
    part = tx.part
    motion.Option = motion.Options.CsysToCsys
    motion.FromCsys = part.CoordinateSystems.CreateCoordinateSystem(
        tx.nx.Point3d(0.0, 0.0, 0.0), builders.matrix(tx, geom.IDENTITY), False
    )
    motion.ToCsys = part.CoordinateSystems.CreateCoordinateSystem(
        tx.nx.Point3d(*(float(v) for v in translation)), builders.matrix(tx, rotation), False
    )


def distance_motion(tx, motion, vector, distance, field="/direction") -> None:
    """Configure a ``ModlMotion`` as a distance along ``vector``."""
    from nx_mcp.kit import expr

    motion.Option = tx.nx.GeometricUtilities.ModlMotion.Options.Distance
    motion.DistanceVector = builders.direction(tx, vector, field=field)
    expr.drive(motion.DistanceValue, distance)


def planes(tx, faces) -> dict:
    """``{tag: (point, unit normal)}`` of the planar faces among ``faces``."""
    out = {}
    for face in faces:
        kind, point, normal = select.face_data(tx, face)
        if kind == select.FACE_PLANE:
            out[int(face.Tag)] = (point, geom.unit(normal))
    return out


def moved_planes(tx, faces, before, wanted, check="offset") -> list[float]:
    """Read back how far each planar face moved along its former normal; each must equal
    ``wanted(normal)``. Returns the offsets."""
    got = []
    for i, face in enumerate(faces):
        old = before.get(int(face.Tag))
        if old is None:
            continue
        point, normal = old
        now = select.face_data(tx, face)[1]
        offset = geom.dot(geom.sub(now, point), normal)
        scale = max(1.0, *map(abs, point), *map(abs, now))
        readback.expect_close(offset, wanted(normal), f"{check}[{i}]", tol=1e-6 * scale)
        got.append(offset)
    return got


def translated_box(low, high, t):
    """The box ``[low, high]`` moved by ``t``."""
    return geom.add(low, t), geom.add(high, t)


def is_identity(rotation) -> bool:
    return all(
        abs(rotation[i][j] - (1.0 if i == j else 0.0)) <= 1e-12 for i in range(3) for j in range(3)
    )
