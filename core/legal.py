"""Contenido legal versionado (Sprint 33, Ley N 29733): Terminos y
Condiciones y Politica de Privacidad. Vive como constantes en vez de en la
BDD porque cambia por decision legal/comercial, no por accion de un
usuario -el versionado es la fecha del texto vigente, y se registra cuando
un tenant lo acepta (Tenant.terms_version_accepted) para poder demostrar,
ante una auditoria, exactamente que version acepto cada negocio.

[HUECO] El texto de abajo es un borrador razonable para operar en Peru
bajo la Ley N 29733, no una revision legal formal -antes del lanzamiento
comercial real, un abogado debe revisarlo y, si corresponde, reemplazarlo."""

TERMS_VERSION = "2027-03-01"

TERMS_TEXT = """TERMINOS Y CONDICIONES DE USO DE FIVUZA
Version vigente: 2027-03-01

1. OBJETO
Fivuza es un software de gestion (ERP) multi-negocio ofrecido como servicio
(SaaS) por Fivuza S.A.C. Al registrar un negocio ("el Cliente") en la
plataforma, el Cliente acepta estos Terminos y la Politica de Privacidad
vigente.

2. USO DEL SERVICIO
El Cliente es responsable de la veracidad de los datos que registra, de
mantener la confidencialidad de las credenciales de sus usuarios, y de
usar la plataforma conforme a la legislacion peruana aplicable.

3. DATOS DEL CLIENTE
Los datos que el Cliente registra en su cuenta (catalogo, ventas, clientes,
empleados) son de su propiedad. Fivuza los trata unicamente para prestar
el servicio contratado, conforme a la Politica de Privacidad.

4. SUSCRIPCION Y PAGO
El acceso al servicio esta sujeto al pago puntual de la suscripcion segun
el plan contratado. La falta de pago puede resultar en la suspension del
acceso, conforme al ciclo de gracia documentado en la plataforma.

5. CANCELACION Y RETENCION DE DATOS
El Cliente puede cancelar su suscripcion en cualquier momento. Tras la
cancelacion, sus datos permanecen disponibles en modo de solo lectura
durante un periodo de gracia (actualmente 30 dias), pasado el cual seran
eliminados de forma permanente e irreversible.

6. LIMITACION DE RESPONSABILIDAD
Fivuza presta el servicio "tal cual" y no garantiza disponibilidad
ininterrumpida. El Cliente es responsable de mantener sus propios respaldos
adicionales de informacion critica.

7. MODIFICACIONES
Estos Terminos pueden actualizarse; la version vigente en cada momento es
la publicada en esta misma seccion de la plataforma."""

PRIVACY_TEXT = """POLITICA DE PRIVACIDAD DE FIVUZA
Version vigente: 2027-03-01

Fivuza S.A.C. trata datos personales conforme a la Ley N 29733 (Ley de
Proteccion de Datos Personales del Peru) y su reglamento.

1. DATOS QUE TRATAMOS
Datos de las personas que operan la cuenta del Cliente (usuarios del
negocio) y, dentro de cada cuenta, los datos personales que el propio
Cliente registra sobre sus clientes finales y empleados (nombre, documento
de identidad, telefono, direccion, historial de compras/asistencia).

2. BASE LEGAL DEL TRATAMIENTO
El tratamiento se realiza para la ejecucion del contrato de servicio SaaS
entre Fivuza y el Cliente. Fivuza actua como encargado del tratamiento
respecto de los datos que el Cliente ingresa sobre terceros (sus propios
clientes y empleados); el Cliente es el titular/responsable de ese
tratamiento frente a esos terceros.

3. RETENCION
Los datos se conservan mientras la cuenta este activa, y durante el
periodo de gracia posterior a una cancelacion (30 dias), tras el cual se
eliminan de forma permanente.

4. DERECHOS ARCO
Cualquier persona cuyos datos personales trate Fivuza (directamente, como
usuario de una cuenta, o indirectamente, como cliente/empleado de un
negocio que usa Fivuza) puede ejercer sus derechos de Acceso,
Rectificacion, Cancelacion y Oposicion escribiendo a
privacidad@fivuza.com, o -para los usuarios de una cuenta- desde la
seccion "Mis datos" de la plataforma, que permite exportar y solicitar la
anonimizacion de los datos personales propios.

5. SEGURIDAD
Los datos se almacenan cifrados en transito (TLS) y en reposo, con acceso
restringido por rol dentro de Fivuza."""


def get_legal_document(document: str) -> dict:
    documents = {"terms": TERMS_TEXT, "privacy": PRIVACY_TEXT}
    if document not in documents:
        raise ValueError(f"Documento legal desconocido: {document}")
    return {"version": TERMS_VERSION, "content": documents[document]}
