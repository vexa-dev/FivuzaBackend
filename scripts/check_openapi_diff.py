"""Diff de compatibilidad del schema OpenAPI entre dos archivos generados
por drf-spectacular (Sprint 32, API Spec §1.1.1).

Compara components.schemas de un schema "base" (main) contra uno "nuevo"
(la rama del PR) y falla si encuentra un cambio incompatible en /v1/:
- un schema completo desaparecido (endpoint/serializer eliminado),
- una propiedad eliminada de un schema que seguia existiendo,
- el tipo (o $ref) de una propiedad que cambio.

Agregar un schema o una propiedad nueva NUNCA es incompatible -eso es
crecimiento aditivo de la API, no una promesa rota.

Uso: python scripts/check_openapi_diff.py schema_main.yml schema_pr.yml [allowlist.yml]
"""

import sys

import yaml


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _property_signature(prop: dict) -> tuple:
    return (prop.get("type"), prop.get("format"), prop.get("$ref"))


def find_breaking_changes(base: dict, new: dict) -> list[str]:
    breaking = []
    base_schemas = base.get("components", {}).get("schemas", {})
    new_schemas = new.get("components", {}).get("schemas", {})

    for schema_name, base_schema in base_schemas.items():
        if schema_name not in new_schemas:
            breaking.append(f"Schema eliminado: {schema_name}")
            continue

        new_schema = new_schemas[schema_name]
        base_properties = base_schema.get("properties", {})
        new_properties = new_schema.get("properties", {})

        for prop_name, base_prop in base_properties.items():
            if prop_name not in new_properties:
                breaking.append(
                    f"{schema_name}.{prop_name}: campo eliminado o renombrado"
                )
                continue

            new_prop = new_properties[prop_name]
            if _property_signature(base_prop) != _property_signature(new_prop):
                breaking.append(
                    f"{schema_name}.{prop_name}: tipo cambiado "
                    f"({_property_signature(base_prop)} -> {_property_signature(new_prop)})"
                )

    return breaking


def load_allowlist(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    data = _load(path) or {}
    entries = data.get("allowed_breaking_changes", [])
    return {
        entry["change"]: entry.get("reason", "Sin justificacion documentada.")
        for entry in entries
    }


def main() -> int:
    if len(sys.argv) not in {3, 4}:
        print(
            "Uso: python scripts/check_openapi_diff.py <schema_base.yml> "
            "<schema_nuevo.yml> [allowlist.yml]"
        )
        return 2

    base = _load(sys.argv[1])
    new = _load(sys.argv[2])
    breaking = find_breaking_changes(base, new)
    allowlist = load_allowlist(sys.argv[3] if len(sys.argv) == 4 else None)
    approved = [change for change in breaking if change in allowlist]
    unapproved = [change for change in breaking if change not in allowlist]

    if approved:
        print("Cambios incompatibles aprobados explicitamente:")
        for change in approved:
            print(f"  - {change}: {allowlist[change]}")

    if not unapproved:
        print("Sin cambios incompatibles en el schema OpenAPI.")
        return 0

    print("Cambios incompatibles detectados en /v1/ respecto a main:")
    for line in unapproved:
        print(f"  - {line}")
    print(
        "\nSi el cambio es intencional (ej. un campo verdaderamente obsoleto), "
        "coordinarlo con el equipo antes de mergear -romper /v1/ afecta a "
        "integraciones externas y a la app movil/PWA en produccion."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
