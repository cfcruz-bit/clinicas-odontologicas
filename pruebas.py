"""Pruebas de Clínicas Odontológicas. Borran todo lo que haya en la base, así que usan su propia rama de Neon.

Ejecutar:  PRUEBAS_DATABASE_URL=<cadena de la rama de pruebas> python pruebas.py
"""
import os
import sys
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import dict_row

# Nunca se toma DATABASE_URL: las pruebas no deben tocar la base de verdad
os.environ["DATABASE_URL"] = os.environ["PRUEBAS_DATABASE_URL"]
import prototipo as m  # noqa: E402

base = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row, prepare_threshold=None)
base.execute("DROP TABLE IF EXISTS reportes, bitacora, citas, trabajo, pacientes, salas, cuentas")
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
caso("La sesion se cierra sola a los 30 minutos sin uso",
     m.app.permanent_session_lifetime.total_seconds() == 1800 and "Expires=" in entrada.headers["Set-Cookie"])

grace.post("/pacientes", data={"accion": "registrar", "nombre": "Luis Pérez", "cedula": "T-100", "telefono": "88880000"})
pid = uno("SELECT id FROM pacientes")["id"]
paciente = lambda: uno("SELECT * FROM pacientes WHERE id = %s", pid)  # noqa: E731
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

grace.post("/citas", data={"paciente_id": pid, "fecha": MANANA, "hora": "11:00", "razon": "Para cancelar"})
cancelar = uno("SELECT id FROM citas WHERE razon = 'Para cancelar'")["id"]
gabriel.post("/citas", data={"accion": "cancelar", "id": cancelar})
caso("Un estudiante no cancela citas de pacientes ajenos", cuantos("citas") == 2)
grace.post("/citas", data={"accion": "cancelar", "id": cancelar})
caso("El estudiante a cargo cancela una cita", cuantos("citas") == 1)
base.execute("INSERT INTO citas (paciente_id, fecha, hora, razon) VALUES (%s, '2020-01-01', '08:00', 'Pasada')", (pid,))
pasada = uno("SELECT id FROM citas WHERE razon = 'Pasada'")["id"]
grace.post("/citas", data={"accion": "cancelar", "id": pasada})
caso("Las citas pasadas no se cancelan: son historial", cuantos("citas") == 2)

gabriel.post("/pacientes", data={"accion": "registrar", "nombre": "Ana Ruiz", "cedula": "T-200"})
grace.post(f"/pacientes/{pid}/editar", data={"nombre": "Luis Pérez", "cedula": "T-200", "telefono": ""})
caso("Al corregir no se puede usar la cedula de otro paciente", paciente()["cedula"] == "T-100")
gabriel.post(f"/pacientes/{pid}/editar", data={"nombre": "Otro", "cedula": "T-100", "telefono": ""})
caso("Un estudiante no corrige pacientes ajenos", paciente()["nombre"] == "Luis Pérez")
grace.post(f"/pacientes/{pid}/editar", data={"nombre": "Luis Pérez", "cedula": "T-100", "telefono": "22220000"})
caso("El estudiante a cargo corrige los datos del paciente", paciente()["telefono"] == "22220000")

caso("Cada estudiante ve solo sus pacientes", "Luis Pérez" not in texto(gabriel.get("/pacientes")))
caso("Cada estudiante ve solo sus citas", "Luis Pérez" not in texto(gabriel.get("/citas")))
caso("Un estudiante no abre el expediente de otro", gabriel.get(f"/pacientes/{pid}").status_code == 302)
caso("El profesor ve a todos los pacientes", "Luis Pérez" in texto(profe.get("/pacientes")))

caso("El profesor no registra pacientes", "accion\" value=\"registrar" not in texto(profe.get("/pacientes")))
profe.post("/pacientes", data={"accion": "registrar", "nombre": "X", "cedula": "T-999"})
caso("El profesor no puede registrar ni por la fuerza", cuantos("pacientes") == 2)
caso("El profesor no agenda citas", "Agendar cita" not in texto(profe.get("/citas")))
profe.post("/pacientes", data={"accion": "aprobar", "id": pid, "aprobado": "1"})
caso("El profesor solo revisa: aprueba al paciente", paciente()["aprobado"] is True)

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

caso("Hay 20 clínicas: la 1 a la 10 en la Sala #1 y la 11 a la 20 en la Sala #2",
     [(f["en_sala"], f["n"]) for f in base.execute("SELECT en_sala, count(*) AS n FROM salas GROUP BY en_sala ORDER BY 1")]
     == [(1, 10), (2, 10)]
     and uno("SELECT en_sala FROM salas WHERE nombre = 'Clínica 10'")["en_sala"] == 1
     and uno("SELECT en_sala FROM salas WHERE nombre = 'Clínica 11'")["en_sala"] == 2)
_, sala_1, sala_2 = texto(admin.get("/salas")).split('class="plano-titulo"')
caso("El plano muestra cada sala con sus clínicas, con un check si están disponibles y una X si no",
     "Sala #1" in sala_1 and "Clínica 10<" in sala_1 and "Clínica 11<" not in sala_1
     and sala_1.count("✓") == 9 and sala_1.count("✗") == 1
     and "Sala #2" in sala_2 and "Clínica 11<" in sala_2 and sala_2.count("✓") == 10)
admin.post("/salas", data={"accion": "agregar", "nombre": "Clínica 21", "en_sala": "2"})
caso("La Facultad agrega una clínica en la sala que elija",
     uno("SELECT en_sala FROM salas WHERE nombre = 'Clínica 21'")["en_sala"] == 2)
admin.post("/salas", data={"accion": "agregar", "nombre": "Clínica 22"})
caso("Sin decir la sala no se agrega la clínica", uno("SELECT 1 FROM salas WHERE nombre = 'Clínica 22'") is None)
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
     all(t in texto(admin.get("/bitacora")) for t in ("Reportó que Clínica 2 no funciona", "Solucionó la falla de Clínica 2")))
caso("La bitacora guarda lo que hizo cada quien", "Pasó a Luis Pérez" in texto(admin.get("/bitacora")))
bitacora = texto(admin.get("/bitacora"))
lugar = uno("SELECT lugar FROM bitacora ORDER BY id DESC LIMIT 1")["lugar"]
caso("Desde la red local la bitacora pone la ubicacion de la conexion, no 'Red local', y no muestra la IP",
     lugar != "Red local" and "Red local" not in bitacora and "127.0.0.1" not in bitacora)
print(f"         (ubicacion encontrada: {lugar or 'sin respuesta del servicio'})")
with m.app.test_request_context(headers={"Fly-Client-IP": "1.2.3.4"}):
    caso("Fuera de Fly no se cree un encabezado de IP que cualquiera puede inventar", m.ip_del_usuario() != "1.2.3.4")

m.crear_tablas()
caso("Crear las tablas otra vez no borra ni duplica nada", cuantos("pacientes") == 2 and cuantos("cuentas") == 5)

print(f"\n{'Todo correcto.' if not fallos else str(len(fallos)) + ' fallos.'}")
sys.exit(1 if fallos else 0)
