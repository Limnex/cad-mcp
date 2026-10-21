"""Sheet planning and strict COM regression tests; never connect to AutoCAD."""

import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cad_mcp.drawing import layouts as native
from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.engineering.layouts import (
    LayoutRequest, confirmation, paper_geometry, plan_sheet, verify_confirmation,
)
from cad_mcp.engineering.templates import SheetTemplate, evaluate_sheet_scales


def request(**changes):
    values = dict(manifest_path="C:/templates/frame.json", sheet_id="A2",layout_name="A2-L",
        layout_id="plant-sheet-1",region_id="main",page_setup_name="PDF-A2",
        content_bounds=[10000,20000,11000,21000],model_mm_per_unit=1,scale_denominator=100)
    values.update(changes)
    return LayoutRequest(**values)


def sheet(**changes):
    values = dict(id="A2",paper_size="A2",orientation="landscape",paper_width=594,paper_height=420,
        block_name="FRAME",source_bounds=[100,200,694,620],fixed_insert_scale=1,
        paper_reference_bounds=[100,200,694,620],printed_bounds=[110,210,684,610],
        usable_regions=[dict(id="main",x=20,y=20,width=480,height=380)],
        reserved_regions=[dict(id="title",x=510,y=10,width=74,height=400)])
    values.update(changes)
    return SheetTemplate(**values)


def plan(req=None, template=None, paper=None, origin=(100,200,0)):
    req = req or request()
    return plan_sheet(req,template or sheet(),req.content_bounds,
        paper or paper_geometry([594,420],[5,5],[5,5],0),origin)


def test_paper_and_frame_fit_precedes_left_shifted_content_center():
    result = plan()
    assert result["frame"]["insertion_point"] == [-5,-5,0]
    assert result["frame"]["reference_bounds_layout"] == [-5,-5,589,415]
    assert result["region"]["layout_bounds"] == [15,15,495,395]
    assert result["viewport"]["paper_center"] == [255,205]
    assert result["viewport"]["paper_center"][0] < 594/2-5
    assert result["viewport"]["model_center"] == [10500,20500,0]
    assert result["viewport"]["paper_size"] == [476,376]


def test_internal_border_is_not_stretched_to_physical_paper():
    result = plan(template=sheet(source_bounds=[110,210,684,610]))
    assert result["frame"]["scale"] == 1
    assert result["frame"]["reference_bounds_layout"] == [-5,-5,589,415]
    assert result["frame"]["expected_bounds_layout"] == [5,5,579,405]


def test_fixed_conversion_uses_actual_nonzero_block_basepoint():
    result = plan(template=sheet(source_bounds=[1000,2000,6940,6200],
        paper_reference_bounds=[1000,2000,6940,6200],printed_bounds=[1100,2100,6840,6100],
        fixed_insert_scale=0.1),origin=(900,1800,30))
    assert result["frame"]["insertion_point"] == [-15,-25,3]
    assert result["frame"]["scale"] == 0.1


@pytest.mark.parametrize("rotation,size,margins",[
    (0,[594,420],[1,2,3,4]),(90,[420,594],[4,1,2,3]),
    (180,[594,420],[3,4,1,2]),(270,[420,594],[2,3,4,1])])
def test_rotation_transforms_asymmetric_physical_margins(rotation,size,margins):
    actual = paper_geometry([594,420],[1,2],[3,4],rotation)
    assert actual["size_mm"] == size
    assert actual["margins_mm"] == margins
    assert actual["paper_origin_layout"] == [-margins[0],-margins[1]]


def test_portrait_frame_matches_rotated_device_paper():
    portrait = sheet(paper_width=420,paper_height=594,orientation="portrait",
        source_bounds=[0,0,420,594],paper_reference_bounds=[0,0,420,594],printed_bounds=[10,10,410,584],
        usable_regions=[dict(id="main",x=20,y=20,width=380,height=500)],reserved_regions=[])
    result = plan(template=portrait,paper=paper_geometry([594,420],[5,5],[5,5],90),origin=(0,0,0))
    assert result["viewport"]["paper_center"] == [205,265]


@pytest.mark.parametrize("changes,message",[
    ({"paper_reference_bounds":None},"显式提供"),
    ({"paper_reference_bounds":[0,0,600,420]},"固定换算"),
    ({"printed_bounds":[100,200,694,620]},"可打印范围"),
    ({"usable_regions":[dict(id="main",x=20,y=20,width=570,height=380)]},"预留区域"),
    ({"usable_regions":[dict(id="main",x=-20,y=20,width=480,height=380)]},"超出纸张"),
    ({"orientation":"portrait"},"横竖方向")])
