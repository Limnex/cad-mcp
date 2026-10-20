"""Bounded block inspection and copy-on-write edits of selected instances."""

import math
import uuid

from ezdxf.math import Matrix44

from cad_mcp.core.runtime import UnsupportedOperationError


MAX_BLOCK_DEPTH = 8
MAX_BLOCK_ITEMS = 1000
_COM_KIND = {"AcDbText": "TEXT", "AcDbMText": "MTEXT", "AcDbBlockReference": "INSERT",
             "AcDbLine": "LINE", "AcDbCircle": "CIRCLE", "AcDbArc": "ARC",
             "AcDbPolyline": "LWPOLYLINE", "AcDbEllipse": "ELLIPSE", "AcDbPoint": "POINT",
             "AcDbAttributeDefinition": "ATTDEF"}
_COPYABLE = {"TEXT", "MTEXT", "LINE", "CIRCLE", "ARC", "LWPOLYLINE", "ELLIPSE", "POINT", "ATTDEF", "INSERT"}


def kind(access, entity):
    return entity.dxftype() if access.mode == "dxf" else _COM_KIND.get(str(entity.ObjectName), str(entity.ObjectName))


def block_name(access, reference):
    return str(reference.dxf.name if access.mode == "dxf" else reference.Name)


def definition(access, reference):
    name = block_name(access, reference)
    if access.mode == "dxf":
        return access.backend.doc.blocks.get(name)
    return access.backend.doc.Blocks.Item(name)


def members(access, block):
    if access.mode == "dxf":
        if len(block) > MAX_BLOCK_ITEMS:
            raise ValueError("块内容超过 1000 项，未继续读取或修改")
        return list(block)
    count = int(block.Count)
    if count > MAX_BLOCK_ITEMS:
        raise ValueError("块内容超过 1000 项，未继续读取或修改")
    return [block.Item(i) for i in range(count)]


def text_value(access, entity):
    if access.mode == "acad":
        return str(entity.TextString)
    return str(entity.text if entity.dxftype() == "MTEXT" else entity.dxf.text)


def check_text(access, entity, value):
    old = text_value(access, entity)
    if "%<" in old or "%<" in value:
        raise UnsupportedOperationError("不支持覆盖块内关联字段文字")
    if access.mode == "dxf" and entity.has_extension_dict:
        if "ACAD_FIELD" in entity.get_extension_dict():
            raise UnsupportedOperationError("不支持覆盖块内关联字段文字")
    if access.mode == "acad" and bool(entity.HasExtensionDictionary):
        dictionary = entity.GetExtensionDictionary()  # Existing dictionary only; no creation in preview.
        for i in range(int(dictionary.Count)):
            if str(dictionary.Item(i).Name).upper() == "ACAD_FIELD":
                raise UnsupportedOperationError("不支持覆盖块内关联字段文字")
    if kind(access, entity) in {"TEXT", "ATTRIB", "ATTDEF", "AcDbAttribute"} and any(c in value for c in "\r\n"):
        raise ValueError("单行块文字/属性不能包含换行")


def attributes(access):
    e = access.entity
    if access.mode == "dxf":
        result = list(e.attribs)
    else:
        result = list(e.GetAttributes()) if bool(e.HasAttributes) else []
    if len(result) > 256:
        raise ValueError("块属性超过 256 项")
    by_tag = {}
    for attr in result:
        tag = str(attr.dxf.tag if access.mode == "dxf" else attr.TagString)
        if tag.casefold() in {key.casefold() for key in by_tag}:
            raise UnsupportedOperationError("块含重复属性标签，不能安全定位")
        by_tag[tag] = attr
    return by_tag


def walk_texts(access):
    found, visited = {}, [0]

    def walk(ref, prefix, chain):
        name = block_name(access, ref)
        if len(chain) >= MAX_BLOCK_DEPTH or name.casefold() in chain:
            raise UnsupportedOperationError("块嵌套过深或存在循环引用")
        block = definition(access, ref)
        for i, item in enumerate(members(access, block)):
            visited[0] += 1
            if visited[0] > MAX_BLOCK_ITEMS:
                raise ValueError("块内容扫描超过 1000 项")
            path = f"{prefix}/{i}" if prefix else str(i)
            k = kind(access, item)
            if k in {"TEXT", "MTEXT"}:
                found[path] = item
            elif k == "INSERT":
                walk(item, path, (*chain, name.casefold()))

    walk(access.entity, "", ())
    return found


