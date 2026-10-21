"""End-to-end sheet workflow against a stateful COM double, not live AutoCAD."""

import copy
import hashlib
import json
import math
from types import SimpleNamespace

import pytest

from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing import layouts as native
from cad_mcp.engineering.layouts import LayoutRequest


class Collection:
    def __init__(self,values=()):
        self.values = list(values)

    @property
    def Count(self):
        return len(self.values)

    def Item(self,key):
        if isinstance(key,int):
            return self.values[key]
        return next(v for v in self.values if getattr(v,"Name",None) == key)


class Entity:
    counter = 32
    def __init__(self,kind,container=None):
        Entity.counter += 1
        self.Handle = format(Entity.counter,"X")
        self.ObjectName = self.EntityName = kind
        self.Layer,self.Visible = "0",True
        self.container,self.xdata = container,{}

    def GetXData(self,app):
        return self.xdata.get(app,([],[]))

    def SetXData(self,codes,values):
        codes,values = list(codes.value),list(values.value)
        self.xdata[values[0]] = codes,values

    def Delete(self):
        self.container.values.remove(self)


class Frame(Entity):
    def __init__(self,container,position,name,sx,sy,sz,rotation):
        super().__init__("AcDbBlockReference",container)
        self.InsertionPoint,self.Name,self.EffectiveName = position,name,name
        self.XScaleFactor,self.YScaleFactor,self.ZScaleFactor = sx,sy,sz
        self.Rotation = rotation

    def GetBoundingBox(self):
        x,y,z = self.InsertionPoint
        return (x,y,z),(x+594*self.XScaleFactor,y+420*self.YScaleFactor,z)


class Viewport(Entity):
    def __init__(self,container,position=(0,0,0),width=200,height=120,number=2):
        super().__init__("AcDbViewport",container)
        self.Center,self.Width,self.Height = position,width,height
        self.Direction,self.Target = (0,0,1),(0,0,0)
        self.TwistAngle,self.DisplayLocked,self.number = 0,False,number
        self.view_center,self.view_height,self.enabled = [0,0],120,True
        self._custom_scale = 1

    def Display(self,value):
        self.enabled = value

    @property
    def ViewportOn(self):
        return self.enabled

    @property
    def CustomScale(self):
        return self._custom_scale

    @CustomScale.setter
    def CustomScale(self,value):
        self._custom_scale = value
        self.view_height = self.Height/value

    def native(self):
        return dict(paper_center=list(self.Center[:2]),paper_size=[self.Width,self.Height],
            view_center_dcs=list(self.view_center),direction=list(self.Direction),target=list(self.Target),
            view_height=self.view_height,twist=self.TwistAngle,status=1 if self.enabled else 0,
            number=self.number,flags=32768+(16384 if self.DisplayLocked else 0),frozen_layers=[])


class PaperBlock(Collection):
    def InsertBlock(self,*args):
        frame = Frame(self,*args)
        self.values.append(frame)
        return frame

    def AddPViewport(self,*args):
        vp = Viewport(self,*args)
        self.values.append(vp)
        return vp


class Plot:
    def __init__(self,name):
        self.Name = name
        self.ConfigName,self.CanonicalMediaName = "PDF.pc3","A2"
        self.PlotRotation,self.PaperUnits,self.PlotType = 0,1,5
        self.StyleSheet,self.CenterPlot = "mono.ctb",False
        self.PlotWithLineweights = self.PlotWithPlotStyles = True
        self.UseStandardScale,self.StandardScale = True,16
        self.PlotOrigin = (15,12)
        self.size,self.margins = (594,420),((5,5),(5,5))
        self.Block = PaperBlock()

    def GetPaperSize(self):
        return self.size

    def GetPaperMargins(self):
        return self.margins

    def GetCanonicalMediaNames(self):
        return ["A2","A3"]

    def GetPlotDeviceNames(self):
        return ["PDF.pc3"]

    def GetPlotStyleTableNames(self):
        return ["mono.ctb"]

    def CopyFrom(self,other):
        for name in ("ConfigName","CanonicalMediaName","PlotRotation","PaperUnits","PlotType",
            "StyleSheet","CenterPlot","PlotWithLineweights","PlotWithPlotStyles",
            "UseStandardScale","StandardScale","PlotOrigin","size","margins"):
            setattr(self,name,copy.deepcopy(getattr(other,name)))

    def RefreshPlotDeviceInfo(self):
        pass


