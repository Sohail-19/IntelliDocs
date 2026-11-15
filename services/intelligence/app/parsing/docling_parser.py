"""Docling layout adapter requiring pre-provisioned, local model artifacts."""

import os
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path
from typing import Any

from intellidocs_intelligence.models import Page, ParseResult, TableBlock, TextBlock

from .base import DocumentParser, bounded_bbox, dependency, result_for
from .languages import language_codes


class DoclingParser(DocumentParser):
    def __init__(self, artifacts_path: Path | None = None) -> None:
        self.artifacts_path = artifacts_path

    def _converter(self) -> Any:
        if self.artifacts_path is None or not self.artifacts_path.is_dir():
            raise RuntimeError(
                "Docling requires a local DOCLING_ARTIFACTS_PATH; no downloads allowed"
            )
        # Set before importing model libraries. Missing artifacts must fail locally.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        options_module = dependency("docling.datamodel.pipeline_options", "docling")
        base_module = dependency("docling.datamodel.base_models", "docling")
        converter_module = dependency("docling.document_converter", "docling")
        options = options_module.PdfPipelineOptions(
            artifacts_path=self.artifacts_path,
            do_ocr=False,
            enable_remote_services=False,
            allow_external_plugins=False,
        )
        return converter_module.DocumentConverter(
            format_options={
                base_module.InputFormat.PDF: converter_module.PdfFormatOption(
                    pipeline_options=options
                ),
            }
        )

    def parse(self, file_path: Path, languages: Sequence[str]) -> ParseResult:
        language_codes(languages)
        document = self._converter().convert(file_path).document
        blocks: dict[int, list[TableBlock | TextBlock]] = {
            int(number): [] for number in document.pages
        }
        warnings: list[str] = []
        labels = {
            "section_header": "heading",
            "title": "heading",
            "list_item": "list_item",
            "page_header": "header_footer",
            "page_footer": "header_footer",
            "text": "paragraph",
            "paragraph": "paragraph",
            "caption": "paragraph",
            "footnote": "paragraph",
            "formula": "paragraph",
            "code": "paragraph",
        }
        for item, _level in document.iterate_items():
            label = str(item.label.value)
            for provenance in getattr(item, "prov", []):
                number = int(provenance.page_no)
                page_size = document.pages[number].size
                width, height = float(page_size.width), float(page_size.height)
                box = provenance.bbox
                top, bottom = float(box.t), float(box.b)
                if str(box.coord_origin).upper().endswith("BOTTOMLEFT"):
                    top, bottom = height - top, height - bottom
                bbox = bounded_bbox([float(box.l), top, float(box.r), bottom], width, height)
                order = len(blocks[number])
                if label == "table":
                    data = item.data
                    rows = [["" for _ in range(data.num_cols)] for _ in range(data.num_rows)]
                    for cell in data.table_cells:
                        for row in range(cell.start_row_offset_idx, cell.end_row_offset_idx):
                            for column in range(cell.start_col_offset_idx, cell.end_col_offset_idx):
                                rows[row][column] = cell.text
                    blocks[number].append(
                        TableBlock(
                            text="",
                            bbox=bbox,
                            reading_order=order,
                            rows=rows,
                        )
                    )
                elif label in labels:
                    text = item.text
                    marker = getattr(item, "marker", "")
                    if label == "list_item" and marker and not text.lstrip().startswith(marker):
                        text = f"{marker} {text}"
                    blocks[number].append(
                        TextBlock.model_validate(
                            {
                                "type": labels[label],
                                "text": text,
                                "bbox": bbox,
                                "reading_order": order,
                                "heading_level": (
                                    getattr(item, "level", None)
                                    if labels[label] == "heading"
                                    else None
                                ),
                            }
                        )
                    )
                else:
                    warnings.append(f"Page {number}: omitted unsupported layout label {label!r}")
        pages = [
            Page(
                page=number,
                language=languages[0],
                blocks=page_blocks,
            )
            for number, page_blocks in sorted(blocks.items())
        ]
        return result_for(
            file_path,
            pages,
            "docling",
            version("docling"),
            warnings,
            sizes={
                int(number): (float(page.size.width), float(page.size.height))
                for number, page in document.pages.items()
            },
            languages=languages,
        )
