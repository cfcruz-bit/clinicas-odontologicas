"""Pruebas de Clínicas Odontológicas. Borran todo lo que haya en la base, así que usan su propia rama de Neon.

Ejecutar:  PRUEBAS_DATABASE_URL=<cadena de la rama de pruebas> python -m pruebas.sistema   (desde la raíz del repositorio)
"""
import io
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal

import psycopg
from PIL import Image
from psycopg.rows import dict_row

# Nunca se toma DATABASE_URL: las pruebas no deben tocar la base de verdad
os.environ["DATABASE_URL"] = os.environ["PRUEBAS_DATABASE_URL"]
# Ni el almacenamiento de fotos de verdad: las pruebas las guardan en una carpeta temporal
os.environ["ALMACEN_CARPETA"] = tempfile.mkdtemp(prefix="clinicas-fotos-")
for variable in ("ALMACEN_BUCKET", "FLY_APP_NAME"):
    os.environ.pop(variable, None)
import prototipo as m  # noqa: E402

base = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row, prepare_threshold=None)
base.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
m.crear_tablas()

def uno(consulta, *args):
    return base.execute(consulta, args or None).fetchone()

def cuantos(tabla):
    return uno(f"SELECT count(*) AS n FROM {tabla}")["n"]

fallos = []

def caso(descripcion, condicion):
    print(("  OK     " if condicion else "  FALLO  ") + descripcion)
    if not condicion:
        fallos.append(descripcion)

def cliente(cif):
    c = m.app.test_client()
    c.post("/", data={"cif": cif, "clave": "uam2026"})
    return c

def texto(r):
    return r.get_data(as_text=True)

def id_de(cif):
    return uno("SELECT id FROM cuentas WHERE cif = %s", cif)["id"]

def sala(nombre):
    return uno("SELECT id FROM salas WHERE nombre = %s", nombre)["id"]

def dia_habil(desde, dias):
    """La fecha a `dias` días de `desde`, corrida al lunes si cae domingo (la clínica no atiende domingos)."""
    d = desde + timedelta(days=dias)
    return d + timedelta(days=1) if d.isoweekday() == 7 else d

grace, gabriel, profe, admin = cliente("00000002"), cliente("00000005"), cliente("00000001"), cliente("00000007")
GRACE, GABRIEL, PROFE, ADMIN, GUSTAVO = (id_de(c) for c in ("00000002", "00000005", "00000001", "00000007", "00000004"))
ahora = datetime.now(m.MANAGUA)
HOY = ahora.date()
MANANA = dia_habil(HOY, 1).isoformat()        # el próximo día de atención
PASADO = dia_habil(date.fromisoformat(MANANA), 1).isoformat()
AYER = (HOY - timedelta(days=1)).isoformat()
MANANA_DE_VERDAD = (HOY + timedelta(days=1)).isoformat()
HOY_ATIENDE = HOY.isoweekday() != 7
MANANA_ATIENDE = MANANA == MANANA_DE_VERDAD
PASADA = "2020-01-06"  # un lunes que ya pasó

print("Clínicas Odontológicas - pruebas\n")
caso("Sin sesion la pagina de pacientes manda a la entrada",
     m.app.test_client().get("/pacientes").headers["Location"].endswith("/"))
caso("La política de privacidad se ve sin entrar (la pide Meta para WhatsApp)",
     "Política de privacidad" in texto(m.app.test_client().get("/privacidad")))
caso("La base tiene las 20 tablas del modelo v14, sus 3 vistas y las 2 tablas de extensión",
     uno("SELECT count(*) AS n FROM pg_tables WHERE schemaname = current_schema()")["n"] == 22
     and uno("SELECT count(*) AS n FROM pg_views WHERE schemaname = current_schema()")["n"] == 3)
caso("La clave se guarda cifrada, no en texto plano", uno("SELECT clave_hash FROM cuentas LIMIT 1")["clave_hash"] != "uam2026")
caso("Con una clave equivocada no se entra",
     m.app.test_client().post("/", data={"cif": "00000002", "clave": "otra"}).status_code == 200)
intento = lambda clave: texto(m.app.test_client().post("/", data={"cif": "00000004", "clave": clave}))  # noqa: E731
for _ in range(m.FALLOS_MAXIMOS - 1):
    intento("otra")
caso("Tras 5 claves equivocadas la cuenta se bloquea", "bloqueada 15 min" in intento("otra"))
caso("Bloqueada, ni con la clave correcta se entra", "Intente de nuevo en 15 min" in intento("uam2026"))
caso("El bloqueo queda en la bitacora",
     uno("SELECT 1 FROM bitacora WHERE cif_registrado = '00000004' AND accion LIKE 'Cuenta bloqueada%%'"))
base.execute("UPDATE cuentas SET bloqueada_hasta = now() - interval '1 second' WHERE cif = '00000004'")
caso("Pasado el bloqueo se entra con la clave correcta",
     m.app.test_client().post("/", data={"cif": "00000004", "clave": "uam2026"}).status_code == 302)
caso("El nombre del usuario sale en la cabecera, no solo el rol", "Grace Alvarado" in texto(grace.get("/menu")))
entrada = m.app.test_client().post("/", data={"cif": "00000002", "clave": "uam2026"})
caso("La sesion se cierra sola a los 15 minutos sin uso (RNF05)",
     m.app.permanent_session_lifetime.total_seconds() == 900 and "Expires=" in entrada.headers["Set-Cookie"])

# ---- Clases y códigos QR: el profesor crea su clase y cada estudiante entra con un código de un solo uso
caso("Sin clase, el estudiante ve el aviso de pedirle el código a su profesor",
     "Todavía no está en ninguna clase" in texto(grace.get("/menu")))
caso("El estudiante no ve las clases", grace.get("/clases").status_code == 302)
admin.post("/clases", data={"nombre": "De la Facultad", "periodo": "2026-II"})
profe.post("/clases", data={"nombre": "", "periodo": "2026-II"})
profe.post("/clases", data={"nombre": "Operatoria II", "periodo": "2026"})
caso("Solo el profesor crea clases, con nombre y un periodo como 2026-II", cuantos("clases") == 0)
creada = profe.post("/clases", data={"nombre": "Operatoria II", "periodo": "2026-II"})
KLASE = uno("SELECT id FROM clases")["id"]
caso("El profesor crea su clase y pasa a la página de sus códigos",
     creada.headers["Location"].endswith(f"/clases/{KLASE}") and uno("SELECT profesor_id FROM clases")["profesor_id"] == PROFE)
profe.post(f"/clases/{KLASE}", data={"cantidad": "0", "dias": "7"})
cliente("00000004").post(f"/clases/{KLASE}", data={"cantidad": "3", "dias": "7"})
caso("Solo el profesor de la clase genera sus códigos, y al menos uno", cuantos("invitaciones") == 0)
profe.post(f"/clases/{KLASE}", data={"cantidad": "4", "dias": "7"})
codigos = [r["codigo"] for r in base.execute("SELECT codigo FROM invitaciones ORDER BY id")]
pagina_clase = texto(profe.get(f"/clases/{KLASE}"))
caso("Cada código es un QR con la dirección pública del sistema, listo para imprimir",
     len(codigos) == 4 and pagina_clase.count('src="data:image/svg+xml;base64,') == 4
     and f"{m.SITIO}/registro/{codigos[0]}" in pagina_clase and "localhost" not in m.SITIO)
caso("Otro profesor no ve la clase", cliente("00000004").get(f"/clases/{KLASE}").status_code == 302)
caso("El estudiante con sesión abre el código y ve a qué clase entra",
     "Operatoria II" in texto(grace.get(f"/registro/{codigos[0]}")))
grace.post(f"/registro/{codigos[0]}", data={"accion": "unirse"})
caso("Al usar el código el estudiante queda en la clase y el código queda usado (en una sola operación)",
     uno("SELECT clase_id FROM cuentas WHERE id = %s", GRACE)["clase_id"] == KLASE
     and uno("SELECT usada_en FROM invitaciones WHERE codigo = %s", codigos[0])["usada_en"] is not None)
caso("La base anota en la bitácora quién usó el código",
     uno("SELECT 1 FROM bitacora WHERE cuenta_id = %s AND accion = 'usó código de invitación'", GRACE))
gabriel.post(f"/registro/{codigos[0]}", data={"accion": "unirse"})
caso("Un código ya usado no sirve otra vez",
     uno("SELECT clase_id FROM cuentas WHERE id = %s", GABRIEL)["clase_id"] is None
     and "ya lo usó otro estudiante" in texto(gabriel.get(f"/registro/{codigos[0]}")))
profe.post(f"/registro/{codigos[1]}", data={"accion": "unirse"})
caso("Un profesor no entra a una clase con un código", uno("SELECT usada_en FROM invitaciones WHERE codigo = %s",
                                                            codigos[1])["usada_en"] is None)
gabriel.post(f"/registro/{codigos[1]}", data={"accion": "unirse"})
caso("Cada estudiante entra con su propio código",
     uno("SELECT clase_id FROM cuentas WHERE id = %s", GABRIEL)["clase_id"] == KLASE
     and "Operatoria II" in texto(gabriel.get("/menu")) and "Todavía no está en ninguna clase" not in texto(gabriel.get("/menu")))
try:
    base.execute("UPDATE cuentas SET clase_id = %s WHERE id = %s", (KLASE, PROFE))
    profesor_en_clase = True
except psycopg.errors.CheckViolation:
    profesor_en_clase = False
caso("La base no deja que un profesor pertenezca a una clase", not profesor_en_clase)

registrar = lambda cedula, telefono="": grace.post("/pacientes", data={  # noqa: E731
    "accion": "registrar", "nombre": "Luis Pérez", "cedula": cedula, "telefono": telefono})
caso("Una cedula sin el formato 000-000000-0000A no se acepta (RNF08)",
     "000-000000-0000A" in texto(registrar("T-100")) and cuantos("pacientes") == 0)
registrar("001-310290-0001A")
caso("Los 6 numeros del medio de la cedula tienen que ser una fecha valida", cuantos("pacientes") == 0)
caso("El telefono tiene que tener 8 digitos", "8 dígitos" in texto(registrar("001-010190-0001A", "8888")) and cuantos("pacientes") == 0)
registrar("0010101900001a", "8888 0000")
pid = uno("SELECT id FROM pacientes")["id"]
paciente = lambda: uno("SELECT * FROM pacientes WHERE id = %s", pid)  # noqa: E731
caso("La cedula y el telefono se guardan con su formato aunque se escriban sin guiones ni espacios",
     paciente()["cedula"] == "001-010190-0001A" and paciente()["telefono"] == "88880000")
registrar("001 010190 0001A")
caso("La misma cedula escrita de otra forma cuenta como repetida", cuantos("pacientes") == 1)
caso("Al registrar, el paso siguiente pide la razon del paciente",
     "por qué está el paciente" in texto(grace.get(f"/pacientes/{pid}/razon")))
grace.post(f"/pacientes/{pid}/razon", data={"razon": ""})
caso("La razon es obligatoria", paciente()["razon"] == "")

S1, S4, S5 = sala("Clínica 1"), sala("Clínica 4"), sala("Clínica 5")
grace.post(f"/pacientes/{pid}/razon", data={"razon": "Dolor en la muela 36", "fecha": AYER, "hora": "09:00", "sala": S1})
caso("La primera cita tampoco se agenda en una fecha pasada",
     cuantos("citas") == 0 and paciente()["razon"] == "")
grace.post(f"/pacientes/{pid}/razon", data={"razon": "Dolor en la muela 36", "fecha": MANANA, "hora": "", "sala": S1})
caso("Si se pone fecha a la primera cita, tambien hace falta la hora", cuantos("citas") == 0)
grace.post(f"/pacientes/{pid}/razon", data={"razon": "Dolor en la muela 36", "fecha": MANANA, "hora": "09:00"})
caso("La cita necesita una clínica (el modelo la exige)", cuantos("citas") == 0)
grace.post(f"/pacientes/{pid}/razon", data={"razon": "Dolor en la muela 36", "fecha": MANANA,
                                            "hora": "09:00", "minutos": "60", "sala": S1, "razon_cita": "Valoración"})
caso("La razon sale al lado del nombre en la lista", "Dolor en la muela 36" in texto(grace.get("/pacientes")))
caso("La razon de la cita sale en la pagina de citas", "Valoración" in texto(grace.get("/citas")))
primera = uno("SELECT * FROM citas")
caso("La cita guarda inicio y fin, la clínica y el estudiante que atiende",
     primera["fin"] - primera["inicio"] == timedelta(hours=1) and primera["sala_id"] == S1
     and primera["estudiante_id"] == GRACE and primera["creado_por_id"] == GRACE)
caso("La cita que agenda el estudiante queda programada, esperando la aprobación de su profesor",
     primera["estado"] == "programada" and primera["aprobado_en"] is None
     and "Espera la aprobación del profesor" in texto(grace.get("/citas")))

formulario = texto(grace.get("/citas"))
caso("La fecha se elige en un calendario que no deja dias anteriores a hoy",
     'type="date"' in formulario and f'min="{m.hoy()}"' in formulario)
caso("Las fechas se ven como dd/mm/aaaa", m.dmy(MANANA) in formulario)
cita = lambda **d: grace.post("/citas", data={"paciente_id": pid, "fecha": MANANA, "hora": "10:00", "minutos": "60",  # noqa: E731
                                              "sala": S4, "razon": "Control", **d}, follow_redirects=True)
cita(fecha="30/11/2026")
caso("Una fecha que no viene del calendario no se agenda", cuantos("citas") == 1)
cita(fecha=AYER)
caso("No se agenda en un dia anterior a hoy, aunque sea del mismo mes", cuantos("citas") == 1)
caso("No se agenda fuera del horario de la clínica (07:00 a 16:00)",
     "de lunes a sábado, de 07:00 a 16:00" in texto(cita(hora="06:30")) and "16:00" in texto(cita(hora="15:30", minutos="60"))
     and cuantos("citas") == 1)
