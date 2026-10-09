"""Legacy entry points resolve typed references and reject unsupported geometry."""

from types import SimpleNamespace as S

from nx_mcp.legacy_repairs import LegacyRepairsMixin


class Line:
    def __init__(self, tag, end):
        self.Tag = tag
        self.StartPoint = S(X=0, Y=0, Z=0)
        self.EndPoint = S(X=end[0], Y=end[1], Z=end[2])


class Harness(LegacyRepairsMixin):
    def __init__(self):
        self.nxopen = S(Line=Line, Edge=type("Edge", (), {}), Face=type("Face", (), {}))
        self.a, self.b = Line(1, [1, 0, 0]), Line(2, [0, 1, 0])
        self.part = S(Sketches=[S(GetAllGeometry=lambda: [self.a, self.b])])
        self.calls = []

    def _resolve(self, ref, kinds):
        return {"a": self.a, "b": self.b}[ref]

    def _work_part(self):
        return self.part

    def _reference(self, *args):
        return {"id": "sketch"}

    def _edit_sketch(self, *args):
        self.calls.append(args)
        return {"edited": True}


def test_delete_feature_updates_native_delete_list_and_reports_reference():
    h = Harness()
    h.session = S(
        UpdateManager=S(
            AddToDeleteList=lambda feature: h.calls.append(feature), DoUpdate=lambda: None
        )
    )
    h._require_api = lambda *args: None
    h._update_model = lambda: h.calls.append("update")
    assert h._delete_feature("a") == {"deleted": [{"id": "sketch"}]}
    assert h.calls == [h.a, "update"]
