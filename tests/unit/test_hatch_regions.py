import json
import math
from types import SimpleNamespace

import ezdxf
from ezdxf.entities import Hatch
import pytest
from pydantic import ValidationError

from cad_mcp import server
from cad_mcp.core.hatch_boundaries import (
    BoundarySource, Curve, HatchRegion, arc_curves, bulge_vertices,
    contains, loop_area, polyline_curves, select_region,
)
from cad_mcp.drawing import hatch_regions
from cad_mcp.drawing import autocad as acad_module
from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing.document import CadDocument
from cad_mcp.dwt import autocad as dwt_autocad


SQUARE = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)]


def square_source(handle="1"):
    return BoundarySource(handle, polyline_curves(SQUARE, True), True)


def notch_sources():
    bulge = -math.tan(math.pi / 8)
    outline = [(0,6,0),(0,8,bulge),(2,10,0),(12,10,bulge),(14,8,0),(14,2,bulge),(12,0,0),(2,0,bulge),(0,2,0),(0,4,0)]
    left = 4 - math.sqrt(3)
    return [
        BoundarySource("1", polyline_curves(outline, False), False),
        BoundarySource("2", arc_curves((4,5), 2), True),
        BoundarySource("3", (Curve((0,4),(left,4)),), False),
        BoundarySource("4", (Curve((0,6),(left,6)),), False),
    ]


def test_exact_composite_notch_retains_arcs_and_excludes_entire_circle():
    plan = select_region(notch_sources(), (10,5))
    assert plan.area == pytest.approx(116 + math.sqrt(3) + 2 * math.pi / 3)
    assert not plan.holes
    assert plan.bounds == ((0,0),(14,10))
    assert contains(plan.outer, (10,5))
    assert not contains(plan.outer, (4,5))
    for index in range(64):
        angle = index * math.tau / 64
        assert not contains(plan.outer, (4 + 1.99 * math.cos(angle), 5 + 1.99 * math.sin(angle)))
    assert any(c.center is not None for c in plan.outer)
    assert all(c.center is not None or c.sweep == 0 for c in plan.outer)


@pytest.mark.parametrize("seed", [(1,1), (5,1), (1,5), (9,9)])
def test_disconnected_circle_is_a_hole(seed):
    plan = select_region([square_source(), BoundarySource("2", arc_curves((5,5), 2), True)], seed)
    assert len(plan.holes) == 1
    assert plan.area == pytest.approx(100 - 4 * math.pi)
    assert plan.excluded_area == pytest.approx(4 * math.pi)
    assert loop_area(plan.outer) > 0
    assert loop_area(plan.holes[0]) < 0


def test_seed_selects_the_circle_face_instead_of_surrounding_face():
    plan = select_region([square_source(), BoundarySource("2", arc_curves((5,5), 2), True)], (5,5))
    assert plan.area == pytest.approx(4 * math.pi)
    assert not plan.holes


def test_multiple_and_nested_holes_are_not_double_subtracted():
    sources = [square_source(), BoundarySource("2", arc_curves((3,3), 1), True), BoundarySource("3", arc_curves((7,7), 1), True), BoundarySource("4", arc_curves((3,3), 0.5), True)]
    plan = select_region(sources, (1,1))
    assert len(plan.holes) == 2
    assert plan.area == pytest.approx(100 - 2 * math.pi)


@pytest.mark.parametrize("translation", [(0,0),(4000000,30000000)])
def test_large_world_coordinates_and_reversed_sources(translation):
    tx,ty = translation
    sources = []
    for source in notch_sources():
        curves = tuple(Curve((c.start[0]+tx,c.start[1]+ty),(c.end[0]+tx,c.end[1]+ty),(c.center[0]+tx,c.center[1]+ty) if c.center is not None else None,c.sweep) for c in reversed(source.curves))
        curves = tuple(c.reversed() for c in curves)
        sources.append(BoundarySource(source.handle, curves, source.closed))
    plan = select_region(sources, (10+tx,5+ty))
    assert plan.area == pytest.approx(116 + math.sqrt(3) + 2 * math.pi / 3, abs=1e-6)


