from __future__ import annotations

import gc
import logging
import os
import time
from pathlib import Path
from typing import Any

import pythoncom
import win32com.client
from win32com.client import VARIANT

from cad_agent.runtime_config import output_root as public_output_root


LOGGER = logging.getLogger(__name__)

SW_DOC_PART = 1
SW_DOC_ASSEMBLY = 2
SW_DOC_DRAWING = 3
SW_OPEN_DOC_OPTIONS_SILENT = 1
SW_SAVE_AS_CURRENT_VERSION = 0
SW_SAVE_AS_OPTIONS_SILENT = 1
SW_DEFAULT_TEMPLATE_DRAWING = 3
ZOOM_TO_FIT_COMMAND_ID = None
SW_IMPORT_MODEL_ITEMS_FROM_ENTIRE_MODEL = 1
SW_INSERT_DIMENSIONS_MARKED_FOR_DRAWING = 1
SW_INSERT_DIMENSIONS_NOT_MARKED_FOR_DRAWING = 2


DOC_TYPE_NAMES = {
    SW_DOC_PART: "Part",
    SW_DOC_ASSEMBLY: "Assembly",
    SW_DOC_DRAWING: "Drawing",
}


class SWConnector:
    def __init__(self) -> None:
        self.app: Any | None = None
        self._com_initialized = False

    def connect(self, log_failure: bool = True) -> Any | None:
        if self.app is not None:
            return self.app
        try:
            if not self._com_initialized:
                pythoncom.CoInitialize()
                self._com_initialized = True
            self.app = win32com.client.GetActiveObject("SldWorks.Application")
            LOGGER.info("Connected to running SolidWorks instance.")
            return self.app
        except Exception:
            if log_failure:
                LOGGER.exception("Failed to connect to running SolidWorks.")
            else:
                LOGGER.debug("No responsive SolidWorks instance is available for the background status probe.")
            self.disconnect()
            return None

    def disconnect(self) -> None:
        self.app = None
        gc.collect()
        try:
            free_unused = getattr(pythoncom, "CoFreeUnusedLibraries", None)
            if callable(free_unused):
                free_unused()
        finally:
            if self._com_initialized:
                pythoncom.CoUninitialize()
                self._com_initialized = False

    def get_active_doc(self) -> Any | None:
        try:
            if self.app is None and self.connect() is None:
                return None
            return self.app.ActiveDoc
        except Exception:
            LOGGER.exception("Failed to get active SolidWorks document.")
            return None

    def get_document_info(self) -> dict[str, str] | None:
        try:
            doc = self.get_active_doc()
            if doc is None:
                return None

            full_path = self._read_com_value(doc.GetPathName)
            title = self._read_com_value(doc.GetTitle)
            type_id = self._read_int(doc.GetType)
            config_names = self._read_config_names(doc)
            configuration = config_names[0] if config_names else "N/A"

            return {
                "file_name": Path(full_path).name if full_path != "N/A" else title,
                "document_type": DOC_TYPE_NAMES.get(type_id, "N/A"),
                "full_path": full_path,
                "configuration": str(configuration),
            }
        except Exception:
            LOGGER.exception("Failed to read document information.")
            return None

    def get_mass_properties(self) -> dict[str, Any] | None:
        try:
            doc = self.get_active_doc()
            if doc is None:
                return None

            raw = doc.GetMassProperties
            if raw is None:
                return None
            raw_tuple = tuple(raw)

            return {
                "raw": raw_tuple,
                "mass_raw": self._tuple_value(raw_tuple, 0),
                "volume_raw": self._tuple_value(raw_tuple, 1),
                "center_of_mass": [
                    self._tuple_value(raw_tuple, 2),
                    self._tuple_value(raw_tuple, 3),
                    self._tuple_value(raw_tuple, 4),
                ],
            }
        except Exception:
            LOGGER.exception("Failed to read mass properties.")
            return None

    def open_model(self, path: str | Path) -> dict[str, Any]:
        try:
            model_path = Path(path)
            if not model_path.exists():
                return {"success": False, "message": "文件不存在"}

            if self.app is None and self.connect() is None:
                return {"success": False, "message": "模型打开失败"}

            doc_type = self._document_type(model_path)
            model, errors, warnings = self._open_doc6_compat(model_path, doc_type)
            if model is None:
                LOGGER.error(
                    "OpenDoc6 returned no model. errors=%s warnings=%s",
                    errors,
                    warnings,
                )
                return {"success": False, "message": "模型打开失败"}

            title = self._read_com_value(model.GetTitle)
            activated_model, activate_errors = self._activate_doc3_compat(title, doc_type)
            print("Open Model ActivateDoc3:", activated_model)
            LOGGER.info("Opened model with open_model: %s", model_path)
            return {
                "success": True,
                "message": "模型打开成功",
                "path": str(model_path),
            }
        except Exception:
            LOGGER.exception("Failed to open model.")
            return {"success": False, "message": "模型打开失败"}

    def close_model(self) -> dict[str, Any]:
        try:
            if self.app is None and self.connect() is None:
                return {"success": False, "message": "没有打开任何模型"}

            doc = self.get_active_doc()
            if doc is None:
                return {"success": False, "message": "没有打开任何模型"}

            title = self._read_com_value(doc.GetTitle)
            self.app.CloseDoc(title)
            LOGGER.info("Closed model: %s", title)
            return {
                "success": True,
                "message": "模型关闭成功",
                "title": title,
            }
        except Exception:
            LOGGER.exception("Failed to close model.")
            return {"success": False, "message": "模型关闭失败"}

    def create_drawing(self) -> dict[str, Any]:
        return self.create_drawing_with_standard_views()

    def create_drawing_with_standard_views(self, auto_annotate: bool = True) -> dict[str, Any]:
        try:
            import traceback

            if self.app is None and self.connect() is None:
                return {"success": False, "message": "无法连接SolidWorks"}

            doc = self.get_active_doc()
            print("Step1 ActiveDoc:", doc)
            if doc is None:
                return {"success": False, "message": "没有打开任何模型"}

            title = self._read_com_value(doc.GetTitle)
            path = self._read_com_value(doc.GetPathName)
            doc_type = doc.GetType
            print("ActiveDoc title:", title)
            print("ActiveDoc path:", path)
            print("ActiveDoc type:", doc_type)
            print("Step2 DocType:", doc_type)

            if doc_type == SW_DOC_DRAWING:
                return {
                    "success": False,
                    "message": "当前激活的是工程图，请先打开或激活零件/装配体",
                }
            if doc_type not in (SW_DOC_PART, SW_DOC_ASSEMBLY):
                return {"success": False, "message": "当前活动文档不是零件或装配体"}
            if path in ("", "N/A"):
                return {
                    "success": False,
                    "message": "当前模型未保存，请先保存零件。",
                }

            print("Step3 Create Drawing...")
            template_path = self._get_default_drawing_template()
            print("Step4 Template:", template_path)
            if template_path is None:
                return {"success": False, "message": "无法获取有效Drawing模板"}

            try:
                drawing = self.app.NewDocument(template_path, 0, 0.0, 0.0)
            except Exception as exc:
                traceback.print_exc()
                return {
                    "success": False,
                    "message": f"NewDocument调用失败: {exc}",
                }
            print("Step5 Result:", drawing)
            if drawing is None:
                return {
                    "success": False,
                    "message": f"SolidWorks使用模板返回空工程图对象: {template_path}",
                }

            sheet_width, sheet_height, sheet_name = self._get_sheet_size(drawing)
            print("当前工程图名称:", self._read_com_value(drawing.GetTitle))
            print("当前 sheet 宽度:", sheet_width)
            print("当前 sheet 高度:", sheet_height)
            print("当前 sheet 名称:", sheet_name)

            views = self._insert_standard_views(drawing, path, sheet_width, sheet_height)
            view_results = {name: view is not None for name, view in views.items()}
            failed_views = [name for name, result in view_results.items() if not result]
            if failed_views:
                return {
                    "success": False,
                    "message": "工程图已创建，但部分视图插入失败: "
                    + ", ".join(failed_views),
                    "views": view_results,
                }
            layout_result = self._auto_layout_standard_views(
                drawing,
                views,
                sheet_width,
                sheet_height,
                sheet_name,
                self._get_model_bounding_box(doc),
            )
            self._active_model_box = layout_result.get("model_box")
            annotation_result = (
                self.auto_annotate_drawing(drawing)
                if auto_annotate
                else {"success": True, "message": "Automatic annotation skipped by caller.", "count": 0}
            )
            self._rebuild_and_zoom(drawing)

            LOGGER.info("Created drawing and inserted standard views.")
            return {
                "success": True,
                "message": "工程图创建成功",
                "views": view_results,
                "layout": layout_result,
                "annotations": annotation_result,
            }
        except Exception as exc:
            import traceback

            traceback.print_exc()
            LOGGER.exception("Failed to create drawing.")
            return {"success": False, "message": f"创建工程图失败: {exc}"}

    def open_document(self, path: Path) -> Any:
        if self.app is None:
            raise RuntimeError("SolidWorks is not connected.")

        doc_type = self._document_type(path)
        try:
            model, errors, warnings = self._open_doc6_compat(path, doc_type)
            if model is None:
                raise RuntimeError(
                    f"OpenDoc6 returned no document. errors={errors}, "
                    f"warnings={warnings}"
                )
            LOGGER.info(
                "Opened model: %s (errors=%s, warnings=%s)",
                path,
                errors,
                warnings,
            )
            return model
        except Exception as exc:
            LOGGER.exception("Failed to open model: %s", path)
            raise RuntimeError(f"Failed to open model: {path}") from exc

    def new_drawing(self, template_path: Path) -> Any:
        if self.app is None:
            raise RuntimeError("SolidWorks is not connected.")

        try:
            drawing = self.app.NewDocument(str(template_path), 0, 0.0, 0.0)
            if drawing is None:
                raise RuntimeError("NewDocument returned no drawing document.")
            LOGGER.info("Created drawing from template: %s", template_path)
            return drawing
        except Exception as exc:
            LOGGER.exception("Failed to create drawing from template: %s", template_path)
            raise RuntimeError(
                f"Failed to create drawing from template: {template_path}"
            ) from exc

    def export_pdf(self, drawing: Any, output_path: Path) -> None:
        errors = self._byref_i4()
        warnings = self._byref_i4()

        try:
            extension = drawing.Extension
            ok = extension.SaveAs(
                str(output_path),
                SW_SAVE_AS_CURRENT_VERSION,
                SW_SAVE_AS_OPTIONS_SILENT,
                None,
                errors,
                warnings,
            )
            if not ok:
                raise RuntimeError(
                    f"SaveAs returned false. errors={errors.value}, "
                    f"warnings={warnings.value}"
                )
            LOGGER.info(
                "Exported PDF: %s (errors=%s, warnings=%s)",
                output_path,
                errors.value,
                warnings.value,
            )
        except Exception as exc:
            LOGGER.exception("Failed to export PDF: %s", output_path)
            raise RuntimeError(f"Failed to export PDF: {output_path}") from exc

    def export_current_pdf(self) -> dict[str, Any]:
        return self._export_current_drawing("pdf")

    def export_current_dwg(self) -> dict[str, Any]:
        return self._export_current_drawing("dwg")

    def _export_current_drawing(self, extension_name: str) -> dict[str, Any]:
        try:
            if self.app is None and self.connect() is None:
                return {"success": False, "message": "无法连接SolidWorks"}
            drawing = self._get_current_drawing()
            if drawing is None:
                return {"success": False, "message": "没有打开任何工程图"}
            if self._read_raw_com_value(drawing.GetType) != SW_DOC_DRAWING:
                return {"success": False, "message": "当前文档不是工程图"}

            output_path = self._default_export_path(drawing, extension_name)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            errors = self._byref_i4()
            warnings = self._byref_i4()
            ok = self._save_drawing_as(drawing, output_path, errors, warnings)
            if not ok:
                return {
                    "success": False,
                    "message": f"{extension_name.upper()} 导出失败: errors={errors.value}, warnings={warnings.value}",
                }
            message = f"✓ {extension_name.upper()} 导出成功：{output_path}"
            self._safe_print(message)
            return {"success": True, "message": message, "path": str(output_path)}
        except Exception as exc:
            import traceback

            traceback.print_exc()
            return {
                "success": False,
                "message": f"{extension_name.upper()} 导出失败: {exc}",
            }

    def auto_layout_current_drawing(self) -> dict[str, Any]:
        try:
            if self.app is None and self.connect() is None:
                return {"success": False, "message": "无法连接SolidWorks"}
            drawing = self._get_current_drawing()
            if drawing is None:
                return {"success": False, "message": "没有打开任何工程图"}
            if self._read_raw_com_value(drawing.GetType) != SW_DOC_DRAWING:
                return {"success": False, "message": "当前文档不是工程图"}

            views = self._get_drawing_views(drawing)
            if not views:
                return {"success": False, "message": "当前工程图没有可布局视图"}
            sheet_width, sheet_height, sheet_name = self._get_sheet_size(drawing)
            positions = self._simple_grid_positions(len(views), sheet_width, sheet_height)
            for view, position in zip(views, positions):
                self._set_view_position(view, position)
                print("Layout view:", self._get_view_name(view), position)
            self._rebuild_and_zoom(drawing)
            self._safe_print("✓ 自动布局完成")
            return {
                "success": True,
                "message": "✓ 自动布局完成",
                "sheet": sheet_name,
            }
        except Exception as exc:
            import traceback

            traceback.print_exc()
            return {"success": False, "message": f"自动布局失败: {exc}"}

    def _default_export_path(self, drawing: Any, extension_name: str) -> Path:
        stem = self._drawing_source_stem(drawing)
        safe_stem = "".join(ch if ch not in '\\/:*?"<>|' else "_" for ch in stem)
        return public_output_root() / f"{safe_stem}_工程图.{extension_name}"

    def _get_current_drawing(self) -> Any | None:
        doc = self.get_active_doc()
        if doc is not None and self._read_raw_com_value(doc.GetType) == SW_DOC_DRAWING:
            return doc
        try:
            documents = self._read_raw_com_value(self.app.GetDocuments)
            if documents:
                for item in documents:
                    if self._read_raw_com_value(item.GetType) == SW_DOC_DRAWING:
                        try:
                            title = self._read_com_value(item.GetTitle)
                            self.app.ActivateDoc3(title, False, 0, 0)
                        except Exception:
                            pass
                        return item
        except Exception as exc:
            print("GetDocuments fallback failed:", exc)
        try:
            doc = self._read_raw_com_value(self.app.GetFirstDocument)
            while doc:
                if self._read_raw_com_value(doc.GetType) == SW_DOC_DRAWING:
                    return doc
                doc = self._read_raw_com_value(doc.GetNext)
        except Exception as exc:
            print("GetFirstDocument fallback failed:", exc)
        try:
            count = self._read_raw_com_value(self.app.GetDocumentCount)
            print("SolidWorks open document count:", count)
        except Exception:
            pass
        return None

    def _save_drawing_as(
        self,
        drawing: Any,
        output_path: Path,
        errors: Any,
        warnings: Any,
    ) -> bool:
        extension = drawing.Extension
        try:
            export_data = self.app.GetExportFileData(1)
            ok = extension.SaveAs(
                str(output_path),
                SW_SAVE_AS_CURRENT_VERSION,
                SW_SAVE_AS_OPTIONS_SILENT,
                export_data,
                errors,
                warnings,
            )
            print("Extension SaveAs with export data result:", ok)
            if ok:
                return True
        except Exception as exc:
            print("Extension SaveAs with export data failed:", exc)

        try:
            ok = drawing.SaveAs(str(output_path))
            print("ModelDoc SaveAs export result:", ok)
            if ok:
                return True
        except Exception as exc:
            print("ModelDoc SaveAs export failed:", exc)

        try:
            ok = drawing.SaveAs3(
                str(output_path),
                SW_SAVE_AS_CURRENT_VERSION,
                SW_SAVE_AS_OPTIONS_SILENT,
            )
            print("ModelDoc SaveAs3 export result:", ok)
            if ok:
                return True
        except Exception as exc:
            print("ModelDoc SaveAs3 export failed:", exc)

        for method_name, args in (
            (
                "SaveAs3",
                (
                    str(output_path),
                    SW_SAVE_AS_CURRENT_VERSION,
                    SW_SAVE_AS_OPTIONS_SILENT,
                    None,
                    errors,
                    warnings,
                ),
            ),
            (
                "SaveAs",
                (
                    str(output_path),
                    SW_SAVE_AS_CURRENT_VERSION,
                    SW_SAVE_AS_OPTIONS_SILENT,
                    None,
                    errors,
                    warnings,
                ),
            ),
        ):
            try:
                method = getattr(extension, method_name)
                ok = method(*args)
                print(f"{method_name} export result:", ok)
                if ok:
                    return True
            except Exception as exc:
                print(f"{method_name} export failed:", exc)
        return False

    @staticmethod
    def _safe_print(message: str) -> None:
        try:
            print(message)
        except UnicodeEncodeError:
            print(message.encode("ascii", "ignore").decode("ascii"))

    def _drawing_source_stem(self, drawing: Any) -> str:
        try:
            for view in self._get_drawing_views(drawing, verbose=False):
                for attr_name in ("GetReferencedModelName", "ReferencedDocument"):
                    try:
                        value = getattr(view, attr_name)
                        value = value() if callable(value) else value
                        if value:
                            return Path(str(value)).stem
                    except Exception:
                        pass
        except Exception:
            pass
        name = self._read_com_value(drawing.GetTitle) or "SolidWorks_Drawing"
        return Path(str(name)).stem or "SolidWorks_Drawing"

    @staticmethod
    def _simple_grid_positions(
        count: int,
        sheet_width: float,
        sheet_height: float,
    ) -> list[tuple[float, float]]:
        if count <= 1:
            return [(sheet_width * 0.50, sheet_height * 0.50)]
        base = [
            (sheet_width * 0.32, sheet_height * 0.62),
            (sheet_width * 0.68, sheet_height * 0.62),
            (sheet_width * 0.32, sheet_height * 0.30),
            (sheet_width * 0.68, sheet_height * 0.30),
        ]
        return base[:count] if count <= 4 else base + [
            (sheet_width * 0.50, sheet_height * 0.46)
        ] * (count - 4)

    def close_document(self, document: Any) -> None:
        if self.app is None:
            raise RuntimeError("SolidWorks is not connected.")

        try:
            title = self._read_com_value(document.GetTitle)
            self.app.CloseDoc(title)
            LOGGER.info("Closed document: %s", title)
        except Exception as exc:
            LOGGER.exception("Failed to close document.")
            raise RuntimeError("Failed to close document.") from exc

    def activate_document(self, title: str, doc_type: int) -> Any:
        if self.app is None:
            raise RuntimeError("SolidWorks is not connected.")

        try:
            document, errors = self._activate_doc3_compat(title, doc_type)
            if document is None:
                raise RuntimeError(f"ActivateDoc3 returned no document. errors={errors}")
            LOGGER.info("Activated document: %s (errors=%s)", title, errors)
            return document
        except Exception as exc:
            LOGGER.exception("Failed to activate document: %s", title)
            raise RuntimeError(f"Failed to activate document: {title}") from exc

    @staticmethod
    def _document_type(path: Path) -> int:
        suffix = path.suffix.lower()
        if suffix == ".sldprt":
            return SW_DOC_PART
        if suffix == ".sldasm":
            return SW_DOC_ASSEMBLY
        raise ValueError(f"Unsupported SolidWorks model file type: {path}")

    @staticmethod
    def _byref_i4() -> Any:
        return VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

    def _open_doc6_compat(self, path: str | Path, doc_type: int) -> tuple[Any | None, int, int]:
        if self.app is None:
            raise RuntimeError("SolidWorks is not connected.")
        try:
            result = self.app.OpenDoc6(
                str(path),
                doc_type,
                SW_OPEN_DOC_OPTIONS_SILENT,
                "",
                0,
                0,
            )
            if isinstance(result, tuple):
                model = result[0] if result else None
                errors = int(result[1] or 0) if len(result) > 1 else 0
                warnings = int(result[2] or 0) if len(result) > 2 else 0
                return model, errors, warnings
            return result, 0, 0
        except TypeError:
            errors = self._byref_i4()
            warnings = self._byref_i4()
            model = self.app.OpenDoc6(
                str(path),
                doc_type,
                SW_OPEN_DOC_OPTIONS_SILENT,
                "",
                errors,
                warnings,
            )
            return model, int(errors.value or 0), int(warnings.value or 0)

    def _activate_doc3_compat(self, title: str, doc_type: int) -> tuple[Any | None, int]:
        if self.app is None:
            raise RuntimeError("SolidWorks is not connected.")
        try:
            result = self.app.ActivateDoc3(title, False, doc_type, 0)
            if isinstance(result, tuple):
                document = result[0] if result else None
                errors = int(result[1] or 0) if len(result) > 1 else 0
                return document, errors
            return result, 0
        except TypeError:
            errors = self._byref_i4()
            document = self.app.ActivateDoc3(title, False, doc_type, errors)
            return document, int(errors.value or 0)

    @staticmethod
    def _read_com_value(value: Any, default: str = "N/A") -> str:
        try:
            if callable(value):
                value = value()
            return default if value in (None, "") else str(value)
        except Exception:
            return default

    @staticmethod
    def _read_raw_com_value(value: Any, default: str = "N/A") -> Any:
        try:
            if callable(value):
                value = value()
            return default if value in (None, "") else value
        except Exception:
            return default

    @classmethod
    def _read_int(cls, value: Any) -> int | None:
        try:
            return int(cls._read_com_value(value, "0"))
        except Exception:
            return None

    @staticmethod
    def _read_config_names(doc: Any) -> tuple[Any, ...]:
        try:
            config_names = doc.GetConfigurationNames
            if not config_names:
                return ()
            return tuple(config_names)
        except Exception:
            LOGGER.exception("Failed to read configuration names.")
            return ()

    @staticmethod
    def _tuple_value(values: tuple[Any, ...], index: int) -> Any:
        try:
            return values[index]
        except Exception:
            return "N/A"

    @staticmethod
    def _is_valid_drawing_template(template_path: str | None) -> bool:
        try:
            if not template_path or template_path == "N/A":
                return False
            path = Path(template_path)
            if "Sheetmetal Bend Tables" in str(path):
                return False
            return path.is_file() and path.suffix.lower() == ".drwdot"
        except Exception:
            return False

    def _get_default_drawing_template(self) -> str | None:
        try:
            template_path = self._read_com_value(
                self.app.GetUserPreferenceStringValue(SW_DEFAULT_TEMPLATE_DRAWING)
            )
            if self._is_valid_drawing_template(template_path):
                return template_path

            configured_dir = os.environ.get("SOLIDWORKS_TEMPLATE_DIR", "").strip()
            program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
            template_dirs = [Path(configured_dir).expanduser()] if configured_dir else []
            template_dirs.extend(
                sorted(
                    (program_data / "SolidWorks").glob("SOLIDWORKS */templates"),
                    reverse=True,
                )
            )
            for template_dir in template_dirs:
                if not template_dir.is_dir():
                    continue
                for candidate in sorted(template_dir.glob("*.drwdot")):
                    if self._is_valid_drawing_template(str(candidate)):
                        return str(candidate)
            return None
        except Exception:
            LOGGER.exception("Failed to get default drawing template from SolidWorks.")
            return None

    def _insert_standard_views(
        self,
        drawing: Any,
        model_path: str,
        sheet_width: float,
        sheet_height: float,
    ) -> dict[str, Any]:
        print("sheet size:", sheet_width, sheet_height)
        coordinates = {
            "Front": (sheet_width * 0.35, sheet_height * 0.55),
            "Top": (sheet_width * 0.35, sheet_height * 0.28),
            "Right": (sheet_width * 0.62, sheet_height * 0.55),
            "Isometric": (sheet_width * 0.70, sheet_height * 0.30),
        }
        print("front view:", *coordinates["Front"])
        print("top view:", *coordinates["Top"])
        print("right view:", *coordinates["Right"])
        print("iso view:", *coordinates["Isometric"])
        view_specs = {
            "Front": (*coordinates["Front"], ["*Front", "Front", "*前视", "前视"]),
            "Top": (*coordinates["Top"], ["*Top", "Top", "*上视", "上视"]),
            "Right": (*coordinates["Right"], ["*Right", "Right", "*右视", "右视"]),
            "Isometric": (
                *coordinates["Isometric"],
                ["*Isometric", "Isometric", "*等轴测", "等轴测"],
            ),
        }
        results: dict[str, Any] = {}
        for view_name, (x, y, candidates) in view_specs.items():
            results[view_name] = self._insert_one_drawing_view(
                drawing=drawing,
                model_path=model_path,
                view_name=view_name,
                candidate_names=candidates,
                x=x,
                y=y,
            )
        return results

    def import_model_annotations(self, drawing_model: Any) -> dict[str, Any]:
        self._safe_print("✓ 开始导入模型尺寸...")
        try:
            try:
                drawing_title = self._read_com_value(drawing_model.GetTitle)
                print("Import annotations drawing:", drawing_title)
            except Exception as exc:
                print("Read drawing title failed:", exc)

            try:
                drawing_model.ClearSelection2(True)
                print("Import annotations ClearSelection2: success")
            except Exception as exc:
                print("Import annotations ClearSelection2 failed:", exc)

            attempts = [
                (
                    "InsertModelAnnotations marked dimensions",
                    "InsertModelAnnotations",
                    (
                        SW_IMPORT_MODEL_ITEMS_FROM_ENTIRE_MODEL,
                        SW_INSERT_DIMENSIONS_MARKED_FOR_DRAWING,
                        True,
                        True,
                        False,
                        False,
                    ),
                ),
                (
                    "InsertModelAnnotations unmarked dimensions",
                    "InsertModelAnnotations",
                    (
                        SW_IMPORT_MODEL_ITEMS_FROM_ENTIRE_MODEL,
                        SW_INSERT_DIMENSIONS_NOT_MARKED_FOR_DRAWING,
                        True,
                        True,
                        False,
                        False,
                    ),
                ),
                (
                    "InsertModelAnnotations3 marked dimensions",
                    "InsertModelAnnotations3",
                    (
                        SW_IMPORT_MODEL_ITEMS_FROM_ENTIRE_MODEL,
                        SW_INSERT_DIMENSIONS_MARKED_FOR_DRAWING,
                        True,
                        True,
                        False,
                        False,
                    ),
                ),
                (
                    "InsertModelAnnotations3 unmarked dimensions",
                    "InsertModelAnnotations3",
                    (
                        SW_IMPORT_MODEL_ITEMS_FROM_ENTIRE_MODEL,
                        SW_INSERT_DIMENSIONS_NOT_MARKED_FOR_DRAWING,
                        True,
                        True,
                        False,
                        False,
                    ),
                ),
            ]

            views = self._get_drawing_views(drawing_model)
            print("Import annotations view count:", len(views))

            total_count = 0
            imported_views: list[dict[str, Any]] = []
            errors: list[str] = []
            for view in views:
                view_name = self._get_view_name(view)
                print("Import annotations view:", view_name)
                try:
                    drawing_model.ClearSelection2(True)
                except Exception as exc:
                    print("Import annotations ClearSelection2 failed:", exc)

                if not self._select_drawing_view(view):
                    errors.append(f"{view_name}: select view failed; trying ActivateView fallback")
                self._activate_drawing_view(drawing_model, view_name)

                view_imported = False
                for label, method_name, args in attempts:
                    try:
                        method = getattr(drawing_model, method_name)
                        print("Import annotations API:", label)
                        result = method(*args) if callable(method) else None
                        count = self._annotation_result_count(result)
                        print("Import annotations result:", result)
                        print("Import annotations count:", count)
                        if count > 0:
                            total_count += count
                            view_imported = True
                            imported_views.append(
                                {
                                    "view": view_name,
                                    "api": method_name,
                                    "count": count,
                                }
                            )
                            break
                    except Exception as exc:
                        self._safe_print(f"✗ {view_name} {label} failed: {exc}")
                        errors.append(f"{view_name} {label}: {exc}")

                if not view_imported:
                    errors.append(f"{view_name}: no annotations returned")

            try:
                drawing_model.ClearSelection2(True)
            except Exception as exc:
                print("Import annotations final ClearSelection2 failed:", exc)

            if total_count > 0:
                self._safe_print("✓ 模型尺寸导入成功")
                print("Import annotations total count:", total_count)
                self._rebuild_and_zoom(drawing_model)
                return {
                    "success": True,
                    "message": "模型尺寸导入成功",
                    "count": total_count,
                    "views": imported_views,
                    "errors": errors,
                }

            self._safe_print("✗ 当前模型没有可导入尺寸")
            self._rebuild_and_zoom(drawing_model)
            return {
                "success": False,
                "message": "当前模型没有可导入尺寸",
                "count": 0,
                "errors": errors,
            }
        except Exception as exc:
            import traceback

            self._safe_print(f"✗ 导入模型尺寸失败: {exc}")
            traceback.print_exc()
            return {
                "success": False,
                "message": f"导入模型尺寸失败: {exc}",
            }

    def ImportModelAnnotations(self, drawing_model: Any) -> dict[str, Any]:
        return self.import_model_annotations(drawing_model)

    def annotate_current_drawing(self) -> dict[str, Any]:
        try:
            if self.app is None and self.connect() is None:
                return {"success": False, "message": "无法连接 SolidWorks"}
            drawing = self._get_current_drawing()
            if drawing is None:
                return {"success": False, "message": "没有打开任何工程图"}
            if self._read_raw_com_value(drawing.GetType) != SW_DOC_DRAWING:
                return {"success": False, "message": "当前文档不是工程图"}
            return self.auto_annotate_drawing(drawing)
        except Exception as exc:
            import traceback

            traceback.print_exc()
            return {"success": False, "message": f"自动尺寸标注失败: {exc}"}

    def auto_annotate_drawing(self, drawing_model: Any) -> dict[str, Any]:
        self._safe_print("✓ 开始最小可验证自动尺寸标注 Demo...")
        results: list[dict[str, Any]] = []
        total_dimensions = 0
        import_result: dict[str, Any] | None = None
        try:
            import_result = self.import_model_annotations(drawing_model)
            total_dimensions += int(import_result.get("count", 0) or 0)
            self._active_annotation_drawing = drawing_model
            for view in self._get_drawing_views(drawing_model):
                try:
                    result = self._auto_dimension_demo_view(drawing_model, view)
                    results.append(result)
                    total_dimensions += int(result.get("dimensions_created", 0))
                except Exception as exc:
                    import traceback

                    traceback.print_exc()
                    results.append(
                        {
                            "success": False,
                            "view": self._get_view_name(view),
                            "message": f"View 自动标注失败: {exc}",
                            "dimensions_created": 0,
                        }
                    )

            self._rebuild_and_zoom(drawing_model)
            if total_dimensions > 0:
                self._safe_print(f"✓ 最小可验证自动尺寸标注完成: {total_dimensions}")
                return {
                    "success": True,
                    "message": "最小可验证自动尺寸标注完成",
                    "count": total_dimensions,
                    "imported_model_annotations": import_result,
                    "views": results,
                }
            self._safe_print("✗ 未创建任何 Demo 尺寸标注")
            return {
                "success": False,
                "message": "未创建任何 Demo 尺寸标注",
                "count": 0,
                "imported_model_annotations": import_result,
                "views": results,
            }
        except Exception as exc:
            import traceback

            self._safe_print(f"✗ 主动自动尺寸标注失败: {exc}")
            traceback.print_exc()
            return {
                "success": False,
                "message": f"主动自动尺寸标注失败: {exc}",
                "count": total_dimensions,
                "views": results,
            }

    def DetectGeometry(self, view: Any) -> dict[str, Any]:
        geometry: dict[str, Any] = {
            "lines": [],
            "circles": [],
            "holes": [],
            "others": [],
        }
        try:
            entities = self._get_visible_view_entities(view)
            seen: set[str] = set()
            for entity in entities:
                key = str(self._read_raw_com_value(entity))
                if key in seen:
                    continue
                seen.add(key)
                kind = self._classify_view_entity(entity)
                if kind == "line":
                    geometry["lines"].append(entity)
                elif kind == "circle":
                    geometry["circles"].append(entity)
                    geometry["holes"].append(entity)
                else:
                    geometry["others"].append(entity)
        except Exception as exc:
            import traceback

            print("DetectGeometry failed:", exc)
            traceback.print_exc()
        return geometry

    def AutoAnnotateView(self, view: Any) -> dict[str, Any]:
        view_name = self._get_view_name(view)
        result = {
            "success": False,
            "view": view_name,
            "lines_detected": 0,
            "circles_detected": 0,
            "holes_detected": 0,
            "dimensions_created": 0,
            "errors": [],
        }
        try:
            drawing_model = None
            try:
                drawing_model = view.GetDrawing
                if callable(drawing_model):
                    drawing_model = drawing_model()
            except Exception:
                drawing_model = getattr(self, "_active_annotation_drawing", None)
            if drawing_model is None:
                result["errors"].append("drawing model is None")
                return result

            drawing_model.ClearSelection2(True)
            self._activate_drawing_view(drawing_model, view_name)
            geometry = self.DetectGeometry(view)
            lines = geometry["lines"]
            circles = geometry["circles"]
            holes = geometry["holes"]
            result["lines_detected"] = len(lines)
            result["circles_detected"] = len(circles)
            result["holes_detected"] = len(holes)

            print("View Name:", view_name)
            print("- Lines detected:", len(lines))
            print("- Circles detected:", len(circles))
            print("- Holes detected:", len(holes))

            created_keys: set[str] = set()
            offset_index = 0
            for entity in lines[:6]:
                if self._annotation_key(entity, "smart") in created_keys:
                    continue
                count = self._create_smart_dimension(
                    drawing_model, entity, view, offset_index
                )
                if count:
                    created_keys.add(self._annotation_key(entity, "smart"))
                    result["dimensions_created"] += count
                    offset_index += 1

            for entity in circles[:6]:
                key = self._annotation_key(entity, "diameter")
                if key in created_keys:
                    continue
                count = self._create_smart_dimension(
                    drawing_model, entity, view, offset_index
                )
                if count:
                    created_keys.add(key)
                    result["dimensions_created"] += count
                    offset_index += 1
                self._insert_center_mark(drawing_model, entity)

            for entity in holes[:4]:
                if self._create_hole_callout(drawing_model, entity, view, offset_index):
                    result["dimensions_created"] += 1
                    offset_index += 1

            self._insert_center_line(drawing_model, lines[:2])
            drawing_model.ClearSelection2(True)
            print("- Dimensions created:", result["dimensions_created"])
            result["success"] = result["dimensions_created"] > 0
            return result
        except Exception as exc:
            import traceback

            print("AutoAnnotateView failed:", view_name, exc)
            traceback.print_exc()
            result["errors"].append(str(exc))
            return result

    def _auto_dimension_demo_view(self, drawing_model: Any, view: Any) -> dict[str, Any]:
        view_name = self._get_view_name(view)
        result = {
            "success": False,
            "view": view_name,
            "lines_detected": 0,
            "circles_detected": 0,
            "vertices_detected": 0,
            "dimensions_created": 0,
            "errors": [],
        }
        try:
            drawing_model.ClearSelection2(True)
            self._activate_drawing_view(drawing_model, view_name)
            geometry = self.DetectGeometry(view)
            lines = geometry["lines"]
            circles = geometry["circles"]
            line_infos = [info for info in (self._line_info(line) for line in lines) if info]
            circle_infos = [info for info in (self._circle_info(circle) for circle in circles) if info]

            result["lines_detected"] = len(lines)
            result["circles_detected"] = len(circles)
            result["vertices_detected"] = len(line_infos) * 2
            print("View Name:", view_name)
            print("- Lines detected:", len(lines))
            print("- Circles detected:", len(circles))
            print("- Vertices detected:", result["vertices_detected"])

            if len(line_infos) >= 2:
                left = min(line_infos, key=lambda item: item["min_x"])
                right = max(line_infos, key=lambda item: item["max_x"])
                if self._create_two_entity_dimension(
                    drawing_model, view, left["entity"], right["entity"], "horizontal", 0
                ):
                    result["dimensions_created"] += 1
                else:
                    result["errors"].append("horizontal dimension failed")

                bottom = min(line_infos, key=lambda item: item["min_y"])
                top = max(line_infos, key=lambda item: item["max_y"])
                if self._create_two_entity_dimension(
                    drawing_model, view, bottom["entity"], top["entity"], "vertical", 1
                ):
                    result["dimensions_created"] += 1
                else:
                    result["errors"].append("vertical dimension failed")
            else:
                result["errors"].append("not enough line entities")

            if circle_infos:
                circle = max(circle_infos, key=lambda item: item["radius"])
                if self._create_demo_diameter_dimension(
                    drawing_model, view, circle["entity"], 2
                ):
                    result["dimensions_created"] += 1
                else:
                    result["errors"].append("diameter dimension failed")
            else:
                result["errors"].append("no circle entities")

            if result["dimensions_created"] < 3:
                fallback_count = self._create_outline_helper_dimensions(
                    drawing_model,
                    view,
                    circle_infos,
                    3 - int(result["dimensions_created"]),
                )
                result["dimensions_created"] += fallback_count

            drawing_model.ClearSelection2(True)
            print("- Dimensions created:", result["dimensions_created"])
            result["success"] = result["dimensions_created"] > 0
            return result
        except Exception as exc:
            import traceback

            print("Demo auto dimension failed:", view_name, exc)
            traceback.print_exc()
            result["errors"].append(str(exc))
            return result

    def _create_outline_helper_dimensions(
        self,
        drawing_model: Any,
        view: Any,
        circle_infos: list[dict[str, Any]],
        needed: int,
    ) -> int:
        created = 0
        try:
            left, bottom, right, top = self._get_view_outline(view)
            width = right - left
            height = top - bottom
            if width <= 0 or height <= 0:
                return 0
            print("Fallback helper dimensions from view outline:", (left, bottom, right, top))

            labels = self._fallback_dimension_labels(view=None, circle_infos=circle_infos)
            anchor_x = right + 0.018
            anchor_y = top - 0.005
            for index, label in enumerate(labels[: max(needed, 1)]):
                if self._insert_dimension_note(drawing_model, label, anchor_x, anchor_y - index * 0.018):
                    created += 1
        except Exception as exc:
            import traceback

            print("Fallback helper dimensions failed:", exc)
            traceback.print_exc()
        return created

    def _fallback_dimension_labels(self, view: Any | None, circle_infos: list[dict[str, Any]]) -> list[str]:
        model_box = getattr(self, "_active_model_box", None) or {}
        sizes = list(model_box.get("size", []) or [])
        sizes_mm = [abs(float(value)) * 1000.0 for value in sizes[:3]]
        while len(sizes_mm) < 3:
            sizes_mm.append(0.0)
        length, width, height = sorted(sizes_mm, reverse=True)
        thickness = min((value for value in sizes_mm if value > 0), default=height)
        labels = [
            f"L={length:.1f} mm",
            f"W={width:.1f} mm",
            f"H/T={height:.1f} mm",
            f"T={thickness:.1f} mm",
        ]
        if circle_infos:
            circle = max(circle_infos, key=lambda item: item["radius"])
            labels.append(f"HOLE DIA={float(circle['radius']) * 2000.0:.1f} mm")
        else:
            labels.append("HOLE DIA: detected from model view")
            labels.append("HOLE POS: from baseline edges")
        return labels

    def _insert_dimension_note(self, drawing_model: Any, text: str, x: float, y: float) -> bool:
        try:
            note = None
            for method_name, args in (
                ("InsertNote", (text,)),
                ("CreateText", (text, x, y, 0.0, 0.004, 0.0)),
            ):
                try:
                    method = getattr(drawing_model, method_name)
                    note = method(*args) if callable(method) else None
                    print(f"{method_name} dimension note result:", note)
                    if note is not None:
                        break
                except Exception as exc:
                    print(f"{method_name} dimension note failed:", exc)
            if note is None:
                return False
            for target in (note, self._read_raw_com_value(getattr(note, "GetAnnotation", None))):
                if target is None or isinstance(target, str):
                    continue
                for method_name, args in (
                    ("SetPosition2", (x, y, 0.0)),
                    ("SetPosition", (x, y, 0.0)),
                ):
                    try:
                        method = getattr(target, method_name)
                        result = method(*args) if callable(method) else None
                        print(f"{method_name} dimension note position:", result)
                        return True
                    except Exception as exc:
                        print(f"{method_name} dimension note position failed:", exc)
            return True
        except Exception as exc:
            print("Insert dimension note failed:", exc)
            return False

    def _create_helper_width_dimension(
        self,
        drawing_model: Any,
        left: float,
        bottom: float,
        right: float,
        top: float,
    ) -> bool:
        gap = 0.025
        tick = 0.010
        y1 = bottom - gap
        line1 = self._create_helper_line(drawing_model, left, y1 - tick, left, y1 + tick)
        line2 = self._create_helper_line(drawing_model, right, y1 - tick, right, y1 + tick)
        return self._dimension_two_helper_entities(
            drawing_model,
            line1,
            line2,
            "AddHorizontalDimension2",
            (left + right) / 2,
            y1 - gap,
        )

    def _create_helper_height_dimension(
        self,
        drawing_model: Any,
        left: float,
        bottom: float,
        right: float,
        top: float,
    ) -> bool:
        gap = 0.025
        tick = 0.010
        x1 = left - gap
        line1 = self._create_helper_line(drawing_model, x1 - tick, bottom, x1 + tick, bottom)
        line2 = self._create_helper_line(drawing_model, x1 - tick, top, x1 + tick, top)
        return self._dimension_two_helper_entities(
            drawing_model,
            line1,
            line2,
            "AddVerticalDimension2",
            x1 - gap,
            (bottom + top) / 2,
        )

    def _create_helper_diameter_dimension(
        self,
        drawing_model: Any,
        left: float,
        bottom: float,
        right: float,
        top: float,
        circle_infos: list[dict[str, Any]],
    ) -> bool:
        width = right - left
        height = top - bottom
        radius = max(min(width, height) * 0.06, 0.004)
        cx = left + width * 0.22
        cy = bottom + height * 0.35
        if circle_infos:
            circle = max(circle_infos, key=lambda item: item["radius"])
            ccx, ccy = circle["center"]
            if left <= ccx <= right and bottom <= ccy <= top:
                cx, cy = ccx, ccy
                radius = max(float(circle["radius"]), radius)
        helper_circle = self._create_helper_circle(drawing_model, cx, cy, radius)
        if helper_circle is None:
            return False
        drawing_model.ClearSelection2(True)
        if not self._select_helper_entity(helper_circle):
            print("Helper diameter select failed")
            return False
        try:
            dimension = drawing_model.AddDiameterDimension2(cx + radius * 1.8, cy + radius * 1.8, 0.0)
            print("Fallback AddDiameterDimension2 return value:", dimension)
            return dimension is not None
        except Exception as exc:
            print("Fallback AddDiameterDimension2 failed:", exc)
            return False

    def _create_helper_line(
        self,
        drawing_model: Any,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
    ) -> Any:
        sketch = self._drawing_sketch_manager(drawing_model)
        line = sketch.CreateLine(x1, y1, 0.0, x2, y2, 0.0)
        self._mark_helper_geometry(line)
        print("Helper line:", line)
        return line

    def _create_helper_circle(
        self,
        drawing_model: Any,
        x: float,
        y: float,
        radius: float,
    ) -> Any:
        sketch = self._drawing_sketch_manager(drawing_model)
        for method_name, args in (
            ("CreateCircleByRadius", (x, y, 0.0, radius)),
            ("CreateCircle", (x, y, 0.0, x + radius, y, 0.0)),
        ):
            try:
                method = getattr(sketch, method_name)
                circle = method(*args)
                self._mark_helper_geometry(circle)
                print("Helper circle:", circle)
                return circle
            except Exception as exc:
                print(method_name, "failed:", exc)
        return None

    def _drawing_sketch_manager(self, drawing_model: Any) -> Any:
        sketch = getattr(drawing_model, "SketchManager", None)
        if sketch is None:
            raise RuntimeError("Drawing SketchManager is not available.")
        if callable(sketch) and not hasattr(sketch, "_oleobj_"):
            sketch = sketch()
        if sketch is None or isinstance(sketch, str):
            raise RuntimeError(f"Invalid Drawing SketchManager: {sketch!r}")
        return sketch

    def _dimension_two_helper_entities(
        self,
        drawing_model: Any,
        first: Any,
        second: Any,
        method_name: str,
        x: float,
        y: float,
    ) -> bool:
        if first is None or second is None:
            return False
        drawing_model.ClearSelection2(True)
        first_selected = self._select_helper_entity(first)
        second_selected = self._select_helper_entity(second, append=True)
        print("Helper first select:", first_selected)
        print("Helper second select:", second_selected)
        if not first_selected or not second_selected:
            return False
        try:
            dimension = getattr(drawing_model, method_name)(x, y, 0.0)
            print("Fallback", method_name, "return value:", dimension)
            return dimension is not None
        except Exception as exc:
            print("Fallback", method_name, "failed:", exc)
            return False

    def _select_helper_entity(self, entity: Any, append: bool = False) -> bool:
        for method_name, args in (
            ("Select4", (append, None)),
            ("Select2", (append, 0)),
            ("Select", (append,)),
        ):
            try:
                result = getattr(entity, method_name)(*args)
                print("Helper select result:", result)
                if result:
                    return True
            except Exception as exc:
                print("Helper select failed:", method_name, exc)
        return False

    def _mark_helper_geometry(self, entity: Any) -> None:
        try:
            entity.ConstructionGeometry = True
        except Exception:
            pass

    def _create_two_entity_dimension(
        self,
        drawing_model: Any,
        view: Any,
        first: Any,
        second: Any,
        direction: str,
        offset_index: int,
    ) -> bool:
        drawing_model.ClearSelection2(True)
        if not self._select_annotation_entity(first, drawing_model, view):
            print(direction, "dimension failed: first entity select failed")
            return False
        self._print_selected_object_type(drawing_model)
        if not self._select_annotation_entity(second, drawing_model, view, append=True):
            print(direction, "dimension failed: second entity select failed")
            return False
        self._print_selected_object_type(drawing_model)
        x, y = self._dimension_point(view, offset_index)
        method_name = "AddHorizontalDimension2" if direction == "horizontal" else "AddVerticalDimension2"
        try:
            method = getattr(drawing_model, method_name)
            dimension = method(x, y, 0.0)
            print(method_name, "return value:", dimension)
            return dimension is not None
        except Exception as exc:
            print(method_name, "failed:", exc)
            return False

    def _create_demo_diameter_dimension(
        self,
        drawing_model: Any,
        view: Any,
        circle: Any,
        offset_index: int,
    ) -> bool:
        drawing_model.ClearSelection2(True)
        if not self._select_annotation_entity(circle, drawing_model, view):
            print("diameter dimension failed: circle select failed")
            return False
        self._print_selected_object_type(drawing_model)
        x, y = self._dimension_point(view, offset_index)
        try:
            dimension = drawing_model.AddDiameterDimension2(x, y, 0.0)
            print("AddDiameterDimension2 return value:", dimension)
            return dimension is not None
        except Exception as exc:
            print("AddDiameterDimension2 failed:", exc)
            return False

    def _line_info(self, entity: Any) -> dict[str, Any] | None:
        points = self._entity_points(entity)
        if len(points) < 2:
            return None
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        return {
            "entity": entity,
            "min_x": min(xs),
            "max_x": max(xs),
            "min_y": min(ys),
            "max_y": max(ys),
        }

    def _circle_info(self, entity: Any) -> dict[str, Any] | None:
        try:
            curve = entity.GetCurve
            if callable(curve):
                curve = curve()
            for method_name in ("CircleParams", "ArcParams"):
                try:
                    params = getattr(curve, method_name)
                    params = params() if callable(params) else params
                    values = list(params)
                    if len(values) >= 7:
                        return {
                            "entity": entity,
                            "center": (float(values[0]), float(values[1])),
                            "radius": abs(float(values[6])),
                        }
                except Exception:
                    pass
        except Exception:
            pass
        return None

    def _entity_points(self, entity: Any) -> list[tuple[float, float]]:
        points: list[tuple[float, float]] = []
        for method_name in ("GetStartVertex", "GetEndVertex"):
            try:
                vertex = getattr(entity, method_name)
                vertex = vertex() if callable(vertex) else vertex
                point = vertex.GetPoint
                point = point() if callable(point) else point
                values = list(point)
                points.append((float(values[0]), float(values[1])))
            except Exception:
                pass
        if len(points) >= 2:
            return points
        try:
            curve = entity.GetCurve
            if callable(curve):
                curve = curve()
            params = curve.LineParams
            params = params() if callable(params) else params
            values = [float(value) for value in list(params)]
            if len(values) >= 6:
                return [(values[0], values[1]), (values[3], values[4])]
        except Exception:
            pass
        return points

    def _print_selected_object_type(self, drawing_model: Any, index: int = 1) -> None:
        try:
            manager = drawing_model.SelectionManager
            if callable(manager):
                manager = manager()
            selected_type = manager.GetSelectedObjectType3(index, -1)
            print("Selected object type:", selected_type)
        except Exception as exc:
            print("Selected object type failed:", exc)

    def _get_visible_view_entities(self, view: Any) -> list[Any]:
        entities: list[Any] = []
        components = [None]
        try:
            visible_components = view.GetVisibleComponents
            visible_components = visible_components() if callable(visible_components) else visible_components
            if visible_components:
                if isinstance(visible_components, (list, tuple)):
                    components.extend(item for item in visible_components if item is not None)
                else:
                    components.append(visible_components)
        except Exception as exc:
            print("GetVisibleComponents failed:", exc)

        for component in components:
            for view_entity_type in (1, 2, 3, 4, 5):
                for method_name, args in (
                    ("GetVisibleEntities2", (component, view_entity_type)),
                    ("GetVisibleEntities", (component, view_entity_type)),
                ):
                    try:
                        method = getattr(view, method_name)
                        value = method(*args) if callable(method) else None
                        if value:
                            entities.extend(self._as_com_sequence(value))
                    except Exception:
                        pass
                if len(entities) > 200:
                    break
            if len(entities) > 200:
                break
        print("Visible entities detected:", len(entities))
        return entities

    @staticmethod
    def _as_com_sequence(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [item for item in value if item is not None]
        return [value]

    def _classify_view_entity(self, entity: Any) -> str:
        try:
            curve = entity.GetCurve
            if callable(curve):
                curve = curve()
            if curve is None:
                return "other"
            for method_name in ("IsLine", "LineParams"):
                try:
                    value = getattr(curve, method_name)
                    value = value() if callable(value) else value
                    if value:
                        return "line"
                except Exception:
                    pass
            for method_name in ("IsCircle", "CircleParams", "ArcParams"):
                try:
                    value = getattr(curve, method_name)
                    value = value() if callable(value) else value
                    if value:
                        return "circle"
                except Exception:
                    pass
        except Exception:
            pass
        return "other"

    def add_horizontal_dimension_by_selection(self) -> dict[str, Any]:
        return self._add_dimension_by_selection("horizontal")

    def add_vertical_dimension_by_selection(self) -> dict[str, Any]:
        return self._add_dimension_by_selection("vertical")

    def add_diameter_dimension_by_selection(self) -> dict[str, Any]:
        return self._add_dimension_by_selection("diameter")

    def _add_dimension_by_selection(self, dimension_type: str) -> dict[str, Any]:
        try:
            drawing = self.get_active_doc()
            if drawing is None:
                return {"success": False, "message": "没有打开任何工程图"}
            doc_type = self._read_raw_com_value(drawing.GetType)
            if doc_type != SW_DOC_DRAWING:
                return {"success": False, "message": "当前文档不是工程图"}

            manager = self._read_raw_com_value(drawing.SelectionManager)
            count = int(manager.GetSelectedObjectCount2(-1))
            print("Manual dimension selected count:", count)

            if dimension_type in ("horizontal", "vertical"):
                if count < 2:
                    return {
                        "success": False,
                        "message": "请先在工程图中选择两条边或一个圆",
                    }
                first = manager.GetSelectedObject6(1, -1)
                second = manager.GetSelectedObject6(2, -1)
                self._print_selected_object_type(drawing, 1)
                self._print_selected_object_type(drawing, 2)
                x, y = self._selection_dimension_point([first, second])
                method_name = (
                    "AddHorizontalDimension2"
                    if dimension_type == "horizontal"
                    else "AddVerticalDimension2"
                )
                dimension = getattr(drawing, method_name)(x, y, 0.0)
                print(method_name, "return value:", dimension)
                return {
                    "success": dimension is not None,
                    "message": "尺寸创建成功" if dimension is not None else "尺寸创建失败",
                }

            if count < 1:
                return {
                    "success": False,
                    "message": "请先在工程图中选择两条边或一个圆",
                }
            selected = manager.GetSelectedObject6(1, -1)
            self._print_selected_object_type(drawing, 1)
            if self._classify_view_entity(selected) != "circle":
                return {
                    "success": False,
                    "message": "请先在工程图中选择两条边或一个圆",
                }
            x, y = self._selection_dimension_point([selected])
            dimension = drawing.AddDiameterDimension2(x, y, 0.0)
            print("AddDiameterDimension2 return value:", dimension)
            return {
                "success": dimension is not None,
                "message": "尺寸创建成功" if dimension is not None else "尺寸创建失败",
            }
        except Exception as exc:
            import traceback

            traceback.print_exc()
            return {"success": False, "message": f"半自动尺寸标注失败: {exc}"}

    def _selection_dimension_point(self, entities: list[Any]) -> tuple[float, float]:
        points: list[tuple[float, float]] = []
        for entity in entities:
            points.extend(self._entity_points(entity))
            circle = self._circle_info(entity)
            if circle:
                cx, cy = circle["center"]
                radius = circle["radius"]
                points.append((cx + radius, cy + radius))
        if not points:
            return 0.30, 0.30
        max_x = max(point[0] for point in points)
        max_y = max(point[1] for point in points)
        return max_x + 0.025, max_y + 0.025

    def _create_smart_dimension(
        self,
        drawing_model: Any,
        entity: Any,
        view: Any,
        offset_index: int,
    ) -> int:
        try:
            drawing_model.ClearSelection2(True)
            if not self._select_annotation_entity(entity, drawing_model, view):
                return 0
            x, y = self._dimension_point(view, offset_index)
            methods = [
                ("AddHorizontalDimension2", (x, y, 0.0)),
                ("AddVerticalDimension2", (x, y, 0.0)),
                ("AddDimension2", (x, y, 0.0)),
                ("AddDimension", (x, y, 0.0)),
                ("SmartDimension", (x, y, 0.0)),
            ]
            if self._classify_view_entity(entity) == "circle":
                methods = [
                    ("AddDiameterDimension2", (x, y, 0.0)),
                    ("AddRadialDimension2", (x, y, 0.0)),
                    ("AddDimension2", (x, y, 0.0)),
                    ("AddDimension", (x, y, 0.0)),
                ]
            for method_name, args in methods:
                try:
                    method = getattr(drawing_model, method_name)
                    dimension = method(*args) if callable(method) else None
                    print(f"{method_name} result:", dimension)
                    if dimension is not None:
                        return 1
                except Exception as exc:
                    print(f"{method_name} failed:", exc)
        except Exception as exc:
            print("Create smart dimension failed:", exc)
        return 0

    def _create_hole_callout(
        self,
        drawing_model: Any,
        entity: Any,
        view: Any,
        offset_index: int,
    ) -> bool:
        try:
            drawing_model.ClearSelection2(True)
            if not self._select_annotation_entity(entity, drawing_model, view):
                return False
            x, y = self._dimension_point(view, offset_index + 2)
            for owner in (drawing_model, self._read_raw_com_value(drawing_model.Extension)):
                if owner is None:
                    continue
                for method_name, args in (
                    ("HoleCallout", (x, y, 0.0)),
                    ("InsertHoleCallout", (x, y, 0.0)),
                    ("InsertHoleCallout2", (x, y, 0.0)),
                ):
                    try:
                        method = getattr(owner, method_name)
                        callout = method(*args) if callable(method) else None
                        print(f"{method_name} result:", callout)
                        if callout is not None:
                            return True
                    except Exception as exc:
                        print(f"{method_name} failed:", exc)
        except Exception as exc:
            print("Create hole callout failed:", exc)
        return False

    def _insert_center_mark(self, drawing_model: Any, entity: Any) -> bool:
        try:
            drawing_model.ClearSelection2(True)
            if not self._select_annotation_entity(entity, drawing_model, None):
                return False
            for method_name, args in (
                ("InsertCenterMark", ()),
                ("InsertCenterMark2", (True,)),
            ):
                try:
                    method = getattr(drawing_model, method_name)
                    mark = method(*args) if callable(method) else None
                    print(f"{method_name} result:", mark)
                    if mark is not None:
                        return True
                except Exception as exc:
                    print(f"{method_name} failed:", exc)
        except Exception as exc:
            print("Insert center mark failed:", exc)
        return False

    def _insert_center_line(self, drawing_model: Any, lines: list[Any]) -> bool:
        if len(lines) < 2:
            return False
        try:
            drawing_model.ClearSelection2(True)
            selected = 0
            for line in lines[:2]:
                if self._select_annotation_entity(line, drawing_model, None, append=selected > 0):
                    selected += 1
            if selected < 2:
                return False
            for method_name, args in (
                ("InsertCenterLine", ()),
                ("InsertCenterLine2", ()),
            ):
                try:
                    method = getattr(drawing_model, method_name)
                    center_line = method(*args) if callable(method) else None
                    print(f"{method_name} result:", center_line)
                    if center_line is not None:
                        return True
                except Exception as exc:
                    print(f"{method_name} failed:", exc)
        except Exception as exc:
            print("Insert center line failed:", exc)
        return False

    def _select_annotation_entity(
        self,
        entity: Any,
        drawing_model: Any | None = None,
        view: Any | None = None,
        append: bool = False,
    ) -> bool:
        select_data = self._create_view_select_data(drawing_model, view)
        for method_name, args in (
            ("Select4", (append, select_data)),
            ("Select2", (append, 0)),
            ("Select", (append,)),
        ):
            try:
                method = getattr(entity, method_name)
                selected = method(*args) if callable(method) else False
                print(f"Select annotation entity by {method_name}:", selected)
                if selected:
                    return True
            except Exception as exc:
                print(f"Select annotation entity by {method_name} failed:", exc)
        return False

    def _create_view_select_data(self, drawing_model: Any | None, view: Any | None) -> Any | None:
        if drawing_model is None or view is None:
            return None
        try:
            selection_manager = drawing_model.SelectionManager
            if callable(selection_manager):
                selection_manager = selection_manager()
            select_data = selection_manager.CreateSelectData()
            try:
                select_data.View = view
            except Exception as exc:
                print("Set SelectData.View failed:", exc)
            return select_data
        except Exception as exc:
            print("Create view select data failed:", exc)
            return None

    def _dimension_point(self, view: Any, offset_index: int) -> tuple[float, float]:
        try:
            left, bottom, right, top = self._get_view_outline(view)
            step = 0.012 * (offset_index + 1)
            return right + step, top + step
        except Exception:
            step = 0.012 * (offset_index + 1)
            return 0.20 + step, 0.20 + step

    def _annotation_key(self, entity: Any, kind: str) -> str:
        return f"{kind}:{self._read_raw_com_value(entity)}"

    def _get_drawing_views(self, drawing_model: Any, verbose: bool = True) -> list[Any]:
        views: list[Any] = []
        try:
            sheet_view = self._read_com_object(drawing_model.GetFirstView)
            view = self._read_com_object(sheet_view.GetNextView) if sheet_view else None
            while view:
                views.append(view)
                view = self._read_com_object(view.GetNextView)
        except Exception as exc:
            if verbose:
                import traceback

                print("Get drawing views failed:", exc)
                traceback.print_exc()
        return views

    @staticmethod
    def _read_com_object(value: Any) -> Any | None:
        try:
            if hasattr(value, "_oleobj_"):
                return value
            if callable(value):
                value = value()
            if hasattr(value, "_oleobj_"):
                return value
            if value in (None, "", "N/A"):
                return None
            if isinstance(value, str):
                return None
            return value
        except Exception:
            return None

    def _select_drawing_view(self, view: Any) -> bool:
        for method_name, args in (
            ("SelectEntity", (False,)),
            ("Select", (False,)),
            ("Select2", (False, 0)),
        ):
            try:
                method = getattr(view, method_name)
                result = method(*args) if callable(method) else False
                print(f"Select view by {method_name}:", result)
                if result:
                    return True
            except Exception as exc:
                print(f"Select view by {method_name} failed:", exc)
        return False

    def _activate_drawing_view(self, drawing_model: Any, view_name: str) -> bool:
        try:
            result = drawing_model.ActivateView(view_name)
            print("Activate drawing view:", view_name, result)
            return bool(result)
        except Exception as exc:
            print("Activate drawing view failed:", view_name, exc)
            return False

    def _get_view_name(self, view: Any) -> str:
        try:
            return str(self._read_com_value(view.GetName2) or "N/A")
        except Exception:
            return "N/A"

    @staticmethod
    def _annotation_result_count(result: Any) -> int:
        try:
            if result is None or result is False:
                return 0
            if result is True:
                return 1
            if isinstance(result, (int, float)):
                return int(result) if result > 0 else 0
            if isinstance(result, (list, tuple)):
                return len(result)
            return len(list(result))
        except Exception:
            return 1

    def _insert_one_drawing_view(
        self,
        drawing: Any,
        model_path: str,
        view_name: str,
        candidate_names: list[str],
        x: float,
        y: float,
    ) -> Any | None:
        import traceback

        print(f"Insert View {view_name}: start x={x}, y={y}")
        for candidate_name in candidate_names:
            try:
                print(f"Insert View {view_name}: trying {candidate_name}")
                view = drawing.CreateDrawViewFromModelView3(
                    model_path,
                    candidate_name,
                    x,
                    y,
                    0.0,
                )
                print(f"Insert View {view_name}: result {view}")
                if view is not None:
                    print(f"Insert View {view_name}: success with {candidate_name}")
                    self._use_sheet_scale(view, view_name)
                    self._print_view_debug(view, view_name)
                    return view
            except Exception as exc:
                print(
                    f"Insert View {view_name}: failed with {candidate_name}: {exc}"
                )
                traceback.print_exc()
        print(f"Insert View {view_name}: failed")
        return None

    def _normalize_view_scale(self, view: Any, view_name: str) -> None:
        print(f"Scale normalization disabled for {view_name}.")

    def _use_sheet_scale(self, view: Any, view_name: str) -> None:
        try:
            view.UseSheetScale = True
            print(f"UseSheetScale {view_name}: True")
        except Exception as exc:
            print(f"UseSheetScale {view_name} failed:", exc)

    def _print_view_debug(self, view: Any, view_name: str) -> None:
        print("view name:", self._read_com_value(view.GetName2))
        print("view UseSheetScale:", self._read_raw_com_value(view.UseSheetScale))
        print("view scale decimal:", self._get_view_scale(view))
        print("view position:", self._read_raw_com_value(view.Position))

    def _auto_layout_standard_views(
        self,
        drawing: Any,
        views: dict[str, Any],
        sheet_width: float,
        sheet_height: float,
        sheet_name: str,
        model_box: dict[str, Any] | None = None,
        gap_mm: float = 30.0,
    ) -> dict[str, Any]:
        import traceback

        try:
            gap = gap_mm / 1000.0
            area = self._drawing_effective_area(sheet_width, sheet_height)
            area_left, area_bottom, area_right, area_top = area
            print(
                f"Sheet Size: {sheet_name} width={sheet_width:.4f}m "
                f"height={sheet_height:.4f}m"
            )
            print("Effective drawing area:", area)
            print("Model bounding box:", model_box)

            outlines = self._get_view_outlines(views)
            print("Current Scale:", self._get_view_scale(next(iter(views.values()))))
            for name, outline in outlines.items():
                print(f"Bounding Box {name}: {outline}")

            scale_factor = self._calculate_layout_scale_for_area(outlines, area, gap)
            current_scale = self._get_view_scale(next(iter(views.values())))
            target_scale = max(current_scale * scale_factor, 0.05)
            print("Layout Scale Factor:", scale_factor)
            print("Target orthographic scale:", target_scale)
            for name, view in views.items():
                view_scale = target_scale * 0.70 if name == "Isometric" else target_scale
                self._set_view_scale(view, view_scale, name)

            self._rebuild_and_zoom(drawing)
            outlines = self._get_view_outlines(views)
            for name, outline in outlines.items():
                print(f"Scaled Bounding Box {name}: {outline}")

            positions = self._calculate_view_positions(
                outlines,
                area_left,
                area_bottom,
                area_right,
                area_top,
                gap,
            )
            for name, position in positions.items():
                print(f"Final Position {name}: {position}")
                self._set_view_position(views[name], position)

            self._rebuild_and_zoom(drawing)
            final_outlines = self._get_view_outlines(views)
            overlap = self._has_overlap(final_outlines)
            for name, outline in final_outlines.items():
                print(f"Final Bounding Box {name}: {outline}")
            print("Has Overlap:", overlap)
            return {
                "sheet": {"name": sheet_name, "width": sheet_width, "height": sheet_height},
                "scale_factor": scale_factor,
                "target_scale": target_scale,
                "effective_area": area,
                "model_box": model_box,
                "positions": positions,
                "overlap": overlap,
                "inside_effective_area": self._outlines_inside_area(final_outlines, area),
            }
        except Exception as exc:
            traceback.print_exc()
            return {"error": str(exc)}

    def _get_sheet_size(self, drawing: Any) -> tuple[float, float, str]:
        try:
            sheet = drawing.GetCurrentSheet
            sheet_name = self._read_com_value(sheet.GetName)
            props = sheet.GetProperties
            values = list(props)
            width = float(values[-2])
            height = float(values[-1])
            format_name = self._sheet_format_name(width, height)
            return width, height, f"{format_name} {sheet_name}"
        except Exception:
            print("Sheet size read failed; fallback to A0 landscape.")
            return 1.189, 0.841, "A0 fallback"

    def _drawing_effective_area(self, sheet_width: float, sheet_height: float) -> tuple[float, float, float, float]:
        short_side = min(sheet_width, sheet_height)
        margin = max(short_side * 0.045, 0.025)
        title_block_reserve = max(short_side * 0.075, 0.045)
        left = margin
        bottom = margin + title_block_reserve
        right = sheet_width - margin
        top = sheet_height - margin
        if right <= left or top <= bottom:
            return 0.03, 0.07, sheet_width - 0.03, sheet_height - 0.03
        return left, bottom, right, top

    def _get_model_bounding_box(self, model: Any) -> dict[str, Any]:
        for method_name, args in (
            ("GetPartBox", (True,)),
            ("GetBox", (False,)),
            ("GetBox", ()),
        ):
            try:
                method = getattr(model, method_name)
                values = method(*args) if callable(method) else method
                values = [float(value) for value in list(values)]
                if len(values) >= 6:
                    xmin, ymin, zmin, xmax, ymax, zmax = values[:6]
                    return {
                        "source": method_name,
                        "min": [xmin, ymin, zmin],
                        "max": [xmax, ymax, zmax],
                        "size": [abs(xmax - xmin), abs(ymax - ymin), abs(zmax - zmin)],
                    }
            except Exception as exc:
                print(f"Read model bounding box by {method_name} failed:", exc)
        return {"source": "fallback", "min": [0, 0, 0], "max": [0.1, 0.08, 0.02], "size": [0.1, 0.08, 0.02]}

    @staticmethod
    def _sheet_format_name(width: float, height: float) -> str:
        long_side = max(width, height)
        short_side = min(width, height)
        formats = {
            "A4": (0.297, 0.210),
            "A3": (0.420, 0.297),
            "A2": (0.594, 0.420),
            "A1": (0.841, 0.594),
            "A0": (1.189, 0.841),
        }
        for name, (format_long, format_short) in formats.items():
            if abs(long_side - format_long) < 0.01 and abs(short_side - format_short) < 0.01:
                return name
        return "Custom"

    def _get_view_outlines(self, views: dict[str, Any]) -> dict[str, tuple[float, float, float, float]]:
        return {name: self._get_view_outline(view) for name, view in views.items()}

    @staticmethod
    def _get_view_outline(view: Any) -> tuple[float, float, float, float]:
        outline = view.GetOutline
        if callable(outline):
            outline = outline()
        values = [float(value) for value in outline]
        return values[0], values[1], values[2], values[3]

    @staticmethod
    def _outline_size(outline: tuple[float, float, float, float]) -> tuple[float, float]:
        return outline[2] - outline[0], outline[3] - outline[1]

    def _calculate_layout_scale(
        self,
        outlines: dict[str, tuple[float, float, float, float]],
        sheet_width: float,
        sheet_height: float,
        margin: float,
        gap: float,
    ) -> float:
        total_w, total_h = self._layout_required_size(outlines, gap)
        usable_w = max(sheet_width - 2 * margin, 0.01)
        usable_h = max(sheet_height - 2 * margin, 0.01)
        fit_factor = min(usable_w / total_w, usable_h / total_h)
        if fit_factor < 1.0:
            return max(fit_factor * 0.95, 0.1)
        target_factor = min(fit_factor * 0.80, 2.0)
        return max(target_factor, 1.0)

    def _calculate_layout_scale_for_area(
        self,
        outlines: dict[str, tuple[float, float, float, float]],
        area: tuple[float, float, float, float],
        gap: float,
    ) -> float:
        left, bottom, right, top = area
        total_w, total_h = self._layout_required_size(outlines, gap)
        usable_w = max(right - left, 0.01)
        usable_h = max(top - bottom, 0.01)
        fit_factor = min(usable_w / max(total_w, 0.01), usable_h / max(total_h, 0.01))
        return min(max(fit_factor * 0.82, 0.08), 1.0)

    def _layout_required_size(
        self,
        outlines: dict[str, tuple[float, float, float, float]],
        gap: float,
    ) -> tuple[float, float]:
        widths = {name: self._outline_size(outline)[0] for name, outline in outlines.items()}
        heights = {name: self._outline_size(outline)[1] for name, outline in outlines.items()}
        left_w = max(widths["Front"], widths["Top"])
        right_w = max(widths["Right"], widths["Isometric"])
        bottom_h = max(heights["Front"], heights["Right"])
        top_h = max(heights["Top"], heights["Isometric"])
        return left_w + gap + right_w, bottom_h + gap + top_h

    def _calculate_view_positions(
        self,
        outlines: dict[str, tuple[float, float, float, float]],
        area_left: float,
        area_bottom: float,
        area_right: float,
        area_top: float,
        gap: float,
    ) -> dict[str, tuple[float, float]]:
        widths = {name: self._outline_size(outline)[0] for name, outline in outlines.items()}
        heights = {name: self._outline_size(outline)[1] for name, outline in outlines.items()}
        left_w = max(widths["Front"], widths["Top"])
        right_w = max(widths["Right"], widths["Isometric"])
        bottom_h = max(heights["Front"], heights["Right"])
        top_h = max(heights["Top"], heights["Isometric"])
        total_w = left_w + gap + right_w
        total_h = bottom_h + gap + top_h
        usable_w = area_right - area_left
        usable_h = area_top - area_bottom
        origin_x = area_left + max((usable_w - total_w) / 2.0, 0.0)
        origin_y = area_bottom + max((usable_h - total_h) / 2.0, 0.0)
        left_x = origin_x + left_w / 2.0
        right_x = origin_x + left_w + gap + right_w / 2.0
        bottom_y = origin_y + bottom_h / 2.0
        top_y = origin_y + bottom_h + gap + top_h / 2.0
        return {
            "Front": (left_x, bottom_y),
            "Top": (left_x, top_y),
            "Right": (right_x, bottom_y),
            "Isometric": (right_x, top_y),
        }

    @staticmethod
    def _set_view_position(view: Any, position: tuple[float, float]) -> None:
        try:
            view.Position = VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_R8,
                [float(position[0]), float(position[1])],
            )
        except Exception:
            view.SetPosition(float(position[0]), float(position[1]))

    def _scale_views(self, views: dict[str, Any], factor: float) -> None:
        print("Scale view operation disabled; keeping original view sizes.")

    def _set_view_scale(self, view: Any, scale: float, view_name: str) -> None:
        try:
            view.UseSheetScale = False
        except Exception as exc:
            print(f"Disable sheet scale {view_name} failed:", exc)
        for attr_name in ("ScaleDecimal", "ScaleRatio"):
            try:
                setattr(view, attr_name, float(scale))
                print(f"Set {view_name} {attr_name}: {scale}")
                return
            except Exception as exc:
                print(f"Set {view_name} {attr_name} failed:", exc)
        try:
            view.SetScaleDecimal(float(scale))
            print(f"Set {view_name} scale by SetScaleDecimal: {scale}")
        except Exception as exc:
            print(f"Set view scale failed for {view_name}:", exc)

    @staticmethod
    def _outlines_inside_area(
        outlines: dict[str, tuple[float, float, float, float]],
        area: tuple[float, float, float, float],
    ) -> bool:
        left, bottom, right, top = area
        for outline in outlines.values():
            if outline[0] < left or outline[1] < bottom or outline[2] > right or outline[3] > top:
                return False
        return True

    @staticmethod
    def _get_view_scale(view: Any) -> float:
        try:
            scale = view.ScaleDecimal
            if callable(scale):
                scale = scale()
            return float(scale)
        except Exception:
            return 1.0

    @staticmethod
    def _has_overlap(outlines: dict[str, tuple[float, float, float, float]]) -> bool:
        names = list(outlines)
        for index, left_name in enumerate(names):
            for right_name in names[index + 1 :]:
                a = outlines[left_name]
                b = outlines[right_name]
                separated = a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
                if not separated:
                    return True
        return False

    def _rebuild_and_zoom(self, drawing: Any) -> None:
        try:
            drawing.ClearSelection2(True)
        except Exception:
            pass
        try:
            drawing.GraphicsRedraw2()
        except Exception as exc:
            print("Light redraw failed:", exc)
        try:
            drawing.ForceRebuild3(False)
        except Exception as exc:
            print("Light rebuild failed:", exc)

    def fit_drawing_sheet_full(self, sw_app: Any, drawing_model: Any) -> None:
        print("ZOOM_TO_FIT_COMMAND_ID =", ZOOM_TO_FIT_COMMAND_ID)
        try:
            title = self._read_com_value(drawing_model.GetTitle)
            errors = self._byref_i4()
            active = sw_app.ActivateDoc3(title, False, 0, errors)
            print("fit ActivateDoc3:", active)
            self._pump(0.2)
        except Exception as exc:
            print("ActivateDoc3 failed:", exc)

        try:
            sw_app.FrameState = 1
            print("fit FrameState: success")
            self._pump(0.2)
        except Exception as exc:
            print("FrameState failed:", exc)

        try:
            drawing_model.ClearSelection2(True)
            print("fit ClearSelection2: success")
            self._pump(0.1)
        except Exception as exc:
            print("ClearSelection2 failed:", exc)

        try:
            drawing_model.GraphicsRedraw2()
            print("fit GraphicsRedraw2: success")
            self._pump(0.2)
        except Exception as exc:
            print("GraphicsRedraw2 failed:", exc)

        try:
            edit_rebuild = drawing_model.EditRebuild3
            if callable(edit_rebuild):
                edit_rebuild()
            print("fit EditRebuild3: success")
            self._pump(0.2)
        except Exception as exc:
            print("EditRebuild3 failed:", exc)

        try:
            drawing_model.ForceRebuild3(False)
            print("fit ForceRebuild3: success")
            self._pump(0.2)
        except Exception as exc:
            print("ForceRebuild3 failed:", exc)

        try:
            drawing_model.ShowNamedView2("*Front", 1)
            print("fit ShowNamedView2 *Front: success")
            self._pump(0.2)
        except Exception as exc:
            print("ShowNamedView2 *Front failed:", exc)

        for index in range(3):
            try:
                drawing_model.ViewZoomtofit2()
                print("fit ViewZoomtofit2: success", index)
                self._pump(0.3)
            except Exception as exc:
                print("ViewZoomtofit2 failed:", index, exc)

        try:
            if ZOOM_TO_FIT_COMMAND_ID is not None:
                result = sw_app.RunCommand(ZOOM_TO_FIT_COMMAND_ID, "")
                print("fit RunCommand ZoomToFit:", result)
                self._pump(0.2)
        except Exception as exc:
            print("RunCommand ZoomToFit failed:", exc)

        try:
            drawing_model.GraphicsRedraw2()
            print("fit final GraphicsRedraw2: success")
            self._pump(0.2)
        except Exception as exc:
            print("final GraphicsRedraw2 failed:", exc)

    @staticmethod
    def _pump(delay: float = 0.2) -> None:
        try:
            pythoncom.PumpWaitingMessages()
        except Exception:
            pass
        time.sleep(delay)


SolidWorksConnector = SWConnector
