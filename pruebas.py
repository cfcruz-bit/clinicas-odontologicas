"""Pruebas de Clínicas Odontológicas. Borran todo lo que haya en la base, así que usan su propia rama de Neon.

Ejecutar:  PRUEBAS_DATABASE_URL=<cadena de la rama de pruebas> python pruebas.py
"""
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta
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
base.execute("DROP TABLE IF EXISTS turnos, historias, precios_anteriores, solicitudes, ordenes, recibos, abonos, tratamientos, tarifario, materiales, fotos, dientes, reportes, bitacora,"
             " citas, trabajo, pacientes, salas, cuentas, invitaciones CASCADE")
m.crear_tablas()

def uno(consulta, *args):
    return base.execute(consulta, args).fetchone()

def cuantos(tabla):
    return uno(f"SELECT count(*) AS n FROM {tabla}")["n"]

fallos = []

def caso(descripcion, condicion):
    print(("  OK     " if condicion else "  FALLO  ") + descripcion)
    if not condicion:
        fallos.append(descripcion)

def cliente(cif):
    c = m.app.test_client()
    c.post("/", data={"accion": "entrar", "cif": cif, "clave": "uam2026"})
    return c

def texto(r):
    return r.get_data(as_text=True)

grace, gabriel, profe, admin = cliente("00000002"), cliente("00000005"), cliente("00000001"), cliente("00000007")

print("Clínicas Odontológicas - pruebas\n")
caso("Sin sesion la pagina de pacientes manda a la entrada",
     m.app.test_client().get("/pacientes").headers["Location"].endswith("/"))
caso("La clave se guarda cifrada, no en texto plano", uno("SELECT clave FROM cuentas LIMIT 1")["clave"] != "uam2026")
caso("Con una clave equivocada no se entra",
     m.app.test_client().post("/", data={"accion": "entrar", "cif": "00000002", "clave": "otra"}).status_code == 200)
intento = lambda clave: texto(m.app.test_client().post("/", data={"cif": "00000004", "clave": clave}))  # noqa: E731
for _ in range(m.FALLOS_MAXIMOS - 1):
    intento("otra")
caso("Tras 5 claves equivocadas la cuenta se bloquea", "bloqueada 15 min" in intento("otra"))
caso("Bloqueada, ni con la clave correcta se entra", "Intente de nuevo en 15 min" in intento("uam2026"))
caso("El bloqueo queda en la bitacora", uno("SELECT 1 FROM bitacora WHERE cif = '00000004' AND accion LIKE 'Cuenta bloqueada%%'"))
base.execute("UPDATE cuentas SET bloqueada_hasta = now() - interval '1 second' WHERE cif = '00000004'")
caso("Pasado el bloqueo se entra con la clave correcta",
     m.app.test_client().post("/", data={"cif": "00000004", "clave": "uam2026"}).status_code == 302)
caso("El nombre del usuario sale en la cabecera, no solo el rol", "Grace Alvarado" in texto(grace.get("/menu")))

entrada = m.app.test_client().post("/", data={"accion": "entrar", "cif": "00000002", "clave": "uam2026"})
caso("La sesion se cierra sola a los 15 minutos sin uso (RNF05)",
     m.app.permanent_session_lifetime.total_seconds() == 900 and "Expires=" in entrada.headers["Set-Cookie"])

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
ahora = datetime.now(m.MANAGUA)
MANANA = (ahora + timedelta(days=1)).date().isoformat()
AYER = (ahora - timedelta(days=1)).date().isoformat()
HACE_UNA_HORA = (ahora - timedelta(hours=1)).strftime("%H:%M")  # puede caer ayer si es antes de la 1 a. m.

grace.post(f"/pacientes/{pid}/razon", data={"razon": "Dolor en la muela 36", "fecha": AYER, "hora": "09:00"})
caso("La primera cita tampoco se agenda en una fecha pasada",
     cuantos("citas") == 0 and paciente()["razon"] == "")
grace.post(f"/pacientes/{pid}/razon", data={"razon": "Dolor en la muela 36", "fecha": MANANA, "hora": ""})
caso("Si se pone fecha a la primera cita, tambien hace falta la hora", cuantos("citas") == 0)
grace.post(f"/pacientes/{pid}/razon", data={"razon": "Dolor en la muela 36", "fecha": MANANA,
                                            "hora": "09:00", "sala": "Clínica 1", "razon_cita": "Valoración"})
caso("La razon sale al lado del nombre en la lista", "Dolor en la muela 36" in texto(grace.get("/pacientes")))
caso("La razon de la cita y la del paciente salen en la pagina de citas",
     all(t in texto(grace.get("/citas")) for t in ("Valoración", "Dolor en la muela 36")))

formulario = texto(grace.get("/citas"))
caso("La fecha se elige en un calendario que no deja dias anteriores a hoy",
     'type="date"' in formulario and f'min="{m.hoy()}"' in formulario)
caso("Las fechas se ven como dd/mm/aaaa", m.dmy(MANANA) in formulario)
grace.post("/citas", data={"paciente_id": pid, "fecha": "30/11/2026", "hora": "10:00", "razon": "Control"})
caso("Una fecha que no viene del calendario no se agenda", cuantos("citas") == 1)
grace.post("/citas", data={"paciente_id": pid, "fecha": AYER, "hora": "10:00", "razon": "Control"})
caso("No se agenda en un dia anterior a hoy, aunque sea del mismo mes", cuantos("citas") == 1)
if HACE_UNA_HORA < ahora.strftime("%H:%M"):
    grace.post("/citas", data={"paciente_id": pid, "fecha": m.hoy(), "hora": HACE_UNA_HORA, "razon": "Control"})
    caso("Hoy no se agenda a una hora que ya paso", cuantos("citas") == 1)
grace.post("/citas", data={"paciente_id": pid, "fecha": MANANA, "hora": "09:00", "sala": "Clínica 1",
                           "razon": "Otra"})
caso("No se agenda una clínica ocupada a la misma hora", cuantos("citas") == 1)

grace.post("/citas", data={"paciente_id": pid, "fecha": MANANA, "hora": "09:00", "sala": "Clínica 5", "razon": "Otra"})
caso("El paciente no tiene dos citas a la misma hora, aunque sea en otra clínica (RF10)", cuantos("citas") == 1)
grace.post("/pacientes", data={"accion": "registrar", "nombre": "Marta Gómez", "cedula": "001-050590-0005E"})
marta = uno("SELECT id FROM pacientes WHERE nombre = 'Marta Gómez'")["id"]
caso("El estudiante no tiene dos citas a la misma hora con pacientes distintos (RF10)",
     "Usted ya tiene otra cita a esa hora" in texto(grace.post("/citas", data={
         "paciente_id": marta, "fecha": MANANA, "hora": "09:00", "razon": "Valoración"}, follow_redirects=True))
     and cuantos("citas") == 1)
base.execute("DELETE FROM pacientes WHERE id = %s", (marta,))  # su trabajo se borra con el
caso("Una cita nueva queda Programada", uno("SELECT estado FROM citas")["estado"] == "Programada")

estado = lambda cid: uno("SELECT estado, motivo_cancelacion, fecha, hora FROM citas WHERE id = %s", cid)  # noqa: E731
grace.post("/citas", data={"paciente_id": pid, "fecha": MANANA, "hora": "11:00", "sala": "Clínica 4", "razon": "Para cancelar"})
cancelar = uno("SELECT id FROM citas WHERE razon = 'Para cancelar'")["id"]
gabriel.post("/citas", data={"accion": "cancelar", "id": cancelar, "motivo": "No es mío"})
caso("Un estudiante no cancela citas de pacientes ajenos", estado(cancelar)["estado"] == "Programada")
grace.post("/citas", data={"accion": "cancelar", "id": cancelar})
caso("Para cancelar hay que escribir el motivo (RF11)", estado(cancelar)["estado"] == "Programada")
grace.post("/citas", data={"accion": "cancelar", "id": cancelar, "motivo": "El paciente viaja"})
caso("Cancelada, la cita no se borra: queda en el historial con su motivo (RF11)",
     cuantos("citas") == 2 and estado(cancelar)["estado"] == "Cancelada"
     and estado(cancelar)["motivo_cancelacion"] == "El paciente viaja")