def test_inconsistent_template_fails_before_any_drawing_writes(changes,message):
    with pytest.raises(ValueError,match=message):
        plan(template=sheet(**changes))


def test_wrong_physical_media_cannot_be_hidden_by_fit_to_paper():
    with pytest.raises(ValueError,match="实际纸张尺寸"):
        plan(paper=paper_geometry([420,297],[0,0],[0,0],0))


def test_meter_and_mm_models_produce_same_physical_framing():
    mm = plan()
    meters = request(content_bounds=[10,20,11,21],model_mm_per_unit=1000)
    m = plan(req=meters)
    assert mm["minimum_scale_denominator"] == m["minimum_scale_denominator"]
    assert m["viewport"]["custom_scale"] == 10
    assert mm["viewport"]["custom_scale"] == 0.01
    result = evaluate_sheet_scales([0,0,100,100],sheet(),[100],model_mm_per_unit=1000)
    assert result["evaluations"][0]["fits"] is False


def test_model_and_annotation_extent_must_fit_requested_scale():
    with pytest.raises(ValueError,match="不自动改比例"):
        plan(req=request(content_bounds=[0,0,60000,40000],scale_denominator=100))


@pytest.mark.parametrize("changes",[
    {"content_handles":["1"]}, {"content_bounds":None},
    {"layout_name":"Model"}, {"layout_name":"bad/name"},
    {"content_bounds":[0,0,0,1]}, {"model_mm_per_unit":float("nan")},
    {"content_bounds":None,"content_handles":["A"]}])
def test_selection_and_units_require_explicit_valid_input(changes):
    with pytest.raises(ValidationError):
        request(**changes)


def test_confirmation_binds_full_plan_and_snapshot(monkeypatch):
    payload = {"plan":plan(),"snapshot_hash":"original","target":"a.dwg"}
    token = confirmation(payload)
    verify_confirmation(token,payload)
    for field in ("snapshot_hash","target"):
        changed = copy.deepcopy(payload)
        changed[field] = "changed"
        with pytest.raises(Exception,match="已变化"):
            verify_confirmation(token,changed)
    monkeypatch.setattr("cad_mcp.engineering.layouts.time.monotonic",lambda: 1e20)
    with pytest.raises(Exception,match="30分钟"):
        verify_confirmation(token,payload)


def desired():
    return dict(paper_center=[100,80],paper_size=[200,120],model_center=[500,600,0],
                custom_scale=0.01,model_view_height=12000)


class StrictViewport:
    # Deliberately no ViewCenter. A fake must enforce the real COM member surface.
    __slots__ = ("Handle","Center","Width","Height","Direction","Target","TwistAngle",
                 "ViewportOn","DisplayLocked","view_center","view_height","displayed","_custom_scale","ignore_scale")

    def __init__(self):
        self.Handle = "A"
        self.Center = (0,0,0)
        self.Width,self.Height = 200,120
        self.Direction,self.Target = (0,0,1),(0,0,0)
        self.TwistAngle = 0
        self.DisplayLocked = False
        self.view_center,self.view_height = [0,0],120
        self.displayed,self.ignore_scale = False,False
        self._custom_scale = 1

    def Display(self,value):
        self.displayed = self.ViewportOn = bool(value)

    @property
    def CustomScale(self):
        return self._custom_scale

    @CustomScale.setter
    def CustomScale(self,value):
        if not self.ignore_scale:
            self._custom_scale = value
            self.view_height = self.Height/value

    def native(self):
        return dict(paper_center=list(self.Center[:2]),paper_size=[self.Width,self.Height],
            view_center_dcs=list(self.view_center),direction=list(self.Direction),target=list(self.Target),
            view_height=self.view_height,twist=self.TwistAngle,status=1,number=2,
            flags=32768+(16384 if self.DisplayLocked else 0),frozen_layers=[])


@pytest.fixture
def viewport_backend(tmp_path,monkeypatch):
    backend = AcadBackend(tmp_path)
    vp = StrictViewport()
    backend.doc = SimpleNamespace(MSpace=False,ActivePViewport=vp,Regen=lambda mode:None,
                                  FullName="C:/test.dwg",Name="test.dwg",Activate=lambda:None)
    def zoom(center,height):
        assert backend.doc.MSpace is True
        assert vp.displayed
        vp.view_center = [center[0]-vp.Target[0],center[1]-vp.Target[1]]
        vp.view_height = height
    backend.acad = SimpleNamespace(ZoomCenter=zoom,ZoomWindow=lambda *args:None,ActiveDocument=backend.doc)
    monkeypatch.setattr(native,"point",tuple)
    monkeypatch.setattr(backend,"_read_native_viewports",lambda entities:{"A":vp.native()})
    return backend,vp