domingo = (HOY + timedelta(days=7 - HOY.isoweekday() or 7)).isoformat()
cita(fecha=domingo)
caso("Los domingos no se agenda", cuantos("citas") == 1)
cita(minutos="300")
caso("Una cita dura a lo sumo 4 horas", cuantos("citas") == 1)
if HOY_ATIENDE and "08:00" < ahora.strftime("%H:%M") < "15:00":
    cita(fecha=m.hoy(), hora=(ahora - timedelta(hours=1)).strftime("%H:%M"))
    caso("Hoy no se agenda a una hora que ya paso", cuantos("citas") == 1)
cita(hora="09:30", sala=S1, razon="Otra")
caso("No se agenda una clínica ocupada en un rato que se encima (09:30 dentro de 09:00-10:00)", cuantos("citas") == 1)
cita(hora="09:00", sala=S5, razon="Otra")
caso("El paciente no tiene dos citas a la misma hora, aunque sea en otra clínica (RF10)", cuantos("citas") == 1)
grace.post("/pacientes", data={"accion": "registrar", "nombre": "Marta Gómez", "cedula": "001-050590-0005E"})
marta = uno("SELECT id FROM pacientes WHERE nombre = 'Marta Gómez'")["id"]
caso("El estudiante no tiene dos citas a la misma hora con pacientes distintos (RF10)",
     "ya tiene otra cita a esa hora" in texto(grace.post("/citas", data={
         "paciente_id": marta, "fecha": MANANA, "hora": "09:00", "sala": S5, "razon": "Valoración"}, follow_redirects=True))
     and cuantos("citas") == 1)
base.execute("DELETE FROM pacientes WHERE id = %s", (marta,))

estado = lambda cid: uno("SELECT c.*, to_char(c.inicio AT TIME ZONE 'America/Managua', 'HH24:MI') AS hora"  # noqa: E731
                         " FROM citas c WHERE c.id = %s", cid)
cita(hora="11:00", razon="Para cancelar")
cancelar = uno("SELECT id FROM citas WHERE razon = 'Para cancelar'")["id"]
gabriel.post("/citas", data={"accion": "cancelar", "id": cancelar, "motivo": "No es mío"})
caso("Un estudiante no cancela citas de pacientes ajenos", estado(cancelar)["estado"] == "programada")
grace.post("/citas", data={"accion": "cancelar", "id": cancelar})
caso("Para cancelar hay que escribir el motivo (RF11)", estado(cancelar)["estado"] == "programada")
grace.post("/citas", data={"accion": "cancelar", "id": cancelar, "motivo": "El paciente viaja"})
caso("Cancelada, la cita no se borra: queda en el historial con su motivo (RF11)",
     cuantos("citas") == 2 and estado(cancelar)["estado"] == "cancelada"
     and estado(cancelar)["motivo_cancelacion"] == "El paciente viaja")
grace.post("/citas", data={"accion": "estado", "id": cancelar, "estado": "confirmada"})
caso("Una cita cancelada ya no cambia de estado", estado(cancelar)["estado"] == "cancelada")
cita(hora="11:00")
control = uno("SELECT id FROM citas WHERE razon = 'Control'")
caso("Al cancelar, la clínica y la hora quedan libres para otra cita", control is not None)
grace.post("/citas", data={"accion": "estado", "id": control["id"], "estado": "no_asistio"})
caso("Una cita que todavía no llega no se marca como que no se presentó", estado(control["id"])["estado"] == "programada")
grace.post("/citas", data={"accion": "estado", "id": control["id"], "estado": "confirmada"})
caso("Sin la aprobación del profesor, la cita no se confirma", estado(control["id"])["estado"] == "programada")
try:
    base.execute("UPDATE citas SET estado = 'confirmada' WHERE id = %s", (control["id"],))
    sin_aprobar = True
except psycopg.errors.CheckViolation:
    sin_aprobar = False
caso("Tampoco directo en la base: una cita sin aprobar no pasa a confirmada", not sin_aprobar)
caso("Al profesor le aparecen las citas por aprobar de sus estudiantes",
     "Citas por aprobar" in texto(profe.get("/menu")) and "Citas (" in texto(profe.get("/menu")))
gustavo = cliente("00000004")
gustavo.post("/citas", data={"accion": "aprobar", "id": control["id"]})
caso("Un profesor que no tiene al estudiante en su clase no aprueba su cita", estado(control["id"])["aprobado_en"] is None)
try:
    base.execute("UPDATE citas SET aprobado_por_id = %s WHERE id = %s", (GUSTAVO, control["id"]))
    ajeno = True
except psycopg.errors.RaiseException:
    ajeno = False
caso("Ni directo en la base: la base revisa que sea profesor de su clase", not ajeno)
profe.post("/citas", data={"accion": "aprobar", "id": control["id"]})
caso("El profesor de su clase aprueba la cita: queda quién y cuándo",
     estado(control["id"])["aprobado_por_id"] == PROFE and estado(control["id"])["aprobado_en"] is not None)
grace.post("/citas", data={"accion": "estado", "id": control["id"], "estado": "confirmada"})
caso("Aprobada, el estudiante la confirma", estado(control["id"])["estado"] == "confirmada")
grace.post("/citas", data={"accion": "reprogramar", "id": control["id"], "fecha": MANANA, "hora": "09:00", "sala": S4})
caso("No se reprograma a una hora en que el paciente ya tiene cita", estado(control["id"])["hora"] == "11:00")
grace.post("/citas", data={"accion": "reprogramar", "id": control["id"], "fecha": MANANA, "hora": "12:30",
                           "minutos": "45", "sala": S4})
reprogramada = estado(control["id"])
caso("Al reprogramar cambia la hora, queda programada y vuelve a esperar la aprobación (RF11)",
     reprogramada["hora"] == "12:30" and reprogramada["estado"] == "programada" and reprogramada["aprobado_en"] is None
     and reprogramada["fin"] - reprogramada["inicio"] == timedelta(minutes=45))
profe.post("/citas", data={"accion": "rechazar", "id": control["id"], "motivo": ""})
caso("Para rechazar una cita el profesor escribe el motivo", estado(control["id"])["estado"] == "programada")
profe.post("/citas", data={"accion": "aprobar", "id": control["id"]})
base.execute("INSERT INTO citas (paciente_id, sala_id, estudiante_id, creado_por_id, inicio, fin, razon)"
             " VALUES (%s, %s, %s, %s, %s::timestamp AT TIME ZONE 'America/Managua',"
             " %s::timestamp AT TIME ZONE 'America/Managua', 'Pasada')",
             (pid, S1, GRACE, GRACE, f"{PASADA} 08:00", f"{PASADA} 09:00"))
pasada = uno("SELECT id FROM citas WHERE razon = 'Pasada'")["id"]
grace.post("/citas", data={"accion": "cancelar", "id": pasada, "motivo": "Tarde"})
caso("Las citas pasadas no se cancelan: son historial", estado(pasada)["estado"] == "programada")
grace.post("/citas", data={"accion": "estado", "id": pasada, "estado": "no_asistio"})
caso("Una cita pasada se marca como que el paciente no se presentó",
     estado(pasada)["estado"] == "no_asistio" and "No se presentó a 1 cita" in texto(grace.get(f"/pacientes/{pid}")))
caso("Los cambios de las citas quedan en la bitacora",
     uno("SELECT 1 FROM bitacora WHERE accion LIKE 'Cita de Luis Pérez del %%: cancelada (El paciente viaja)'")
     and uno("SELECT 1 FROM bitacora WHERE accion LIKE 'Reprogramó la cita de Luis Pérez%%'")
     and uno("SELECT 1 FROM bitacora WHERE accion LIKE 'Aprobó la cita de Luis Pérez%%'"))
try:
    base.execute("INSERT INTO citas (paciente_id, sala_id, estudiante_id, creado_por_id, inicio, fin, razon)"
                 " VALUES (%s, %s, %s, %s, now() + interval '30 days', now() + interval '30 days 1 hour', 'X')",
                 (pid, S1, PROFE, PROFE))
    rol_mal = True
except psycopg.errors.RaiseException:
    rol_mal = False
caso("La base no acepta una cita atendida por alguien que no es estudiante", not rol_mal)

gabriel.post("/pacientes", data={"accion": "registrar", "nombre": "Ana Ruiz", "cedula": "001-020290-0002B"})
grace.post(f"/pacientes/{pid}/editar", data={"nombre": "Luis Pérez", "cedula": "0010202900002b", "telefono": ""})
caso("Al corregir no se puede usar la cedula de otro paciente", paciente()["cedula"] == "001-010190-0001A")
gabriel.post(f"/pacientes/{pid}/editar", data={"nombre": "Otro", "cedula": "001-010190-0001A", "telefono": ""})
caso("Un estudiante no corrige pacientes ajenos", paciente()["nombre"] == "Luis Pérez")
grace.post(f"/pacientes/{pid}/editar", data={"nombre": "Luis Pérez", "cedula": "001-010190-0001A", "telefono": "22220000"})
caso("El estudiante a cargo corrige los datos del paciente", paciente()["telefono"] == "22220000")
caso("Sin escribir un número de expediente, conserva el que tenía", paciente()["expediente"] == f"EXP-{pid:05d}")

caso("Cada estudiante ve solo sus pacientes", "Luis Pérez" not in texto(gabriel.get("/pacientes")))
caso("Cada estudiante ve solo sus citas", "Luis Pérez" not in texto(gabriel.get("/citas")))
caso("Un estudiante no abre el expediente de otro", gabriel.get(f"/pacientes/{pid}").status_code == 302)
caso("El profesor ve a los pacientes de los estudiantes de su clase", "Luis Pérez" in texto(profe.get("/pacientes")))
caso("Un profesor no ve a los pacientes de estudiantes que no están en su clase (RNF06)",
     "Luis Pérez" not in texto(gustavo.get("/pacientes")) and gustavo.get(f"/pacientes/{pid}").status_code == 302)
busca = lambda c, q: texto(c.get("/pacientes", query_string={"buscar": q}))  # noqa: E731
caso("Se busca un paciente por parte de su nombre (RF06)",
     "Luis Pérez" in busca(profe, "luis") and "Ana Ruiz" not in busca(profe, "luis"))
caso("Se busca por la cédula, con o sin guiones", "Luis Pérez" in busca(profe, "0010101") and "Luis Pérez" in busca(profe, "001-0101"))
caso("La búsqueda del estudiante solo mira sus pacientes", "Luis Pérez" not in busca(gabriel, "Luis"))
caso("Un % no trae a todos los pacientes", "Ningún paciente coincide" in busca(profe, "%"))

caso("El profesor no registra pacientes", "accion\" value=\"registrar" not in texto(profe.get("/pacientes")))
profe.post("/pacientes", data={"accion": "registrar", "nombre": "X", "cedula": "001-040490-0004D"})
caso("El profesor no puede registrar ni por la fuerza", cuantos("pacientes") == 2)
profe.post("/citas", data={"paciente_id": pid, "fecha": PASADO, "hora": "08:00", "minutos": "30", "sala": S5,
                           "razon": "Revisión con el profesor"})
del_profe = uno("SELECT * FROM citas WHERE razon = 'Revisión con el profesor'")
caso("El profesor agenda una cita para un estudiante de su clase y queda aprobada sola",
     del_profe and del_profe["creado_por_id"] == PROFE and del_profe["estudiante_id"] == GRACE
     and del_profe["aprobado_por_id"] == PROFE and del_profe["aprobado_en"] is not None)
gustavo.post("/citas", data={"paciente_id": pid, "fecha": PASADO, "hora": "10:00", "sala": S5, "razon": "Ajena"})
admin.post("/citas", data={"paciente_id": pid, "fecha": PASADO, "hora": "10:00", "sala": S5, "razon": "Facultad"})
caso("Ni otro profesor ni la Facultad agendan citas de ese paciente",
     uno("SELECT 1 FROM citas WHERE razon IN ('Ajena', 'Facultad')") is None and "Agendar cita" not in texto(admin.get("/citas")))
profe.post("/pacientes", data={"accion": "aprobar", "id": pid, "aprobado": "1"})
caso("El profesor aprueba al paciente", paciente()["aprobado_en"] is not None)
caso("Queda quién aprobó y cuándo (RF04)", paciente()["aprobado_por_id"] == PROFE)
profe.post("/pacientes", data={"accion": "rechazar", "id": pid, "observacion": ""})
caso("Para rechazar hace falta la observación (RF04)", paciente()["aprobado_en"] is not None and paciente()["rechazo"] == "")
grace.post("/pacientes", data={"accion": "rechazar", "id": pid, "observacion": "Yo no"})
caso("El estudiante no rechaza pacientes", paciente()["rechazo"] == "")
profe.post("/pacientes", data={"accion": "rechazar", "id": pid, "observacion": "Falta la valoración periodontal"})
caso("El profesor rechaza al paciente con su observación",
     paciente()["aprobado_en"] is None and paciente()["rechazo"] == "Falta la valoración periodontal"
     and paciente()["rechazado_por_id"] == PROFE)
inicio = texto(grace.get("/menu"))
caso("El estudiante ve en Inicio el rechazo con la observación",
     "Falta la valoración periodontal" in inicio and "Mis pacientes (1)" in inicio)
caso("El rechazo queda en la bitacora", uno("SELECT 1 FROM bitacora WHERE accion = 'Rechazó a Luis Pérez: Falta la valoración periodontal'"))
profe.post("/pacientes", data={"accion": "aprobar", "id": pid, "aprobado": "1"})
caso("Al aprobarlo después, se borra el rechazo", paciente()["aprobado_en"] is not None and paciente()["rechazo"] == ""
     and "Falta la valoración" not in texto(grace.get("/menu")))

FIRMA = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAgAAAAECAYAAACzzX7wAAAA"
         "FUlEQVR4nGNkYGD4z4AHMOGTpI4CAIIUAQeqcFo1AAAAAElFTkSuQmCC")