grace.post("/citas", data={"accion": "estado", "id": cancelar, "estado": "Confirmada"})
caso("Una cita cancelada ya no cambia de estado", estado(cancelar)["estado"] == "Cancelada")
grace.post("/citas", data={"paciente_id": pid, "fecha": MANANA, "hora": "11:00", "sala": "Clínica 4", "razon": "Control"})
control = uno("SELECT id FROM citas WHERE razon = 'Control'")
caso("Al cancelar, la clínica y la hora quedan libres para otra cita", control is not None)
grace.post("/citas", data={"accion": "estado", "id": control["id"], "estado": "No se presentó"})
caso("Una cita que todavía no llega no se marca como que no se presentó", estado(control["id"])["estado"] == "Programada")
grace.post("/citas", data={"accion": "estado", "id": control["id"], "estado": "Confirmada"})
caso("El estudiante confirma la cita", estado(control["id"])["estado"] == "Confirmada")
grace.post("/citas", data={"accion": "reprogramar", "id": control["id"], "fecha": MANANA, "hora": "09:00", "sala": ""})
caso("No se reprograma a una hora en que el paciente ya tiene cita", estado(control["id"])["hora"] == "11:00")
grace.post("/citas", data={"accion": "reprogramar", "id": control["id"], "fecha": MANANA, "hora": "12:30", "sala": "Clínica 4"})
caso("Al reprogramar cambia la hora y la cita vuelve a quedar Programada (RF11)",
     estado(control["id"])["hora"] == "12:30" and estado(control["id"])["estado"] == "Programada")
base.execute("INSERT INTO citas (paciente_id, fecha, hora, razon) VALUES (%s, '2020-01-01', '08:00', 'Pasada')", (pid,))
pasada = uno("SELECT id FROM citas WHERE razon = 'Pasada'")["id"]
grace.post("/citas", data={"accion": "cancelar", "id": pasada, "motivo": "Tarde"})
caso("Las citas pasadas no se cancelan: son historial", estado(pasada)["estado"] == "Programada")
grace.post("/citas", data={"accion": "estado", "id": pasada, "estado": "No se presentó"})
caso("Una cita pasada se marca como que el paciente no se presentó",
     estado(pasada)["estado"] == "No se presentó" and "No se presentó a 1 cita" in texto(grace.get(f"/pacientes/{pid}")))
caso("Los cambios de estado de las citas quedan en la bitacora",
     uno("SELECT 1 FROM bitacora WHERE accion LIKE 'Cita de Luis Pérez del %%: cancelada (El paciente viaja)'")
     and uno("SELECT 1 FROM bitacora WHERE accion LIKE 'Reprogramó la cita de Luis Pérez%%'"))

gabriel.post("/pacientes", data={"accion": "registrar", "nombre": "Ana Ruiz", "cedula": "001-020290-0002B"})
grace.post(f"/pacientes/{pid}/editar", data={"nombre": "Luis Pérez", "cedula": "0010202900002b", "telefono": ""})
caso("Al corregir no se puede usar la cedula de otro paciente", paciente()["cedula"] == "001-010190-0001A")
gabriel.post(f"/pacientes/{pid}/editar", data={"nombre": "Otro", "cedula": "001-010190-0001A", "telefono": ""})
caso("Un estudiante no corrige pacientes ajenos", paciente()["nombre"] == "Luis Pérez")
grace.post(f"/pacientes/{pid}/editar", data={"nombre": "Luis Pérez", "cedula": "001-010190-0001A", "telefono": "22220000"})
caso("El estudiante a cargo corrige los datos del paciente", paciente()["telefono"] == "22220000")

caso("Cada estudiante ve solo sus pacientes", "Luis Pérez" not in texto(gabriel.get("/pacientes")))
caso("Cada estudiante ve solo sus citas", "Luis Pérez" not in texto(gabriel.get("/citas")))
caso("Un estudiante no abre el expediente de otro", gabriel.get(f"/pacientes/{pid}").status_code == 302)
caso("El profesor ve a todos los pacientes", "Luis Pérez" in texto(profe.get("/pacientes")))
busca = lambda c, q: texto(c.get("/pacientes", query_string={"buscar": q}))  # noqa: E731
caso("Se busca un paciente por parte de su nombre (RF06)",
     "Luis Pérez" in busca(profe, "luis") and "Ana Ruiz" not in busca(profe, "luis"))
caso("Se busca por la cédula, con o sin guiones", "Luis Pérez" in busca(profe, "0010101") and "Luis Pérez" in busca(profe, "001-0101"))
caso("La búsqueda del estudiante solo mira sus pacientes", "Luis Pérez" not in busca(gabriel, "Luis"))
caso("Un % no trae a todos los pacientes", "Ningún paciente coincide" in busca(profe, "%"))

caso("El profesor no registra pacientes", "accion\" value=\"registrar" not in texto(profe.get("/pacientes")))
profe.post("/pacientes", data={"accion": "registrar", "nombre": "X", "cedula": "001-040490-0004D"})
caso("El profesor no puede registrar ni por la fuerza", cuantos("pacientes") == 2)
caso("El profesor no agenda citas", "Agendar cita" not in texto(profe.get("/citas")))
profe.post("/pacientes", data={"accion": "aprobar", "id": pid, "aprobado": "1"})
caso("El profesor solo revisa: aprueba al paciente", paciente()["aprobado"] is True)
caso("Queda quién aprobó y cuándo (RF04)", paciente()["decision_por"] == "00000001" and paciente()["decision_cuando"])
profe.post("/pacientes", data={"accion": "rechazar", "id": pid, "observacion": ""})
caso("Para rechazar hace falta la observación (RF04)", paciente()["aprobado"] is True and paciente()["rechazo"] == "")
grace.post("/pacientes", data={"accion": "rechazar", "id": pid, "observacion": "Yo no"})
caso("El estudiante no rechaza pacientes", paciente()["rechazo"] == "")
profe.post("/pacientes", data={"accion": "rechazar", "id": pid, "observacion": "Falta la valoración periodontal"})
caso("El profesor rechaza al paciente con su observación",
     paciente()["aprobado"] is False and paciente()["rechazo"] == "Falta la valoración periodontal")
inicio = texto(grace.get("/menu"))
caso("El estudiante ve en Inicio el rechazo con la observación",
     "Falta la valoración periodontal" in inicio and "Mis pacientes (1)" in inicio)
caso("El rechazo queda en la bitacora", uno("SELECT 1 FROM bitacora WHERE accion = 'Rechazó a Luis Pérez: Falta la valoración periodontal'"))
profe.post("/pacientes", data={"accion": "aprobar", "id": pid, "aprobado": "1"})
caso("Al aprobarlo después, se borra el rechazo", paciente()["aprobado"] is True and paciente()["rechazo"] == ""
     and "Falta la valoración" not in texto(grace.get("/menu")))

FIRMA = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAgAAAAECAYAAACzzX7wAAAA"
         "FUlEQVR4nGNkYGD4z4AHMOGTpI4CAIIUAQeqcFo1AAAAAElFTkSuQmCC")
grace.post(f"/pacientes/{pid}/consentimiento", data={"firma": "Luis Pérez"})
caso("La firma escrita a mano no cuenta: el paciente la dibuja", paciente()["firma"] == "")
grace.post(f"/pacientes/{pid}/consentimiento", data={"firma": FIRMA})
hoja = texto(grace.get(f"/pacientes/{pid}/consentimiento"))
caso("El consentimiento se imprime con la firma dibujada del paciente",
     paciente()["firma"] == FIRMA and "firma-trazo" in hoja)
caso("El docente no firma el consentimiento", "Firma del docente" not in hoja)
caso("La lista dice que ya firmo", "Firmado" in texto(grace.get("/pacientes")))

pdf = grace.get(f"/pacientes/{pid}/expediente.pdf")
caso("El expediente se descarga como PDF, no como txt",
     pdf.data[:4] == b"%PDF" and pdf.headers["Content-Disposition"].endswith(".pdf"))

caso("Solo la administracion traspasa pacientes",
     grace.get(f"/pacientes/{pid}/traspaso").status_code == 302)
admin.post(f"/pacientes/{pid}/traspaso", data={"estudiante": "00000005", "resumen": ""})
caso("Al traspasar hay que decir que se le hizo al paciente", paciente()["estudiante"] == "00000002")
admin.post(f"/pacientes/{pid}/traspaso", data={"estudiante": "00000005", "resumen": "Profilaxis y obturación del 36"})
archivado = base.execute("SELECT * FROM trabajo WHERE paciente_id = %s AND hasta IS NOT NULL", (pid,)).fetchall()
caso("Al cambiar de estudiante se archiva el trabajo del anterior con su resumen",
     paciente()["estudiante"] == "00000005" and len(archivado) == 1
     and archivado[0]["resumen"] == "Profilaxis y obturación del 36")
caso("El historial muestra que le hizo cada estudiante",
     all(t in texto(gabriel.get(f"/pacientes/{pid}")) for t in ("Grace Alvarado", "Gabriel Espinoza",
                                                                "Profilaxis y obturación del 36")))