def inspect_block(access):
    attrs = {tag: text_value(access, item) for tag, item in attributes(access).items()}
    texts = {path: text_value(access, item) for path, item in walk_texts(access).items()}
    return attrs, texts


def _static_block(access, ref):
    name = block_name(access, ref)
    block = definition(access, ref)
    if access.mode == "dxf":
        if block.block.is_xref or block.block.is_xref_overlay or name.startswith("*"):
            raise UnsupportedOperationError("不编辑外部参照或匿名/动态块的几何和固定文字")
        if block.block_record.has_extension_dict:
            raise UnsupportedOperationError("块带有扩展数据，不能证明克隆可保留全部行为")
    elif bool(ref.IsDynamicBlock) or bool(block.IsXRef) or name.startswith("*"):
        raise UnsupportedOperationError("不编辑外部参照或匿名/动态块的几何和固定文字")
    elif bool(block.HasExtensionDictionary):
        raise UnsupportedOperationError("块定义含扩展字典，不能证明克隆可保留全部行为")
    return block


def preflight_block(access, patch, before):
    attrs = attributes(access)
    if "block_attributes" in patch:
        normalized = dict(before["block_attributes"])
        by_fold = {key.casefold(): key for key in attrs}
        seen = set()
        for tag, value in patch["block_attributes"].items():
            folded = tag.casefold()
            if folded in seen:
                raise ValueError("更新中出现重复属性标签")
            seen.add(folded)
            if folded not in by_fold:
                raise ValueError(f"块属性标签不存在: {tag}")
            tag = by_fold[folded]
            attr = attrs[tag]
            if access.mode == "dxf" and attr.is_const:
                raise UnsupportedOperationError(f"常量属性不可按实例修改: {tag}")
            access.resource(str(attr.dxf.layer if access.mode == "dxf" else attr.Layer), layer=True)
            check_text(access, attr, value)
            normalized[tag] = value
        patch["block_attributes"] = normalized

    transform = {"position", "rotation", "x_scale", "y_scale"} & set(patch)
    if transform:
        block = _static_block(access, access.entity)
        if any(before[name] <= 0 for name in ("x_scale", "y_scale")):
            raise UnsupportedOperationError("平面块编辑不处理已有镜像或零比例块")
        if access.mode == "dxf" and (access.entity.mcount > 1):
            raise UnsupportedOperationError("不处理阵列块引用的几何编辑")
        sz = float(access.entity.dxf.zscale if access.mode == "dxf" else access.entity.ZScaleFactor)
        if sz <= 0:
            raise UnsupportedOperationError("平面块编辑不处理 Z 比例为零或镜像的块")
        uniform = (bool(block.block_record.dxf.scale) if access.mode == "dxf"
                   else int(block.BlockScaling) == 1)
        if uniform and any(not math.isclose(patch.get(key, before[key]), sz) for key in ("x_scale", "y_scale")):
            raise UnsupportedOperationError("该块要求 XYZ 统一缩放；平面编辑保留 Z 比例，因此拒绝此缩放请求")
        if access.mode == "acad":
            access._attribute_targets = attribute_targets(access, patch, before)

    if "block_texts" in patch:
        texts = walk_texts(access)
        normalized = dict(before["block_texts"])
        for path, value in patch["block_texts"].items():
            if path not in texts:
                raise ValueError(f"块内文字路径不存在: {path}；请先读取属性")
            item = texts[path]
            access.resource(str(item.dxf.layer if access.mode == "dxf" else item.Layer), layer=True)
            check_text(access, item, value)
            normalized[path] = value
        patch["block_texts"] = normalized

        # Validate every ancestor that will be copied, before creating any block.
        def validate(ref, paths):
            access.resource(str(ref.dxf.layer if access.mode == "dxf" else ref.Layer), layer=True)
            block = _static_block(access, ref)
            items = members(access, block)
            for item in items:
                if kind(access, item) not in _COPYABLE:
                    raise UnsupportedOperationError("固定文字隔离编辑暂不克隆含填充、尺寸或未知对象的块")
                if access.mode == "dxf" and item.has_extension_dict:
                    raise UnsupportedOperationError("块内对象含扩展数据，不能安全克隆")
                if access.mode == "dxf" and item.get_reactors():
                    raise UnsupportedOperationError("块内对象存在关联依赖，不能安全克隆")
            for i, item in enumerate(items):
                sub = [path.split("/", 1)[1] for path in paths if path.startswith(f"{i}/")]
                if sub:
                    validate(item, sub)

        changed = [path for path, value in normalized.items() if before["block_texts"][path] != value]
        if changed:
            validate(access.entity, changed)


