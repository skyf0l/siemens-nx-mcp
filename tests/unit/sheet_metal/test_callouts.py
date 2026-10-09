"""nx_create_sheet_metal_callout and the managed-callout @refresher on the shared fake."""

import json

import pytest

from nx_mcp.core import catalogue
from nx_mcp.core.errors import NXError
from nx_mcp.core.tx import Tx
from nx_mcp.domains.sheet_metal.callouts import MANAGED, refresh_callouts
from tests.kit import run
from tests.kit.kernel import services
from tests.unit.sheet_metal.fakes import Annotation, sm_nx

TOOL = "nx_create_sheet_metal_callout"


def pmi(nx, origin=(50.0, -20.0, 0.0)):
    """Commit hook: NX commits one annotation (or rewrites the existing one)."""
    made = []

    def hook(b):
        existing = b.args[0] if b.args else None
        note = existing or Annotation("SM_PMI", origin)
        if existing is None:
            nx.part.Notes.append(note)
        object.__setattr__(b, "GetCommittedObjects", lambda: [note])
        made.append(note)
        return note

    nx.on_commit("SheetMetalPmiBuilder", hook)
    return made


def test_body_callout_writes_the_measured_thickness_and_a_managed_record():
    nx = sm_nx()
    body = nx.tab(t=2)
    made = pmi(nx)
    ok = run(nx, TOOL, kind="body", body=body.ref, position=[50, -20, 0])
    assert ok.ok, ok
    b = nx.last("SheetMetalPmiBuilder")
    assert b.PMIType == "SheetMetalPMIBuilder.Types.Body" and b.SelectedBody.Value is body
    assert b.Text.TextBlock.SetText.calls[0][0][0] == [
        "Sheet metal (managed measurement)",
        "Thickness: 2.000 mm",
    ]
    assert b.AssociatedObjects.Nxobjects.Add.calls[0][0][0] == [body]
    record = json.loads(made[0].attributes[MANAGED])
    assert record["kind"] == "body" and record["body"] == f"handle-{body.Tag}"
    assert record["measured"] == {"thickness": 2.0}
    assert ok.data["measured"] == {"thickness": 2.0} and ok.data["automatic"] is True
    assert nx.live_builders == 0


def test_bend_callout_measures_each_bend_face():
    nx = sm_nx()
    body = nx.tab()
    bend = nx.add_bend(body, radius=3, angle=90, nf=0.33)
    pmi(nx)
    ok = run(nx, TOOL, kind="bend", body=body.ref, faces=[bend.ref], position=[0, 0, 0],
             automatic=False)  # fmt: skip
    assert ok.ok, ok
    assert ok.data["text"] == [
        "Bend data (measured snapshot)",
        "Bend 1: R 3.000 mm; angle 90.000 deg; K 0.330",
    ]
    assert nx.last("SheetMetalPmiBuilder").SelectedFace.Add.calls[0][0][0] == [bend]
    assert MANAGED not in nx.part.Notes[0].attributes


@pytest.mark.parametrize(
    ("args", "code", "field"),
    [
        ({"kind": "body", "faces": "BEND"}, "NX_INVALID_ARGUMENT", "/faces"),
        ({"kind": "bend"}, "NX_INVALID_ARGUMENT", "/faces"),
        ({"kind": "bend", "faces": "WEB"}, "NX_WRONG_GEOMETRY", "/faces/0"),
        ({"kind": "body", "body": "PLAIN"}, "NX_WRONG_GEOMETRY", "/body"),
    ],
)
def test_callout_refusals_change_nothing(args, code, field):
    nx = sm_nx()
    body = nx.tab()
    bend = nx.add_bend(body)
    plain = nx.block(10, 10, 10)
    names = {"BEND": [bend.ref], "WEB": [body.faces[0].ref], "PLAIN": plain.ref}
    call = {"body": body.ref, "position": [0, 0, 0]}
    call.update({k: names.get(v, v) if isinstance(v, str) else v for k, v in args.items()})
    bad = run(nx, TOOL, **call)
    assert bad.error["code"] == code and bad.error["field"] == field
    assert bad.mutation_outcome == "not_started" and nx.builders == []


def test_missing_sheet_metal_pmi_license_is_reported_and_rolled_back():
    nx = sm_nx()
    body = nx.tab()
    nx.refuse("SheetMetalPmiBuilder", 948801, "License Error")
    bad = run(nx, TOOL, kind="body", body=body.ref, position=[0, 0, 0])
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE" and bad.error["nx_code"] == 948801
    assert bad.error["category"] == "unavailable" and nx.live_builders == 0
    assert nx.part.Notes == []


def refresher_tx(nx):
    tx = Tx(services(nx), catalogue.load()[TOOL], operation_id="refresh")
    tx.units = {"length": "mm", "angle": "deg"}
    return tx


def managed(nx, body, measured, faces=()):
    note = Annotation("SM_PMI", (5.0, 6.0, 0.0))
    note.attributes[MANAGED] = json.dumps({
        "kind": "bend" if faces else "body",
        "body": f"handle-{body.Tag}",
        "faces": [f"handle-{f.Tag}" for f in faces],
        "measured": measured,
    })  # fmt: skip
    nx.part.Notes.append(note)
    return note


def test_refresher_rewrites_only_callouts_whose_values_changed():
    nx = sm_nx()
    body = nx.tab(t=2)
    bend = nx.add_bend(body, angle=90)
    same = managed(nx, body, {"thickness": 2.0})
    moved = managed(nx, body, {"bends": [{"radius": 3.0, "angle": 80.0, "neutral_factor": 0.33}]},
                    faces=[bend])  # fmt: skip
    pmi(nx)
    rows = refresh_callouts(refresher_tx(nx), None)
    assert [r["id"] for r in rows] == [nx.ref_of(moved, "annotation")]
    b = nx.last("SheetMetalPmiBuilder")
    assert b.args == (moved,) and b.PMIType == "SheetMetalPMIBuilder.Types.Bend"
    assert b.Text.TextBlock.SetText.calls[0][0][0][1].startswith("Bend 1: R 3.000 mm; angle 90.000")
    assert json.loads(moved.attributes[MANAGED])["measured"]["bends"][0]["angle"] == 90.0
    assert json.loads(same.attributes[MANAGED])["measured"] == {"thickness": 2.0}
    assert refresh_callouts(refresher_tx(nx), None) == []  # nothing changed since


def test_refresher_skips_disabled_and_fails_on_a_lost_source():
    nx = sm_nx()
    body = nx.tab()
    note = managed(nx, body, {"thickness": 2.0})
    note.attributes[MANAGED] = "disabled"
    assert refresh_callouts(refresher_tx(nx), None) == []
    stale = managed(nx, body, {"thickness": 2.0})
    stale.attributes[MANAGED] = stale.attributes[MANAGED].replace(f"handle-{body.Tag}", "gone")
    with pytest.raises(NXError) as error:
        refresh_callouts(refresher_tx(nx), None)
    assert error.value.code == "NX_STALE_SOURCE"


def test_refresher_without_callouts_touches_nothing():
    nx = sm_nx()
    nx.tab()
    assert refresh_callouts(refresher_tx(nx), None) == [] and nx.builders == []