caso("El paciente ya no le sale al estudiante anterior", "Luis Pérez" not in texto(grace.get("/pacientes")))

caso("Hay 50 clínicas, como en el plano: de la 1 a la 30 de pregrado y de la 31 a la 50 de posgrado",
     [(f["en_sala"], f["n"]) for f in base.execute("SELECT en_sala, count(*) AS n FROM salas GROUP BY en_sala ORDER BY 1")]
     == [(1, 30), (2, 20)]
     and uno("SELECT en_sala FROM salas WHERE nombre = 'Clínica 30'")["en_sala"] == 1
     and uno("SELECT en_sala FROM salas WHERE nombre = 'Clínica 31'")["en_sala"] == 2)
_, sala_1, sala_2 = texto(admin.get("/salas")).split('class="plano-titulo"')
import re  # noqa: E402
en_orden = lambda html: [int(n) for n in re.findall(r'title="Clínica (\d+):', html)]  # noqa: E731
caso("El plano muestra cada sala con sus clínicas en orden, con un check si están disponibles y una X si no",
     "Clínicas pregrado" in sala_1 and en_orden(sala_1) == list(range(1, 31))
     and sala_1.count('"marca-sala">✓') == 29 and sala_1.count('"marca-sala">✗') == 1
     and "Clínicas posgrado" in sala_2 and en_orden(sala_2) == list(range(31, 51))
     and sala_2.count('"marca-sala">✓') == 20)
admin.post("/salas", data={"accion": "agregar", "nombre": "Clínica 51", "en_sala": "2"})
caso("La Facultad agrega una clínica en pregrado o posgrado",
     uno("SELECT en_sala FROM salas WHERE nombre = 'Clínica 51'")["en_sala"] == 2)
admin.post("/salas", data={"accion": "agregar", "nombre": "Clínica 52"})
admin.post("/salas", data={"accion": "agregar", "nombre": "Clínica 52", "en_sala": "3"})
caso("Sin decir si es de pregrado o posgrado no se agrega la clínica", uno("SELECT 1 FROM salas WHERE nombre = 'Clínica 52'") is None)
sala1 = uno("SELECT id FROM salas WHERE nombre = 'Clínica 1'")["id"]
admin.post("/salas", data={"sala": sala1})
caso("La administracion cambia la disponibilidad de una clínica",
     uno("SELECT disponible FROM salas WHERE id = %s", sala1)["disponible"] is False)
caso("El estudiante no administra las clínicas", "Agregar clínica" not in texto(grace.get("/salas")))

sala2 = uno("SELECT id FROM salas WHERE nombre = 'Clínica 2'")["id"]
falla = lambda: uno("SELECT * FROM reportes WHERE sala_id = %s AND resuelto_cuando IS NULL", sala2)  # noqa: E731
grace.post("/salas", data={"accion": "reportar", "sala": sala2, "motivo": "  "})
caso("Para reportar una clínica hay que decir que no funciona", falla() is None)
profe.post("/salas", data={"accion": "reportar", "sala": sala2, "motivo": "No enciende la lámpara"})
caso("Solo los estudiantes reportan salas", falla() is None)
grace.post("/salas", data={"accion": "reportar", "sala": sala2, "motivo": "No enciende la lámpara"})
caso("El estudiante marca una clínica como no funcional con su motivo",
     falla()["motivo"] == "No enciende la lámpara" and falla()["cif"] == "00000002"
     and uno("SELECT disponible FROM salas WHERE id = %s", sala2)["disponible"] is False)
gabriel.post("/salas", data={"accion": "reportar", "sala": sala2, "motivo": "Otra cosa"})
caso("Una clínica ya reportada no se reporta dos veces", cuantos("reportes") == 1)
caso("La Facultad ve el aviso en Inicio y en la pestaña Clínicas",
     "Hay 1 clínica que no funciona" in texto(admin.get("/menu")) and "Clínicas (1)" in texto(admin.get("/menu")))
caso("La Facultad ve qué falla y quién lo reportó",
     all(t in texto(admin.get("/salas")) for t in ("No enciende la lámpara", "Grace Alvarado", "Marcar como solucionada")))
caso("El estudiante no ve el aviso de la Facultad", "Hay 1 clínica" not in texto(grace.get("/menu")))
gabriel.post("/citas", data={"paciente_id": pid, "fecha": MANANA, "hora": "15:00", "sala": "Clínica 2", "razon": "X"})
caso("No se agenda una cita en una clínica que no funciona",
     uno("SELECT count(*) AS n FROM citas WHERE sala = 'Clínica 2' AND hora = '15:00'")["n"] == 0)
reporte = falla()["id"]
gabriel.post("/salas", data={"accion": "resolver", "reporte": reporte})
caso("Solo la Facultad marca una falla como solucionada", falla() is not None)
admin.post("/salas", data={"accion": "resolver", "reporte": reporte})
resuelto = uno("SELECT * FROM reportes WHERE id = %s", reporte)
caso("Al solucionarla la clínica vuelve a estar disponible y el aviso desaparece",
     resuelto["resuelto_por"] == "00000007" and resuelto["resuelto_cuando"] is not None
     and uno("SELECT disponible FROM salas WHERE id = %s", sala2)["disponible"] is True
     and "Clínicas (" not in texto(admin.get("/menu")))
caso("La bitacora guarda el reporte y la solucion",
     all(t in texto(admin.get("/bitacora")) for t in ("Reportó Clínica 2 (necesita reparación)", "Solucionó la falla de Clínica 2")))
caso("La bitacora guarda lo que hizo cada quien", "Pasó a Luis Pérez" in texto(admin.get("/bitacora")))
bitacora = texto(admin.get("/bitacora"))
lugar = uno("SELECT lugar FROM bitacora ORDER BY id DESC LIMIT 1")["lugar"]
caso("Desde la red local la bitacora pone la ubicacion de la conexion, no 'Red local', y no muestra la IP",
     lugar != "Red local" and "Red local" not in bitacora and "127.0.0.1" not in bitacora)
print(f"         (ubicacion encontrada: {lugar or 'sin respuesta del servicio'})")
with m.app.test_request_context(headers={"Fly-Client-IP": "1.2.3.4"}):
    caso("Fuera de Fly no se cree un encabezado de IP que cualquiera puede inventar", m.ip_del_usuario() != "1.2.3.4")

caso("En Inicio ya no hay secciones en proceso", "En proceso" not in texto(grace.get("/menu")))

# Odontograma (Luis Pérez ya es de Gabriel y tiene el consentimiento firmado)
odonto = f"/pacientes/{pid}/odontograma"
caso("El odontograma muestra los 32 dientes", texto(gabriel.get(odonto)).count('class="diente') == 32)
gabriel.post(odonto, data={"accion": "diente", "diente": "46", "estado": "Caries", "nota": "oclusal profunda"})
caso("El estudiante a cargo anota el estado de un diente",
     uno("SELECT estado, nota FROM dientes WHERE paciente_id = %s AND diente = 46", pid)
     == {"estado": "Caries", "nota": "oclusal profunda"})
gabriel.post(odonto, data={"accion": "diente", "diente": "99", "estado": "Caries"})
gabriel.post(odonto, data={"accion": "diente", "diente": "11", "estado": "Roto"})
caso("No se anotan dientes que no existen ni estados inventados", cuantos("dientes") == 1)
grace.post(odonto, data={"accion": "diente", "diente": "11", "estado": "Caries"})
profe.post(odonto, data={"accion": "diente", "diente": "11", "estado": "Caries"})
caso("Ni otro estudiante ni el profesor anotan en el odontograma", cuantos("dientes") == 1)
gabriel.post(odonto, data={"accion": "pedir"})
caso("Sin la foto presencial no se pide la revision", paciente()["odonto_revision"] == "")
gabriel.post(odonto, data={"accion": "foto", "foto": (io.BytesIO(b"no es una imagen"), "boca.jpg")})
caso("Un archivo que no es una imagen no se guarda como foto", cuantos("fotos") == 0)
grande = io.BytesIO()
Image.new("RGB", (4000, 3000), "pink").save(grande, "PNG")
grande.seek(0)
gabriel.post(odonto, data={"accion": "foto", "foto": (grande, "boca.png")})
foto = f"/pacientes/{pid}/fotos/{uno('SELECT id FROM fotos')['id']}.jpg"
guardada = Image.open(io.BytesIO(gabriel.get(foto).data))
caso("La foto presencial se guarda achicada y en JPEG",
     guardada.format == "JPEG" and max(guardada.size) == m.FOTO_LADO)