grace.post(f"/pacientes/{pid}/consentimiento", data={"firma": "Luis Pérez"})
caso("La firma escrita a mano no cuenta: el paciente la dibuja", paciente()["firma"] == "")
grace.post(f"/pacientes/{pid}/consentimiento", data={"firma": FIRMA})
hoja = texto(grace.get(f"/pacientes/{pid}/consentimiento"))
caso("El consentimiento se imprime con la firma dibujada del paciente",
     paciente()["firma"] == FIRMA and paciente()["firmado_en"] is not None and "firma-trazo" in hoja)
caso("El docente no firma el consentimiento", "Firma del docente" not in hoja)
caso("La lista dice que ya firmo", "Firmado" in texto(grace.get("/pacientes")))

pdf = grace.get(f"/pacientes/{pid}/expediente.pdf")
caso("El expediente se descarga como PDF, no como txt",
     pdf.data[:4] == b"%PDF" and pdf.headers["Content-Disposition"].endswith(".pdf"))

caso("Solo la administracion traspasa pacientes",
     grace.get(f"/pacientes/{pid}/traspaso").status_code == 302)
admin.post(f"/pacientes/{pid}/traspaso", data={"estudiante": GABRIEL, "resumen": ""})
caso("Al traspasar hay que decir que se le hizo al paciente", paciente()["estudiante_id"] == GRACE)
admin.post(f"/pacientes/{pid}/traspaso", data={"estudiante": GABRIEL, "resumen": "Profilaxis y obturación del 36"})
archivado = base.execute("SELECT * FROM traspasos WHERE paciente_id = %s", (pid,)).fetchall()
caso("Al cambiar de estudiante queda el traspaso con el resumen del trabajo del anterior",
     paciente()["estudiante_id"] == GABRIEL and len(archivado) == 1 and archivado[0]["de_id"] == GRACE
     and archivado[0]["resumen"] == "Profilaxis y obturación del 36")
caso("El historial muestra que le hizo cada estudiante",
     all(t in texto(gabriel.get(f"/pacientes/{pid}")) for t in ("Grace Alvarado", "Gabriel Espinoza",
                                                                "Profilaxis y obturación del 36")))
caso("El paciente ya no le sale al estudiante anterior", "Luis Pérez" not in texto(grace.get("/pacientes")))
caso("Sus citas anteriores conservan quién las atendió",
     uno("SELECT estudiante_id FROM citas WHERE id = %s", pasada)["estudiante_id"] == GRACE)

caso("Hay 50 clínicas, como en el plano: de la 1 a la 30 de pregrado y de la 31 a la 50 de posgrado",
     [(f["en_sala"], f["n"]) for f in base.execute("SELECT en_sala, count(*) AS n FROM salas GROUP BY en_sala ORDER BY 1")]
     == [(1, 30), (2, 20)]
     and uno("SELECT en_sala FROM salas WHERE nombre = 'Clínica 30'")["en_sala"] == 1
     and uno("SELECT en_sala FROM salas WHERE nombre = 'Clínica 31'")["en_sala"] == 2)
_, sala_1, sala_2 = texto(admin.get("/salas")).split('class="plano-titulo"')
en_orden = lambda html: [int(n) for n in re.findall(r'title="Clínica (\d+):', html)]  # noqa: E731
caso("El plano muestra cada sala con sus clínicas en orden, con un check si están habilitadas",
     "Clínicas pregrado" in sala_1 and en_orden(sala_1) == list(range(1, 31)) and sala_1.count('"marca-sala">✓') == 30
     and "Clínicas posgrado" in sala_2 and en_orden(sala_2) == list(range(31, 51)) and sala_2.count('"marca-sala">✓') == 20)
admin.post("/salas", data={"accion": "agregar", "nombre": "Clínica 51", "en_sala": "2"})
caso("La Facultad agrega una clínica en pregrado o posgrado",
     uno("SELECT en_sala FROM salas WHERE nombre = 'Clínica 51'")["en_sala"] == 2)
admin.post("/salas", data={"accion": "agregar", "nombre": "Clínica 52"})
admin.post("/salas", data={"accion": "agregar", "nombre": "Clínica 52", "en_sala": "3"})
caso("Sin decir si es de pregrado o posgrado no se agrega la clínica", uno("SELECT 1 FROM salas WHERE nombre = 'Clínica 52'") is None)
admin.post("/salas", data={"sala": S1})
caso("La Facultad cambia si una clínica está habilitada",
     uno("SELECT habilitada FROM salas WHERE id = %s", S1)["habilitada"] is False)
admin.post("/salas", data={"sala": S1})
caso("El estudiante no administra las clínicas", "Agregar clínica" not in texto(grace.get("/salas")))

S2 = sala("Clínica 2")
falla = lambda: uno("SELECT * FROM reportes WHERE sala_id = %s AND resuelto_en IS NULL", S2)  # noqa: E731
grace.post("/salas", data={"accion": "reportar", "sala": S2, "motivo": "  "})
caso("Para reportar una clínica hay que decir que no funciona", falla() is None)
profe.post("/salas", data={"accion": "reportar", "sala": S2, "motivo": "No enciende la lámpara"})
caso("Solo los estudiantes reportan salas", falla() is None)
grace.post("/salas", data={"accion": "reportar", "sala": S2, "motivo": "No enciende la lámpara", "tipo": "equipo"})
caso("El estudiante marca una clínica como no funcional con qué falla",
     falla()["descripcion"] == "No enciende la lámpara" and falla()["tipo"] == "equipo"
     and falla()["reportado_por_id"] == GRACE and uno("SELECT habilitada FROM salas WHERE id = %s", S2)["habilitada"] is False)
gabriel.post("/salas", data={"accion": "reportar", "sala": S2, "motivo": "Otra cosa"})
caso("Una clínica ya reportada no se reporta dos veces", cuantos("reportes") == 1)
caso("La Facultad ve el aviso en Inicio y en la pestaña Clínicas",
     "Hay 1 clínica que no funciona" in texto(admin.get("/menu")) and "Clínicas (1)" in texto(admin.get("/menu")))
caso("La Facultad ve qué falla y quién lo reportó",
     all(t in texto(admin.get("/salas")) for t in ("No enciende la lámpara", "Grace Alvarado", "Marcar como solucionada")))
caso("El estudiante no ve el aviso de la Facultad", "Hay 1 clínica" not in texto(grace.get("/menu")))
gabriel.post("/citas", data={"paciente_id": pid, "fecha": MANANA, "hora": "15:00", "sala": S2, "razon": "X"})
caso("No se agenda una cita en una clínica que no funciona", uno("SELECT 1 FROM citas WHERE sala_id = %s", S2) is None)
reporte = falla()["id"]
gabriel.post("/salas", data={"accion": "resolver", "reporte": reporte})
caso("Solo la Facultad marca una falla como solucionada", falla() is not None)
admin.post("/salas", data={"accion": "resolver", "reporte": reporte})
resuelto = uno("SELECT * FROM reportes WHERE id = %s", reporte)
caso("Al solucionarla la clínica vuelve a estar habilitada y el aviso desaparece",
     resuelto["resuelto_por_id"] == ADMIN and resuelto["resuelto_en"] is not None
     and uno("SELECT habilitada FROM salas WHERE id = %s", S2)["habilitada"] is True
     and "Clínicas (" not in texto(admin.get("/menu")))
caso("La bitacora guarda el reporte y la solucion",
     all(t in texto(admin.get("/bitacora")) for t in ("Reportó Clínica 2 (equipo)", "Solucionó la falla de Clínica 2")))
caso("La bitacora guarda lo que hizo cada quien", "Pasó a Luis Pérez" in texto(admin.get("/bitacora")))
bitacora = texto(admin.get("/bitacora"))
lugar = uno("SELECT lugar FROM bitacora ORDER BY id DESC LIMIT 1")["lugar"]
caso("Desde la red local la bitacora pone la ubicacion de la conexion, no 'Red local', y no muestra la IP",
     lugar != "Red local" and "Red local" not in bitacora and "127.0.0.1" not in bitacora)
print(f"         (ubicacion encontrada: {lugar or 'sin respuesta del servicio'})")
caso("La bitácora guarda la cuenta y también el CIF tal como era",
     uno("SELECT cuenta_id, cif_registrado FROM bitacora WHERE accion LIKE 'Solucionó la falla%%'")
     == {"cuenta_id": ADMIN, "cif_registrado": "00000007"})
with m.app.test_request_context(headers={"Fly-Client-IP": "1.2.3.4"}):
    caso("Fuera de Fly no se cree un encabezado de IP que cualquiera puede inventar", m.ip_del_usuario() != "1.2.3.4")

# Odontograma (Luis Pérez ya es de Gabriel y tiene el consentimiento firmado)
odonto = f"/pacientes/{pid}/odontograma"
caso("El odontograma muestra los 32 dientes", texto(gabriel.get(odonto)).count('class="diente') == 32)
gabriel.post(odonto, data={"accion": "diente", "diente": "46", "estado": "caries", "nota": "oclusal profunda"})
caso("El estudiante a cargo anota el estado de un diente",
     uno("SELECT estado, nota FROM dientes WHERE paciente_id = %s AND diente = 46", pid)
     == {"estado": "caries", "nota": "oclusal profunda"})
gabriel.post(odonto, data={"accion": "diente", "diente": "99", "estado": "caries"})
gabriel.post(odonto, data={"accion": "diente", "diente": "11", "estado": "Roto"})
caso("No se anotan dientes que no existen ni estados inventados", cuantos("dientes") == 1)
try:
    base.execute("INSERT INTO dientes (paciente_id, diente, estado) VALUES (%s, 19, 'sano')", (pid,))
    fdi = True
except psycopg.errors.CheckViolation:
    fdi = False
caso("La base solo acepta la numeración FDI", not fdi)
grace.post(odonto, data={"accion": "diente", "diente": "11", "estado": "caries"})
profe.post(odonto, data={"accion": "diente", "diente": "11", "estado": "caries"})
caso("Ni otro estudiante ni el profesor anotan en el odontograma", cuantos("dientes") == 1)
gabriel.post(odonto, data={"accion": "pedir"})
caso("Sin una foto del paciente no se pide la revision", paciente()["odonto_revision"] == "")
gabriel.post(odonto, data={"accion": "foto", "foto": (io.BytesIO(b"no es una imagen"), "boca.jpg")})
caso("Un archivo que no es una imagen no se guarda como foto", cuantos("fotos") == 0)
grande = io.BytesIO()
Image.new("RGB", (4000, 3000), "pink").save(grande, "PNG")
grande.seek(0)
gabriel.post(odonto, data={"accion": "foto", "tipo": "intraoral", "descripcion": "Arcada superior", "foto": (grande, "boca.png")})
foto = f"/pacientes/{pid}/fotos/{uno('SELECT id FROM fotos')['id']}.jpg"
guardada = Image.open(io.BytesIO(gabriel.get(foto).data))
caso("La foto se guarda achicada y en JPEG, con su tipo y descripción",
     guardada.format == "JPEG" and max(guardada.size) == m.FOTO_LADO
     and uno("SELECT tipo, descripcion FROM fotos") == {"tipo": "intraoral", "descripcion": "Arcada superior"})
caso("Otro estudiante no ve las fotos del paciente", grace.get(foto).status_code == 302)
url_foto = uno("SELECT url FROM fotos")["url"]
caso("La foto no va dentro de la base: se guarda solo la dirección del archivo",
     url_foto.startswith("local:fotos/") and m.ruta_local(url_foto).is_file())
caso("La dirección de un archivo no puede salirse de la carpeta de fotos", m.ruta_local("local:../../etc/passwd") is None)
gabriel.post(odonto, data={"accion": "pedir"})
caso("El profesor ve en Inicio el odontograma que espera su revision",
     paciente()["odonto_revision"] == "pedida" and "Luis Pérez" in texto(profe.get("/menu")))
profe.post(odonto, data={"accion": "devolver", "nota": ""})
caso("Para devolverlo hay que decir que corregir", paciente()["odonto_revision"] == "pedida")
profe.post(odonto, data={"accion": "devolver", "nota": "Falta revisar el 36"})
caso("El profesor lo devuelve con lo que hay que corregir",
     paciente()["odonto_revision"] == "devuelta" and "Falta revisar el 36" in texto(gabriel.get(odonto)))
gabriel.post(odonto, data={"accion": "pedir"})
gabriel.post(odonto, data={"accion": "aprobar"})
caso("El estudiante no aprueba su propio odontograma", paciente()["odonto_revision"] == "pedida")
profe.post(odonto, data={"accion": "aprobar"})
caso("El profesor aprueba el odontograma",
     paciente()["odonto_revision"] == "aprobada" and paciente()["odonto_revisor_id"] == PROFE)
gabriel.post(odonto, data={"accion": "diente", "diente": "36", "estado": "obturado"})
caso("Si se cambia despues de aprobado, hay que volver a pedir la revision", paciente()["odonto_revision"] == "")

# Tarifario, tratamientos, órdenes de cobro y pagos
caso("Solo la Facultad lleva el tarifario",
     grace.get("/tarifario").status_code == 302 and profe.get("/tarifario").status_code == 302)
