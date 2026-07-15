import psycopg2
try:
    psycopg2.connect('postgres://fivuza:password@localhost:5432/fivuza_db')
except Exception as e:
    import traceback
    traceback.print_exc()