def test_actual_zoom_and_independent_native_readback(viewport_backend):
    backend,vp = viewport_backend
    result = backend._configure_sheet_viewport(vp,desired())
    assert result["model_center"] == [500,600,0]
    assert result["custom_scale"] == 0.01
    assert result["locked"] is True
    assert backend.doc.MSpace is False
    with pytest.raises(AttributeError):
        vp.ViewCenter = (500,600)


def test_silent_zoom_failure_is_not_reported_as_success(viewport_backend):
    backend,vp = viewport_backend
    backend.acad.ZoomCenter = lambda *args:None
    with pytest.raises(RuntimeError,match="实际模型中心"):
        backend._configure_sheet_viewport(vp,desired())
    assert vp.DisplayLocked is False
    assert backend.doc.MSpace is False


def test_failed_scale_write_is_not_reported_as_success(viewport_backend):
    backend,vp = viewport_backend
    vp.ignore_scale = True
    # A stale native viewport height must not be hidden by a fake CustomScale.
    def wrong_zoom(center,height):
        vp.view_center = list(center[:2])
        vp.view_height = 100
    backend.acad.ZoomCenter = wrong_zoom
    with pytest.raises(RuntimeError,match="实际比例"):
        backend._configure_sheet_viewport(vp,desired())


@pytest.mark.parametrize("changes,message",[
    ({"direction":[0,1,0]},"俯视图"),({"twist":0.1},"俯视图"),
    ({"flags":32768+65536},"裁切"),({"status":0},"关闭"),
    ({"frozen_layers":["F"]},"冻结图层"),({"paper_center":[0,0]},"纸面中心"),
    ({"paper_size":[201,120]},"纸面尺寸"),({"flags":32768},"锁定状态")])
def test_validator_rejects_bad_native_state(viewport_backend,changes,message):
    backend,vp = viewport_backend
    backend._configure_sheet_viewport(vp,desired())
    actual = vp.native()
    actual.update(changes)
    with pytest.raises(RuntimeError,match=message):
        native.verify_viewport(actual,desired())


def test_native_center_accounts_for_target_and_dcs_offset(viewport_backend):
    _,vp = viewport_backend
    vp.Center = (100,80,0)
    vp.Target = (400,550,0)
    vp.view_center = [100,50]
    vp.view_height = 12000
    vp.DisplayLocked = True
    actual = native.verify_viewport(vp.native(),desired())
    assert actual["model_center"] == [500,600,0]


def test_native_receipt_is_complete_and_has_no_drawing_writes(tmp_path):
    code = native.viewport_reader(["A"],tmp_path/"receipt.txt")
    assert "entget" in code
    assert all(word not in code for word in ("entmod","setvar","command-s","vla-put"))
    values = [100,80,200,120,500,600,0,0,1,0,0,0,12000,0,1,2,49152]
    receipt = "A\n"+"\n".join(map(str,values))+"\n0\nDONE\n"
    actual = native.parse_viewports(receipt,["A"])["A"]
    assert native.verify_viewport(actual,desired())["locked"] is True
    for text in (receipt.replace("DONE","ERROR"),receipt+"extra",receipt.replace("12000","nan")):
        with pytest.raises(RuntimeError):
            native.parse_viewports(text,["A"])


def test_new_tools_are_strict_and_refuse_dxf(monkeypatch):
    from cad_mcp import server
    from cad_mcp.drawing.document import CadDocument
    monkeypatch.setattr(server,"_backend",SimpleNamespace())
    response = json.loads(server.cad_preview_sheet_layout(
        server.PreviewSheetLayoutInput(**request().model_dump())))
    assert response["ok"] is False
    assert "acad" in response["error"]
    assert "cad_apply_sheet_layout" in __import__("cad_mcp.core.semantics",fromlist=["CREATE_TOOLS"]).CREATE_TOOLS
    with pytest.raises(ValidationError):
        server.PreviewSheetLayoutInput(**request().model_dump(),unexpected=1)


