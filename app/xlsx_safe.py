"""Utilidad única para libros Excel generados por la app."""
from typing import Any

_FORMULA_PREFIXES = ("=", "+", "-", "@")


def neutralize_formulas(wb: Any) -> None:
    """
    Excel interpreta como fórmula el texto que empieza con '=', '+', '-' o '@'. Los textos
    ingresados por usuarios (nombres, notas, descripciones) pasan por acá antes de exportarse
    para evitar inyección de fórmulas (CSV/XLSX injection). Se antepone un apóstrofo.
    """
    for sheet in wb.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.lstrip().startswith(_FORMULA_PREFIXES):
                    cell.value = "'" + cell.value