class Layouts(Collection):
    def Add(self,name):
        layout = Plot(name)
        self.values.append(layout)
        # AutoCAD can create a floating viewport when a new layout initializes.
        system = Viewport(layout.Block,number=1)
        default = Viewport(layout.Block,number=2)
        layout.Block.values.extend([system,default])
        return layout


class Layers(Collection):
    def Add(self,name):
        layer = SimpleNamespace(Name=name,Plottable=True,Lock=False,Freeze=False,LayerOn=True)
        self.values.append(layer)
        return layer


class Apps(Collection):
    def Add(self,name):
        self.values.append(SimpleNamespace(Name=name))


class Doc:
    def __init__(self):
        self.Layouts = Layouts([Plot("Model")])
        self.ActiveLayout = self.Layouts.Item("Model")
        self.PlotConfigurations = Collection([Plot("PDF-A2")])
        self.Layers = Layers()
        self.Layers.Add("0")
        self.RegisteredApplications = Apps()
        self.MSpace,self.ActivePViewport = False,None
        self.variables = {"TILEMODE":1,"DBMOD":0}
        self.FullName,self.Name = "C:/test/plant.dwg","plant.dwg"
        self.mutations = []

    @property
    def PaperSpace(self):
        return self.ActiveLayout.Block

    def GetVariable(self,name):
        return self.variables[name]

    def SetVariable(self,name,value):
        self.mutations.append((name,value))
        self.variables[name] = value

    def Regen(self,mode):
        pass

    def Activate(self):
        self.application.ActiveDocument = self


@pytest.fixture
def workflow(tmp_path,monkeypatch):
    backend = AcadBackend(tmp_path)
    backend.doc = Doc()
    content = Entity("AcDbLine")
    content.StartPoint,content.EndPoint = (10000,20000,0),(11000,21000,0)
    content.GetBoundingBox = lambda:(content.StartPoint,content.EndPoint)
    backend.msp = Collection([content])
    backend.doc.Layouts.Item("Model").Block = backend.msp
    definition_line = Entity("AcDbLine")
    definition_line.GetBoundingBox = lambda:((10,10,0),(584,410,0))
    block = Collection([definition_line])
    block.Name,block.Origin = "FRAME",(0,0,0)
    backend.doc.Blocks = Collection([block])
    # Emulate the outer semantic transaction; the actual transaction is covered
    # separately below and by existing semantic boundary tests.
    backend._undo_mark_open = True
    monkeypatch.setattr(backend,"_check_doc",lambda:None)
    monkeypatch.setattr(backend,"_scan_checkpoint",lambda i:None)
    monkeypatch.setattr(native,"point",tuple)
    monkeypatch.setattr(backend,"_read_native_viewports",lambda entities:{e.Handle:e.native() for e in entities})
    monkeypatch.setattr(backend,"_read_native_plot_scale",lambda plot:{"flags":16,"standard_scale_code":16,"standard_scale_factor":1.0})
    def zoom(center,height):
        vp = backend.doc.ActivePViewport
        vp.view_center = [center[0]-vp.Target[0],center[1]-vp.Target[1]]
        vp.view_height = height
    backend.acad = SimpleNamespace(ZoomCenter=zoom,ZoomWindow=lambda *args:None,ActiveDocument=backend.doc)
    backend.doc.application = backend.acad
    source = tmp_path/"frame.dwg"
    source.write_bytes(b"template fixture")
    manifest = tmp_path/"frame.json"
    manifest.write_text(json.dumps(dict(template_id="FRAME",source_dwg=str(source),
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),sheets=[dict(id="A2",
            paper_size="A2",paper_width=594,paper_height=420,block_name="FRAME",source_bounds=[0,0,594,420],
            paper_reference_bounds=[0,0,594,420],printed_bounds=[10,10,584,410],fixed_insert_scale=1,
            usable_regions=[dict(id="main",x=20,y=20,width=480,height=380)],
            reserved_regions=[dict(id="title",x=510,y=10,width=74,height=400)])])),encoding="utf-8")
    params = dict(manifest_path=str(manifest),sheet_id="A2",layout_name="A2-L",layout_id="sheet1",
        region_id="main",page_setup_name="PDF-A2",content_bounds=[10000,20000,11000,21000],
        model_mm_per_unit=1,scale_denominator=100)
    return backend,params