def test_plot_scale_receipt_reads_numeric_factor_without_writes(tmp_path):
    code = native.plot_scale_reader("AB",tmp_path/"scale.txt")
    assert "(assoc 147 d)" in code
    assert all(word not in code for word in ("entmod","setvar","command-s","vla-put"))
    actual = native.parse_plot_scale("AB\n16\n16\n1.0\nDONE\n","AB")
    assert actual == {"flags":16,"standard_scale_code":16,"standard_scale_factor":1.0}
    for receipt in ("AB\n16\n16\n0\nDONE", "AB\n16\n16\nnan\nDONE",
                    "AB\n16\n16\n1\nERROR", "CD\n16\n16\n1\nDONE",
                    "AB\n16\n16\n1\nDONE\nextra"):
        with pytest.raises(RuntimeError,match="实际打印倍率"):
            native.parse_plot_scale(receipt,"AB")


def test_active_x_plot_scale_values_are_the_documented_metric_constants():
    from cad_mcp.dwt.autocad import _STANDARD_SCALE, _STANDARD_SCALE_R, AcadDwtMixin
    ratios = ["1:1","1:2","1:4","1:5","1:8","1:10","1:16","1:20",
              "1:30","1:40","1:50","1:100","2:1","4:1","8:1","10:1","100:1"]
    # Golden ActiveX AcPlotScale values from AutoCAD's type library; 1..15 are imperial.
    assert [_STANDARD_SCALE[name] for name in ratios] == list(range(16,33))
    assert _STANDARD_SCALE["fit"] == 0
    assert _STANDARD_SCALE_R.get(1) is None
    for code, expected in [(1,None),(16,"1:1"),(31,"10:1")]:
        missing=[]
        payload=AcadDwtMixin._plot_scale_payload(SimpleNamespace(UseStandardScale=True,StandardScale=code),missing,"plot")
        assert payload["standard_scale"] == expected
        assert bool(missing) == (expected is None)


def test_native_readers_generate_balanced_lisp(tmp_path):
    for code in [native.plot_scale_reader("AB",tmp_path/"a(1).txt"),
                 native.viewport_reader(["AB"],tmp_path/"a(1).txt")]:
        depth=0
        quoted=False
        escaped=False
        for character in code:
            if quoted:
                if escaped: escaped=False
                elif character=="\\": escaped=True
                elif character=='"': quoted=False
            elif character=='"': quoted=True
            elif character=='(': depth+=1
            elif character==')':
                depth-=1
                assert depth>=0
        assert depth==0 and not quoted


def test_plot_scale_parser_accepts_observed_rtos_integer_roundoff_only():
    actual=native.parse_plot_scale("1A37F\n687.9999999999999\n1.000000000000000\n0.0006510416666667\nDONE\n","1A37F")
    assert actual["flags"]==688
    assert actual["standard_scale_code"]==1
    assert actual["standard_scale_factor"]==pytest.approx(1/1536)
    with pytest.raises(RuntimeError):
        native.parse_plot_scale("AB\n687.9\n16\n1\nDONE\n","AB")


def test_paper_view_is_fitted_before_entering_floating_model_space(viewport_backend):
    backend,vp = viewport_backend
    events = []
    doc = backend.doc
    class StalePaperView:
        def __init__(self):
            self.__dict__.update(doc.__dict__)
            self.__dict__.pop("MSpace")
            self._mspace = False
            self.fitted = False
        @property
        def MSpace(self):
            return self._mspace
        @MSpace.setter
        def MSpace(self,value):
            if value and not self.fitted:
                raise RuntimeError("floating viewport is outside paper view")
            events.append(("mspace",value))
            self._mspace = value
    backend.doc = StalePaperView()
    backend.acad.ActiveDocument = backend.doc
    def fit(low,high):
        assert not backend.doc.MSpace
        assert low[0] < 0 and high[0] > 594
        backend.doc.fitted = True
        events.append(("fit",True))
    backend.acad.ZoomWindow = fit
    backend._configure_sheet_viewport(vp,desired(),paper_view_bounds=[0,0,594,420])
    assert events.index(("fit",True)) < events.index(("mspace",True))
    assert not backend.doc.MSpace


def test_model_activation_failure_always_returns_to_paper(viewport_backend):
    backend,vp = viewport_backend
    def failed_zoom(*args):
        raise RuntimeError("zoom failed")
    backend.acad.ZoomCenter = failed_zoom
    with pytest.raises(RuntimeError,match="zoom failed"):
        backend._configure_sheet_viewport(vp,desired())
    assert backend.doc.MSpace is False
