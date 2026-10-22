"""Repair the narrowly identified pdfplot17 XMP stream serialization defect.

An incremental replacement keeps all page, font, outline and geometry bytes
intact. This is not a general PDF repairer; unfamiliar dictionaries fail closed.
"""

from pathlib import Path
import re
from xml.etree import ElementTree


_BROKEN_METADATA = re.compile(
    rb"(?m)^(\d+) (\d+) obj\s*(<<\s*/Subtype\s*/XML\s*/Type\s*/Metadata\s*>>)stream"
    rb"\r?\n(.*?)\r?\nendstream\s+endobj", re.DOTALL)


def repair_autocad_metadata(path: Path) -> bool:
    """Replace only the exact missing-Length/missing-EOL stream seen in AutoCAD."""
    data = path.read_bytes()
    matches = list(_BROKEN_METADATA.finditer(data))
    if not matches:
        return False
    if len(matches) != 1 or b"pdfplot" not in data or b"/Encrypt" in data or b"/ByteRange" in data:
        raise RuntimeError("PDF 元数据异常不符合已验证的驱动格式，未发布文件")
    tail = re.search(rb"startxref\s+(\d+)\s+%%EOF\s*\Z", data)
    if tail is None:
        raise RuntimeError("PDF 尾部不完整，未发布文件")
    previous = int(tail.group(1))
    xref = data[previous:tail.start()]
    trailer = re.fullmatch(rb"xref\s+(.*?)trailer\s*(<<.*?>>)\s*", xref, re.DOTALL)
    if trailer is None or b"/Prev" in trailer.group(2) or b"/XRefStm" in trailer.group(2):
        raise RuntimeError("PDF 交叉引用格式不符合已验证的驱动格式，未发布文件")
    match = matches[0]
    number, generation = int(match.group(1)), int(match.group(2))
    entries = trailer.group(1).splitlines()
    index, found = 0, False
    while index < len(entries):
        header = entries[index].split()
        if len(header) != 2:
            raise RuntimeError("PDF 交叉引用表不完整")
        start, count = map(int, header)
        rows = entries[index + 1:index + 1 + count]
        if len(rows) != count:
            raise RuntimeError("PDF 交叉引用表不完整")
        if start <= number < start + count:
            entry = rows[number - start].split()
            found = entry == [f"{match.start():010d}".encode(), f"{generation:05d}".encode(), b"n"]
        index += count + 1
    if not found or re.search(rb"/Root\s+\d+\s+\d+\s+R\b", trailer.group(2)) is None:
        raise RuntimeError("PDF 元数据对象归属无法确认，未发布文件")
    payload = match.group(4) + b"\n"
    ElementTree.fromstring(payload)
    offset = len(data) + 1
    replacement = (f"{number} {generation} obj\n<< /Subtype /XML /Type /Metadata /Length {len(payload)} >>\nstream\n".encode()
                   + payload + b"endstream\nendobj\n")
    xref_offset = offset + len(replacement)
    updated_trailer = trailer.group(2)[:-2] + f" /Prev {previous} >>".encode()
    suffix = (f"xref\n{number} 1\n{offset:010d} {generation:05d} n \ntrailer\n".encode()
              + updated_trailer + f"\nstartxref\n{xref_offset}\n%%EOF\n".encode())
    path.write_bytes(data + b"\n" + replacement + suffix)
    return True
