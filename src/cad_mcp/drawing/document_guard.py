"""Preflight operations that AutoCAD can use to replace an untouched startup tab."""

from cad_mcp.core.runtime import ConflictError


def assert_safe_document_open(application):
    documents = application.Documents
    for index in range(int(documents.Count)):
        document = documents.Item(index)
        # FullName is just Name until the drawing has a file path. Do not dirty,
        # save, close or recreate a startup drawing to defeat AutoCAD's behavior.
        full_name, name = str(document.FullName), str(document.Name)
        if full_name.casefold() in ("", name.casefold()) and bool(document.Saved):
            if int(document.GetVariable("DBMOD")) == 0:
                raise ConflictError(
                    f"原有未保存路径的空白图纸 {name} 可能被 AutoCAD 自动关闭，已停止打开或新建图纸",
                    details={"protected_document": name, "action": "请先手动为该图纸另存为新文件，再执行本操作"})