def apply(backend,params):
    preview = backend.preview_sheet_layout(**params)
    return backend.apply_sheet_layout(**params,confirmation_token=preview["confirmation_token"])


def test_preview_changes_no_layout_or_page_settings(workflow):
    backend,params = workflow
    before = copy.deepcopy(backend.doc.__dict__)
    result = backend.preview_sheet_layout(**params)
    assert result["confirmation_token"]
    assert backend.doc.Layouts.Count == 1
    assert backend.doc.mutations == []
    assert backend.doc.ActiveLayout.Name == "Model"
    assert backend.doc.PlotConfigurations.Item("PDF-A2").PlotOrigin == (15,12)
    assert backend.doc.RegisteredApplications.Count == 0


def test_complete_workflow_removes_only_new_defaults_and_is_repeatable(workflow):
    backend,params = workflow
    first = apply(backend,params)
    assert first["verified"] and first["created"]
    layout = backend.doc.Layouts.Item("A2-L")
    assert layout.PlotOrigin == (0,0)
    assert layout.Block.Count == 3 # system viewport + frame + owned viewport
    assert backend.doc.ActiveLayout.Name == "Model"
    assert backend.doc.GetVariable("TILEMODE") == 1
    check = backend.validate_sheet_layout(**params)
    assert check["verified"],check
    second = apply(backend,params)
    assert second["verified"] and not second["created"]
    assert (second["frame_handle"],second["viewport_handle"]) == (first["frame_handle"],first["viewport_handle"])
    assert layout.Block.Count == 3


def test_model_extent_change_invalidates_preview_before_writes(workflow):
    backend,params = workflow
    preview = backend.preview_sheet_layout(**params)
    backend.msp.Item(0).EndPoint = (11001,21000,0)
    with pytest.raises(Exception,match="已变化"):
        backend.apply_sheet_layout(**params,confirmation_token=preview["confirmation_token"])
    assert backend.doc.Layouts.Count == 1
    assert backend.doc.mutations == []


def test_manual_viewport_edit_is_a_conflict_not_silently_overwritten(workflow):
    backend,params = workflow
    apply(backend,params)
    layout = backend.doc.Layouts.Item("A2-L")
    vp = next(e for e in layout.Block.values if e.ObjectName == "AcDbViewport" and e.number != 1)
    vp.view_center[0] += 100
    preview = backend.preview_sheet_layout(**params)
    assert preview["confirmation_token"] is None
    assert any("人工修改" in c for c in preview["plan"]["conflicts"])
    assert backend.validate_sheet_layout(**params)["verified"] is False


def test_manual_page_setting_change_is_a_conflict(workflow):
    backend,params = workflow
    apply(backend,params)
    backend.doc.Layouts.Item("A2-L").PlotOrigin = (2,3)
    preview = backend.preview_sheet_layout(**params)
    assert any("页面设置已被人工修改" in c for c in preview["plan"]["conflicts"])
    assert preview["confirmation_token"] is None


def test_existing_unmanaged_default_viewport_requires_user_resolution(workflow):
    backend,params = workflow
    layout = backend.doc.Layouts.Add("A2-L")
    floating = next(e for e in layout.Block.values if e.number == 2)
    floating.Center,floating.Width,floating.Height = (250,200,0),500,400
    preview = backend.preview_sheet_layout(**params)
    assert any("未归属视口" in c for c in preview["plan"]["conflicts"])
    assert layout.Block.Count == 2
    assert preview["confirmation_token"] is None


def test_bad_paper_readback_stops_before_inserting_frame(workflow,monkeypatch):
    backend,params = workflow
    preview = backend.preview_sheet_layout(**params)
    original = Plot.CopyFrom
    def bad_copy(layout,source):
        original(layout,source)
        layout.size = (420,297)
    monkeypatch.setattr(Plot,"CopyFrom",bad_copy)
    with pytest.raises(RuntimeError,match="实际纸张"):
        backend.apply_sheet_layout(**params,confirmation_token=preview["confirmation_token"])
    assert all(e.ObjectName != "AcDbBlockReference" for e in backend.doc.Layouts.Item("A2-L").Block.values)
    assert backend.doc.ActiveLayout.Name == "Model"


