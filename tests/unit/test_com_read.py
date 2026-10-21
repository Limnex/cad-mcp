"""Realistic OUT-argument signatures used by late-bound AutoCAD entities."""


import pytest
import pythoncom

from cad_mcp.drawing.com_read import bounding_box,out_pair


def test_generated_bounding_box_return_signature():
    class Entity:
        def GetBoundingBox(self):
            return (1,2,3),(4,5,6)
    assert bounding_box(Entity()) == ((1.,2.,3.),(4.,5.,6.))


def test_dynamic_bounding_box_requires_two_byref_outputs():
    class Entity:
        def GetBoundingBox(self,minimum,maximum):
            minimum.value=(1.,2.,3.)
            maximum.value=(4.,5.,6.)
    assert bounding_box(Entity()) == ((1.,2.,3.),(4.,5.,6.))


def test_com_argument_count_error_selects_documented_out_signature():
    class Entity:
        calls=[]
        def GetBoundingBox(self,*args):
            self.calls.append(len(args))
            if not args:
                raise pythoncom.com_error(-2147352562,"bad parameter count",None,None)
            args[0].value=(0.,0.,0.)
            args[1].value=(594.,420.,0.)
    entity=Entity()
    assert bounding_box(entity)[1] == (594.,420.,0.)
    assert entity.calls == [0,2]


def test_operational_com_error_is_not_replayed_as_signature_fallback():
    class Entity:
        calls=0
        def GetBoundingBox(self,*args):
            self.calls+=1
            raise pythoncom.com_error(-2147024891,"access denied",None,None)
    entity=Entity()
    with pytest.raises(pythoncom.com_error):
        bounding_box(entity)
    assert entity.calls == 1


@pytest.mark.parametrize("bounds",[
    None, ((0,0),(1,1)), ((0,0,0),(1,1,float('nan'))), ((2,0,0),(1,1,0)),
    ((0,0,0),None), ((0,0,0),(1,1,float('inf')))])
def test_incomplete_or_invalid_bounds_are_not_reported_as_success(bounds):
    class Entity:
        def GetBoundingBox(self):
            return bounds
    with pytest.raises(RuntimeError):
        bounding_box(Entity())


def test_xdata_out_arguments_preserve_leading_application_name():
    class Entity:
        def GetXData(self,application,codes,values):
            assert application == "CAD_MCP_LAYOUT_V1"
            codes.value=(1001,1000)
            values.value=(application,"metadata")
    assert out_pair(Entity(),"GetXData",leading=("CAD_MCP_LAYOUT_V1",)) == (
        (1001,1000),("CAD_MCP_LAYOUT_V1","metadata"))