caso("Otro estudiante no ve las fotos del paciente", grace.get(foto).status_code == 302)
url_foto = uno("SELECT url FROM fotos")["url"]
caso("La foto no va dentro de la base: se guarda solo la dirección del archivo",
     url_foto.startswith("local:fotos/") and m.ruta_local(url_foto).is_file()
     and uno("SELECT count(*) AS n FROM information_schema.columns WHERE table_name = 'fotos'"
             " AND data_type = 'bytea'")["n"] == 0)
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
     paciente()["odonto_revision"] == "aprobada" and paciente()["odonto_revisor"] == "00000001")
gabriel.post(odonto, data={"accion": "diente", "diente": "36", "estado": "Obturado"})
caso("Si se cambia despues de aprobado, hay que volver a pedir la revision", paciente()["odonto_revision"] == "")

# Tarifario, tratamientos y abonos
caso("Solo la Facultad lleva el tarifario",
     grace.get("/tarifario").status_code == 302 and profe.get("/tarifario").status_code == 302)
admin.post("/tarifario", data={"accion": "agregar", "nombre": "Profilaxis", "precio": "abc"})
admin.post("/tarifario", data={"accion": "agregar", "nombre": "Profilaxis", "precio": "-5"})
caso("El precio tiene que ser un monto valido", cuantos("tarifario") == 0)
admin.post("/tarifario", data={"accion": "agregar", "nombre": "Profilaxis", "precio": "350"})
admin.post("/tarifario", data={"accion": "agregar", "nombre": "Obturación con resina", "precio": "800.50"})
admin.post("/tarifario", data={"accion": "agregar", "nombre": "profilaxis", "precio": "1"})
caso("La Facultad agrega procedimientos y no se repiten", cuantos("tarifario") == 2)
profilaxis = uno("SELECT id FROM tarifario WHERE nombre = 'Profilaxis'")["id"]
resina = uno("SELECT id FROM tarifario WHERE nombre = 'Obturación con resina'")["id"]
trat = f"/pacientes/{pid}/tratamientos"
gabriel.post(trat, data={"accion": "anotar", "procedimiento": resina, "diente": "36"})
gabriel.post(trat, data={"accion": "anotar", "procedimiento": profilaxis, "diente": ""})
gabriel.post(trat, data={"accion": "anotar", "procedimiento": profilaxis, "diente": "99"})
caso("El estudiante anota en el plan, con diente o sin el, pero no un diente que no existe",
     cuantos("tratamientos") == 2)
profe.post(trat, data={"accion": "anotar", "procedimiento": profilaxis})
grace.post(trat, data={"accion": "anotar", "procedimiento": profilaxis})
caso("Ni el profesor ni otro estudiante anotan tratamientos", cuantos("tratamientos") == 2)
admin.post("/tarifario", data={"accion": "precio", "id": resina, "precio": "900"})
caso("Cambiar el precio en el tarifario no altera lo ya anotado",
     uno("SELECT precio FROM tratamientos WHERE procedimiento_id = %s", resina)["precio"] == Decimal("800.50")
     and uno("SELECT precio FROM tarifario WHERE id = %s", resina)["precio"] == Decimal("900"))
hecho = uno("SELECT id FROM tratamientos WHERE procedimiento_id = %s", resina)["id"]
gabriel.post(trat, data={"accion": "realizado", "id": hecho})
caso("Lo realizado queda con su fecha", uno("SELECT realizado FROM tratamientos WHERE id = %s", hecho)["realizado"] == m.hoy())
gabriel.post(trat, data={"accion": "quitar", "id": hecho})
caso("Lo realizado ya no se quita del plan", cuantos("tratamientos") == 2)
ana = uno("SELECT id FROM pacientes WHERE cedula = '001-020290-0002B'")["id"]
gabriel.post(f"/pacientes/{ana}/tratamientos", data={"accion": "anotar", "procedimiento": profilaxis})
de_ana = uno("SELECT id FROM tratamientos WHERE paciente_id = %s", ana)["id"]
gabriel.post(f"/pacientes/{ana}/tratamientos", data={"accion": "realizado", "id": de_ana})
caso("Sin la firma del consentimiento no se marca nada como realizado",
     uno("SELECT realizado FROM tratamientos WHERE id = %s", de_ana)["realizado"] is None)
gabriel.post(f"/pacientes/{ana}/tratamientos", data={"accion": "quitar", "id": de_ana})
caso("Lo pendiente se puede quitar del plan", cuantos("tratamientos") == 2)
gabriel.post(trat, data={"accion": "abono", "monto": "100"})
caso("Los abonos los registra solo la Facultad", cuantos("abonos") == 0)
admin.post(trat, data={"accion": "abono", "monto": "0"})
admin.post(trat, data={"accion": "abono", "monto": "500"})
cobros = texto(gabriel.get(trat))
caso("El plan suma todo y el saldo es lo realizado menos lo abonado",
     cuantos("abonos") == 1 and "C$ 1,150.50" in cobros and "C$ 300.50" in cobros)
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
subir = lambda c, **d: c.post("/control", data={"accion": "subir", "tratamiento": hecho, "numero": "A-0083",  # noqa: E731
                                                 "fecha": m.hoy(), "foto": recibo_jpg(), **d})
subir(gabriel, numero="")
subir(gabriel, fecha=MANANA)
subir(gabriel, foto=(io.BytesIO(b"no es una imagen"), "recibo.jpg"))
caso("El recibo necesita número, una fecha que ya llegó y la foto", cuantos("recibos") == 0)
subir(grace)
caso("Un estudiante no sube recibos de tratamientos que hizo otro", cuantos("recibos") == 0)
subir(gabriel, tratamiento=uno("SELECT id FROM tratamientos WHERE realizado IS NULL")["id"])
caso("El recibo es solo de un tratamiento ya realizado", cuantos("recibos") == 0)
subir(gabriel)
subir(gabriel)
caso("El estudiante sube el recibo con su foto, sin repetirlo", cuantos("recibos") == 1)
recibo = uno("SELECT id FROM recibos")["id"]
hoja = texto(gabriel.get("/control"))
caso("La hoja muestra nº de paciente, de expediente, de recibo, tratamiento y fecha",
     all(t in hoja for t in (f">{pid}<", "EXP-0457", "A-0083", "Obturación con resina del 36", m.dmy(m.hoy()))))
caso("La foto del recibo la ven el estudiante y el profesor, no otro estudiante",
     gabriel.get(f"/control/recibos/{recibo}.jpg").mimetype == "image/jpeg"
     and profe.get(f"/control/recibos/{recibo}.jpg").mimetype == "image/jpeg"
     and grace.get(f"/control/recibos/{recibo}.jpg").status_code == 302)
caso("Otro estudiante no ve el recibo en su hoja", "A-0083" not in texto(grace.get("/control")))
caso("Al profesor le aparece cuántos recibos esperan su firma", "Hoja de control (1)" in texto(profe.get("/menu")))
gabriel.post("/control", data={"accion": "firmar", "id": recibo})
caso("El estudiante no firma sus recibos", uno("SELECT firmado FROM recibos")["firmado"] is None)
profe.post("/control", data={"accion": "firmar", "id": recibo})
caso("El profesor firma el recibo con su nombre y la fecha",
     uno("SELECT firmado_por, firmado FROM recibos")["firmado_por"] == "00000001"
     and "Dra. Grace Mendoza" in texto(gabriel.get("/control")))
gabriel.post("/control", data={"accion": "quitar", "id": recibo})
caso("Un recibo firmado ya no se quita", cuantos("recibos") == 1)
cliente("00000004").post("/control", data={"accion": "quitar_firma", "id": recibo})
caso("Solo el profesor que firmó puede quitar su firma",
     uno("SELECT firmado_por FROM recibos")["firmado_por"] == "00000001")
caso("Los recibos del paciente se ven junto a sus abonos", "A-0083" in texto(admin.get(trat)))
admin.post(f"/pacientes/{pid}/cerrar", data={"accion": "eliminar", "cedula": "001-010190-0001A", "texto": "Prueba"})
caso("Un paciente con recibos firmados por el docente no se elimina",
     paciente() is not None and cuantos("recibos") == 1)
caso("La Facultad filtra la hoja por estudiante",
     "A-0083" in texto(admin.get("/control?estudiante=00000005"))
     and "A-0083" not in texto(admin.get("/control?estudiante=00000002")))
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

# Inventario
caso("Solo la Facultad lleva el inventario", grace.get("/inventario").status_code == 302)
admin.post("/inventario", data={"accion": "agregar", "nombre": "Guantes", "unidad": "cajas", "cantidad": "10",
                                "minimo": "3"})