admin.post("/tarifario", data={"accion": "agregar", "codigo": "PRO-01", "nombre": "Profilaxis", "precio": "abc"})
admin.post("/tarifario", data={"accion": "agregar", "codigo": "PRO-01", "nombre": "Profilaxis", "precio": "-5"})
caso("El precio tiene que ser un monto valido", cuantos("tarifario") == 0)
admin.post("/tarifario", data={"accion": "agregar", "nombre": "Profilaxis", "precio": "350"})
caso("Cada procedimiento lleva su código (el modelo lo exige)", cuantos("tarifario") == 0)
admin.post("/tarifario", data={"accion": "agregar", "codigo": "PRO-01", "nombre": "Profilaxis", "precio": "350"})
admin.post("/tarifario", data={"accion": "agregar", "codigo": "OPE-01", "nombre": "Obturación con resina", "precio": "800.50"})
admin.post("/tarifario", data={"accion": "agregar", "codigo": "PRO-02", "nombre": "profilaxis", "precio": "1"})
caso("La Facultad agrega procedimientos y no se repiten", cuantos("tarifario") == 2)
profilaxis = uno("SELECT id FROM tarifario WHERE nombre = 'Profilaxis'")["id"]
resina = uno("SELECT id FROM tarifario WHERE nombre = 'Obturación con resina'")["id"]
trat = f"/pacientes/{pid}/tratamientos"
gabriel.post(trat, data={"accion": "anotar", "procedimiento": resina, "diente": "36", "superficie": "oclusal"})
gabriel.post(trat, data={"accion": "anotar", "procedimiento": profilaxis, "diente": ""})
gabriel.post(trat, data={"accion": "anotar", "procedimiento": profilaxis, "diente": "99"})
gabriel.post(trat, data={"accion": "anotar", "procedimiento": profilaxis, "superficie": "mesial"})
caso("El estudiante anota en el plan, con diente y cara o sin diente; no un diente que no existe ni una cara sin diente",
     cuantos("tratamientos") == 2
     and uno("SELECT diente, superficie FROM tratamientos WHERE tarifario_id = %s", resina) == {"diente": 36, "superficie": "oclusal"})
gabriel.post(trat, data={"accion": "anotar", "procedimiento": resina, "diente": "17"})
caso("El tratamiento apunta al diente del odontograma: si no estaba anotado, queda como sano",
     uno("SELECT estado FROM dientes WHERE paciente_id = %s AND diente = 17", pid)["estado"] == "sano")
base.execute("DELETE FROM tratamientos WHERE diente = 17")
profe.post(trat, data={"accion": "anotar", "procedimiento": profilaxis})
grace.post(trat, data={"accion": "anotar", "procedimiento": profilaxis})
caso("Ni el profesor ni otro estudiante anotan tratamientos", cuantos("tratamientos") == 2)
admin.post("/tarifario", data={"accion": "precio", "id": resina, "precio": "900"})
caso("Cambiar el precio en el tarifario no altera lo ya anotado",
     uno("SELECT precio FROM tratamientos WHERE tarifario_id = %s", resina)["precio"] == Decimal("800.50")
     and uno("SELECT precio FROM tarifario WHERE id = %s", resina)["precio"] == Decimal("900"))
hecho = uno("SELECT id FROM tratamientos WHERE tarifario_id = %s", resina)["id"]
gabriel.post(trat, data={"accion": "realizado", "id": hecho})
caso("Para marcarlo realizado hay que decir en qué cita", uno("SELECT cita_id FROM tratamientos WHERE id = %s", hecho)["cita_id"] is None)
gabriel.post(trat, data={"accion": "realizado", "id": hecho, "cita": del_profe["id"]})
caso("No en una cita que todavía no pasa", uno("SELECT cita_id FROM tratamientos WHERE id = %s", hecho)["cita_id"] is None)
atendida = uno("INSERT INTO citas (paciente_id, sala_id, estudiante_id, creado_por_id, aprobado_por_id, aprobado_en,"
               " inicio, fin, estado, razon) VALUES (%s, %s, %s, %s, %s, now(), %s::timestamp AT TIME ZONE"
               " 'America/Managua', %s::timestamp AT TIME ZONE 'America/Managua', 'atendida', 'Obturación')"
               " RETURNING id", pid, S5, GABRIEL, GABRIEL, PROFE, f"{PASADA} 10:00", f"{PASADA} 11:00")["id"]
gabriel.post(trat, data={"accion": "realizado", "id": hecho, "cita": atendida})
caso("Lo realizado queda ligado a su cita: de ella salen quién lo hizo y cuándo",
     uno("SELECT realizado_por_id, (realizado_en AT TIME ZONE 'America/Managua')::date::text AS dia"
         " FROM tratamientos_realizados WHERE id = %s", hecho) == {"realizado_por_id": GABRIEL, "dia": PASADA})
try:
    base.execute("UPDATE citas SET estado = 'cancelada' WHERE id = %s", (atendida,))
    cancelada_con = True
except psycopg.errors.RaiseException:
    cancelada_con = False
caso("Una cita con tratamientos realizados ya no se puede cancelar (lo cuida la base)", not cancelada_con)
gabriel.post(trat, data={"accion": "quitar", "id": hecho})
caso("Lo realizado ya no se quita del plan", cuantos("tratamientos") == 2)
gustavo.post(trat, data={"accion": "aprobar_tratamiento", "id": hecho})
caso("Un profesor que no es de su clase no aprueba el tratamiento",
     uno("SELECT aprobado_por_id FROM tratamientos WHERE id = %s", hecho)["aprobado_por_id"] is None)
profe.post(trat, data={"accion": "aprobar_tratamiento", "id": hecho})
caso("El profesor de su clase aprueba el tratamiento",
     uno("SELECT aprobado_por_id FROM tratamientos WHERE id = %s", hecho)["aprobado_por_id"] == PROFE)
ana = uno("SELECT id FROM pacientes WHERE cedula = '001-020290-0002B'")["id"]
gabriel.post(f"/pacientes/{ana}/tratamientos", data={"accion": "anotar", "procedimiento": profilaxis})
de_ana = uno("SELECT id FROM tratamientos WHERE paciente_id = %s", ana)["id"]
gabriel.post(f"/pacientes/{ana}/tratamientos", data={"accion": "realizado", "id": de_ana, "cita": atendida})
caso("Sin la firma del consentimiento no se marca nada como realizado (y nunca con la cita de otro paciente)",
     uno("SELECT cita_id FROM tratamientos WHERE id = %s", de_ana)["cita_id"] is None)
gabriel.post(f"/pacientes/{ana}/tratamientos", data={"accion": "quitar", "id": de_ana})
caso("Lo pendiente se puede quitar del plan", cuantos("tratamientos") == 2)
gabriel.post(trat, data={"accion": "orden"})
caso("El estudiante no genera la orden de cobro (la generan el profesor o la Facultad)", cuantos("ordenes") == 0)
profe.post(trat, data={"accion": "orden"})
OID = uno("SELECT id FROM ordenes")["id"]
caso("El profesor genera la orden con lo del plan; el total lo calcula la base",
     uno("SELECT total, estado FROM resumen_ordenes WHERE id = %s", OID) == {"total": Decimal("1150.50"), "estado": "pendiente"}
     and uno("SELECT count(*) AS n FROM tratamientos WHERE orden_id = %s", OID)["n"] == 2)
gabriel.post(trat, data={"accion": "pago", "orden": OID, "monto": "100", "numero": "X-1"})
caso("Los pagos los registra solo la Facultad (o el estudiante con la foto del recibo)", cuantos("recibos") == 0)
admin.post(trat, data={"accion": "pago", "orden": OID, "monto": "0", "numero": "R-1"})
admin.post(trat, data={"accion": "pago", "orden": OID, "monto": "5000", "numero": "R-1"})
admin.post(trat, data={"accion": "pago", "orden": OID, "monto": "500", "numero": "R-1", "metodo": "transferencia"})
caso("Un pago tiene monto, no pasa del saldo y, si no es en efectivo, lleva referencia", cuantos("recibos") == 0)
try:
    base.execute("INSERT INTO recibos (orden_id, numero, monto, metodo_pago) VALUES (%s, 'X', 5000, 'efectivo')", (OID,))
    de_mas = True
except psycopg.errors.RaiseException:
    de_mas = False
caso("Ni directo en la base se paga más de lo que se debe", not de_mas)
admin.post(trat, data={"accion": "pago", "orden": OID, "monto": "500", "numero": "R-500"})
cobros = texto(gabriel.get(trat))
caso("El plan suma todo y el saldo es lo realizado menos lo pagado",
     cuantos("recibos") == 1 and "C$ 1,150.50" in cobros and "C$ 300.50" in cobros
     and uno("SELECT estado FROM resumen_ordenes WHERE id = %s", OID)["estado"] == "abonada")
admin.post("/tarifario", data={"accion": "activo", "id": profilaxis})
gabriel.post(trat, data={"accion": "anotar", "procedimiento": profilaxis})
caso("Un procedimiento desactivado ya no se anota", cuantos("tratamientos") == 2)
pdf = gabriel.get(f"/pacientes/{pid}/expediente.pdf")
caso("El expediente en PDF sale con el odontograma y los tratamientos", pdf.data[:4] == b"%PDF")

# Hoja de control de expediente y recibos de pago
def recibo_jpg():
    b = io.BytesIO()
    Image.new("RGB", (800, 600), "white").save(b, "JPEG")
    b.seek(0)
    return b, "recibo.jpg"

gabriel.post(f"/pacientes/{pid}/editar", data={"nombre": "Luis Pérez", "cedula": "001-010190-0001A", "telefono": "",
                                                "expediente": "EXP-0457"})
caso("El estudiante anota el número de expediente del paciente", paciente()["expediente"] == "EXP-0457")
caso("Se busca un paciente por su número de expediente (RF06)", "Luis Pérez" in busca(profe, "exp-0457"))
subir = lambda c, **d: c.post("/control", data={"accion": "subir", "orden": OID, "numero": "A-0083",  # noqa: E731
                                                 "monto": "150", "foto": recibo_jpg(), **d})
subir(gabriel, numero="")
subir(gabriel, foto=(io.BytesIO(b"no es una imagen"), "recibo.jpg"))
subir(gabriel, monto="9000")
caso("El recibo necesita número, la foto y un monto que no pase el saldo", cuantos("recibos") == 1)
subir(grace)
caso("Un estudiante no sube recibos de pacientes ajenos", cuantos("recibos") == 1)
subir(gabriel)
subir(gabriel)
caso("El estudiante sube el recibo con su foto, sin repetir el número", cuantos("recibos") == 2)
recibo = uno("SELECT id FROM recibos WHERE numero = 'A-0083'")["id"]
hoja = texto(gabriel.get("/control"))
caso("La hoja muestra nº de paciente, de expediente, de recibo, lo que se pagó, la fecha y el monto",
     all(t in hoja for t in (f">{pid}<", "EXP-0457", "A-0083", "Obturación con resina del 36", m.dmy(m.hoy()), "C$ 150.00")))
caso("La foto del recibo la ven el estudiante y el profesor, no otro estudiante",
     gabriel.get(f"/control/recibos/{recibo}.jpg").mimetype == "image/jpeg"
     and profe.get(f"/control/recibos/{recibo}.jpg").mimetype == "image/jpeg"
     and grace.get(f"/control/recibos/{recibo}.jpg").status_code == 302)
caso("Otro estudiante no ve el recibo en su hoja", "A-0083" not in texto(grace.get("/control")))
caso("Al profesor le aparece cuántos recibos esperan su firma", "Hoja de control (2)" in texto(profe.get("/menu")))
gabriel.post("/control", data={"accion": "firmar", "id": recibo})
caso("El estudiante no firma sus recibos", uno("SELECT firmado_por_id FROM recibos WHERE id = %s", recibo)["firmado_por_id"] is None)
profe.post("/control", data={"accion": "firmar", "id": recibo})
caso("El profesor firma el recibo con su nombre",
     uno("SELECT firmado_por_id FROM recibos WHERE id = %s", recibo)["firmado_por_id"] == PROFE
     and "Dra. Grace Mendoza" in texto(gabriel.get("/control")))
gabriel.post("/control", data={"accion": "quitar", "id": recibo})
caso("Un recibo firmado ya no se anula", uno("SELECT anulado_en FROM recibos WHERE id = %s", recibo)["anulado_en"] is None)
gustavo.post("/control", data={"accion": "quitar_firma", "id": recibo})
caso("Solo el profesor que firmó puede quitar su firma",
     uno("SELECT firmado_por_id FROM recibos WHERE id = %s", recibo)["firmado_por_id"] == PROFE)
caso("Los recibos del paciente se ven en sus pagos", "A-0083" in texto(admin.get(trat)))
admin.post(f"/pacientes/{pid}/cerrar", data={"accion": "eliminar", "cedula": "001-010190-0001A", "texto": "Prueba"})
caso("Un paciente con recibos firmados por el docente no se elimina", paciente()["eliminado_en"] is None)
caso("La Facultad filtra la hoja por estudiante",
     "A-0083" in texto(admin.get(f"/control?estudiante={GABRIEL}"))
     and "A-0083" not in texto(admin.get(f"/control?estudiante={GRACE}")))
caso("El expediente en PDF sale con los recibos", gabriel.get(f"/pacientes/{pid}/expediente.pdf").data[:4] == b"%PDF")

# Comentarios: una pestaña que lleva al formulario de Google, para estudiantes y profesores
m.COMENTARIOS = ""
caso("Sin el enlace del formulario la pestaña Comentarios no aparece ni abre",
     ">Comentarios<" not in texto(grace.get("/menu")) and grace.get("/comentarios").status_code == 302)
m.COMENTARIOS = "https://forms.gle/formulario-de-prueba"
pagina = texto(grace.get("/comentarios"))
caso("El estudiante y el profesor ven la pestaña Comentarios, que abre el formulario en una pestaña nueva",
     ">Comentarios<" in texto(grace.get("/menu")) and ">Comentarios<" in texto(profe.get("/menu"))
     and 'href="https://forms.gle/formulario-de-prueba" target="_blank"' in pagina
     and profe.get("/comentarios").status_code == 200)
caso("La Facultad no tiene la pestaña Comentarios",
     ">Comentarios<" not in texto(admin.get("/menu")) and admin.get("/comentarios").status_code == 302)

# Inventario: la cantidad sale de los movimientos
caso("Solo la Facultad lleva el inventario", grace.get("/inventario").status_code == 302)
admin.post("/inventario", data={"accion": "agregar", "nombre": "Guantes", "unidad": "cajas", "cantidad": "10",
                                "minimo": "3"})
