from django.db import migrations


SQL = """
UPDATE products
SET search_vector =
    setweight(to_tsvector('simple', coalesce(name, '')), 'A') ||
    setweight(to_tsvector('simple', coalesce(description, '')), 'B');

CREATE OR REPLACE FUNCTION products_search_vector_update() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('simple', coalesce(NEW.name, '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(NEW.description, '')), 'B');
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS products_search_vector_trigger ON products;
CREATE TRIGGER products_search_vector_trigger
BEFORE INSERT OR UPDATE OF name, description ON products
FOR EACH ROW EXECUTE FUNCTION products_search_vector_update();
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS products_search_vector_trigger ON products;
DROP FUNCTION IF EXISTS products_search_vector_update();
"""


class Migration(migrations.Migration):
    dependencies = [("inventario", "0011_category_allowed_attributes")]
    operations = [migrations.RunSQL(SQL, REVERSE_SQL)]