def _write_text(access, item, value):
    if access.mode == "acad":
        item.TextString = value
        item.Update()
    elif item.dxftype() == "MTEXT":
        item.text = value
    else:
        item.dxf.text = value
    if text_value(access, item) != value:
        raise RuntimeError("块内容写后回读不一致")


def clone_texts(access, updates):
    """Copy only modified definition paths; retarget the original reference in place."""
    doc = access.backend.doc

    def clone(ref, values):
        source = definition(access, ref)
        name = "CADMCP_EDIT_" + uuid.uuid4().hex[:16]
        items = members(access, source)
        if access.mode == "dxf":
            target = doc.blocks.new(name, base_point=source.block.dxf.base_point)
            target.block.dxf.flags = source.block.dxf.flags
            target.block_record.dxf.units = source.block_record.dxf.units
            target.block_record.dxf.explode = source.block_record.dxf.explode
            target.block_record.dxf.scale = source.block_record.dxf.scale
            for item in items:
                target.add_entity(item.copy())
        else:
            from cad_mcp.dwt.autocad import _dispatch_array
            from cad_mcp.drawing.autocad import _pt
            target = doc.Blocks.Add(_pt(*source.Origin), name)
            target.Units = source.Units
            target.Explodable = source.Explodable
            target.BlockScaling = source.BlockScaling
            if items:
                doc.CopyObjects(_dispatch_array(items), target)
        copied = members(access, target)
        if len(copied) != len(items) or [kind(access, e) for e in copied] != [kind(access, e) for e in items]:
            raise RuntimeError("块克隆的对象数量或顺序不一致")
        for i, item in enumerate(copied):
            key = str(i)
            if key in values:
                _write_text(access, item, values[key])
            sub = {path.split("/", 1)[1]: value for path, value in values.items() if path.startswith(key + "/")}
            if sub:
                new_child = clone(items[i], sub)
                if access.mode == "dxf":
                    item.dxf.name = new_child
                else:
                    item.Name = new_child
        return name

    name = clone(access.entity, updates)
    if access.mode == "dxf":
        access.entity.dxf.name = name
    else:
        access.entity.Name = name


def transform_block(access, patch, before):
    """Transform reference plus attached attributes without changing its Z scale."""
    from ezdxf.math import Vec3
    e = access.entity
    if access.mode == "dxf":
        old_matrix = e.matrix44()
        old_insert = e.dxf.insert
        e.dxf.insert = Vec3(*patch.get("position", before["position"]), old_insert.z)
        e.dxf.rotation = patch.get("rotation", before["rotation"])
        e.dxf.xscale = patch.get("x_scale", before["x_scale"])
        e.dxf.yscale = patch.get("y_scale", before["y_scale"])
        new_matrix = e.matrix44()
        old_matrix.inverse()
        transform = old_matrix @ new_matrix
        for attr in e.attribs:
            attr.transform(transform)
    else:
        from cad_mcp.drawing.autocad import _pt
        if "position" in patch:
            e.InsertionPoint = _pt(*patch["position"], float(e.InsertionPoint[2]))
        for name, native in (("rotation", "Rotation"), ("x_scale", "XScaleFactor"), ("y_scale", "YScaleFactor")):
            if name in patch:
                setattr(e, native, math.radians(patch[name]) if name == "rotation" else patch[name])
        # Native setters may or may not move attached attributes. Set their
        # precomputed absolute targets once, so neither case causes double moves.
        for attr, target, alignment in access._attribute_targets:
            for prop in ("Height", "ScaleFactor", "Rotation", "ObliqueAngle"):
                setattr(attr, prop, target[prop])
            if alignment in {0, 3, 5}:
                attr.InsertionPoint = _pt(*target["InsertionPoint"])
            if alignment != 0:
                attr.TextAlignmentPoint = _pt(*target["TextAlignmentPoint"])
            attr.Update()
            for prop in ("Height", "ScaleFactor", "Rotation", "ObliqueAngle"):
                if not math.isclose(float(getattr(attr, prop)), target[prop], rel_tol=1e-9, abs_tol=1e-8):
                    raise RuntimeError(f"块属性几何 {prop} 写后回读不一致")
            prop = "InsertionPoint" if alignment in {0, 3, 5} else "TextAlignmentPoint"
            if any(not math.isclose(float(a), b, rel_tol=1e-9, abs_tol=1e-8)
                   for a, b in zip(getattr(attr, prop), target[prop])):
                raise RuntimeError("块属性位置写后回读不一致")


