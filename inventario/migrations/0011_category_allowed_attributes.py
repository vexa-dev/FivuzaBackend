from django.db import migrations, models


def populate_allowed_attributes(apps, schema_editor):
    Category = apps.get_model("inventario", "Category")
    VariantAttributeValue = apps.get_model("inventario", "VariantAttributeValue")
    through = Category.allowed_attributes.through

    relations = set(
        VariantAttributeValue.objects.values_list(
            "variant__product__category_id",
            "attribute_value__attribute_id",
        ).distinct()
    )
    relations.update(
        Category.objects.exclude(primary_attribute_id=None).values_list(
            "id", "primary_attribute_id"
        )
    )

    through.objects.bulk_create(
        [
            through(category_id=category_id, attribute_id=attribute_id)
            for category_id, attribute_id in relations
        ],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [("inventario", "0010_remove_attribute_is_primary_and_more")]

    operations = [
        migrations.AddField(
            model_name="category",
            name="allowed_attributes",
            field=models.ManyToManyField(
                blank=True,
                related_name="categories",
                to="inventario.attribute",
            ),
        ),
        migrations.RunPython(populate_allowed_attributes, migrations.RunPython.noop),
    ]