admin.post("/inventario", data={"accion": "agregar", "nombre": "guantes", "cantidad": "1", "minimo": "0"})
admin.post("/inventario", data={"accion": "agregar", "nombre": "Resina", "cantidad": "-1", "minimo": "0"})
caso("Se agregan materiales sin repetirlos ni con cantidades negativas", cuantos("materiales") == 1)
guantes = lambda: uno("SELECT * FROM existencias WHERE nombre = 'Guantes'")  # noqa: E731
caso("Lo que ya hay entra como el primer movimiento", guantes()["cantidad"] == 10 and cuantos("movimientos_inventario") == 1)
admin.post("/inventario", data={"accion": "salida", "id": guantes()["id"], "cantidad": "11"})
caso("No sale mas de lo que hay", guantes()["cantidad"] == 10)
admin.post("/inventario", data={"accion": "salida", "id": guantes()["id"], "cantidad": "8"})
inicio = texto(admin.get("/menu"))
caso("Bajo el minimo la Facultad recibe el aviso",
     guantes()["cantidad"] == 2 and "Inventario (1)" in inicio and "se está acabando" in inicio)
admin.post("/inventario", data={"accion": "entrada", "id": guantes()["id"], "cantidad": "5"})
caso("Con una entrada el aviso desaparece", guantes()["cantidad"] == 7 and "Inventario (" not in texto(admin.get("/menu")))
admin.post("/inventario", data={"accion": "ajuste", "id": guantes()["id"], "cantidad": "-1"})
caso("Un error se corrige con un ajuste (negativo si faltan)", guantes()["cantidad"] == 6)
try:
    base.execute("UPDATE movimientos_inventario SET cantidad = 100")
    editable = True
except psycopg.errors.RaiseException:
    editable = False
caso("Los movimientos no se editan ni se borran: el historial no se puede alterar", not editable)
caso("La Facultad ve los últimos movimientos", "Ajuste" in texto(admin.get("/inventario")))

# Cuentas
caso("Solo la Facultad administra las cuentas", grace.get("/cuentas").status_code == 302)
nueva = {"accion": "crear", "cif": "00000009", "nombre": "Nuevo Estudiante", "rol": "estudiante",
         "correo": "nuevo@uam.edu.ni", "anio": "2"}
admin.post("/cuentas", data={**nueva, "clave": "corta"})
caso("La contraseña de una cuenta nueva tiene al menos 8 caracteres", cuantos("cuentas") == 5)
admin.post("/cuentas", data={**nueva, "correo": "", "clave": "clave-segura-1"})
admin.post("/cuentas", data={**nueva, "anio": "", "clave": "clave-segura-1"})
caso("La cuenta necesita correo y, si es de estudiante, el año de la carrera (el modelo lo exige)", cuantos("cuentas") == 5)
admin.post("/cuentas", data={**nueva, "clave": "clave-segura-1"})
admin.post("/cuentas", data={**nueva, "cif": "00000002", "clave": "clave-segura-1"})
admin.post("/cuentas", data={**nueva, "cif": "00000010", "clave": "clave-segura-1"})
caso("La Facultad crea una cuenta con su contraseña, guardada cifrada; el CIF y el correo no se repiten",
     cuantos("cuentas") == 6 and uno("SELECT clave_hash FROM cuentas WHERE cif = '00000009'")["clave_hash"] != "clave-segura-1")
try:
    base.execute("INSERT INTO cuentas (cif, nombre, correo, rol, clave_hash) VALUES ('Z1', 'Z', 'z@uam.edu.ni', 'estudiante', 'x')")
    sin_anio = True
except psycopg.errors.CheckViolation:
    sin_anio = False
caso("La base no deja un estudiante sin año de la carrera", not sin_anio)
nuevo = m.app.test_client()
caso("La cuenta nueva entra con su contraseña",
     nuevo.post("/", data={"cif": "00000009", "clave": "clave-segura-1"}).status_code == 302)
admin.post("/cuentas", data={"accion": "activa", "cif": "00000009"})
caso("Una cuenta desactivada no entra y la sesion que tenia abierta se cierra",
     "desactivada" in texto(m.app.test_client().post("/", data={"cif": "00000009", "clave": "clave-segura-1"}))
     and nuevo.get("/menu").status_code == 302)
NUEVO = id_de("00000009")
caso("A un estudiante desactivado no se le pasan pacientes",
     f'value="{NUEVO}"' not in texto(admin.get(f"/pacientes/{pid}/traspaso")))
admin.post("/cuentas", data={"accion": "activa", "cif": "00000007"})
caso("La Facultad no desactiva su propia cuenta", uno("SELECT activa FROM cuentas WHERE cif = '00000007'")["activa"])
admin.post("/cuentas", data={"accion": "activa", "cif": "00000009"})
for _ in range(m.FALLOS_MAXIMOS):
    m.app.test_client().post("/", data={"cif": "00000009", "clave": "equivocada"})
admin.post("/cuentas", data={"accion": "clave", "cif": "00000009", "clave": "otra-clave-2"})
caso("Ponerle una contraseña nueva desbloquea la cuenta",
     m.app.test_client().post("/", data={"cif": "00000009", "clave": "otra-clave-2"}).status_code == 302)

# Registro con el código de una clase: el estudiante nuevo crea su propia cuenta
anon = m.app.test_client()
reg = f"/registro/{codigos[2]}"
caso("La página del código abre sin sesión y dice de qué clase es", "Operatoria II" in texto(anon.get(reg)))
datos = {"accion": "crear", "cif": "R001", "nombre": "Rosa Registro", "clave": "mi-clave-123", "clave2": "mi-clave-123",
         "correo": "rosa@uam.edu.ni", "anio": "3"}
anon.post("/registro/codigo-inventado", data=datos)
caso("Con un código inventado no se crea ninguna cuenta",
     cuantos("cuentas") == 6 and "ya no sirve" in texto(anon.get("/registro/codigo-inventado")))
for mal in ({"cif": "R 001"}, {"cif": "00000002"}, {"nombre": ""}, {"clave": "corta", "clave2": "corta"},
            {"clave2": "otra-clave-123"}, {"correo": "rosa"}, {"correo": "nuevo@uam.edu.ni"}, {"anio": "9"}):
    anon.post(reg, data={**datos, **mal})
caso("Para registrarse hace falta un CIF y un correo nuevos, el nombre, el año y una contraseña de 8 caracteres escrita dos veces",
     cuantos("cuentas") == 6)
entra = anon.post(reg, data=datos)
rosa_r = uno("SELECT * FROM cuentas WHERE cif = 'R001'") or {}
caso("El estudiante crea su cuenta y queda en la clase del código, con la contraseña cifrada",
     rosa_r.get("nombre") == "Rosa Registro" and rosa_r["rol"] == "estudiante" and rosa_r["clase_id"] == KLASE
     and "mi-clave-123" not in rosa_r["clave_hash"] and rosa_r["correo"] == "rosa@uam.edu.ni" and rosa_r["anio"] == 3)
caso("Al registrarse queda dentro del sistema como estudiante, sin pacientes de nadie",
     entra.status_code == 302 and "Rosa Registro (Estudiante)" in texto(anon.get("/menu"))
     and "Luis Pérez" not in texto(anon.get("/pacientes")))
caso("Después entra con su CIF y la contraseña que creó",
     m.app.test_client().post("/", data={"cif": "R001", "clave": "mi-clave-123"}).status_code == 302)
caso("El registro queda en la bitácora",
     uno("SELECT 1 FROM bitacora WHERE cif_registrado = 'R001' AND accion LIKE 'Creó su cuenta%%Operatoria II'"))
m.app.test_client().post(reg, data={**datos, "cif": "R002", "correo": "r2@uam.edu.ni"})
caso("Con el código ya usado no se registra nadie más", cuantos("cuentas") == 7)
base.execute("UPDATE invitaciones SET creado_en = now() - interval '2 days', vence_en = now() - interval '1 day'"
             " WHERE codigo = %s", (codigos[3],))
m.app.test_client().post(f"/registro/{codigos[3]}", data={**datos, "cif": "R005", "correo": "r5@uam.edu.ni"})
caso("Pasada la fecha el código vence", cuantos("cuentas") == 7 and "ya venció" in texto(m.app.test_client().get(f"/registro/{codigos[3]}")))
caso("El profesor ve quiénes entraron a su clase y los códigos usados",
     all(t in texto(profe.get(f"/clases/{KLASE}")) for t in ("Rosa Registro", "Grace Alvarado", "usados")))
# Con el QR de otro profesor, el estudiante pasa a esa clase (un estudiante está en una clase a la vez)
gustavo.post("/clases", data={"nombre": "Periodoncia I", "periodo": "2026-II"})
OTRA = uno("SELECT id FROM clases WHERE nombre = 'Periodoncia I'")["id"]
gustavo.post(f"/clases/{OTRA}", data={"cantidad": "2", "dias": "7"})
de_otra = [r["codigo"] for r in base.execute("SELECT codigo FROM invitaciones WHERE clase_id = %s ORDER BY id", (OTRA,))]
r001 = cliente("R001")
r001.post("/", data={"cif": "R001", "clave": "mi-clave-123"})
r001.post(f"/registro/{de_otra[0]}", data={"accion": "unirse"})
caso("Con el código de otro profesor el estudiante pasa a esa clase y queda en la bitácora de dónde venía",
     uno("SELECT clase_id FROM cuentas WHERE cif = 'R001'")["clase_id"] == OTRA
     and uno("SELECT 1 FROM bitacora WHERE cif_registrado = 'R001' AND detalle LIKE %s", f"%clase {OTRA} (antes: {KLASE})"))
anon2 = m.app.test_client()
anon2.post(f"/registro/{de_otra[1]}", data={"accion": "entrar"})
anon2.post("/", data={"cif": "00000009", "clave": "otra-clave-2"})
caso("Si ya tiene cuenta, inicia sesión desde el código y vuelve a él para entrar a la clase",
     "Entrar a la clase" in texto(anon2.get(f"/registro/{de_otra[1]}")))

# ---- RF06/RNF06: el profesor ve a los pacientes de los estudiantes de su clase; RF10: turnos clínicos
vistos = lambda c: texto(c.get("/pacientes"))  # noqa: E731
caso("El profesor de la clase ve a los pacientes de su estudiante y otro profesor no (RNF06)",
     "Luis Pérez" in vistos(profe) and "Luis Pérez" not in vistos(gustavo))
grace.post("/cuentas/00000005", data={"accion": "datos", "nombre": "X", "rol": "estudiante", "correo": "x@uam.edu.ni",
                                      "anio": "4", "clase": OTRA})
caso("Solo la Facultad cambia la clase de un estudiante en su ficha",
     uno("SELECT clase_id FROM cuentas WHERE id = %s", GABRIEL)["clase_id"] == KLASE)
admin.post("/cuentas/00000005", data={"accion": "datos", "nombre": "Gabriel Espinoza", "rol": "estudiante",
                                      "correo": "gabriel@uam.edu.ni", "anio": "4", "clase": OTRA})
caso("La Facultad pone correo, año y clase en la ficha de la cuenta",
     uno("SELECT correo, anio, clase_id FROM cuentas WHERE id = %s", GABRIEL)
     == {"correo": "gabriel@uam.edu.ni", "anio": 4, "clase_id": OTRA})
caso("Al cambiar de clase, sus pacientes pasan a verse por el profesor de la clase nueva",
     "Luis Pérez" in vistos(gustavo) and "Luis Pérez" not in vistos(profe))
caso("Lo que su profesor anterior ya aprobó sigue aprobado",
     paciente()["aprobado_por_id"] == PROFE and uno("SELECT aprobado_por_id FROM tratamientos WHERE id = %s", hecho)["aprobado_por_id"] == PROFE)
admin.post("/cuentas/00000005", data={"accion": "datos", "nombre": "Gabriel Espinoza", "rol": "estudiante",
                                      "correo": "gabriel@uam.edu.ni", "anio": "4", "clase": KLASE})
admin.post("/cuentas/00000005", data={"accion": "datos", "nombre": "Gabriel Espinoza", "rol": "estudiante",
                                      "correo": "gabriel@uam.edu.ni", "anio": ""})
caso("Un estudiante sin año de la carrera no se guarda", uno("SELECT anio FROM cuentas WHERE id = %s", GABRIEL)["anio"] == 4)
est = "00000005"
admin.post(f"/cuentas/{est}", data={"accion": "turno", "dia": "1", "desde": "10:00", "hasta": "08:00"})
admin.post(f"/cuentas/{est}", data={"accion": "turno", "dia": "1", "desde": "06:00", "hasta": "08:00"})
admin.post(f"/cuentas/{est}", data={"accion": "turno", "dia": "7", "desde": "08:00", "hasta": "10:00"})
caso("Un turno al revés, fuera del horario de la clínica o en domingo no se guarda", cuantos("turnos") == 0)
admin.post(f"/cuentas/{est}", data={"accion": "turno", "dia": "1", "desde": "08:00", "hasta": "12:00"})
admin.post(f"/cuentas/{est}", data={"accion": "turno", "dia": "1", "desde": "11:00", "hasta": "13:00"})
caso("La Facultad asigna turnos clínicos y no deja que se crucen (RF10)", cuantos("turnos") == 1)
try:
    base.execute("INSERT INTO turnos (estudiante_id, dia, desde, hasta) VALUES (%s, 1, '09:00', '10:00')", (GABRIEL,))
    encimado = True
except psycopg.errors.ExclusionViolation:
    encimado = False
caso("Ni directo en la base se enciman dos turnos", not encimado)
lunes = HOY + timedelta(days=8 - HOY.isoweekday())
martes = lunes + timedelta(days=1)
with m.app.app_context():
    caso("Con turnos, una cita fuera de ellos no se puede agendar",
         "fuera de los turnos" in m.error_de_cita(m.rango_cita(martes.isoformat(), "09:00", 60), S5, pid, GABRIEL))
    caso("Dentro del turno sí se puede",
         m.error_de_cita(m.rango_cita(lunes.isoformat(), "09:00", 60), S5, pid, GABRIEL) is None)
    caso("Pero no si se sale del turno antes de terminar",
         "fuera de los turnos" in m.error_de_cita(m.rango_cita(lunes.isoformat(), "11:30", 60), S5, pid, GABRIEL))