def test_ignored_plot_origin_write_stops_before_inserting_frame(workflow,monkeypatch):
    backend,params = workflow
    preview = backend.preview_sheet_layout(**params)
    original = Plot.__setattr__
    def ignore_origin(self,name,value):
        if name == "PlotOrigin" and tuple(value) == (0,0):
            return
        original(self,name,value)
    monkeypatch.setattr(Plot,"__setattr__",ignore_origin)
    with pytest.raises(RuntimeError,match="打印偏移"):
        backend.apply_sheet_layout(**params,confirmation_token=preview["confirmation_token"])
    assert all(e.ObjectName != "AcDbBlockReference" for e in backend.doc.Layouts.Item("A2-L").Block.values)


def test_selected_handles_ignore_unrelated_distant_geometry(workflow):
    backend,params = workflow
    selected = backend._handle_str(backend.msp.Item(0).Handle)
    distant = Entity("AcDbLine")
    distant.GetBoundingBox = lambda:((1e9,1e9,0),(1e9+10,1e9+10,0))
    backend.msp.values.append(distant)
    params.pop("content_bounds")
    params["content_handles"] = [selected]
    result = backend.preview_sheet_layout(**params)
    assert result["plan"]["geometry"]["content_bounds"] == [10000,20000,11000,21000]


def test_plot_probe_uses_byref_for_dynamic_com_wrappers(workflow):
    backend,_ = workflow
    class OutPlot:
        PaperUnits,PlotRotation = 1,0
        def GetPaperSize(self,width,height):
            width.value,height.value = 594.,420.
        def GetPaperMargins(self,lower,upper):
            lower.value,upper.value = (1.,2.),(3.,4.)
    geometry = backend._sheet_plot_geometry(OutPlot())
    assert geometry["margins_mm"] == [1,2,3,4]


def test_missing_paper_evidence_does_not_create_or_change_layout(workflow):
    backend,params = workflow
    setup = backend.doc.PlotConfigurations.Item("PDF-A2")
    setup.GetPaperMargins = lambda:None
    with pytest.raises(RuntimeError,match="GetPaperMargins"):
        backend.preview_sheet_layout(**params)
    assert backend.doc.Layouts.Count == 1
    assert backend.doc.mutations == []


def prepare_transaction_double(backend,monkeypatch,*,restore=True):
    """Exercise the real transaction context, substituting only native undo."""
    from cad_mcp.core import semantics
    class Dictionaries(Collection):
        def Add(self,name):
            marker = SimpleNamespace(Name=name)
            marker.Delete = lambda:self.values.remove(marker)
            self.values.append(marker)
            return marker
    backend.doc.Dictionaries = Dictionaries()
    monkeypatch.setattr(semantics,"_dictionary",lambda *args,**kwargs:None)
    monkeypatch.setattr(semantics,"_acad_named_item",lambda collection,name:
        next((v for v in collection.values if v.Name == name),None))
    backend._undo_mark_open = False
    saved = {}
    def begin():
        saved["doc"] = copy.deepcopy(backend.doc.__dict__)
        backend._undo_mark_open = True
        return True
    def commit(started):
        backend._undo_mark_open = False
    def rollback(started):
        if restore:
            backend.doc.__dict__.clear()
            backend.doc.__dict__.update(copy.deepcopy(saved["doc"]))
            backend.msp = backend.doc.Layouts.Item("Model").Block
            backend.doc.application = backend.acad
        else:
            backend.doc.Dictionaries.values.clear()
        backend._undo_mark_open = False
        return True
    monkeypatch.setattr(backend,"_begin_undo",begin)
    monkeypatch.setattr(backend,"_commit_undo",commit)
    monkeypatch.setattr(backend,"_rollback_undo",rollback)


def test_real_transaction_rolls_back_entire_failed_new_layout(workflow,monkeypatch):
    backend,params = workflow
    prepare_transaction_double(backend,monkeypatch)
    backend.acad.ZoomCenter = lambda *args:None
    preview = backend.preview_sheet_layout(**params)
    with pytest.raises(RuntimeError,match="实际模型中心") as failure:
        backend.apply_sheet_layout(**params,confirmation_token=preview["confirmation_token"])
    assert failure.value.rollback_status == "succeeded"
    assert backend.doc.Layouts.Count == 1
    assert backend.doc.Layers.Count == 1
    assert backend.doc.ActiveLayout.Name == "Model"
    assert backend.doc.GetVariable("TILEMODE") == 1
    assert not hasattr(backend,"_layout_rollback_probe")