admin.post("/inventario", data={"accion": "agregar", "nombre": "guantes", "cantidad": "1", "minimo": "0"})
admin.post("/inventario", data={"accion": "agregar", "nombre": "Resina", "cantidad": "-1", "minimo": "0"})
caso("Se agregan materiales sin repetirlos ni con cantidades negativas", cuantos("materiales") == 1)
guantes = lambda: uno("SELECT * FROM materiales")  # noqa: E731
admin.post("/inventario", data={"accion": "salida", "id": guantes()["id"], "cantidad": "11"})
caso("No sale mas de lo que hay", guantes()["cantidad"] == 10)
admin.post("/inventario", data={"accion": "salida", "id": guantes()["id"], "cantidad": "7"})
inicio = texto(admin.get("/menu"))
caso("Al llegar al minimo la Facultad recibe el aviso",
     guantes()["cantidad"] == 3 and "Inventario (1)" in inicio and "se está acabando" in inicio)
admin.post("/inventario", data={"accion": "entrada", "id": guantes()["id"], "cantidad": "5"})
caso("Con una entrada el aviso desaparece", guantes()["cantidad"] == 8 and "Inventario (" not in texto(admin.get("/menu")))

# Cuentas
caso("Solo la Facultad administra las cuentas", grace.get("/cuentas").status_code == 302)
nueva = {"accion": "crear", "cif": "00000009", "nombre": "Nuevo Estudiante", "rol": "estudiante"}
admin.post("/cuentas", data={**nueva, "clave": "corta"})
caso("La contraseña de una cuenta nueva tiene al menos 8 caracteres", cuantos("cuentas") == 5)
admin.post("/cuentas", data={**nueva, "clave": "clave-segura-1"})
admin.post("/cuentas", data={**nueva, "cif": "00000002", "clave": "clave-segura-1"})
caso("La Facultad crea una cuenta con su contraseña, guardada cifrada",
     cuantos("cuentas") == 6 and uno("SELECT clave FROM cuentas WHERE cif = '00000009'")["clave"] != "clave-segura-1")
nuevo = m.app.test_client()
caso("La cuenta nueva entra con su contraseña",
     nuevo.post("/", data={"cif": "00000009", "clave": "clave-segura-1"}).status_code == 302)
admin.post("/cuentas", data={"accion": "activa", "cif": "00000009"})
caso("Una cuenta desactivada no entra y la sesion que tenia abierta se cierra",
     "desactivada" in texto(m.app.test_client().post("/", data={"cif": "00000009", "clave": "clave-segura-1"}))
     and nuevo.get("/menu").status_code == 302)
caso("A un estudiante desactivado no se le pasan pacientes",
     'value="00000009"' not in texto(admin.get(f"/pacientes/{pid}/traspaso")))
admin.post("/cuentas", data={"accion": "activa", "cif": "00000007"})
caso("La Facultad no desactiva su propia cuenta", uno("SELECT activa FROM cuentas WHERE cif = '00000007'")["activa"])
admin.post("/cuentas", data={"accion": "activa", "cif": "00000009"})
for _ in range(m.FALLOS_MAXIMOS):
    m.app.test_client().post("/", data={"cif": "00000009", "clave": "equivocada"})
admin.post("/cuentas", data={"accion": "clave", "cif": "00000009", "clave": "otra-clave-2"})
caso("Ponerle una contraseña nueva desbloquea la cuenta",
     m.app.test_client().post("/", data={"cif": "00000009", "clave": "otra-clave-2"}).status_code == 302)
# Grupos de estudiantes: la Facultad crea un código QR y cada estudiante crea su propia cuenta
grupo = lambda c=admin, **d: c.post("/cuentas", data={"accion": "grupo", "nombre": "Operatoria 2026", "cupo": "2",  # noqa: E731
                                                       "dias": "7", **d})
grupo(nombre="")
grupo(cupo="0")
grupo(dias="400")
grupo(grace)
caso("Solo la Facultad crea grupos, con nombre, cupo y días válidos", cuantos("invitaciones") == 0)
creado = grupo()
inv = uno("SELECT id, codigo FROM invitaciones")
del_grupo, reg = f"/cuentas/grupos/{inv['id']}", f"/registro/{inv['codigo']}"
pagina = texto(admin.get(del_grupo))
caso("Al crear el grupo la Facultad ve su código QR y la dirección pública para registrarse, no localhost",
     creado.headers["Location"].endswith(del_grupo) and 'src="data:image/svg+xml;base64,' in pagina
     and f"{m.SITIO}{reg}<" in pagina and m.SITIO.startswith("https://") and "localhost" not in m.SITIO)
caso("Ni el estudiante ni el profesor ven el código del grupo",
     grace.get(del_grupo).status_code == 302 and profe.get(del_grupo).status_code == 302)
anon = m.app.test_client()
caso("La página de registro abre sin sesión y dice de qué grupo es", "Operatoria 2026" in texto(anon.get(reg)))
datos = {"cif": "R001", "nombre": "Rosa Registro", "clave": "mi-clave-123", "clave2": "mi-clave-123",
         "correo": "rosa@uam.edu.ni", "anio": "3"}
anon.post("/registro/codigo-inventado", data=datos)
caso("Con un código inventado no se crea ninguna cuenta",
     cuantos("cuentas") == 6 and "ya no está disponible" in texto(anon.get("/registro/codigo-inventado")))
for mal in ({"cif": "R 001"}, {"cif": "00000002"}, {"nombre": ""}, {"clave": "corta", "clave2": "corta"},
            {"clave2": "otra-clave-123"}, {"correo": "rosa"}, {"anio": "9"}):
    anon.post(reg, data={**datos, **mal})
caso("Para registrarse hace falta un CIF nuevo, el nombre y una contraseña de 8 caracteres escrita dos veces",
     cuantos("cuentas") == 6)
entra = anon.post(reg, data=datos)
rosa = uno("SELECT * FROM cuentas WHERE cif = 'R001'") or {}
caso("El estudiante crea su cuenta con su CIF, su nombre y su contraseña, guardada cifrada",
     rosa.get("nombre") == "Rosa Registro" and rosa["rol"] == "estudiante" and rosa["invitacion_id"] == inv["id"]
     and "mi-clave-123" not in rosa["clave"])
caso("Al registrarse guarda su correo y su año de la carrera (RF02)",
     rosa.get("correo") == "rosa@uam.edu.ni" and rosa.get("anio") == 3)
caso("Al registrarse queda dentro del sistema como estudiante, sin pacientes de nadie",
     entra.status_code == 302 and "Rosa Registro (Estudiante)" in texto(anon.get("/menu"))
     and "Luis Pérez" not in texto(anon.get("/pacientes")))
caso("Después entra con su CIF y la contraseña que creó",
     m.app.test_client().post("/", data={"cif": "R001", "clave": "mi-clave-123"}).status_code == 302)
caso("El registro queda en la bitácora",
     uno("SELECT 1 FROM bitacora WHERE cif = 'R001' AND accion LIKE 'Creó su cuenta%%Operatoria 2026'"))
m.app.test_client().post(reg, data={**datos, "cif": "R002", "nombre": "Raúl Registro"})
m.app.test_client().post(reg, data={**datos, "cif": "R003", "nombre": "Sobra Uno"})
caso("Cuando se llena el cupo ya no se registra nadie más",
     cuantos("cuentas") == 8 and "el grupo ya está completo" in texto(m.app.test_client().get(reg)))
caso("La Facultad ve quiénes se registraron en el grupo",
     all(t in texto(admin.get(del_grupo)) for t in ("Rosa Registro", "Raúl Registro", "2 de 2")))
grupo(nombre="Grupo B", cupo="5")
inv_b = uno("SELECT id, codigo FROM invitaciones WHERE nombre = 'Grupo B'")
grace.post(f"/cuentas/grupos/{inv_b['id']}")
caso("Solo la Facultad cierra el registro de un grupo",
     not uno("SELECT cerrado FROM invitaciones WHERE id = %s", inv_b["id"])["cerrado"])
admin.post(f"/cuentas/grupos/{inv_b['id']}")
m.app.test_client().post(f"/registro/{inv_b['codigo']}", data={**datos, "cif": "R004"})
caso("Con el registro cerrado el código ya no sirve",
     cuantos("cuentas") == 8 and "cerró el registro" in texto(m.app.test_client().get(f"/registro/{inv_b['codigo']}")))
grupo(nombre="Grupo C", cupo="5")
base.execute("UPDATE invitaciones SET vence = now() - interval '1 second' WHERE nombre = 'Grupo C'")
inv_c = uno("SELECT codigo FROM invitaciones WHERE nombre = 'Grupo C'")
m.app.test_client().post(f"/registro/{inv_c['codigo']}", data={**datos, "cif": "R005"})
caso("Pasada la fecha el código vence", cuantos("cuentas") == 8)