@pytest.mark.parametrize("seed", [(20,20),(0,5),(float("nan"),1)])
def test_seed_must_be_finite_inside_and_off_the_boundary(seed):
    with pytest.raises(ValueError):
        select_region([square_source()], seed)


@pytest.mark.parametrize("case", ["gap","dangling","overlap","self_crossing","different_elevation","duplicate"])
def test_invalid_composite_boundaries_are_rejected(case):
    if case == "gap":
        sources = [BoundarySource("1", polyline_curves(SQUARE, False), False)]
    elif case == "dangling":
        sources = [square_source(), BoundarySource("2", (Curve((0,5),(-1,5)),), False)]
    elif case == "overlap":
        sources = [square_source(), BoundarySource("2", (Curve((0,0),(5,0)),), False)]
    elif case == "self_crossing":
        sources = [BoundarySource("1", polyline_curves([(0,0,0),(3,2,0),(0,2,0),(2,0,0)],True),True)]
    elif case == "different_elevation":
        sources = [square_source(), BoundarySource("2", arc_curves((5,5),1),True,2)]
    else:
        sources = [square_source(),square_source()]
    with pytest.raises(ValueError):
        select_region(sources, (1,1))


@pytest.mark.parametrize("changes", [
    {"boundary_handle":"1"}, {"seed_point":None}, {"boundary_handles":["1","1"]},
    {"boundary_handles":["A"]}, {"associative":True}, {"seed_point":[1,float("inf")]},
])
def test_composite_input_contract(changes):
    values = dict(boundary_handles=["1","2"],seed_point=[1,1],pattern="GRASS",scale=25)
    values.update(changes)
    with pytest.raises(ValidationError):
        server.DrawHatchInput(**values)