def attribute_targets(access, patch, before):
    """Compute planar TEXT geometry offline before any native property setter."""
    from ezdxf.entities import Text
    e = access.entity
    z, sz = float(e.InsertionPoint[2]), float(e.ZScaleFactor)

    def matrix(pos, rotation, sx, sy):
        return Matrix44.chain(Matrix44.scale(sx, sy, sz), Matrix44.z_rotate(math.radians(rotation)),
                              Matrix44.translate(*pos, z))

    old = matrix(before["position"], before["rotation"], before["x_scale"], before["y_scale"])
    old.inverse()
    new = matrix(patch.get("position", before["position"]), patch.get("rotation", before["rotation"]),
                 patch.get("x_scale", before["x_scale"]), patch.get("y_scale", before["y_scale"]))
    transform = old @ new
    alignments = {0: (0, 0), 1: (1, 0), 2: (2, 0), 3: (3, 0), 4: (4, 0), 5: (5, 0),
                  6: (0, 3), 7: (1, 3), 8: (2, 3), 9: (0, 2), 10: (1, 2), 11: (2, 2),
                  12: (0, 1), 13: (1, 1), 14: (2, 1)}
    targets = []
    for attr in attributes(access).values():
        if bool(attr.MTextAttribute):
            raise UnsupportedOperationError("带多行属性的块暂不变换几何；仍可单独修改属性内容")
        if tuple(map(float, attr.Normal)) != (0.0, 0.0, 1.0):
            raise UnsupportedOperationError("块属性文字不在 XY 平面")
        access.resource(str(attr.Layer), layer=True)
        alignment = int(attr.Alignment)
        if alignment not in alignments:
            raise UnsupportedOperationError("块属性文字对齐方式无法安全变换")
        halign, valign = alignments[alignment]
        text = Text.new(dxfattribs={"text": str(attr.TextString), "insert": tuple(attr.InsertionPoint),
            "align_point": tuple(attr.TextAlignmentPoint), "height": float(attr.Height),
            "width": float(attr.ScaleFactor), "rotation": math.degrees(float(attr.Rotation)),
            "oblique": math.degrees(float(attr.ObliqueAngle)), "halign": halign, "valign": valign})
        text.transform(transform)
        targets.append((attr, {"InsertionPoint": tuple(text.dxf.insert),
            "TextAlignmentPoint": tuple(text.dxf.align_point), "Height": float(text.dxf.height),
            "ScaleFactor": float(text.dxf.width), "Rotation": math.radians(text.dxf.rotation) % math.tau,
            "ObliqueAngle": math.radians(text.dxf.oblique)}, alignment))
    return targets


def write_block(access, patch, before):
    if {"position", "rotation", "x_scale", "y_scale"} & set(patch):
        transform_block(access, patch, before)
    if "block_texts" in patch:
        updates = {key: value for key, value in patch["block_texts"].items() if value != before["block_texts"][key]}
        if updates:
            clone_texts(access, updates)
    if "block_attributes" in patch:
        attrs = attributes(access)
        for tag, value in patch["block_attributes"].items():
            if before["block_attributes"][tag] != value:
                _write_text(access, attrs[tag], value)