# ---- RF06/RNF06: el profesor ve solo a los pacientes de sus estudiantes; RF10: turnos clínicos
gustavo = cliente("00000004")
vistos = lambda c: texto(c.get("/pacientes"))  # noqa: E731
con_paciente = uno("SELECT p.id, p.nombre, p.estudiante FROM pacientes p WHERE eliminado IS NULL AND estudiante IS NOT NULL"
                   " ORDER BY id LIMIT 1")
caso("Sin profesor asignado, todos los profesores ven a los pacientes del estudiante",
     con_paciente["nombre"] in vistos(profe) and con_paciente["nombre"] in vistos(gustavo))
grace.post(f"/cuentas/{con_paciente['estudiante']}", data={"accion": "datos", "nombre": "X", "rol": "estudiante",
                                                           "profesor": "00000001"})
caso("Solo la Facultad asigna el profesor", not uno("SELECT profesor FROM cuentas WHERE cif = %s",
                                                    con_paciente["estudiante"])["profesor"])
nombre_est = uno("SELECT nombre FROM cuentas WHERE cif = %s", con_paciente["estudiante"])["nombre"]
admin.post(f"/cuentas/{con_paciente['estudiante']}", data={"accion": "datos", "nombre": nombre_est, "rol": "estudiante",
                                                           "correo": "est@uam.edu.ni", "anio": "4", "profesor": "00000001"})
fila = uno("SELECT profesor, correo, anio FROM cuentas WHERE cif = %s", con_paciente["estudiante"])
caso("La Facultad pone profesor, correo y año en la ficha de la cuenta",
     fila == {"profesor": "00000001", "correo": "est@uam.edu.ni", "anio": 4})
caso("El profesor a cargo ve a los pacientes de su estudiante y otro profesor ya no (RNF06)",
     con_paciente["nombre"] in vistos(profe) and con_paciente["nombre"] not in vistos(gustavo)
     and gustavo.get(f"/pacientes/{con_paciente['id']}").status_code == 302
     and profe.get(f"/pacientes/{con_paciente['id']}").status_code == 200)
admin.post(del_grupo, data={"accion": "profesor", "profesor": "00000004"})
caso("La Facultad pone un profesor a cargo de todo un grupo",
     {f["profesor"] for f in base.execute("SELECT profesor FROM cuentas WHERE invitacion_id = %s", (inv["id"],))} == {"00000004"})
est = con_paciente["estudiante"]
admin.post(f"/cuentas/{est}", data={"accion": "turno", "dia": "0", "desde": "10:00", "hasta": "08:00"})
caso("Un turno al revés no se guarda", cuantos("turnos") == 0)
admin.post(f"/cuentas/{est}", data={"accion": "turno", "dia": "0", "desde": "08:00", "hasta": "12:00"})
admin.post(f"/cuentas/{est}", data={"accion": "turno", "dia": "0", "desde": "11:00", "hasta": "13:00"})
caso("La Facultad asigna turnos clínicos y no deja que se crucen (RF10)", cuantos("turnos") == 1)
lunes = datetime.now() + timedelta(days=7 - datetime.now().weekday())
martes = lunes + timedelta(days=1)
contexto = m.app.test_request_context()
contexto.push()
caso("Con turnos, una cita fuera de ellos no se puede agendar",
     "fuera de sus turnos" in m.error_de_cita(martes.date().isoformat(), "09:00", "", con_paciente["id"], est))
caso("Dentro del turno sí se puede",
     m.error_de_cita(lunes.date().isoformat(), "09:00", "", con_paciente["id"], est) is None)
t_id = uno("SELECT id FROM turnos")["id"]
admin.post(f"/cuentas/{est}", data={"accion": "quitar_turno", "id": t_id})
contexto.pop()
caso("La Facultad quita un turno", cuantos("turnos") == 0)

# Dar de alta y eliminar pacientes: solo la Facultad
cerrar = f"/pacientes/{pid}/cerrar"
caso("Solo la Facultad da de alta o elimina pacientes",
     gabriel.get(cerrar).status_code == 302 and profe.get(cerrar).status_code == 302)
gabriel.post(cerrar, data={"accion": "finalizar", "texto": "Alta"})
profe.post(cerrar, data={"accion": "finalizar", "texto": "Alta"})
caso("Ni el estudiante ni el profesor le dan de alta por la fuerza", paciente()["finalizado"] is None)
admin.post(cerrar, data={"accion": "finalizar", "texto": ""})
caso("Para darle de alta hay que decir como termino el tratamiento", paciente()["finalizado"] is None)
admin.post(cerrar, data={"accion": "finalizar", "texto": "Completó el plan y fue dado de alta"})
caso("La Facultad da de alta al paciente",
     paciente()["finalizado"] == m.hoy() and paciente()["finalizado_por"] == "00000007"
     and "Completó el plan y fue dado de alta" in texto(gabriel.get(f"/pacientes/{pid}")))
dientes_antes, citas_antes = cuantos("dientes"), cuantos("citas")
gabriel.post(odonto, data={"accion": "diente", "diente": "21", "estado": "Caries"})
gabriel.post("/citas", data={"paciente_id": pid, "fecha": MANANA, "hora": "16:00", "razon": "Control"})
gabriel.post(f"/pacientes/{pid}/editar", data={"nombre": "Cambiado", "cedula": "001-010190-0001A", "telefono": ""})
caso("De alta, el estudiante ya no cambia el odontograma, las citas ni los datos",
     cuantos("dientes") == dientes_antes and cuantos("citas") == citas_antes and paciente()["nombre"] == "Luis Pérez")
caso("De alta, ya no sale para agendarle citas",
     f'<option value="{pid}"' not in texto(gabriel.get("/citas")))
admin.post(trat, data={"accion": "abono", "monto": "300.50"})
caso("De alta, la Facultad todavia registra abonos para saldar la cuenta", cuantos("abonos") == 2)
gabriel.post("/control", data={"accion": "subir", "tratamiento": hecho, "numero": "A-0120", "fecha": m.hoy(),
                               "foto": recibo_jpg()})
caso("De alta, todavía se sube un recibo que llegó tarde", cuantos("recibos") == 2)
tarde = uno("SELECT id, url FROM recibos WHERE numero = 'A-0120'")
gabriel.post("/control", data={"accion": "quitar", "id": tarde["id"]})
caso("Al quitar un recibo también se borra su foto", cuantos("recibos") == 1 and not m.ruta_local(tarde["url"]).exists())
caso("De alta, ya no se cambia de estudiante", admin.get(f"/pacientes/{pid}/traspaso").status_code == 302)
admin.post(cerrar, data={"accion": "reabrir"})
gabriel.post(odonto, data={"accion": "diente", "diente": "21", "estado": "Caries"})
caso("Si se anula el alta, el estudiante vuelve a poder cambiarlo",
     paciente()["finalizado"] is None and cuantos("dientes") == dientes_antes + 1)

gabriel.post("/pacientes", data={"accion": "registrar", "nombre": "Repetido Por Error", "cedula": "001-030390-0003C"})
borrar = uno("SELECT id FROM pacientes WHERE cedula = '001-030390-0003C'")["id"]
gabriel.post(f"/pacientes/{borrar}/odontograma", data={"accion": "foto", "foto": recibo_jpg()})
foto_borrar = uno("SELECT url FROM fotos WHERE paciente_id = %s", borrar)["url"]
gabriel.post(f"/citas", data={"paciente_id": borrar, "fecha": MANANA, "hora": "17:00", "razon": "Valoración"})
admin.post(f"/pacientes/{borrar}/cerrar", data={"accion": "eliminar", "cedula": "001-040490-0004D", "texto": "Duplicado"})
admin.post(f"/pacientes/{borrar}/cerrar", data={"accion": "eliminar", "cedula": "001-030390-0003C", "texto": ""})
caso("Para eliminarlo hay que escribir su cedula y el motivo", cuantos("pacientes") == 3)
gabriel.post(f"/pacientes/{borrar}/cerrar", data={"accion": "eliminar", "cedula": "001-030390-0003C", "texto": "Duplicado"})
caso("El estudiante no elimina pacientes", cuantos("pacientes") == 3)
admin.post(f"/pacientes/{borrar}/cerrar", data={"accion": "eliminar", "cedula": "001-030390-0003C", "texto": "Duplicado"})
caso("Eliminar no borra: el paciente deja de verse pero su expediente se conserva (RNF07)",
     cuantos("pacientes") == 3 and uno("SELECT eliminado FROM pacientes WHERE id = %s", borrar)["eliminado"]
     and uno("SELECT 1 FROM citas WHERE paciente_id = %s", borrar) is not None
     and "Repetido Por Error" not in texto(gabriel.get("/pacientes")) and gabriel.get(f"/pacientes/{borrar}").status_code == 302)