def test_undo_claim_without_restored_page_settings_is_blocked(workflow,monkeypatch):
    backend,params = workflow
    apply(backend,params)
    prepare_transaction_double(backend,monkeypatch,restore=False)
    preview = backend.preview_sheet_layout(**params)
    def fail_after_plot_change(*args):
        backend.doc.Layouts.Item("A2-L").PlotOrigin = (99,99)
        raise RuntimeError("injected plot failure")
    monkeypatch.setattr(backend,"_materialize_sheet_layout",fail_after_plot_change)
    with pytest.raises(RuntimeError) as failure:
        backend.apply_sheet_layout(**params,confirmation_token=preview["confirmation_token"])
    assert failure.value.rollback_status == "failed_or_unverified"
    assert backend._semantic_write_blocked is True


def test_layout_switch_status_does_not_look_like_manual_edit(workflow,monkeypatch):
    backend,params = workflow
    apply(backend,params)
    def native_in_inactive_layout(entities):
        result = {e.Handle:e.native() for e in entities}
        for state in result.values():
            state["status"] = -1
        return result
    monkeypatch.setattr(backend,"_read_native_viewports",native_in_inactive_layout)
    preview = backend.preview_sheet_layout(**params)
    assert preview["plan"]["conflicts"] == []
    assert backend.validate_sheet_layout(**params)["verified"]


def test_printed_bounds_cannot_conceal_actual_outer_border_clipping(workflow):
    backend,params = workflow
    block = backend.doc.Blocks.Item("FRAME")
    block.Item(0).GetBoundingBox = lambda:((0,0,0),(594,420,0))
    with pytest.raises(Exception,match="实际图框可打印几何"):
        backend.preview_sheet_layout(**params)
    assert backend.doc.Layouts.Count == 1


def test_nonplot_reference_rectangle_is_not_a_printed_border(workflow):
    backend,params = workflow
    reference_layer = backend.doc.Layers.Add("REFERENCE")
    reference_layer.Plottable = False
    reference = Entity("AcDbLine")
    reference.Layer = "REFERENCE"
    reference.GetBoundingBox = lambda:((0,0,0),(594,420,0))
    backend.doc.Blocks.Item("FRAME").values.append(reference)
    assert backend.preview_sheet_layout(**params)["confirmation_token"]


def test_native_reader_restores_other_active_document(workflow,monkeypatch):
    backend,_ = workflow
    other = Doc()
    other.FullName,other.Name = "C:/other.dwg","other.dwg"
    other.application = backend.acad
    backend.acad.ActiveDocument = other
    vp = Viewport(Collection())
    # Substitute only the transport; invoke the real reader and receipt parser.
    values = [100,80,200,120,500,600,0,0,1,0,0,0,12000,0,1,2,49152]
    def send(command,**kwargs):
        assert backend.acad.ActiveDocument is backend.doc
        from pathlib import Path
        script = Path(command.split('"')[1])
        receipt = script.parent/"viewports.txt"
        receipt.write_text(vp.Handle+"\n"+"\n".join(map(str,values))+"\n0\nDONE\n",encoding="ascii")
        assert kwargs["completion_check"]()
    monkeypatch.setattr(backend,"_send_command_and_wait",send)
    result = native.AcadLayoutMixin._read_native_viewports(backend,[vp])
    assert result[vp.Handle]["number"] == 2
    assert backend.acad.ActiveDocument is other


def test_nested_printed_geometry_uses_basepoint_scale_and_rotation(workflow):
    backend,_ = workflow
    child_line = Entity("AcDbLine")
    child_line.GetBoundingBox = lambda:((100,100,0),(110,120,0))
    child = Collection([child_line])
    child.Name,child.Origin = "CHILD",(100,100,0)
    backend.doc.Blocks.values.append(child)
    insert = Entity("AcDbBlockReference")
    insert.Name,insert.Normal,insert.InsertionPoint = "CHILD",(0,0,1),(30,40,0)
    insert.XScaleFactor,insert.YScaleFactor,insert.Rotation = 2,3,math.pi/2
    backend.doc.Blocks.Item("FRAME").values = [insert]
    assert backend._block_printed_bounds("FRAME") == pytest.approx([-30,40,30,60])