t_id = uno("SELECT id FROM turnos")["id"]
admin.post(f"/cuentas/{est}", data={"accion": "quitar_turno", "id": t_id})
caso("La Facultad quita un turno", cuantos("turnos") == 0)

# Dar de alta y eliminar pacientes: solo la Facultad
cerrar = f"/pacientes/{pid}/cerrar"
caso("Solo la Facultad da de alta o elimina pacientes",
     gabriel.get(cerrar).status_code == 302 and profe.get(cerrar).status_code == 302)
gabriel.post(cerrar, data={"accion": "finalizar", "texto": "Alta"})
profe.post(cerrar, data={"accion": "finalizar", "texto": "Alta"})
caso("Ni el estudiante ni el profesor le dan de alta por la fuerza", paciente()["finalizado_en"] is None)
admin.post(cerrar, data={"accion": "finalizar", "texto": ""})
caso("Para darle de alta hay que decir como termino el tratamiento", paciente()["finalizado_en"] is None)
admin.post(cerrar, data={"accion": "finalizar", "texto": "Completó el plan y fue dado de alta"})
caso("La Facultad da de alta al paciente",
     paciente()["finalizado_en"] is not None and paciente()["finalizado_por_id"] == ADMIN
     and "Completó el plan y fue dado de alta" in texto(gabriel.get(f"/pacientes/{pid}")))
dientes_antes, citas_antes = cuantos("dientes"), cuantos("citas")
gabriel.post(odonto, data={"accion": "diente", "diente": "21", "estado": "caries"})
gabriel.post("/citas", data={"paciente_id": pid, "fecha": MANANA, "hora": "14:00", "sala": S5, "razon": "Control"})
gabriel.post(f"/pacientes/{pid}/editar", data={"nombre": "Cambiado", "cedula": "001-010190-0001A", "telefono": ""})
caso("De alta, el estudiante ya no cambia el odontograma, las citas ni los datos",
     cuantos("dientes") == dientes_antes and cuantos("citas") == citas_antes and paciente()["nombre"] == "Luis Pérez")
caso("De alta, ya no sale para agendarle citas", "Luis Pérez (001-010190-0001A)" not in texto(gabriel.get("/citas")))
admin.post(trat, data={"accion": "pago", "orden": OID, "monto": "300.50", "numero": "R-301"})
caso("De alta, la Facultad todavia registra pagos para saldar la cuenta", cuantos("recibos") == 3)
gabriel.post("/control", data={"accion": "subir", "orden": OID, "numero": "A-0120", "monto": "50", "foto": recibo_jpg()})
caso("De alta, todavía se sube un recibo que llegó tarde", cuantos("recibos") == 4)
tarde = uno("SELECT id, url FROM recibos WHERE numero = 'A-0120'")
gabriel.post("/control", data={"accion": "quitar", "id": tarde["id"]})
caso("Un recibo no se borra: queda anulado, con su foto, y deja de contar en el saldo",
     cuantos("recibos") == 4 and uno("SELECT anulado_en FROM recibos WHERE id = %s", tarde["id"])["anulado_en"] is not None
     and m.ruta_local(tarde["url"]).exists()
     and uno("SELECT pagado FROM resumen_ordenes WHERE id = %s", OID)["pagado"] == Decimal("950.50"))
caso("De alta, ya no se cambia de estudiante", admin.get(f"/pacientes/{pid}/traspaso").status_code == 302)
admin.post(cerrar, data={"accion": "reabrir"})
gabriel.post(odonto, data={"accion": "diente", "diente": "21", "estado": "caries"})
caso("Si se anula el alta, el estudiante vuelve a poder cambiarlo",
     paciente()["finalizado_en"] is None and cuantos("dientes") == dientes_antes + 1)

gabriel.post("/pacientes", data={"accion": "registrar", "nombre": "Repetido Por Error", "cedula": "001-030390-0003C"})
borrar = uno("SELECT id FROM pacientes WHERE cedula = '001-030390-0003C'")["id"]
gabriel.post(f"/pacientes/{borrar}/odontograma", data={"accion": "foto", "foto": recibo_jpg()})
foto_borrar = uno("SELECT url FROM fotos WHERE paciente_id = %s", borrar)["url"]
gabriel.post("/citas", data={"paciente_id": borrar, "fecha": MANANA, "hora": "13:00", "sala": S5, "razon": "Valoración"})
admin.post(f"/pacientes/{borrar}/cerrar", data={"accion": "eliminar", "cedula": "001-040490-0004D", "texto": "Duplicado"})
admin.post(f"/pacientes/{borrar}/cerrar", data={"accion": "eliminar", "cedula": "001-030390-0003C", "texto": ""})
caso("Para eliminarlo hay que escribir su cedula y el motivo", uno("SELECT eliminado_en FROM pacientes WHERE id = %s", borrar)["eliminado_en"] is None)
gabriel.post(f"/pacientes/{borrar}/cerrar", data={"accion": "eliminar", "cedula": "001-030390-0003C", "texto": "Duplicado"})
caso("El estudiante no elimina pacientes", uno("SELECT eliminado_en FROM pacientes WHERE id = %s", borrar)["eliminado_en"] is None)
admin.post(f"/pacientes/{borrar}/cerrar", data={"accion": "eliminar", "cedula": "001-030390-0003C", "texto": "Duplicado"})
caso("Eliminar no borra: el paciente deja de verse pero su expediente se conserva (RNF07)",
     cuantos("pacientes") == 3 and uno("SELECT eliminado_en FROM pacientes WHERE id = %s", borrar)["eliminado_en"]
     and uno("SELECT 1 FROM citas WHERE paciente_id = %s", borrar) is not None
     and "Repetido Por Error" not in texto(gabriel.get("/pacientes")) and gabriel.get(f"/pacientes/{borrar}").status_code == 302)
caso("Sus citas que venían se cancelan, para que la clínica quede libre",
     uno("SELECT estado FROM citas WHERE paciente_id = %s", borrar)["estado"] == "cancelada")
caso("Sus fotos también se conservan", m.ruta_local(foto_borrar).exists())
caso("La cédula de un paciente eliminado no se registra otra vez: hay que restaurarlo",
     "paciente eliminado" in texto(gabriel.post("/pacientes", data={"accion": "registrar", "nombre": "Otra vez",
                                                                    "cedula": "001030390-0003C"})) and cuantos("pacientes") == 3)
caso("La eliminacion queda en la bitacora con el motivo",
     "Eliminó al paciente Repetido Por Error (001-030390-0003C): Duplicado" in texto(admin.get("/bitacora")))
admin.post("/pacientes", data={"accion": "restaurar", "id": borrar})
caso("La Facultad restaura al paciente eliminado con todo su expediente",
     uno("SELECT eliminado_en FROM pacientes WHERE id = %s", borrar)["eliminado_en"] is None
     and "Repetido Por Error" in texto(gabriel.get("/pacientes")))

# ---- Historia clínica, registro con datos generales y expediente correlativo (RF03, RF05)
gabriel.post("/pacientes", data={"accion": "registrar", "nombre": "Rosa Méndez", "cedula": "001-150885-0009F",
                                 "telefono": "88112233", "nacimiento": "1985-08-15", "sexo": "F",
                                 "direccion": "Managua", "emergencia_nombre": "Juan Méndez",
                                 "emergencia_telefono": "8877 6655"})
rosa = uno("SELECT * FROM pacientes WHERE nombre = 'Rosa Méndez'")
caso("Sin número de expediente físico se le da uno correlativo (RF03)", rosa["expediente"] == f"EXP-{rosa['id']:05d}")
caso("La fecha de nacimiento y el sexo quedan en el paciente; el contacto de emergencia, en la historia",
     rosa["sexo"] == "F" and rosa["nacimiento"] == date(1985, 8, 15)
     and uno("SELECT emergencia_telefono FROM historias WHERE paciente_id = %s", rosa["id"])["emergencia_telefono"] == "88776655")
gabriel.post("/pacientes", data={"accion": "registrar", "nombre": "Futuro", "cedula": "001-150885-0019F",
                                 "nacimiento": "2999-01-01"})
caso("La fecha de nacimiento no puede ser después de hoy", uno("SELECT 1 FROM pacientes WHERE nombre = 'Futuro'") is None)
gabriel.post("/pacientes", data={"accion": "registrar", "nombre": "Mismo Expediente", "cedula": "001-150885-0029F",
                                 "expediente": "EXP-0457"})
caso("El número de expediente no se repite", uno("SELECT 1 FROM pacientes WHERE nombre = 'Mismo Expediente'") is None)
hist = f"/pacientes/{rosa['id']}/historia"
de_rosa = lambda c: uno(f"SELECT {c} FROM historias WHERE paciente_id = %s", rosa["id"])[c]  # noqa: E731
vacia = {c: "" for c in {**m.DEL_PACIENTE, **m.HISTORIA}}
gabriel.post(hist, data={**vacia, "presion": "alta"})
caso("La presión se escribe como sistólica/diastólica", not de_rosa("presion"))
gabriel.post(hist, data={**vacia, "sexo": "F", "alergias": "Penicilina", "presion": "150/95", "diagnostico": "Caries en 36"})
caso("El estudiante a cargo guarda la historia clínica, aunque esté a medias (RF05)",
     de_rosa("diagnostico") == "Caries en 36" and de_rosa("actualizada_por_id") == GABRIEL)
exp_rosa = texto(gabriel.get(f"/pacientes/{rosa['id']}"))
caso("El expediente avisa de las alergias y de la presión alta",
     "Alergias: Penicilina" in exp_rosa and "Presión arterial alta: 150/95" in exp_rosa)
caso("Los cambios de la historia quedan en la bitácora (historial de cambios)",
     uno("SELECT 1 FROM bitacora WHERE accion LIKE 'Actualizó la historia clínica de Rosa Méndez:%%alergias%%'"))
grace.post(hist, data={**vacia, "alergias": "Ninguna"})
caso("Otro estudiante no cambia la historia", de_rosa("alergias") == "Penicilina")
caso("El profesor la lee pero no la cambia",
     "Penicilina" in texto(profe.get(hist)) and 'name="alergias"' not in texto(profe.get(hist)))
caso("Guardada a medias, la historia queda como borrador (RF05)",
     de_rosa("completa") is False and "Borrador" in texto(gabriel.get(hist)) and "Borrador" in exp_rosa)
gabriel.post(hist, data={**vacia, "sexo": "F", "alergias": "Penicilina", "presion": "150/95",
                         "diagnostico": "Caries en 36", "accion": "completa"})
caso("Si falta algo no se marca completa, pero lo escrito se guarda y se dice qué falta",
     de_rosa("completa") is False and de_rosa("diagnostico") == "Caries en 36"
     and "fecha de nacimiento" in texto(gabriel.get(hist)))
llena = {**vacia, "nacimiento": "1985-08-15", "sexo": "F", "emergencia_nombre": "Juan Méndez",
         "emergencia_telefono": "88776655", "antecedentes": "Ninguno", "alergias": "Penicilina",
         "medicamentos": "Ninguna", "presion": "150/95", "examen_intraoral": "Caries en 36",
         "diagnostico": "Caries en 36"}
gabriel.post(hist, data={**llena, "accion": "completa"})
caso("Con todo lo necesario se marca completa y queda en la bitácora",
     de_rosa("completa") is True and "Completa" in texto(gabriel.get(f"/pacientes/{rosa['id']}"))
     and uno("SELECT 1 FROM bitacora WHERE accion = 'Marcó como completa la historia clínica de Rosa Méndez'"))
gabriel.post(hist, data={**llena, "accion": "borrador"})
caso("Se puede volver a borrador para seguir corrigiendo", de_rosa("completa") is False)
gabriel.post(hist, data={**llena, "accion": "completa"})
caso("La historia sale en el expediente en PDF", gabriel.get(f"/pacientes/{rosa['id']}/expediente.pdf").data[:4] == b"%PDF")

# ---- Radiografías y límite de las imágenes (RF05, RNF09)
odonto_rosa = f"/pacientes/{rosa['id']}/odontograma"
gabriel.post(odonto_rosa, data={"accion": "foto", "tipo": "radiografia", "foto": recibo_jpg()})
caso("Se suben radiografías junto a las fotos",
     uno("SELECT tipo FROM fotos WHERE paciente_id = %s", rosa["id"])["tipo"] == "radiografia")
gabriel.post(odonto_rosa, data={"accion": "pedir"})
caso("Con solo una radiografía no se pide la revisión: falta una foto del paciente",
     uno("SELECT odonto_revision FROM pacientes WHERE id = %s", rosa["id"])["odonto_revision"] == "")
pesada = io.BytesIO(b"\xff\xd8" + os.urandom(m.IMAGEN_MAXIMA))
gabriel.post(odonto_rosa, data={"accion": "foto", "foto": (pesada, "pesada.jpg")})
gif = io.BytesIO()
Image.new("RGB", (50, 50), "blue").save(gif, "GIF")
gif.seek(0)
gabriel.post(odonto_rosa, data={"accion": "foto", "foto": (gif, "boca.gif")})
caso("Las imágenes de más de 5 MB o que no son JPG ni PNG no se aceptan (RNF09)",
     uno("SELECT count(*) AS n FROM fotos WHERE paciente_id = %s", rosa["id"])["n"] == 1)

# ---- Tarifario con código, área e historial de precios (RF15)
admin.post("/tarifario", data={"accion": "agregar", "nombre": "Gratis", "precio": "0", "codigo": "G-1"})
caso("El precio tiene que ser mayor que cero", uno("SELECT 1 FROM tarifario WHERE nombre = 'Gratis'") is None)
admin.post("/tarifario", data={"accion": "agregar", "nombre": "Extracción simple", "precio": "500", "codigo": "cir-01",
                               "area": "Cirugía"})
caso("El procedimiento lleva código y área clínica",
     uno("SELECT codigo, area FROM tarifario WHERE nombre = 'Extracción simple'") == {"codigo": "CIR-01", "area": "Cirugía"})
