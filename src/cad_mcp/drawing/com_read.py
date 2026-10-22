"""Read-only ActiveX OUT-argument compatibility for generated/dynamic dispatch."""

from __future__ import annotations

import math

import pythoncom
import win32com.client


_ARGUMENT_COUNT_ERRORS = {0x8002000E, 0x8002000F}


def _missing_output_arguments(error, method):
    """Recognize CPython argument binding failures, including through COM proxies."""
    if f"{method}() missing 2 required positional arguments:" not in str(error):
        return False
    traceback = error.__traceback__
    while traceback is not None:
        # A method that was entered raised an operational error, even if its
        # message happens to resemble Python's missing-argument diagnostic.
        if traceback.tb_frame.f_code.co_name == method:
            return False
        traceback = traceback.tb_next
    return True


def out_pair(obj, method, *, leading=(), variant_type=None):
    """Read two OUT arguments; never replay operational/permission COM errors.

    Generated pywin32 wrappers return a pair. Late-bound wrappers may require
    explicit BYREF VARIANTs. An argument-count rejection has executed no write;
    only that signature mismatch (or Python's missing-argument TypeError) selects
    the second documented calling convention.
    """
    if variant_type is None:
        variant_type = pythoncom.VT_VARIANT
    function = getattr(obj,method)
    try:
        values = function(*leading)
    except (TypeError,pythoncom.com_error) as exc:
        if isinstance(exc, TypeError):
            if not _missing_output_arguments(exc, method):
                raise
        elif (exc.hresult & 0xFFFFFFFF) not in _ARGUMENT_COUNT_ERRORS:
            raise
        initial = None if variant_type == pythoncom.VT_VARIANT else 0.0
        first = win32com.client.VARIANT(pythoncom.VT_BYREF | variant_type,initial)
        second = win32com.client.VARIANT(pythoncom.VT_BYREF | variant_type,initial)
        function(*leading,first,second)
        values = first.value,second.value
    if values is None and method == "GetXData":
        return None,None
    if not isinstance(values,(tuple,list)) or len(values) != 2:
        raise RuntimeError(f"{method} 未返回完整的两个输出值")
    return values[0],values[1]


def bounding_box(entity):
    low,high = out_pair(entity,"GetBoundingBox")
    try:
        low,high = tuple(map(float,low)),tuple(map(float,high))
        if len(low) != 3 or len(high) != 3 or not all(math.isfinite(v) for v in (*low,*high)):
            raise ValueError("无效坐标")
        if any(a>b for a,b in zip(low,high)):
            raise ValueError("最小点超过最大点")
    except (TypeError,ValueError) as exc:
        raise RuntimeError("GetBoundingBox 未返回有效、完整的三维边界") from exc
    return low,high
