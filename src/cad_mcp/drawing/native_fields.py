"""Read native FIELD dictionaries independently of evaluated Attribute.TextString."""

import re
import tempfile
from pathlib import Path

from cad_mcp.drawing.layouts import lisp_string
from cad_mcp.core.runtime import UnsupportedOperationError


def field_reader(handle, receipt):
    if not re.fullmatch(r"[0-9a-fA-F]{1,40}", handle):
        raise ValueError("属性句柄无效")
    # entget/dictsearch only: never create an extension dictionary while reading.
    return (
        '((lambda (/ f e d root field data result p child visited) '
        f'(setq f (open {lisp_string(str(receipt))} "w")) '
        '(if f (progn '
        '(setq result (vl-catch-all-apply (function (lambda () '
        f'(setq e (handent "{handle.upper()}")) '
        '(setq d (entget e)) '
        '(if (/= (cdr (assoc 0 d)) "ATTRIB") (/ 1 0)) '
        '(setq root (cdr (assoc 360 d))) '
        '(if (null root) (/ 1 0)) '
        '(setq data (dictsearch root "ACAD_FIELD")) '
        '(setq field (cdr (assoc -1 data))) '
        '(if (null field) (/ 1 0)) '
        '(setq data (dictsearch field "TEXT")) '
        '(setq field (cdr (assoc -1 data))) '
        '(if (null field) (/ 1 0)) '
        '(setq child (list field) visited nil) '
        '(while child '
        '(if (or (member (car child) visited) (>= (length visited) 256)) (/ 1 0)) '
        '(setq visited (cons (car child) visited)) '
        '(setq data (entget (car child)) child (cdr child)) '
        '(if (/= (cdr (assoc 0 data)) "FIELD") '
        '(if (/= (cdr (assoc 0 data)) "ACAD_FIELD") (/ 1 0))) '
        '(foreach p data '
        '(cond ((member (car p) (list 2 3)) '
        '(write-line (strcat "CODE " (vl-princ-to-string (vl-string->list (cdr p)))) f)) '
        '((= (car p) 331) (write-line (strcat "REF " (cdr (assoc 5 (entget (cdr p))))) f)) '
        '((= (car p) 360) (setq child (append child (list (cdr p))))))) '
        '))) nil)) '
        '(write-line (if (vl-catch-all-error-p result) "ERROR" "DONE") f) '
        '(close f))) (princ)))\n')


def read_attribute_field(backend, attribute):
    previous = backend.acad.ActiveDocument
    identity = lambda doc: (str(doc.FullName).casefold(), str(doc.Name).casefold())
    changed = identity(previous) != identity(backend.doc)
    try:
        if changed:
            backend.doc.Activate()
        if identity(backend.acad.ActiveDocument) != identity(backend.doc):
            raise RuntimeError("无法激活绑定图纸回读原生字段")
        with tempfile.TemporaryDirectory(prefix="cad-mcp-field-", ignore_cleanup_errors=True) as directory:
            receipt = Path(directory) / "field.txt"
            script = Path(directory) / "read.lsp"
            script.write_text(field_reader(str(attribute.Handle), receipt), encoding="utf-8")
            backend._send_command_and_wait(
                f'(load {lisp_string(str(script))})\n',
                completion_check=lambda: receipt.is_file() and receipt.read_bytes().rstrip().endswith((b"DONE", b"ERROR")))
            lines = receipt.read_text(encoding="ascii").splitlines()
            if not lines or lines[-1] != "DONE" or any(not row.startswith(("CODE ", "REF ")) for row in lines[:-1]):
                raise UnsupportedOperationError("无法从原生 FIELD 字典证明属性关联，已停止并回滚")
            code = []
            for row in lines[:-1]:
                if row.startswith("CODE "):
                    numbers = row[5:]
                    if numbers == "nil":
                        continue
                    if re.fullmatch(r"\((?:\d+\s*)*\)", numbers) is None:
                        raise RuntimeError("原生属性字段代码回执无效")
                    code.append("".join(chr(int(n)) for n in numbers[1:-1].split()))
            return {
                "code": "".join(code),
                "references": [row[4:].upper() for row in lines[:-1] if row.startswith("REF ")],
            }
    finally:
        if changed:
            previous.Activate()
            if identity(backend.acad.ActiveDocument) != identity(previous):
                raise RuntimeError("字段回读后原活动文档恢复失败")