admin.post("/tarifario", data={"accion": "agregar", "nombre": "Otra extracción", "precio": "5", "codigo": "CIR-01"})
caso("El código no se repite", uno("SELECT 1 FROM tarifario WHERE nombre = 'Otra extracción'") is None)
admin.post("/tarifario", data={"accion": "precio", "id": resina, "precio": "950"})
periodos = base.execute("SELECT precio, vigente_desde::text, vigente_hasta::text FROM precios_anteriores WHERE tarifario_id = %s"
                        " ORDER BY vigente_hasta", (resina,)).fetchall()
caso("Cada precio anterior queda con el periodo en que estuvo vigente, uno detrás del otro",
     [p["precio"] for p in periodos] == [Decimal("800.50"), Decimal("900")] and periodos[0]["vigente_desde"] == "-infinity"
     and periodos[0]["vigente_hasta"] == periodos[1]["vigente_desde"]
     and "Precios anteriores" in texto(admin.get("/tarifario")))

# ---- Plan por fases, avance y orden de cobro (RF09, RF16)
trat_rosa = f"/pacientes/{rosa['id']}/tratamientos"
gabriel.post(trat_rosa, data={"accion": "anotar", "procedimiento": resina, "fase": "Inventada"})
gabriel.post(trat_rosa, data={"accion": "anotar", "procedimiento": resina, "fase": "rehabilitacion"})
caso("Cada tratamiento va en una fase del plan (RF09)",
     [t["fase"] for t in base.execute("SELECT fase FROM tratamientos WHERE paciente_id = %s", (rosa["id"],))] == ["rehabilitacion"])
caso("Se ve el avance del plan", "0 de 1" in texto(gabriel.get(trat_rosa)))
admin.post(trat_rosa, data={"accion": "orden"})
orden = uno("SELECT * FROM resumen_ordenes WHERE paciente_id = %s", rosa["id"])
caso("La Facultad también genera la orden, con el total del tarifario (RF16)",
     orden["total"] == Decimal("950") and uno("SELECT orden_id FROM tratamientos WHERE paciente_id = %s", rosa["id"])
     ["orden_id"] == orden["id"])
admin.post(trat_rosa, data={"accion": "orden"})
caso("Lo que ya está en una orden no entra en otra", cuantos("ordenes") == 2)
gabriel.post(trat_rosa, data={"accion": "quitar", "id": uno("SELECT id FROM tratamientos WHERE paciente_id = %s",
                                                              rosa["id"])["id"]})
caso("Lo que ya está en una orden no se quita del plan",
     uno("SELECT count(*) AS n FROM tratamientos WHERE paciente_id = %s", rosa["id"])["n"] == 1)
hoja_orden = texto(gabriel.get(f"/ordenes/{orden['id']}"))
caso("La orden se ve para imprimirla, con número y total", f"Orden de cobro Nº {orden['id']}" in hoja_orden
     and "C$ 950.00" in hoja_orden and grace.get(f"/ordenes/{orden['id']}").status_code == 302)
gabriel.post(f"/ordenes/{orden['id']}", data={"numero": "C-1"})
caso("El estudiante no cobra la orden", uno("SELECT pagado FROM resumen_ordenes WHERE id = %s", orden["id"])["pagado"] == 0)
admin.post(f"/ordenes/{orden['id']}", data={"numero": "C-950"})
admin.post(f"/ordenes/{orden['id']}", data={"accion": "anular"})
caso("La Facultad cobra la orden: queda el recibo por todo el saldo y la orden pagada",
     uno("SELECT estado FROM resumen_ordenes WHERE id = %s", orden["id"])["estado"] == "pagada"
     and uno("SELECT monto FROM recibos WHERE numero = 'C-950'")["monto"] == Decimal("950"))
caso("Una orden con pagos no se anula", uno("SELECT anulado_en FROM ordenes WHERE id = %s", orden["id"])["anulado_en"] is None)
gabriel.post(trat_rosa, data={"accion": "anotar", "procedimiento": resina})
profe.post(trat_rosa, data={"accion": "orden"})
tercera = uno("SELECT max(id) AS id FROM ordenes")["id"]
admin.post(f"/ordenes/{tercera}", data={"accion": "anular"})
caso("Una orden sin pagos se anula y sus tratamientos quedan libres para otra",
     uno("SELECT estado FROM resumen_ordenes WHERE id = %s", tercera)["estado"] == "anulada"
     and uno("SELECT count(*) AS n FROM tratamientos WHERE orden_id = %s", tercera)["n"] == 0)

# ---- Solicitudes de materiales (RF17)
admin.post("/inventario", data={"accion": "agregar", "nombre": "Algodón", "unidad": "rollos", "cantidad": "3", "minimo": "1"})
algodon = uno("SELECT id FROM materiales WHERE nombre = 'Algodón'")["id"]
gabriel.post("/materiales", data={"material": algodon, "cantidad": "2", "para": AYER})
caso("La solicitud es para una cita de hoy en adelante", cuantos("solicitudes") == 0)
gabriel.post("/materiales", data={"material": algodon, "cantidad": "5", "para": MANANA})
gabriel.post("/materiales", data={"material": algodon, "cantidad": "2", "para": MANANA})
mucho = uno("SELECT id FROM solicitudes WHERE cantidad = 5")["id"]
poco = uno("SELECT id FROM solicitudes WHERE cantidad = 2")["id"]
estado_sol = lambda i: uno("SELECT estado FROM solicitudes WHERE id = %s", i)["estado"]  # noqa: E731
caso("La Facultad ve en Inicio las solicitudes que esperan", "Hay 2 solicitudes de material" in texto(admin.get("/menu")))
admin.post("/inventario", data={"accion": "entregar", "id": mucho})
caso("No se entrega más de lo que hay", estado_sol(mucho) == "pendiente")
admin.post("/inventario", data={"accion": "entregar", "id": poco})
caso("Al entregar se aprueba y sale de la bodega en un movimiento ligado a la solicitud",
     estado_sol(poco) == "aprobada" and uno("SELECT cantidad FROM existencias WHERE id = %s", algodon)["cantidad"] == 1
     and uno("SELECT tipo, cantidad FROM movimientos_inventario WHERE solicitud_id = %s", poco) == {"tipo": "salida", "cantidad": 2})
admin.post("/inventario", data={"accion": "rechazar", "id": mucho})
admin.post("/inventario", data={"accion": "rechazar", "id": mucho, "nota": "No alcanza; pida 1"})
caso("Para rechazar hace falta el motivo, y el estudiante lo ve",
     estado_sol(mucho) == "rechazada" and "No alcanza; pida 1" in texto(gabriel.get("/materiales")))
caso("Solo el estudiante pide materiales", profe.get("/materiales").status_code == 302)

# ---- Corregir cuentas y filtrarlas (RF02)
admin.post("/cuentas/00000004", data={"accion": "datos", "nombre": "Dr. Gustavo Rivas Pérez", "rol": "profesor",
                                      "correo": "gustavo@uam.edu.ni"})
caso("La Facultad corrige el nombre y el correo de una cuenta (RF02)",
     uno("SELECT nombre, correo FROM cuentas WHERE cif = '00000004'")
     == {"nombre": "Dr. Gustavo Rivas Pérez", "correo": "gustavo@uam.edu.ni"})
admin.post("/cuentas/00000007", data={"accion": "datos", "nombre": "Gina Saavedra", "rol": "estudiante",
                                      "correo": "gina@uam.edu.ni", "anio": "1"})
caso("Nadie se cambia el rol a sí mismo", uno("SELECT rol FROM cuentas WHERE cif = '00000007'")["rol"] == "facultad")
solo_profes = texto(admin.get("/cuentas", query_string={"rol": "profesor"}))
caso("Las cuentas se filtran por rol", "Gustavo Rivas Pérez" in solo_profes and "Gabriel Espinoza" not in solo_profes)
caso("Una cuenta con correo provisional sale marcada para corregirla",
     "Correo pendiente" in texto(admin.get("/cuentas")) and "correo provisional" in texto(admin.get("/cuentas/00000001")))

# ---- Fallas con su tipo e historial (RF14)
S9 = sala("Clínica 9")
grace.post("/salas", data={"accion": "reportar", "sala": S9, "motivo": "Hay restos en la escupidera", "tipo": "limpieza"})
caso("Al reportar se dice qué falla en la clínica (RF14)", uno("SELECT tipo FROM reportes WHERE sala_id = %s", S9)["tipo"] == "limpieza")
admin.post("/salas", data={"accion": "resolver", "reporte": uno("SELECT id FROM reportes WHERE sala_id = %s", S9)["id"]})
caso("La Facultad ve el historial de fallas solucionadas",
     all(t in texto(admin.get("/salas")) for t in ("Historial de fallas solucionadas", "Hay restos en la escupidera")))

# ---- Inicio con la jornada y recordatorios (RF13, RF12)
def nueva_cita(fecha, hora, estado="programada", aprobada=True, razon="Control", minutos=10):
    """Una cita de Rosa (de Gabriel, de la clase de la Dra. Mendoza) directo en la base, en la Clínica 1."""
    return uno("INSERT INTO citas (paciente_id, sala_id, estudiante_id, creado_por_id, aprobado_por_id, aprobado_en,"
               " inicio, fin, estado, razon) VALUES (%s, %s, %s, %s, %s, CASE WHEN %s THEN now() END,"
               " (%s || ' ' || %s)::timestamp AT TIME ZONE 'America/Managua',"
               " (%s || ' ' || %s)::timestamp AT TIME ZONE 'America/Managua' + %s * interval '1 minute', %s, %s)"
               " RETURNING id", rosa["id"], S1, GABRIEL, GABRIEL, PROFE if aprobada else None, aprobada,
               fecha, hora, fecha, hora, minutos, estado, razon)["id"]
if HOY_ATIENDE:
    nueva_cita(m.hoy(), "07:00", razon="Control de hoy")
    caso("El Inicio del estudiante muestra sus citas de hoy (RF13)", "Control de hoy" in texto(gabriel.get("/menu")))
    inicio_profe = texto(profe.get("/menu"))
    caso("Al profesor le salen las citas de hoy de sus estudiantes y los pacientes por aprobar",
         "Control de hoy" in inicio_profe and "Pacientes por aprobar" in inicio_profe and "Rosa Méndez" in inicio_profe)
    caso("A otro profesor no le salen", "Control de hoy" not in texto(gustavo.get("/menu")))
if MANANA_ATIENDE:
    nueva_cita(MANANA, "10:15", razon="Control de mañana")
    caso("El estudiante ve las citas aprobadas de mañana sin confirmar con el recordatorio por WhatsApp (RF12)",
         "wa.me/50588112233" in texto(gabriel.get("/menu")))

# ---- Bitácora con filtros (RF18)
por_accion = texto(admin.get("/bitacora", query_string={"accion": "solicitud"}))
caso("La bitácora se filtra por acción (RF18)", "Rechazó la solicitud" in por_accion and "Entró al sistema" not in por_accion)
por_usuario = texto(admin.get("/bitacora", query_string={"usuario": "00000005"}))
caso("y por usuario", "Pidió 5 rollos de Algodón" in por_usuario and "Rechazó la solicitud" not in por_usuario)
caso("y por fechas", "Ningún movimiento con esos filtros" in texto(admin.get("/bitacora", query_string={"hasta": "2020-01-01"})))

m.crear_tablas()
caso("Crear las tablas otra vez no borra ni duplica nada", cuantos("pacientes") == 4 and cuantos("cuentas") == 7
     and cuantos("salas") == 51)
os.environ["FLY_APP_NAME"] = "prueba"
try:
    m.crear_tablas()
    detenido = False
except SystemExit:
    detenido = True
finally:
    del os.environ["FLY_APP_NAME"]
caso("Publicado en Fly sin almacenamiento configurado, el despliegue se detiene antes de cambiar la base", detenido)

# ---- Recordatorios automáticos por WhatsApp (RF12). WhatsApp se simula: no sale ningún mensaje
base.execute("UPDATE pacientes SET telefono = '88112233', finalizado_en = NULL, eliminado_en = NULL WHERE id = %s", (rosa["id"],))
base.execute("UPDATE citas SET recordada_en = now() WHERE recordada_en IS NULL")  # solo cuentan las de esta prueba
enviados = []
def envio_falso(telefono, variables, cita_id):
    if variables[2] == "07:40" and len([e for e in enviados if e[1][2] == "07:40"]) == 0:
        enviados.append(("falló", variables))
        raise OSError("Meta no respondió")
    enviados.append((telefono, variables))
    return "wamid.prueba"
m.enviar_whatsapp = envio_falso
avisos = []
m.enviar_aviso_cita = lambda telefono, variables: avisos.append((telefono, variables))
consola = m.app.test_cli_runner()
for v in ("WHATSAPP_TOKEN", "WHATSAPP_NUMERO_ID", "WHATSAPP_PLANTILLA"):
    os.environ.pop(v, None)
caso("Sin configurar WhatsApp no se envía nada",
     "no está configurado" in consola.invoke(m.recordar).output and not enviados)