caso("Sus fotos también se conservan", m.ruta_local(foto_borrar).exists())
caso("La cédula de un paciente eliminado no se registra otra vez: hay que restaurarlo",
     "paciente eliminado" in texto(gabriel.post("/pacientes", data={"accion": "registrar", "nombre": "Otra vez",
                                                                    "cedula": "001030390-0003C"})) and cuantos("pacientes") == 3)
caso("La eliminacion queda en la bitacora con el motivo",
     "Eliminó al paciente Repetido Por Error (001-030390-0003C): Duplicado" in texto(admin.get("/bitacora")))

admin.post("/pacientes", data={"accion": "restaurar", "id": borrar})
caso("La Facultad restaura al paciente eliminado con todo su expediente",
     uno("SELECT eliminado FROM pacientes WHERE id = %s", borrar)["eliminado"] is None
     and "Repetido Por Error" in texto(gabriel.get("/pacientes")))

# ---- Historia clínica, registro con datos generales y expediente correlativo (RF03, RF05)
gabriel.post("/pacientes", data={"accion": "registrar", "nombre": "Rosa Méndez", "cedula": "001-150885-0009F",
                                 "telefono": "88112233", "nacimiento": "1985-08-15", "sexo": "Femenino",
                                 "direccion": "Managua", "emergencia_nombre": "Juan Méndez",
                                 "emergencia_telefono": "8877 6655"})
rosa = uno("SELECT * FROM pacientes WHERE nombre = 'Rosa Méndez'")
caso("Sin número de expediente físico se le da uno correlativo (RF03)", rosa["expediente"] == f"EXP-{rosa['id']:05d}")
caso("Los datos generales del registro quedan en la historia clínica",
     uno("SELECT sexo, emergencia_telefono FROM historias WHERE paciente_id = %s", rosa["id"])
     == {"sexo": "Femenino", "emergencia_telefono": "88776655"})
gabriel.post("/pacientes", data={"accion": "registrar", "nombre": "Futuro", "cedula": "001-150885-0019F",
                                 "nacimiento": "2999-01-01"})
caso("La fecha de nacimiento no puede ser después de hoy", uno("SELECT 1 FROM pacientes WHERE nombre = 'Futuro'") is None)
hist = f"/pacientes/{rosa['id']}/historia"
de_rosa = lambda c: uno(f"SELECT {c} FROM historias WHERE paciente_id = %s", rosa["id"])[c]  # noqa: E731
vacia = {c: "" for c in m.HISTORIA}
gabriel.post(hist, data={**vacia, "presion": "alta"})
caso("La presión se escribe como sistólica/diastólica", de_rosa("presion") == "")
gabriel.post(hist, data={**vacia, "sexo": "Femenino", "alergias": "Penicilina", "presion": "150/95",
                         "diagnostico": "Caries en 36"})
caso("El estudiante a cargo guarda la historia clínica, aunque esté a medias (RF05)",
     de_rosa("diagnostico") == "Caries en 36" and de_rosa("actualizado_por") == "00000005")
exp_rosa = texto(gabriel.get(f"/pacientes/{rosa['id']}"))
caso("El expediente avisa de las alergias y de la presión alta",
     "Alergias: Penicilina" in exp_rosa and "Presión arterial alta: 150/95" in exp_rosa)
caso("Los cambios de la historia quedan en la bitácora (historial de cambios)",
     uno("SELECT 1 FROM bitacora WHERE accion LIKE 'Actualizó la historia clínica de Rosa Méndez:%%alergias%%'"))
grace.post(hist, data={**vacia, "alergias": "Ninguna"})
caso("Otro estudiante no cambia la historia", de_rosa("alergias") == "Penicilina")
caso("El profesor la lee pero no la cambia",
     "Penicilina" in texto(profe.get(hist)) and 'name="alergias"' not in texto(profe.get(hist)))
caso("La historia sale en el expediente en PDF", gabriel.get(f"/pacientes/{rosa['id']}/expediente.pdf").data[:4] == b"%PDF")

# ---- Radiografías y límite de las imágenes (RF05, RNF09)
odonto_rosa = f"/pacientes/{rosa['id']}/odontograma"
gabriel.post(odonto_rosa, data={"accion": "foto", "tipo": "Radiografía", "foto": recibo_jpg()})
caso("Se suben radiografías junto a las fotos", uno("SELECT tipo FROM fotos WHERE paciente_id = %s", rosa["id"])["tipo"]
     == "Radiografía")