def test_full_apply_restores_original_active_document(workflow):
    backend,params = workflow
    other = Doc()
    other.FullName,other.Name = "C:/other.dwg","other.dwg"
    other.application = backend.acad
    backend.acad.ActiveDocument = other
    result = apply(backend,params)
    assert result["verified"]
    assert backend.acad.ActiveDocument is other
    assert backend.doc.ActiveLayout.Name == "Model"


def test_saved_page_setup_refreshes_empty_runtime_media_cache(workflow):
    backend,params = workflow
    setup=backend.doc.PlotConfigurations.Item("PDF-A2")
    setup.ready=False
    setup.GetCanonicalMediaNames=lambda:["A2"] if setup.ready else []
    setup.RefreshPlotDeviceInfo=lambda:setattr(setup,"ready",True)
    assert backend.preview_sheet_layout(**params)["confirmation_token"]
    assert backend.doc.mutations == []
    assert backend.doc.GetVariable("DBMOD") == 0


def test_resource_refresh_cannot_silently_change_stored_page_settings(workflow):
    backend,params = workflow
    setup=backend.doc.PlotConfigurations.Item("PDF-A2")
    setup.RefreshPlotDeviceInfo=lambda:setattr(setup,"CanonicalMediaName","A3")
    with pytest.raises(Exception,match="状态发生变化"):
        backend.preview_sheet_layout(**params)
    assert backend.doc.Layouts.Count == 1


def test_numeric_print_scale_catches_false_one_to_one_enum_readback(workflow,monkeypatch):
    backend,params=workflow
    assert apply(backend,params)["verified"]
    monkeypatch.setattr(backend,"_read_native_plot_scale",lambda plot:{
        "flags":16,"standard_scale_code":16,"standard_scale_factor":1/1536})
    result=backend.validate_sheet_layout(**params)
    assert result["verified"] is False
    assert "布局原生打印倍率不是1:1" in result["mismatches"]


def test_native_plot_scale_transport_restores_active_document(workflow,monkeypatch):
    backend,_=workflow
    other=Doc()
    other.FullName,other.Name="C:/other.dwg","other.dwg"
    other.application=backend.acad
    backend.acad.ActiveDocument=other
    def send(command,**kwargs):
        assert backend.acad.ActiveDocument is backend.doc
        from pathlib import Path
        script=Path(command.split('"')[1])
        (script.parent/"plot-scale.txt").write_text("AB\n16\n16\n1\nDONE\n",encoding="ascii")
        assert kwargs["completion_check"]()
    monkeypatch.setattr(backend,"_send_command_and_wait",send)
    result=native.AcadLayoutMixin._read_native_plot_scale(backend,SimpleNamespace(Handle="AB"))
    assert result["standard_scale_factor"]==1.0
    assert backend.acad.ActiveDocument is other


def test_explicit_preset_viewport_is_preserved_and_repeatable(workflow):
    backend,params = workflow
    layout = backend.doc.Layouts.Add("A2-L")
    preset = next(e for e in layout.Block.values if e.number == 2)
    params["existing_viewport_handle"] = backend._handle_str(preset.Handle)
    original_handle = preset.Handle
    preview = backend.preview_sheet_layout(**params)
    assert preview["plan"]["conflicts"] == []
    assert any("保留句柄" in a for a in preview["plan"]["actions"])
    assert layout.Block.Count == 2
    first = apply(backend,params)
    assert first["verified"]
    assert first["viewport_handle"] == str(int(original_handle,16))
    assert layout.Block.Count == 3
    assert preset in layout.Block.values
    second = apply(backend,params)
    assert second["verified"]
    assert second["viewport_handle"] == first["viewport_handle"]
    assert layout.Block.Count == 3


