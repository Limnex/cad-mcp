"""One native EXTEND reproduction with raw command feedback; never save DWGs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import traceback


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
if sys.stdout.encoding and "utf" not in sys.stdout.encoding.lower():
    sys.stdout.reconfigure(encoding="utf-8")


def equivalent_setting(name, left, right):
    if name == "LOGFILEPATH":
        return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))
    return left == right


def equivalent_settings(left, right):
    return left.keys() == right.keys() and all(equivalent_setting(name, left[name], right[name]) for name in left)


def main():
    if os.environ.get("CAD_MCP_RUN_ACAD_INTEGRATION") != "1":
        raise SystemExit("Requires explicit CAD_MCP_RUN_ACAD_INTEGRATION=1 and an already running AutoCAD.")
    manual = ROOT / ".cache" / "manual"
    manual.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="native-extend-diagnostic-", dir=manual))
    (output / "commandlogs").mkdir()
    os.environ["CAD_DRAWINGS_DIR"] = str(output / "drawings")
    os.environ["CAD_LOG_DIR"] = str(output / "logs")

    import pythoncom
    from win32com.client import VARIANT
    from cad_mcp.drawing.autocad import AcadBackend, _lisp_string, _pt
    from cad_mcp.drawing.native_extend import SETTINGS, _command, _snapshot, set_short_variable

    def documents(application):
        docs = application.Documents
        return sorted((str(docs.Item(i).Name), str(docs.Item(i).FullName), bool(docs.Item(i).Saved),
                       int(docs.Item(i).ModelSpace.Count)) for i in range(int(docs.Count)))

    def values(document):
        return {name: document.GetVariable(name) for name in [*SETTINGS, "LOGFILEMODE", "LOGFILEPATH"]}

    def set_aux(document, name, value):
        assert name in {"LOGFILEMODE", "LOGFILEPATH"}
        payload = VARIANT(pythoncom.VT_I2, int(value)) if name == "LOGFILEMODE" else str(value)
        document.SetVariable(name, payload)
        actual = document.GetVariable(name)
        assert equivalent_setting(name, actual, value), (name, value, actual)

    def restore(document, saved):
        # Stop and flush command logging before restoring its directory.
        if int(document.GetVariable("LOGFILEMODE")) != 0:
            set_aux(document, "LOGFILEMODE", 0)
        for name, value in saved.items():
            if name == "LOGFILEMODE":
                continue
            if not equivalent_setting(name, document.GetVariable(name), value):
                if name == "LOGFILEPATH":
                    set_aux(document, name, value)
                else:
                    set_short_variable(document, name, int(value))
        if int(saved["LOGFILEMODE"]) != 0:
            set_aux(document, "LOGFILEMODE", int(saved["LOGFILEMODE"]))
        assert equivalent_settings(values(document), saved)

    report = {"output": str(output), "native_dispatch_count": 0,
              "diagnostic_only_difference": "CMDECHO=1 plus read-only trace/logging",
              "parameters": {"target": [[0, 0, 0], [10, 0, 0]],
                             "boundary": [[20, -5, 0], [20, 5, 0]],
                             "endpoint": "end", "extend_edges": False, "ucs_rotation_degrees": 31}}
    report_path = output / "report.json"
    def save_report():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"diagnostic_output": str(output)}, ensure_ascii=False), flush=True)
    raw = AcadBackend(output / "drawings")
    application = original = None
    original_values = temporary_values = None
    original_ucs = None
    setup_undo = operation_undo = False
    dispatched = completed = unknown = False
    failed = False
    raw._acquire_autocad_lock()
    try:
        application = raw._connect()  # Existing instance only; never starts CAD.
        raw._wait_operation_ready()
        report["before_documents"] = documents(application)
        if report["before_documents"]:
            original = application.ActiveDocument
            original_values = values(original)
            original_ucs = {name: original.GetVariable(name) for name in ["UCSORG", "UCSXDIR", "UCSYDIR"]}
            report["before_settings"] = original_values
            report["original_active_document"] = str(original.Name)
        save_report()
        raw.new_drawing("native-extend-single-diagnostic.dwg")
        # Re-read the unique added document through Documents.Item rather than
        # relying on the dynamic Dispatch returned by Documents.Add.
        prior_names = {item[0] for item in report["before_documents"]}
        collection = application.Documents
        candidates = []
        for index in range(int(collection.Count)):
            candidate = collection.Item(index)
            if str(candidate.Name) not in prior_names:
                candidates.append(candidate)
        assert len(candidates) == 1, "Cannot uniquely identify the newly created diagnostic drawing"
        candidate = candidates[0]
        assert str(candidate.FullName) == "", "Diagnostic drawing unexpectedly has a saved path"
        assert int(candidate.ModelSpace.Count) == 0, "Diagnostic drawing unexpectedly contains model entities"
        raw._use_doc(candidate)
        raw.doc.Activate()
        report["temporary_document"] = str(raw.doc.Name)
        report["document_binding"] = "unique new Documents.Item; unsaved empty modelspace"
        temporary_values = values(raw.doc)
        setup_undo = raw._begin_undo()
        assert setup_undo, "Cannot create setup undo group"
        raw._send_command_and_wait('(command-s "_.UCS" "_Z" 31)\n')
        for name, value in {"OSMODE": 167, "TRIMEXTENDMODE": 1, "EDGEMODE": 1, "PROJMODE": 1}.items():
            set_short_variable(raw.doc, name, value)
        subject = raw.msp.AddLine(_pt(0, 0), _pt(10, 0))
        boundary = raw.msp.AddLine(_pt(20, -5), _pt(20, 5))
        raw._commit_undo(setup_undo)
        setup_undo = False
        assert int(raw.msp.Count) == 2
        report["before_target"], report["before_boundary"] = _snapshot(subject), _snapshot(boundary)
        report["zoom_to_test_entities"] = os.environ.get("CAD_MCP_DIAGNOSTIC_ZOOM") == "1"
        if report["zoom_to_test_entities"]:
            application.ZoomExtents()
            raw._wait_operation_ready()
        report["view_and_ucs"] = {name: raw.doc.GetVariable(name) for name in
                                  ["UCSORG", "UCSXDIR", "UCSYDIR", "VIEWDIR", "VIEWCTR", "VIEWSIZE", "SCREENSIZE"]}

        set_aux(raw.doc, "LOGFILEMODE", 0)
        set_aux(raw.doc, "LOGFILEPATH", str(output / "commandlogs") + os.sep)
        set_aux(raw.doc, "LOGFILEMODE", 1)
        report["command_log"] = str(raw.doc.GetVariable("LOGFILENAME"))
        receipt, trace_path = output / "complete.txt", output / "selection-trace.txt"
        command = _command(report["before_target"]["handle"], [report["before_boundary"]["handle"]],
                           "end", False, receipt)
        assert command.count('(setvar "CMDECHO" 0)') == 1
        command = command.replace('(setvar "CMDECHO" 0)', '(setvar "CMDECHO" 1)')
        # Keep every complete input expression short. The former 2198-character
        # inline diagnostic entered a continuation prompt before any trace ran.
        helper_name = "cadmcp_trace_" + output.name.rsplit("-", 1)[-1]
        helper_ready = output / "trace-helper-ready.txt"
        variable_names = " ".join(f'"{name}"' for name in SETTINGS)
        helper = (
            "((lambda (/ ready_stream) "
            f"(defun {helper_name} (phase entity boundaryset pickpoint a z filename / fields stream) "
            '(setq fields (list (cons "phase" phase) (cons "target" (cdr (assoc 5 (entget entity)))) '
            '(cons "boundary" (cdr (assoc 5 (entget (ssname boundaryset 0))))) '
            '(cons "boundary_count" (sslength boundaryset)) '
            '(cons "start_parameter" a) (cons "end_parameter" z) '
            '(cons "pick_ucs" pickpoint) (cons "pick_wcs" (trans pickpoint 1 0)) '
            '(cons "last_prompt" (getvar "LASTPROMPT")) (cons "errno" (getvar "ERRNO")) '
            '(cons "target_data" (entget entity)) '
            f'(cons "settings" (mapcar \'(lambda (n) (list n (getvar n))) \'({variable_names}))))) '
            '(setq stream (open filename (if (= phase "before") "w" "a"))) '
            '(write-line (vl-prin1-to-string fields) stream) (close stream) (princ)) '
            f'(setq ready_stream (open {_lisp_string(str(helper_ready))} "w")) '
            '(write-line "OK" ready_stream) (close ready_stream) (princ)))\n')
        native_call = '(command-s "_.EXTEND" ss "" (list e p) "")'
        assert command.count(native_call) == 1
        command = command.replace(native_call,
            f'({helper_name} "before" e ss p a z {_lisp_string(str(trace_path))}) ' + native_call +
            f' ({helper_name} "after" e ss p a z {_lisp_string(str(trace_path))})')
        command = command.replace('(close f) (princ)))', f'(close f) (setq {helper_name} nil) (princ)))')
        def validate_expression(expression):
            depth, quoted, escaped = 0, False, False
            for char in expression:
                if quoted:
                    if escaped: escaped = False
                    elif char == "\\": escaped = True
                    elif char == '"': quoted = False
                elif char == '"': quoted = True
                elif char == '(': depth += 1
                elif char == ')':
                    depth -= 1
                    assert depth >= 0
            assert depth == 0 and not quoted
            assert expression.endswith("\n")
        validate_expression(helper)
        validate_expression(command)
        report["input_lengths"] = {"helper": len(helper), "native_operation": len(command)}
        (output / "trace-helper.lsp").write_text(helper, encoding="utf-8")
        helper_file = output / "trace-helper.lsp"
        helper_loader = f'(load {_lisp_string(str(helper_file))})\n'
        assert len(helper_loader) < 1800
        raw._send_command_and_wait(helper_loader, completion_check=helper_ready.is_file)
        (output / "native-command.lsp").write_text(command, encoding="utf-8")
        save_report()
        operation_undo = raw._begin_undo()
        assert operation_undo, "Cannot create native operation undo group"
        dispatched = True
        report["native_dispatch_count"] = 1
        native_loader = f'(load {_lisp_string(str(output / "native-command.lsp"))})\n'
        assert len(native_loader) < 1800
        raw._send_command_and_wait(native_loader, completion_check=receipt.is_file)
        completed = True
        report["receipt"] = receipt.read_text(encoding="ascii").strip()
        report["after_target"], report["after_boundary"] = _snapshot(subject), _snapshot(boundary)
        report["geometry_changed"] = report["before_target"] != report["after_target"]
        report["boundary_unchanged"] = report["before_boundary"] == report["after_boundary"]
        raw._commit_undo(operation_undo)
        operation_undo = False
        set_aux(raw.doc, "LOGFILEMODE", 0)
        report["command_log_exists"] = Path(report["command_log"]).is_file()
        report["selection_trace_exists"] = trace_path.is_file()
        assert report["receipt"] == "OK", "Diagnostic native expression reported an error"
        assert report["selection_trace_exists"] and report["command_log_exists"], "Missing diagnostic evidence"
    except Exception as exc:
        failed = True
        unknown = dispatched and not completed
        report["error"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
    finally:
        try:
            if unknown:
                report["temporary_drawing_left_open"] = True
            else:
                if raw.doc is not None and raw._own_document:
                    raw._wait_operation_ready()
                    raw._commit_undo(setup_undo or operation_undo)
                    if temporary_values is not None:
                        restore(raw.doc, temporary_values)
                    raw.close_document(save=False)
                    report["temporary_closed_without_save"] = True
                if original is not None:
                    original.Activate()
                    restore(original, original_values)
                    assert {name: original.GetVariable(name) for name in original_ucs} == original_ucs
                    report["original_settings_and_ucs_restored"] = True
                if application is not None:
                    report["after_documents"] = documents(application)
                    assert report["after_documents"] == report["before_documents"]
                    report["original_documents_unchanged"] = True
        except Exception as exc:
            failed = True
            report["cleanup_error"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
        finally:
            raw._release_autocad_lock()
            report["execution_unknown"] = unknown
            save_report()
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 1 if failed or unknown else 0


if __name__ == "__main__":
    raise SystemExit(main())
