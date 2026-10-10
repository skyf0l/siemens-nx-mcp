"""nx_set_name (domains/session/naming.py): rename by reference, NX's stored name read
back, every reference kept (the renamed object re-anchored), clear name errors, batches."""

from tests.kit import fake_nx, run
from tests.kit.kernel import services
from tests.kit.objects import Feature
from tests.unit.sketch.fakes import sketch_nx


def feature(nx, name):
    f = nx._own(Feature(name), "feature")
    nx.part.Features.append(f)
    return f


def test_rename_returns_stored_name_and_keeps_every_reference():
    nx = fake_nx()
    f, other = feature(nx, "EXTRUDE(1)"), feature(nx, "BLEND(2)")
    old, kept = f.ref, other.ref
    ok = run(nx, "nx_set_name", object=old, name="Boss")
    assert ok.ok and ok.mutation_outcome == "committed", ok
    assert f.Name == "Boss" and ok.data["name"] == "Boss" and ok.data["requested"] == "Boss"
    assert ok.data["object"]["kind"] == "feature" and ok.data["object"]["name"] == "Boss"
    # A rename keeps the object: its ref and the part's other refs stay valid.
    assert ok.data["object"]["id"] == old and old in nx.refs and kept in nx.refs
    assert ok.data["references"] == "preserved"
    assert ok.changes["references"] == "preserved"
    assert services(nx).history[-1]["method"] == "nx_set_name"  # undoable
    again = run(nx, "nx_set_name", object=kept, name="Fillet")
    assert again.ok and other.Name == "Fillet"


def test_normalised_name_is_read_back():
    nx = fake_nx()
    f = feature(nx, "EXTRUDE(1)")
    f.SetName = lambda name: setattr(f, "Name", name.upper())
    ok = run(nx, "nx_set_name", object=f.ref, name="Boss")
    assert ok.data["name"] == "BOSS" and ok.data["requested"] == "Boss"


def test_rename_refuses_bad_names_and_stale_refs():
    nx = fake_nx()
    f = feature(nx, "EXTRUDE(1)")
    for name, says in (("", "must not be empty"), ("   ", "must not be empty"),
                       ("a\nb", "control character"), ("a\tb", "control character")):  # fmt: skip
        bad = run(nx, "nx_set_name", object=f.ref, name=name)
        assert bad.error["code"] == "NX_INVALID_ARGUMENT", (name, bad)
        assert says in bad.error["message"] and bad.error["field"] == "/name"
        assert bad.mutation_outcome == "not_started"
    long = run(nx, "nx_set_name", object=f.ref, name="x" * 133)
    assert long.error["code"] == "NX_INVALID_ARGUMENT" and "132" in long.error["message"]
    stale = run(nx, "nx_set_name", object="f:999", name="A")
    assert stale.error["code"] == "NX_STALE_REFERENCE"
    assert stale.mutation_outcome == "not_started" and f.Name == "EXTRUDE(1)"
    display_name = run(nx, "nx_set_name", object="EXTRUDE(1)", name="A")
    assert display_name.error["code"] == "NX_INVALID_ARGUMENT"  # references only


def test_rename_runs_in_a_batch_on_an_earlier_steps_result():
    nx = sketch_nx()
    ops = [
        {"tool": "nx_create_sketch", "arguments": {"placement": {"principal": "xy"}}},
        {"tool": "nx_set_name", "arguments": {"object": "$0.data.sketch", "name": "Profile"}},
        {"tool": "nx_finish_sketch", "arguments": {"sketch": "$0.data.sketch"}},
    ]
    ok = run(nx, "nx_run_batch", operations=ops)
    assert ok.ok, ok
    sketch_id = ok.data["results"][0]["data"]["sketch"]["id"]
    assert ok.data["results"][1]["data"]["object"]["id"] == sketch_id
    assert nx.part.Sketches[0].Name == "Profile" and sketch_id in nx.refs


def test_real_references_keep_ids_and_reanchor_the_renamed_object(rig, monkeypatch):
    """On the host's real reference table: same ids after a rename; the renamed object's
    anchor follows its new journal identifier (as a sketch's does in NX)."""
    monkeypatch.setenv("NX_MCP_DEV", "1")  # the rig's build has no acceptance evidence
    first, second = rig.nx.Body("B1"), rig.nx.Body("B2")

    def set_name(name, body=first):
        body.Name = name.upper()  # NX stores most names in upper case
        body.JournalIdentifier = name.upper()

    first.SetName = set_name
    rig.part.Bodies.extend([first, second])
    listed = rig.run("nx_list_objects", {"kind": "body"})["data"]["items"]
    ids = [row["id"] for row in listed]
    ok = rig.run("nx_set_name", {"object": ids[0], "name": "Plate"})
    assert ok["ok"], ok
    assert ok["data"]["name"] == "PLATE" and ok["data"]["requested"] == "Plate"
    assert ok["data"]["object"]["id"] == ids[0] and ok["data"]["object"]["name"] == "PLATE"
    again = rig.run("nx_list_objects", {"kind": "body"})["data"]["items"]
    assert [row["id"] for row in again] == ids  # nothing went stale
    assert again[0]["anchor"].endswith(":PLATE#0") and again[0]["part"] == listed[0]["part"]