@pytest.fixture
def dxf_backend(tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("composite.dxf")
    return backend


def add_dxf_source(backend, source):
    if source.closed and all(c.center == source.curves[0].center and c.center is not None for c in source.curves):
        entity = backend.msp.add_circle(source.curves[0].center, source.curves[0].radius)
    elif len(source.curves) == 1 and source.curves[0].center is None:
        entity = backend.msp.add_line(source.curves[0].start, source.curves[0].end)
    else:
        vertices = bulge_vertices(source.curves)
        if not source.closed:
            vertices.append((*source.curves[-1].end,0))
        entity = backend.msp.add_lwpolyline(vertices,format="xyb",close=source.closed)
    return backend._handle_str(entity.dxf.handle)


@pytest.mark.parametrize("shape", ["hole","notch"])
@pytest.mark.parametrize("pattern", ["GRASS","SOLID"])
def test_dxf_real_hatch_roundtrip_preserves_holes_and_exact_arcs(dxf_backend,tmp_path,shape,pattern):
    backend = dxf_backend
    sources = notch_sources() if shape=="notch" else [square_source(),BoundarySource("2",arc_curves((5,5),2),True)]
    seed = (10,5) if shape=="notch" else (1,1)
    handles = [add_dxf_source(backend,s) for s in sources]
    expected = select_region(sources,seed)
    existing_handles = set(backend.doc.entitydb)
    params = dict(boundary_handles=handles,seed_point=seed,pattern=pattern,scale=25 if pattern=="GRASS" else 1,layer="COMPOSITE")
    preview = backend.draw_hatch(**params)
    assert preview["would_write"] is False
    assert set(backend.doc.entitydb)==existing_handles
    assert "COMPOSITE" not in backend.doc.layers
    result = backend.draw_hatch(**params,dry_run=False)
    assert result["verified"] is True
    assert result["source_geometry_unchanged"] is True
    assert result["boundary_area"]==pytest.approx(expected.area)
    assert len(backend.msp)==len(sources)+1
    path=tmp_path/"written.dxf"
    backend.doc.saveas(path)
    loaded=ezdxf.readfile(path)
    hatch=loaded.entitydb[format(int(result["hatch_handle"]),"X")]
    assert hatch.dxf.pattern_name==pattern
    assert hatch.dxf.associative==0
    assert hatch.dxf.hatch_style==0
    assert len(hatch.paths)==len(expected.loops)
    loops=[polyline_curves(p.vertices,True) for p in hatch.paths]
    assert abs(loop_area(loops[0]))-sum(abs(loop_area(p)) for p in loops[1:])==pytest.approx(expected.area)
    if shape=="notch":
        assert not contains(loops[0],(4,5))
    else:
        assert contains(loops[0],(5,5)) and contains(loops[1],(5,5))
    assert any(any(abs(v[2])>0 for v in p.vertices) for p in hatch.paths)
    assert not loaded.audit().has_errors


def test_dxf_composite_readback_failure_leaves_only_sources(monkeypatch,dxf_backend):
    backend=dxf_backend
    handles=[add_dxf_source(backend,s) for s in [square_source(),BoundarySource("2",arc_curves((5,5),2),True)]]
    original=Hatch.set_pattern_fill
    def corrupt(hatch,*args,**kwargs):
        original(hatch,*args,**kwargs)
        hatch.dxf.pattern_scale=999
    monkeypatch.setattr(Hatch,"set_pattern_fill",corrupt)
    with pytest.raises(RuntimeError,match="读回不一致"):
        backend.draw_hatch(boundary_handles=handles,seed_point=(1,1),pattern="GRASS",scale=25,layer="FAILED",dry_run=False)
    assert len(backend.msp)==2
    assert "FAILED" not in backend.doc.layers


def test_dxf_composite_rejects_tilted_or_wrong_layout_sources(dxf_backend):
    backend=dxf_backend
    entity=backend.msp.add_lwpolyline(SQUARE,format="xyb",close=True)
    entity.dxf.extrusion=(0,1,1)
    with pytest.raises(ValueError,match="法向"):
        backend.draw_hatch(boundary_handles=[backend._handle_str(entity.dxf.handle)],seed_point=(1,1))
    entity=backend.doc.layout().add_circle((0,0),2)
    with pytest.raises(ValueError,match="模型空间"):
        backend.draw_hatch(boundary_handles=[backend._handle_str(entity.dxf.handle)],seed_point=(0,0))


class NativePolyline:
    EntityName="AcDbPolyline"
    Normal=(0,0,1)
    Elevation=0.0
    def __init__(self,state,handle,coordinates,closed=False,bulges=None):
        self.state,self.Handle,self.Coordinates=state,handle,list(coordinates)
        self.Closed=closed
        self.bulges=list(bulges or [0.0]*(len(coordinates)//2))
        self.deleted=False
    def GetBulge(self,i):
        return self.bulges[i]
    def SetBulge(self,i,value):
        self.bulges[i]=value
    def Delete(self):
        self.deleted=True
        self.state.events.append(("delete",self.Handle))


class NativeHatch:
    EntityName="AcDbHatch"
    AssociativeHatch=False
    Elevation=0.0
    def __init__(self,state,handle,pattern):
        self.state,self.Handle,self.PatternName=state,handle,pattern
        self.Layer=state.layer
        self.PatternScale=1
        self.PatternAngle=0
        self.loops=[]
        self.deleted=False
    def AppendOuterLoop(self,objects):
        self.state.events.append(("append_outer",self.Handle))
        self.loops.append(objects[0])
    def AppendInnerLoop(self,objects):
        self.state.events.append(("append_inner",self.Handle))
        self.loops.append(objects[0])
    @property
    def NumberOfLoops(self):
        return len(self.loops)
    @property
    def Area(self):
        areas=[abs(loop_area(polyline_curves([(e.Coordinates[i],e.Coordinates[i+1],e.bulges[i//2]) for i in range(0,len(e.Coordinates),2)],True))) for e in self.loops]
        return areas[0]-sum(areas[1:])
    def GetBoundingBox(self):
        p=self.loops[0]
        curves=polyline_curves([(p.Coordinates[i],p.Coordinates[i+1],p.bulges[i//2]) for i in range(0,len(p.Coordinates),2)],True)
        low,high=HatchRegion(curves,(),(),(1,1),0).bounds
        return (*low,0),(*high,0)
    def Evaluate(self):
        if self.state.corrupt:
            self.PatternScale=999
    def Delete(self):
        self.deleted=True
        self.state.events.append(("delete_hatch",self.Handle))


@pytest.fixture
def native_backend(monkeypatch,tmp_path):
    state=SimpleNamespace(events=[],entities={},next_handle=256,layer="FILL",corrupt=False)
    def register(entity):
        state.entities[str(int(entity.Handle,16))]=entity
        return entity
    square=NativePolyline(state,"1",[v for x,y,_ in SQUARE for v in (x,y)],True)
    register(square)
    circle=SimpleNamespace(EntityName="AcDbCircle",Normal=(0,0,1),Center=(5,5,0),Radius=2,Handle="2",deleted=False)
    register(circle)
    def new_handle():
        state.next_handle+=1
        return format(state.next_handle,"X")
    class ModelSpace:
        def AddLightWeightPolyline(self,coordinates):
            return register(NativePolyline(state,new_handle(),coordinates.value))
        def AddHatch(self,pattern_type,pattern,associative,kind):
            entity=register(NativeHatch(state,new_handle(),pattern))
            state.events.append(("add_hatch",entity.Handle))
            return entity
    backend=AcadBackend(tmp_path)
    backend.doc=SimpleNamespace(Regen=lambda mode:None)
    backend.msp=ModelSpace()
    monkeypatch.setattr(backend,"_ensure_connected",lambda:None)
    monkeypatch.setattr(backend,"_find",lambda h: state.entities.get(h) if h in state.entities and not state.entities[h].deleted else None)
    monkeypatch.setattr(backend,"_apply_layer",lambda layer:setattr(state,"layer",layer))
    monkeypatch.setattr(dwt_autocad,"_dispatch_array",lambda objects:list(objects))
    monkeypatch.setattr(acad_module,"_dispatch_array",lambda objects:list(objects))
    state.backend=backend
    return state


def test_native_composite_hole_readback_and_helper_cleanup(native_backend):
    state=native_backend
    result=state.backend.draw_hatch(boundary_handles=["1","2"],seed_point=(1,1),pattern="GRASS",scale=25,dry_run=False)
    assert result["verified"] is True
    assert result["hole_count"]==1
    assert result["readback"]["loop_count"]==2
    assert result["readback"]["hatch_style"]==0
    assert result["readback"]["area"]==pytest.approx(100-4*math.pi)
    live=[e for e in state.entities.values() if not e.deleted]
    assert len(live)==3
    assert not state.entities["1"].deleted and not state.entities["2"].deleted
    index=next(i for i,e in enumerate(state.events) if e[0]=="add_hatch")
    assert state.events[index+1][0]=="append_outer"
    assert state.events[index+2][0]=="append_inner"


def test_native_composite_failed_readback_removes_hatch_and_helpers(native_backend):
    state=native_backend
    state.corrupt=True
    with pytest.raises(RuntimeError,match="读回不一致"):
        state.backend.draw_hatch(boundary_handles=["1","2"],seed_point=(1,1),pattern="GRASS",scale=25,dry_run=False)
    assert set(h for h,e in state.entities.items() if not e.deleted)=={"1","2"}


def test_native_single_closed_self_crossing_polyline_is_rejected(native_backend):
    state=native_backend
    poly=state.entities["1"]
    poly.Coordinates=[0,0,3,2,0,2,2,0]
    poly.Area=1
    with pytest.raises(ValueError,match="自交"):
        state.backend.draw_hatch(boundary_handle="1",dry_run=True)
    assert not state.events


def test_public_mcp_composite_call_creates_a_hole(monkeypatch,dxf_backend):
    backend=dxf_backend
    handles=[add_dxf_source(backend,s) for s in [square_source(),BoundarySource("2",arc_curves((5,5),2),True)]]
    monkeypatch.setattr(server,"_backend",backend)
    response=json.loads(server.cad_draw_hatch(server.DrawHatchInput(boundary_handles=handles,seed_point=(1,1),pattern="GRASS",scale=25,dry_run=False)))
    assert response["ok"] is True
    assert response["verified"] is True
    assert response["hole_count"]==1


@pytest.mark.parametrize("composite", [True, False])
def test_hatch_member_maps_are_rebuilt_after_outer_loop_before_any_property_write(monkeypatch,native_backend,composite):
    """Reproduce AddHatch's stale dynamic map, rather than a permissive fake."""
    import pythoncom
    from win32com.client import build, dynamic

    state=native_backend
    state.entities["1"].Area=100.0
    original_add=state.backend.msp.AddHatch
    complete_map=object()
    pending={}

    class TypeInfo:
        def GetDocumentation(self,index):
            return ("IAcadHatch",None,None,None)
        def GetTypeAttr(self):
            attributes=[None]*12
            attributes[5]=pythoncom.TKIND_DISPATCH
            attributes[11]=0
            return tuple(attributes)

    class Ole:
        def __init__(self,target):
            self.target=target
        def GetTypeInfo(self):
            assert len(self.target.loops)==1, "metadata must be refreshed after AppendOuterLoop"
            state.events.append(("get_valid_typeinfo",self.target.Handle))
            return TypeInfo()

    class EarlyBinding:
        def __init__(self,target):
            object.__setattr__(self,"target",target)
            object.__setattr__(self,"_oleobj_",Ole(target))
        def AppendOuterLoop(self,objects):
            self.target.AppendOuterLoop(objects)
        def __getattr__(self,name):
            raise AttributeError("provisional AddHatch member map: "+name)
        def __setattr__(self,name,value):
            raise AttributeError("provisional AddHatch property cannot be set: "+name)

    def add(*args):
        target=original_add(*args)
        early=EarlyBinding(target)
        pending["ole"]=early._oleobj_
        return early

    def build_members(info,attributes,namespace,for_user):
        assert isinstance(info,TypeInfo)
        state.events.append(("eager_member_map",pending["ole"].target.Handle))
        return complete_map

    def dispatch(ole,members,name):
        assert ole is pending["ole"] and members is complete_map and name=="IAcadHatch"
        state.events.append(("rebound_hatch",ole.target.Handle))
        return ole.target

    monkeypatch.setattr(state.backend.msp,"AddHatch",add)
    monkeypatch.setattr(build,"DispatchItem",build_members)
    monkeypatch.setattr(dynamic,"CDispatch",dispatch)
    arguments={"boundary_handles":["1","2"],"seed_point":(1,1)} if composite else {"boundary_handle":"1"}
    result=state.backend.draw_hatch(**arguments,pattern="GRASS",scale=25,dry_run=False)
    assert result["verified"] is True
    index=next(i for i,event in enumerate(state.events) if event[0]=="add_hatch")
    assert [event[0] for event in state.events[index:index+5]]==[
        "add_hatch","append_outer","get_valid_typeinfo","eager_member_map","rebound_hatch",
    ]
    assert sum(not e.deleted for e in state.entities.values())==3


def test_composite_cleanup_error_keeps_original_failure(monkeypatch,native_backend):
    state=native_backend
    def evaluate(hatch):
        raise RuntimeError("original evaluation failure")
    def delete(hatch):
        raise RuntimeError("secondary cleanup failure")
    monkeypatch.setattr(NativeHatch,"Evaluate",evaluate)
    monkeypatch.setattr(NativeHatch,"Delete",delete)
    with pytest.raises(RuntimeError) as caught:
        state.backend.draw_hatch(boundary_handles=["1","2"],seed_point=(1,1),pattern="GRASS",scale=25,dry_run=False)
    assert "original evaluation failure" in str(caught.value)
    assert "secondary cleanup failure" in str(caught.value)
    assert str(caught.value.__cause__)=="original evaluation failure"
    assert not state.entities["1"].deleted and not state.entities["2"].deleted
