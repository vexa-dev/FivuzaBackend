from django.db import migrations


SQL = """
UPDATE customers
SET search_vector =
    setweight(to_tsvector('simple', coalesce(name, '')), 'A') ||
    setweight(to_tsvector('simple', coalesce(document_number, '')), 'A') ||
    setweight(to_tsvector('simple', coalesce(phone, '')), 'B') ||
    setweight(to_tsvector('simple', coalesce(address, '')), 'C');

CREATE OR REPLACE FUNCTION customers_search_vector_update() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('simple', coalesce(NEW.name, '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(NEW.document_number, '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(NEW.phone, '')), 'B') ||
        setweight(to_tsvector('simple', coalesce(NEW.address, '')), 'C');
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS customers_search_vector_trigger ON customers;
CREATE TRIGGER customers_search_vector_trigger
BEFORE INSERT OR UPDATE OF name, document_number, phone, address ON customers
FOR EACH ROW EXECUTE FUNCTION customers_search_vector_update();
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS customers_search_vector_trigger ON customers;
DROP FUNCTION IF EXISTS customers_search_vector_update();
"""


class Migration(migrations.Migration):
    dependencies = [("ventas", "0006_sprint34_indexes")]
    operations = [migrations.RunSQL(SQL, REVERSE_SQL)]