os.environ.update(WHATSAPP_TOKEN="x", WHATSAPP_NUMERO_ID="1", WHATSAPP_PLANTILLA="recordatorio_cita")
m.HORARIO_RECORDATORIOS = (0, 0)
caso("Fuera del horario no se envía", "Fuera del horario" in consola.invoke(m.recordar).output and not enviados)
m.HORARIO_RECORDATORIOS = (0, 24)
recordada = lambda cid: uno("SELECT recordada_en FROM citas WHERE id = %s", cid)["recordada_en"]  # noqa: E731
if MANANA_ATIENDE:
    c_manana, c_confirmada = nueva_cita(MANANA, "07:10"), nueva_cita(MANANA, "07:20", "confirmada")
    c_pasado, c_falla = nueva_cita(PASADO, "07:30"), nueva_cita(MANANA, "07:40")
    c_sin_aprobar = nueva_cita(MANANA, "07:50", aprobada=False)
    salida = consola.invoke(m.recordar).output
    caso("Se envía el recordatorio de la cita programada de mañana, con nombre, fecha, hora y clínica (RF12)",
         ("88112233", ["Rosa Méndez", m.dmy(MANANA), "07:10", "Clínica 1"]) in enviados and recordada(c_manana))
    caso("No se recuerdan las confirmadas, las de pasado mañana ni las que el profesor no ha aprobado",
         not recordada(c_confirmada) and not recordada(c_pasado) and not recordada(c_sin_aprobar)
         and not any(e[1][2] in ("07:20", "07:30", "07:50") for e in enviados))
    caso("Si un envío falla, los demás siguen y ese queda para la próxima hora",
         "No se pudo enviar" in salida and not recordada(c_falla) and "Recordatorios enviados: 1 de 2" in salida)
    antes_envios = len(enviados)
    consola.invoke(m.recordar)
    caso("La siguiente corrida no repite los enviados y reintenta el que falló",
         len(enviados) == antes_envios + 1 and recordada(c_falla))
    caso("El envío queda en la bitácora, a nombre del sistema",
         uno("SELECT 1 FROM bitacora WHERE cuenta_id IS NULL AND cif_registrado = 'SISTEMA'"
             " AND accion LIKE 'Recordatorio automático por WhatsApp a Rosa Méndez%%'"))
    caso("El estudiante ve en Inicio que ya se envió el recordatorio", "Recordatorio enviado" in texto(gabriel.get("/menu")))
    gabriel.post("/citas", data={"accion": "reprogramar", "id": c_manana, "fecha": PASADO, "hora": "08:50", "sala": S1})
    caso("Al reprogramar, el recordatorio se vuelve a enviar para la fecha nueva cuando el profesor la apruebe",
         not recordada(c_manana) and uno("SELECT aprobado_en FROM citas WHERE id = %s", c_manana)["aprobado_en"] is None
         and len(avisos) == 0)
    profe.post("/citas", data={"accion": "aprobar", "id": c_manana})
    caso("Al aprobar la cita, al paciente le llega por WhatsApp el aviso con la fecha nueva (RF12)",
         avisos[-1:] == [("88112233", ["Rosa", m.dmy(PASADO), "08:50", "Clínica 1"])]
         and uno("SELECT 1 FROM bitacora WHERE accion LIKE 'Aviso por WhatsApp a Rosa Méndez: cita agendada%%'"))
gabriel.post("/citas", data={"paciente_id": rosa["id"], "fecha": PASADO, "hora": "11:30", "sala": S4, "razon": "Limpieza"})
antes_avisos = len(avisos)
profe.post("/citas", data={"paciente_id": rosa["id"], "fecha": PASADO, "hora": "12:30", "sala": S4, "razon": "Revisión"})
caso("La cita que agenda el estudiante no avisa todavía; la que agenda el profesor ya va aprobada y avisa (RF12)",
     len(avisos) == antes_avisos + 1 and avisos[-1] == ("88112233", ["Rosa", m.dmy(PASADO), "12:30", "Clínica 4"]))
def aviso_que_falla(telefono, variables):
    raise OSError("Meta no respondió")
m.enviar_aviso_cita = aviso_que_falla
profe.post("/citas", data={"paciente_id": rosa["id"], "fecha": PASADO, "hora": "13:30", "sala": S4, "razon": "Control"})
caso("Si el aviso falla, la cita queda agendada igual y se anota en la bitácora",
     uno("SELECT 1 FROM citas WHERE paciente_id = %s AND razon = 'Control' AND creado_por_id = %s", rosa["id"], PROFE)
     and uno("SELECT 1 FROM bitacora WHERE accion LIKE 'No se pudo avisar por WhatsApp a Rosa Méndez%%'"))
m.enviar_aviso_cita = lambda telefono, variables: avisos.append((telefono, variables))
base.execute("UPDATE pacientes SET telefono = '' WHERE id = %s", (rosa["id"],))
antes_avisos = len(avisos)
profe.post("/citas", data={"paciente_id": rosa["id"], "fecha": PASADO, "hora": "14:30", "sala": S4, "razon": "Control"})
caso("A un paciente sin teléfono no se le intenta avisar", len(avisos) == antes_avisos)
base.execute("UPDATE pacientes SET telefono = '88112233' WHERE id = %s", (rosa["id"],))

# El paciente responde con los botones del recordatorio (Meta llama a /whatsapp)
import hashlib, hmac, json  # noqa: E401,E402
os.environ.update(WHATSAPP_VERIFICAR="palabra-de-prueba", WHATSAPP_SECRETO="secreto-de-prueba")
respuestas = []
m.responder_whatsapp = lambda telefono, texto: respuestas.append((telefono, texto))
meta = m.app.test_client()
caso("Meta comprueba la dirección con la palabra acordada",
     meta.get("/whatsapp?hub.mode=subscribe&hub.verify_token=palabra-de-prueba&hub.challenge=123").data == b"123"
     and meta.get("/whatsapp?hub.mode=subscribe&hub.verify_token=otra&hub.challenge=123").status_code == 403)
def boton(payload, telefono="50588112233", firma=None):
    cuerpo = json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"from": telefono, "type": "button", "button": {"payload": payload, "text": "Sí"}}]}}]}]}).encode()
    firma = firma or "sha256=" + hmac.new(b"secreto-de-prueba", cuerpo, hashlib.sha256).hexdigest()
    return meta.post("/whatsapp", data=cuerpo, headers={"X-Hub-Signature-256": firma, "Content-Type": "application/json"})
estado_de = lambda cid: uno("SELECT estado, motivo_cancelacion FROM citas WHERE id = %s", cid)  # noqa: E731
c_si, c_no = nueva_cita(PASADO, "15:30"), nueva_cita(PASADO, "15:45")
caso("Sin la firma de Meta no se acepta nada",
     boton(f"asistira:{c_si}", firma="sha256=falsa").status_code == 403 and estado_de(c_si)["estado"] == "programada")
caso("Un botón desde otro teléfono no cambia la cita",
     boton(f"asistira:{c_si}", telefono="50599998888").status_code == 200 and estado_de(c_si)["estado"] == "programada")
boton(f"asistira:{c_si}")
caso("Si el paciente toca «Sí, asistiré», la cita queda confirmada y se le responde (RF12)",
     estado_de(c_si)["estado"] == "confirmada" and any("confirmada" in r[1] for r in respuestas))
boton(f"no_asistira:{c_no}")
caso("Si toca «No podré asistir», la cita queda cancelada con el motivo y la clínica se libera",
     estado_de(c_no) == {"estado": "cancelada", "motivo_cancelacion": "El paciente avisó por WhatsApp que no podrá asistir"})
boton(f"no_asistira:{c_si}")
caso("Una cita ya confirmada no se cambia con otro botón", estado_de(c_si)["estado"] == "confirmada")
caso("La respuesta del paciente queda en la bitácora",
     uno("SELECT 1 FROM bitacora WHERE cif_registrado = 'SISTEMA' AND accion LIKE 'Rosa Méndez confirmó su cita%%por WhatsApp'"))

# El bot: el paciente escribe y se le contesta con el menú numerado
preguntas = []
m.preguntar_whatsapp = lambda telefono, texto, botones: preguntas.append((telefono, texto, botones))
def escribir(contenido, telefono="50588112233"):
    """Manda a /whatsapp un mensaje escrito (str) o un toque en un botón del bot (dict con el id del botón)."""
    mensaje = ({"type": "text", "text": {"body": contenido}} if isinstance(contenido, str) else
               {"type": "interactive", "interactive": {"type": "button_reply", "button_reply": contenido}})
    cuerpo = json.dumps({"entry": [{"changes": [{"value": {"messages": [{"from": telefono, **mensaje}]}}]}]}).encode()
    firma = "sha256=" + hmac.new(b"secreto-de-prueba", cuerpo, hashlib.sha256).hexdigest()
    respuestas.clear()
    meta.post("/whatsapp", data=cuerpo, headers={"X-Hub-Signature-256": firma, "Content-Type": "application/json"})
    return respuestas[-1][1] if respuestas else ""
base.execute("UPDATE citas SET estado = 'cancelada' WHERE paciente_id = %s AND estado IN ('programada', 'confirmada')"
             " AND NOT EXISTS (SELECT 1 FROM tratamientos t WHERE t.cita_id = citas.id)", (rosa["id"],))
b0 = nueva_cita(MANANA, "08:00", aprobada=False)
saludo = escribir("Buenas, ¿a qué hora es mi cita?")
caso("Si el paciente escribe, el bot lo saluda por su nombre y le da el menú (RF12)",
     "Hola, Rosa" in saludo and "1. Ver mis próximas citas" in saludo and "5. Que mi estudiante me contacte" in saludo)
caso("Una cita que el profesor no ha aprobado no se confirma por WhatsApp",
     "espera la aprobación" in escribir("2") and estado_de(b0)["estado"] == "programada")
base.execute("UPDATE citas SET estado = 'cancelada' WHERE id = %s", (b0,))
b1, b2 = nueva_cita(MANANA, "14:00"), nueva_cita(PASADO, "15:50")
caso("A un número que no es de ningún paciente le dice que no está registrado",
     "no está registrado" in escribir("hola", telefono="50599998888"))
lista = escribir("1")
caso("Con 1 le muestra sus próximas citas, con fecha, hora, clínica y estado",
     f"{m.dmy(MANANA)} a las 14:00 en la Clínica 1: programada" in lista and f"{m.dmy(PASADO)} a las 15:50" in lista)
caso("Con 2 confirma la próxima cita", "quedó confirmada" in escribir("2") and estado_de(b1)["estado"] == "confirmada")
caso("Si ya estaba confirmada, se lo dice sin cambiar nada", "ya está confirmada" in escribir(" 2 "))
escribir("3")
caso("Con 3 no cancela todavía: pregunta con botones", estado_de(b1)["estado"] == "confirmada" and preguntas
     and preguntas[-1][2] == [(f"cancelar:{b1}", "Sí, cancelarla"), ("menu", "No, mantenerla")])
caso("«No, mantenerla» vuelve al menú y la cita sigue igual",
     "1. Ver mis próximas citas" in escribir({"id": "menu", "title": "No, mantenerla"})
     and estado_de(b1)["estado"] == "confirmada")
escribir({"id": f"cancelar:{b1}", "title": "Sí, cancelarla"}, telefono="50599998888")
caso("El botón de cancelar desde otro teléfono no cambia la cita", estado_de(b1)["estado"] == "confirmada")
caso("«Sí, cancelarla» cancela la cita aunque ya estuviera confirmada, con su motivo",
     "quedó cancelada" in escribir({"id": f"cancelar:{b1}", "title": "Sí, cancelarla"})
     and estado_de(b1) == {"estado": "cancelada", "motivo_cancelacion": "El paciente avisó por WhatsApp que no podrá asistir"})
caso("Con 4 le da la ubicación y el horario", escribir("4") == m.UBICACION)
caso("Con 5 le avisa al estudiante y le dice quién es",
     "Gabriel Espinoza" in escribir("5") and cuantos("pedidos_contacto") == 1)
escribir("5")
caso("Pedir 5 otra vez no repite el aviso", cuantos("pedidos_contacto") == 1)
inicio = texto(gabriel.get("/menu"))
caso("El estudiante ve en Inicio que el paciente pidió que le contacte", "Inicio (1)" in inicio
     and "pidió" in inicio and "Rosa Méndez" in inicio and 'href="https://wa.me/50588112233"' in inicio)
pedido = uno("SELECT id FROM pedidos_contacto")["id"]
grace.post(f"/contactos/{pedido}")
caso("Otro estudiante no puede marcarlo como atendido", uno("SELECT atendido_en FROM pedidos_contacto")["atendido_en"] is None)
gabriel.post(f"/contactos/{pedido}")
caso("El estudiante lo marca como contactado y queda en la bitácora",
     uno("SELECT atendido_por_id FROM pedidos_contacto")["atendido_por_id"] == GABRIEL
     and "Inicio (1)" not in texto(gabriel.get("/menu"))
     and uno("SELECT 1 FROM bitacora WHERE accion = 'Contactó a Rosa Méndez, que lo pidió por WhatsApp'"))
base.execute("UPDATE citas SET estado = 'cancelada' WHERE paciente_id = %s AND estado IN ('programada', 'confirmada')"
             " AND NOT EXISTS (SELECT 1 FROM tratamientos t WHERE t.cita_id = citas.id)", (rosa["id"],))
caso("Sin citas próximas se lo dice y le ofrece pedir contacto", "No tiene citas próximas" in escribir("1"))
for v in ("WHATSAPP_TOKEN", "WHATSAPP_NUMERO_ID", "WHATSAPP_PLANTILLA", "WHATSAPP_VERIFICAR", "WHATSAPP_SECRETO"):
    os.environ.pop(v, None)

# ---- Respaldo diario y restauración (RNF07)
consola = m.app.test_cli_runner()
todas = [t["tablename"] for t in base.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")]
antes = {t: cuantos(t) for t in todas}
salida = consola.invoke(m.respaldar).output
respaldo = salida.split("Respaldo guardado: ")[1].split(" ")[0]
caso("El respaldo queda guardado en el almacén", (m.CARPETA_LOCAL / respaldo).is_file())
caso("No se restaura encima de una base que tiene datos",
     "ya tiene datos" in str(consola.invoke(m.restaurar, [respaldo]).exception) and cuantos("cuentas") == antes["cuentas"])
base.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
restaurado = consola.invoke(m.restaurar, [respaldo])
caso("Restaurado en una base vacía, vuelven todas las filas de todas las tablas",
     restaurado.exception is None and {t: cuantos(t) for t in todas} == antes)
gabriel.post("/pacientes", data={"accion": "registrar", "nombre": "Después del respaldo", "cedula": "001-010101-0099Z"})
caso("Después de restaurar los números siguen sin chocar", uno("SELECT 1 FROM pacientes WHERE nombre = 'Después del respaldo'"))

print(f"\n{'Todo correcto.' if not fallos else str(len(fallos)) + ' fallos.'}")
sys.exit(1 if fallos else 0)