gabriel.post(odonto_rosa, data={"accion": "pedir"})
caso("Con solo una radiografía no se pide la revisión: falta la foto presencial",
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
admin.post("/tarifario", data={"accion": "agregar", "nombre": "Gratis", "precio": "0"})
caso("El precio tiene que ser mayor que cero", uno("SELECT 1 FROM tarifario WHERE nombre = 'Gratis'") is None)
admin.post("/tarifario", data={"accion": "agregar", "nombre": "Extracción simple", "precio": "500", "codigo": "cir-01",
                               "area": "Cirugía"})
caso("El procedimiento lleva código y área clínica",
     uno("SELECT codigo, area FROM tarifario WHERE nombre = 'Extracción simple'") == {"codigo": "CIR-01", "area": "Cirugía"})
admin.post("/tarifario", data={"accion": "agregar", "nombre": "Otra extracción", "precio": "5", "codigo": "CIR-01"})
caso("El código no se repite", uno("SELECT 1 FROM tarifario WHERE nombre = 'Otra extracción'") is None)
caso("El precio anterior queda en el historial al cambiarlo",
     uno("SELECT precio, cambiado_por FROM precios_anteriores WHERE procedimiento_id = %s", resina)
     == {"precio": Decimal("800.50"), "cambiado_por": "00000007"} and "Precios anteriores" in texto(admin.get("/tarifario")))

# ---- Plan por fases, avance y orden de cobro (RF09, RF16)
trat_rosa = f"/pacientes/{rosa['id']}/tratamientos"
gabriel.post(trat_rosa, data={"accion": "anotar", "procedimiento": resina, "fase": "Inventada"})
gabriel.post(trat_rosa, data={"accion": "anotar", "procedimiento": resina, "fase": "Higiénica"})
caso("Cada tratamiento va en una fase del plan (RF09)",
     [t["fase"] for t in base.execute("SELECT fase FROM tratamientos WHERE paciente_id = %s", (rosa["id"],))] == ["Higiénica"])
caso("Se ve el avance del plan", "0 de 1" in texto(gabriel.get(trat_rosa)))
gabriel.post(trat_rosa, data={"accion": "orden"})
orden = uno("SELECT * FROM ordenes WHERE paciente_id = %s", rosa["id"])
caso("Se genera la orden de cobro con el total, a partir del tarifario (RF16)",
     orden["total"] == Decimal("900") and uno("SELECT orden_id FROM tratamientos WHERE paciente_id = %s", rosa["id"])
     ["orden_id"] == orden["id"])
gabriel.post(trat_rosa, data={"accion": "orden"})
caso("Lo que ya está en una orden no entra en otra", uno("SELECT count(*) AS n FROM ordenes")["n"] == 1)
gabriel.post(trat_rosa, data={"accion": "quitar", "id": uno("SELECT id FROM tratamientos WHERE paciente_id = %s",
                                                              rosa["id"])["id"]})
caso("Lo que ya está en una orden no se quita del plan",
     uno("SELECT count(*) AS n FROM tratamientos WHERE paciente_id = %s", rosa["id"])["n"] == 1)
hoja_orden = texto(gabriel.get(f"/ordenes/{orden['id']}"))
caso("La orden se ve para imprimirla, con número y total", f"Orden de cobro Nº {orden['id']}" in hoja_orden
     and "C$ 900.00" in hoja_orden and grace.get(f"/ordenes/{orden['id']}").status_code == 302)
gabriel.post(f"/ordenes/{orden['id']}")
caso("El estudiante no marca la orden como pagada", uno("SELECT pagada FROM ordenes")["pagada"] is None)
admin.post(f"/ordenes/{orden['id']}")
caso("La Facultad la cobra y queda el abono del paciente",
     uno("SELECT pagada FROM ordenes")["pagada"] is not None
     and uno("SELECT monto FROM abonos WHERE paciente_id = %s", rosa["id"])["monto"] == Decimal("900"))

# ---- Solicitudes de materiales (RF17)
admin.post("/inventario", data={"accion": "agregar", "nombre": "Algodón", "unidad": "rollos", "cantidad": "3", "minimo": "1"})
algodon = uno("SELECT id FROM materiales WHERE nombre = 'Algodón'")["id"]
gabriel.post("/materiales", data={"material": algodon, "cantidad": "2", "para": AYER})
caso("La solicitud es para una cita de hoy en adelante", uno("SELECT count(*) AS n FROM solicitudes")["n"] == 0)
gabriel.post("/materiales", data={"material": algodon, "cantidad": "5", "para": MANANA})
gabriel.post("/materiales", data={"material": algodon, "cantidad": "2", "para": MANANA})
mucho = uno("SELECT id FROM solicitudes WHERE cantidad = 5")["id"]
poco = uno("SELECT id FROM solicitudes WHERE cantidad = 2")["id"]
estado_sol = lambda i: uno("SELECT estado FROM solicitudes WHERE id = %s", i)["estado"]  # noqa: E731
caso("La Facultad ve en Inicio las solicitudes que esperan", "Hay 2 solicitudes de material" in texto(admin.get("/menu")))
admin.post("/inventario", data={"accion": "entregar", "id": mucho})
caso("No se entrega más de lo que hay", estado_sol(mucho) == "Pendiente")
admin.post("/inventario", data={"accion": "entregar", "id": poco})
caso("Al entregar se descuenta del inventario",
     estado_sol(poco) == "Entregada" and uno("SELECT cantidad FROM materiales WHERE id = %s", algodon)["cantidad"] == 1)
admin.post("/inventario", data={"accion": "rechazar", "id": mucho})
admin.post("/inventario", data={"accion": "rechazar", "id": mucho, "nota": "No alcanza; pida 1"})
caso("Para rechazar hace falta el motivo, y el estudiante lo ve",
     estado_sol(mucho) == "Rechazada" and "No alcanza; pida 1" in texto(gabriel.get("/materiales")))
caso("Solo el estudiante pide materiales", profe.get("/materiales").status_code == 302)

# ---- Corregir cuentas y filtrarlas (RF02)
admin.post("/cuentas", data={"accion": "editar", "cif": "00000004", "nombre": "Dr. Gustavo Rivas Pérez", "rol": "profesor"})
caso("La Facultad corrige el nombre de una cuenta (RF02)", m.app.test_client() and
     uno("SELECT nombre FROM cuentas WHERE cif = '00000004'")["nombre"] == "Dr. Gustavo Rivas Pérez")
admin.post("/cuentas", data={"accion": "editar", "cif": "00000007", "nombre": "Gina Saavedra", "rol": "estudiante"})
caso("Nadie se cambia el rol a sí mismo", uno("SELECT rol FROM cuentas WHERE cif = '00000007'")["rol"] == "admin")
solo_profes = texto(admin.get("/cuentas", query_string={"rol": "profesor"}))
caso("Las cuentas se filtran por rol", "Gustavo Rivas Pérez" in solo_profes and "Gabriel Espinoza" not in solo_profes)

# ---- Fallas con su estado e historial (RF14)
sala9 = uno("SELECT id FROM salas WHERE nombre = 'Clínica 9'")["id"]
grace.post("/salas", data={"accion": "reportar", "sala": sala9, "motivo": "Hay restos en la escupidera",
                           "tipo": "Necesita limpieza"})
caso("Al reportar se dice qué necesita la clínica (RF14)",
     uno("SELECT tipo FROM reportes WHERE sala_id = %s", sala9)["tipo"] == "Necesita limpieza")
admin.post("/salas", data={"accion": "resolver", "reporte": uno("SELECT id FROM reportes WHERE sala_id = %s", sala9)["id"]})
caso("La Facultad ve el historial de fallas solucionadas",
     all(t in texto(admin.get("/salas")) for t in ("Historial de fallas solucionadas", "Hay restos en la escupidera")))

# ---- Inicio con la jornada y recordatorios (RF13, RF12)
base.execute("INSERT INTO citas (paciente_id, fecha, hora, razon) VALUES (%s, %s, '23:59', 'Control de hoy')",
             (rosa["id"], m.hoy()))
base.execute("INSERT INTO citas (paciente_id, fecha, hora, razon) VALUES (%s, %s, '10:15', 'Control de mañana')",
             (rosa["id"], MANANA))
caso("El Inicio del estudiante muestra sus citas de hoy (RF13)", "Control de hoy" in texto(gabriel.get("/menu")))
inicio_profe = texto(profe.get("/menu"))
caso("Al profesor le salen las citas de hoy de todos y los pacientes por aprobar",
     "Control de hoy" in inicio_profe and "Pacientes por aprobar" in inicio_profe and "Rosa Méndez" in inicio_profe)
caso("El estudiante ve las citas de mañana sin confirmar con el recordatorio por WhatsApp (RF12)",
     "wa.me/50588112233" in texto(gabriel.get("/menu")))

# ---- Bitácora con filtros (RF18)
por_accion = texto(admin.get("/bitacora", query_string={"accion": "solicitud"}))
caso("La bitácora se filtra por acción (RF18)", "Rechazó la solicitud" in por_accion and "Entró al sistema" not in por_accion)
por_usuario = texto(admin.get("/bitacora", query_string={"usuario": "00000005"}))
caso("y por usuario", "Pidió 5 rollos de Algodón" in por_usuario and "Rechazó la solicitud" not in por_usuario)
caso("y por fechas", "Ningún movimiento con esos filtros" in texto(admin.get("/bitacora", query_string={"hasta": "2020-01-01"})))

m.crear_tablas()
caso("Crear las tablas otra vez no borra ni duplica nada", cuantos("pacientes") == 4 and cuantos("cuentas") == 8)

# Una base que nació con 20 clínicas (10 y 10) se completa a las 50 del plano sin perder lo que tenía
base.execute("DELETE FROM salas WHERE substring(nombre FROM '[0-9]+')::int > 20")
base.execute("UPDATE salas SET en_sala = 2 WHERE substring(nombre FROM '[0-9]+')::int > 10")
base.execute("UPDATE salas SET disponible = false WHERE nombre = 'Clínica 15'")
citas_antes = cuantos("citas")
m.crear_tablas()
m.crear_tablas()
caso("Una base con las 20 clínicas de antes pasa a las 50 del plano y conserva lo que tenía",
     [(f["en_sala"], f["n"]) for f in base.execute("SELECT en_sala, count(*) AS n FROM salas GROUP BY en_sala ORDER BY 1")]
     == [(1, 30), (2, 20)]
     and uno("SELECT en_sala, disponible FROM salas WHERE nombre = 'Clínica 15'") == {"en_sala": 1, "disponible": False}
     and uno("SELECT en_sala FROM salas WHERE nombre = 'Clínica 31'")["en_sala"] == 2 and cuantos("citas") == citas_antes)

# Una base de antes, con las fotos dentro (bytea) y creado_por en invitaciones, se pone al día sola
columnas = lambda tabla: {c["column_name"] for c in base.execute(  # noqa: E731
    "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (tabla,))}
jpg = recibo_jpg()[0].getvalue()
base.execute("ALTER TABLE fotos ALTER COLUMN url DROP NOT NULL")
base.execute("ALTER TABLE fotos ADD COLUMN imagen bytea")
vieja = base.execute("INSERT INTO fotos (paciente_id, imagen) VALUES (%s, %s) RETURNING id", (pid, jpg)).fetchone()["id"]
base.execute("ALTER TABLE invitaciones ADD COLUMN creado_por text REFERENCES cuentas")
m.crear_tablas()
movida = uno("SELECT url FROM fotos WHERE id = %s", vieja)["url"]
caso("Las fotos que estaban dentro de la base pasan al almacenamiento y se quita la columna vieja",
     movida.startswith("local:fotos/") and m.ruta_local(movida).read_bytes() == jpg and "imagen" not in columnas("fotos")
     and uno("SELECT is_nullable FROM information_schema.columns WHERE table_name = 'fotos'"
             " AND column_name = 'url'")["is_nullable"] == "NO")
caso("invitaciones ya no tiene creado_por: cuentas e invitaciones no se apuntan entre sí",
     "creado_por" not in columnas("invitaciones"))
os.environ["FLY_APP_NAME"] = "prueba"
try:
    m.crear_tablas()
    detenido = False
except SystemExit:
    detenido = True
finally:
    del os.environ["FLY_APP_NAME"]
caso("Publicado en Fly sin almacenamiento configurado, el despliegue se detiene antes de cambiar la base", detenido)

print(f"\n{'Todo correcto.' if not fallos else str(len(fallos)) + ' fallos.'}")
sys.exit(1 if fallos else 0)