@pytest.mark.parametrize("candidate",["system","unknown","wrong_layout","uninitialized","frame"])
def test_invalid_explicit_preset_is_readonly_conflict(workflow,candidate):
    backend,params = workflow
    layout = backend.doc.Layouts.Add("A2-L")
    vp = next(e for e in layout.Block.values if e.number == 2)
    if candidate == "system":
        vp = next(e for e in layout.Block.values if e.number == 1)
    elif candidate == "wrong_layout":
        vp = backend.doc.Layouts.Add("other").Block.Item(1)
    elif candidate == "uninitialized":
        vp.number = 0
    elif candidate == "frame":
        vp = layout.Block.InsertBlock((0,0,0),"FRAME",1,1,1,0)
    params["existing_viewport_handle"] = "999999" if candidate == "unknown" else backend._handle_str(vp.Handle)
    count = layout.Block.Count
    preview = backend.preview_sheet_layout(**params)
    assert preview["plan"]["conflicts"]
    assert preview["confirmation_token"] is None
    assert layout.Block.Count == count
    assert backend.doc.mutations == []


def test_preset_edit_after_preview_invalidates_confirmation(workflow):
    backend,params = workflow
    layout = backend.doc.Layouts.Add("A2-L")
    vp = layout.Block.Item(1)
    params["existing_viewport_handle"] = backend._handle_str(vp.Handle)
    preview = backend.preview_sheet_layout(**params)
    vp.Width += 10
    with pytest.raises(Exception,match="已变化"):
        backend.apply_sheet_layout(**params,confirmation_token=preview["confirmation_token"])
    assert layout.Block.Count == 2


def test_explicit_preset_failure_restores_original_geometry(workflow,monkeypatch):
    backend,params = workflow
    layout = backend.doc.Layouts.Add("A2-L")
    vp = layout.Block.Item(1)
    params["existing_viewport_handle"] = backend._handle_str(vp.Handle)
    before = vp.native()
    prepare_transaction_double(backend,monkeypatch)
    backend.acad.ZoomCenter = lambda *args:None
    preview = backend.preview_sheet_layout(**params)
    with pytest.raises(RuntimeError) as failure:
        backend.apply_sheet_layout(**params,confirmation_token=preview["confirmation_token"])
    assert failure.value.rollback_status == "succeeded"
    restored = backend.doc.Layouts.Item("A2-L").Block
    assert restored.Count == 2
    assert next(e for e in restored.values if e.Handle == vp.Handle).native() == before


def test_explicit_preset_gets_verified_engineering_identity(workflow,monkeypatch):
    from cad_mcp.core import semantics
    backend,params = workflow
    layout = backend.doc.Layouts.Add("A2-L")
    vp = layout.Block.Item(1)
    params["existing_viewport_handle"] = backend._handle_str(vp.Handle)
    obj = SimpleNamespace(name="Sheet",object_type="drawing_sheet",source="user",status="confirmed",parent_id=None)
    monkeypatch.setattr(semantics,"read_object",lambda *args:obj)
    monkeypatch.setattr(backend,"set_entity_metadata",lambda handle,data:
        setattr(vp,"engineering_metadata",data))
    monkeypatch.setattr(semantics,"entity_metadata",lambda backend,entity:
        getattr(entity,"engineering_metadata",None))
    preview = backend.preview_sheet_layout(**params)
    binding = dict(project_id="project",object_id="sheet",role="sheet_layout",view_id="A2-L")
    result = backend.apply_sheet_layout(**params,semantic_binding=binding,confirmation_token=preview["confirmation_token"])
    assert result["verified"]
    assert vp.engineering_metadata["semantic_id"] == "sheet"
    assert vp.engineering_metadata["project_id"] == "project"
    assert vp.engineering_metadata["entity_count"] == 1
    assert vp.engineering_metadata["materialized_hash"] == backend._entity_state_hash(vp)


def test_preset_existing_identity_or_layer_lock_blocks_adoption(workflow,monkeypatch):
    from cad_mcp.core import semantics
    backend,params = workflow
    layout = backend.doc.Layouts.Add("A2-L")
    vp = layout.Block.Item(1)
    params["existing_viewport_handle"] = backend._handle_str(vp.Handle)
    monkeypatch.setattr(semantics,"entity_metadata",lambda *args:{"project_id":"another"})
    preview = backend.preview_sheet_layout(**params)
    assert any("已有工程身份" in c for c in preview["plan"]["conflicts"])
    monkeypatch.setattr(semantics,"entity_metadata",lambda *args:None)
    layer = backend.doc.Layers.Add("locked-preset")
    layer.Lock = True
    vp.Layer = layer.Name
    preview = backend.preview_sheet_layout(**params)
    assert any("已锁定" in c for c in preview["plan"]["conflicts"])
    assert layout.Block.Count == 2
